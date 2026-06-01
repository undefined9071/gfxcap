"""gfxcli -- gfxcap analysis command-line tool.

Single-file CLI shipped in the portable bundle alongside the embedded
Python 3.6 that gfxcap.pyd was built against.

    <bundle>/
        gfxcap.dll, gfxcap.pyd
        analysis/
            python36/python.exe
            gfxcli.py            <- this file

Verbs:
    dump   export a single EID's full pipeline state for AI use
    list   walk every event into a grep-friendly TSV index + Markdown
           views so an LLM can find the EID it wants by shader name,
           keyword variant, render target, marker scope, or bind-set
           cluster

Design principles (see DESIGN.md):
    - information density and reliability are top priority; disk and
      time are not constrained
    - failures must NEVER be silent: every per-target export creates
      its .md file even on failure (with STATUS: FAILED + reason), so
      a downstream LLM cannot mistake "missing in output" for "did not
      exist on the GPU"
    - README.md carries metadata, draw call args, coverage, navigation,
      and the full error list -- one entry-point file the LLM reads
      first
    - **portability is load-bearing**: the bundle must work no matter
      where it's extracted, moved, or renamed. We must never:
        * persist absolute paths to disk (no config / cache file)
        * bake the bundle location into output that survives the run
        * depend on the user's cwd for resolving bundle-internal paths
      All bundle-internal paths are derived from `__file__` at every
      invocation so move-and-rename of the install folder is a no-op.
      User-supplied paths (rdc, --out) are resolved to absolute at
      entry so subprocess calls and persisted metadata don't rot if
      the user changes cwd mid-run.

Usage (from the bundle root, with the embed):
    analysis\\python36\\python.exe analysis\\gfxcli.py dump \\
        --rdc path\\to\\capture.rdc \\
        --eid 4302
"""
import argparse
import datetime
import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
import traceback
from pathlib import Path


# ===========================================================================
# bootstrap
# ===========================================================================

def _bootstrap_gfxcap_module():
    here = Path(__file__).resolve().parent          # <bundle>/analysis
    bundle = here.parent                             # <bundle>

    bundle_str = str(bundle)
    if bundle_str not in sys.path:
        sys.path.insert(0, bundle_str)

    if sys.platform == "win32":
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(bundle_str)
            except OSError:
                pass
        os.environ["PATH"] = bundle_str + os.pathsep + os.environ.get("PATH", "")

    return bundle


def _import_gfxcap():
    try:
        import gfxcap  # type: ignore
        return gfxcap
    except ImportError as e:
        print("error: failed to import gfxcap module: {}".format(e),
              file=sys.stderr)
        print("hint:  run via the bundled analysis/python36/python.exe so",
              file=sys.stderr)
        print("       gfxcap.pyd's Python 3.6 ABI matches.", file=sys.stderr)
        sys.exit(4)


# ===========================================================================
# error collector
# ===========================================================================

class ErrorCollector(object):
    """Authoritative failure log used by the README."""

    def __init__(self):
        self.entries = []

    def add(self, group, target, reason, exc=None):
        entry = {"group": group, "target": target, "reason": str(reason)}
        if exc is not None:
            entry["traceback"] = "".join(
                traceback.format_exception_only(type(exc), exc)
            ).strip()
        self.entries.append(entry)

    def has_errors(self):
        return bool(self.entries)

    def by_group(self):
        groups = {}
        for e in self.entries:
            groups.setdefault(e["group"], []).append(e)
        return groups


# ===========================================================================
# helpers
# ===========================================================================

def _default_out(rdc, eid):
    return rdc.parent / "{}_eid{}".format(rdc.stem, eid)


def _check_rdc(rdc):
    if not rdc.exists():
        print("error: capture not found: {}".format(rdc), file=sys.stderr)
        sys.exit(2)


def _normalize_args_paths(args):
    """Resolve user-supplied paths (rdc, out) to absolute at the entry
    point so subprocess calls (HLSLDecompiler), persisted metadata, and
    any cwd changes mid-run all see a stable path."""
    args.rdc = Path(os.path.abspath(str(args.rdc)))
    if args.out is not None:
        args.out = Path(os.path.abspath(str(args.out)))


def _ts(p):
    try:
        m = p.stat().st_mtime
        return datetime.datetime.fromtimestamp(m).isoformat()
    except OSError:
        return "?"


def _api_name(controller):
    try:
        props = controller.GetAPIProperties()
        return str(props.pipelineType).split(".")[-1]
    except Exception:
        return "unknown"


def _find_action(actions, eid, parents=None):
    """Return (action, parents_list) where parents_list is the chain of
    ancestor ActionDescriptions from root to the immediate parent.
    Returns (None, []) if not found."""
    if parents is None:
        parents = []
    for a in actions:
        if getattr(a, "eventId", None) == eid:
            return a, list(parents)
        children = getattr(a, "children", None)
        if children:
            found, p = _find_action(children, eid, parents + [a])
            if found is not None:
                return found, p
    return None, []


def _marker_path(parents):
    """Build a 'Frame > Opaque > GBuffer' breadcrumb from a parents list.
    Only ancestors with a non-empty customName contribute (those are the
    push-marker frames)."""
    names = []
    for p in parents:
        cn = getattr(p, "customName", None)
        if cn:
            names.append(str(cn))
    return " > ".join(names) if names else ""


def _action_name(action, controller):
    """Best-effort action display name. customName is set for user markers;
    for plain draw/dispatch calls we ask the action to format itself
    against the structured file (which carries the chunk name)."""
    if action is None:
        return "?"
    custom = getattr(action, "customName", None)
    if custom:
        return str(custom)
    try:
        sdf = controller.GetStructuredFile()
        if sdf is not None and hasattr(action, "GetName"):
            return str(action.GetName(sdf))
    except Exception:
        pass
    return "?"


def _enum_str(v):
    s = str(v)
    if "." in s:
        return s.rsplit(".", 1)[-1]
    return s


def _is_null_id(rid):
    try:
        return int(rid) == 0
    except Exception:
        try:
            return not bool(rid)
        except Exception:
            return False


def _hex_dump(data, base_offset=0, width=16):
    lines = []
    if data is None:
        return lines
    n = len(data)
    for off in range(0, n, width):
        chunk = data[off:off + width]
        clusters = []
        for i in range(0, len(chunk), 4):
            clusters.append(" ".join("{:02X}".format(b) for b in chunk[i:i + 4]))
        lines.append("0x{:04X}: {}".format(base_offset + off, "  ".join(clusters)))
    return lines


def _md_header(name, fields):
    out = ["# {}".format(name), ""]
    for k, v in fields:
        out.append("- {}: {}".format(k, v))
    out.append("")
    return out


def _write_md(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_failure_md(path, name, reason):
    _write_md(path, [
        "# {}".format(name),
        "",
        "STATUS: FAILED",
        "REASON: {}".format(reason),
        "",
    ])


# ===========================================================================
# capture open
# ===========================================================================

def _open_capture(gfxcap, rdc):
    cap = gfxcap.OpenCaptureFile()

    open_result = cap.OpenFile(str(rdc), "rdc", None)
    if hasattr(open_result, "Succeeded"):
        if not open_result.Succeeded():
            raise RuntimeError("OpenFile failed: {}".format(open_result))
    elif hasattr(open_result, "code"):
        if open_result.code != 0:
            raise RuntimeError("OpenFile failed: {}".format(open_result))

    try:
        opts = gfxcap.ReplayOptions()
    except Exception:
        opts = None

    rep = cap.OpenCapture(opts, None) if opts is not None else cap.OpenCapture(None)
    if isinstance(rep, tuple):
        status, controller = rep
        if hasattr(status, "Succeeded") and not status.Succeeded():
            raise RuntimeError("OpenCapture failed: {}".format(status))
    else:
        controller = rep

    if controller is None:
        raise RuntimeError("OpenCapture returned no controller")

    return cap, controller


# ===========================================================================
# stages: full industry names for directories so an LLM doesn't have to
# decode "vs" / "ps" / "hs" abbreviations.
# ===========================================================================

# (D3D11Pipe attr, dir name, ShaderStage enum value, human-friendly title)
STAGES = [
    ("vertexShader",   "vertex_shader",   0, "Vertex Shader"),
    ("hullShader",     "hull_shader",     1, "Hull Shader"),
    ("domainShader",   "domain_shader",   2, "Domain Shader"),
    ("geometryShader", "geometry_shader", 3, "Geometry Shader"),
    ("pixelShader",    "pixel_shader",    4, "Pixel Shader"),
    ("computeShader",  "compute_shader",  5, "Compute Shader"),
]

STAGE_TITLE = {short: title for _, short, _, title in STAGES}


def _is_shader_bound(shader):
    if shader is None:
        return False
    rid = getattr(shader, "resourceId", None)
    if rid is None:
        return False
    return not _is_null_id(rid)


def _get_pipe(controller, errors):
    api = _api_name(controller).lower()
    try:
        if api in ("d3d11",):
            return ("d3d11", controller.GetD3D11PipelineState())
        elif api in ("d3d12",):
            return ("d3d12", controller.GetD3D12PipelineState())
        elif api in ("vulkan",):
            return ("vulkan", controller.GetVulkanPipelineState())
        elif api in ("opengl", "gl"):
            return ("opengl", controller.GetGLPipelineState())
    except Exception as e:
        errors.add("pipeline", "GetPipelineState", "pipeline state query failed", e)
        return (api, None)
    errors.add("pipeline", "api", "unsupported API: {}".format(api))
    return (api, None)


# ===========================================================================
# shader export: dxbc + asm + hlsl
# ===========================================================================

def _export_shader_files(stage_dir, controller, gfxcap, pipe_resource_id,
                         shader, errors, stage_short, bundle_root):
    """Write shader.dxbc, shader.asm, shader.hlsl."""
    results = {"dxbc": False, "asm": False, "hlsl": False}
    refl = getattr(shader, "reflection", None)

    # SWIG rejects Python None as ResourceId; fabricate an empty one for
    # APIs that don't carry a pipeline state object (D3D11/GL).
    if pipe_resource_id is None:
        try:
            pipe_resource_id = gfxcap.ResourceId()
        except Exception:
            pass

    # ---------- shader.dxbc ----------
    dxbc_path = stage_dir / "shader.dxbc"
    raw_bytes = None
    if refl is None:
        dxbc_path.write_bytes(b"")
        errors.add("shader_dxbc", stage_short, "no shader reflection available")
    else:
        try:
            raw_bytes = bytes(refl.rawBytes)
            dxbc_path.write_bytes(raw_bytes)
            results["dxbc"] = True
        except Exception as e:
            dxbc_path.write_bytes(b"")
            errors.add("shader_dxbc", stage_short, "rawBytes read failed", e)

    # ---------- shader.asm ----------
    asm_path = stage_dir / "shader.asm"
    if refl is None:
        asm_path.write_text("// STATUS: FAILED\n// no reflection\n", encoding="utf-8")
        errors.add("shader_asm", stage_short, "no shader reflection")
    else:
        try:
            asm = controller.DisassembleShader(pipe_resource_id, refl, "")
            if asm is None:
                asm = ""
            asm_path.write_text(str(asm), encoding="utf-8")
            results["asm"] = True
        except Exception as e:
            asm_path.write_text("// STATUS: FAILED\n// {}\n".format(e),
                                encoding="utf-8")
            errors.add("shader_asm", stage_short, "DisassembleShader failed", e)

    # ---------- shader.hlsl ----------
    hlsl_path = stage_dir / "shader.hlsl"
    if not results["dxbc"] or not raw_bytes:
        hlsl_path.write_text(
            "// STATUS: FAILED\n// no DXBC available to decompile\n",
            encoding="utf-8")
        errors.add("shader_hlsl", stage_short, "no DXBC")
    else:
        ok = _decompile_hlsl(dxbc_path, hlsl_path, errors, stage_short, bundle_root)
        results["hlsl"] = ok

    return results


def _decompile_hlsl(dxbc_path, hlsl_path, errors, stage_short, bundle_root):
    """Run the bundled HLSLDecompiler against shader.dxbc.

    Calling convention:
        HLSLDecompiler.exe <input> <-dxbc|-dxil|-spirv> [output.hlsl]
    The exe shells out to dxbc2dxil.exe via the system PATH so we prepend
    the plugin dir to env["PATH"].
    """
    bat = bundle_root / "plugins" / "hlsl-decompiler" / "HLSLDecompiler.bat"
    if not bat.exists():
        hlsl_path.write_text(
            "// STATUS: FAILED\n// HLSLDecompiler.bat missing at {}\n".format(bat),
            encoding="utf-8")
        errors.add("shader_hlsl", stage_short, "HLSLDecompiler.bat missing")
        return False

    expected_hlsl = dxbc_path.with_suffix(".hlsl")
    try:
        expected_hlsl.unlink()
    except OSError:
        pass

    env = os.environ.copy()
    env["PATH"] = str(bat.parent) + os.pathsep + env.get("PATH", "")
    # subprocess runs with cwd=plugin_dir; the dxbc / output paths must be
    # absolute so they resolve correctly from that cwd. dxbc2dxil otherwise
    # fails with "specified path not found" (ERROR_PATH_NOT_FOUND 0x80070003).
    # os.path.abspath also tolerates not-yet-existing output paths.
    dxbc_abs = os.path.abspath(str(dxbc_path))
    hlsl_abs = os.path.abspath(str(hlsl_path))
    try:
        r = subprocess.run(
            [str(bat), dxbc_abs, "-dxbc", hlsl_abs],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(bat.parent),
            env=env,
            timeout=120)
    except Exception as e:
        hlsl_path.write_text(
            "// STATUS: FAILED\n// subprocess: {}\n".format(e),
            encoding="utf-8")
        errors.add("shader_hlsl", stage_short, "subprocess error", e)
        return False

    if r.returncode != 0:
        hlsl_path.write_text(
            "// STATUS: FAILED\n// HLSLDecompiler exit {}\n// stderr: {}\n".format(
                r.returncode, r.stderr.decode("utf-8", errors="replace")),
            encoding="utf-8")
        errors.add("shader_hlsl", stage_short,
                   "HLSLDecompiler exit {}".format(r.returncode))
        return False

    try:
        produced_size = expected_hlsl.stat().st_size
    except OSError:
        produced_size = 0
    if produced_size == 0:
        hlsl_path.write_text(
            "// STATUS: FAILED\n"
            "// decompiler exited 0 but produced no output\n"
            "// stdout: {}\n// stderr: {}\n".format(
                r.stdout.decode("utf-8", errors="replace")[:500],
                r.stderr.decode("utf-8", errors="replace")[:500]),
            encoding="utf-8")
        errors.add("shader_hlsl", stage_short, "decompiler produced no output")
        return False

    if expected_hlsl.resolve() != hlsl_path.resolve():
        try:
            hlsl_path.write_bytes(expected_hlsl.read_bytes())
        except Exception as e:
            errors.add("shader_hlsl", stage_short, "copy failed", e)
            return False
    return True


# ===========================================================================
# reflection.md / io_signatures.md
# ===========================================================================

def _write_reflection_md(stage_dir, refl, errors, stage_short,
                          shader=None, res_lookup=None):
    target = stage_dir / "reflection.md"
    if refl is None:
        _write_failure_md(target, "{} reflection".format(STAGE_TITLE.get(stage_short, stage_short)),
                          "no reflection available")
        errors.add("reflection", stage_short, "no reflection")
        return False

    title = STAGE_TITLE.get(stage_short, stage_short)
    lines = ["# {} -- shader reflection".format(title), ""]
    lines.append("Identity, debug metadata, and declared cbuffer / SRV / "
                 "sampler / UAV layout from the shader's reflection.")
    lines.append("")

    # ---------- identity ----------
    shader_rid = getattr(shader, "resourceId", None) if shader is not None else None
    if shader_rid is None:
        shader_rid = getattr(refl, "resourceId", None)
    if res_lookup is not None and shader_rid is not None:
        lines.append("- resource_name: `{}`".format(
            _resource_name(res_lookup, shader_rid, default="(unnamed)")))
    lines.append("- resource_id: `{}`".format(shader_rid))
    lines.append("- entry_point: `{}`".format(getattr(refl, "entryPoint", "?")))
    lines.append("- stage: {}".format(_enum_str(getattr(refl, "stage", "?"))))
    lines.append("- encoding: {}".format(_enum_str(getattr(refl, "encoding", "?"))))
    lines.append("- raw_bytes_size: {}".format(len(getattr(refl, "rawBytes", b"") or b"")))

    # output_topology is only meaningful for hull / domain / geometry / mesh
    # shaders. RenderDoc happily populates the field on other stages with
    # whatever fxc/dxc dropped in there, so gate explicitly.
    stage_name = _enum_str(getattr(refl, "stage", "?"))
    if stage_name in ("Hull", "Domain", "Geometry", "Mesh", "Task"):
        topo = getattr(refl, "outputTopology", None)
        if topo is not None:
            topo_str = _enum_str(topo)
            if topo_str and topo_str.lower() not in ("unknown", "?"):
                lines.append("- output_topology: {}".format(topo_str))

    # dispatch_threads_dimension is the compute/mesh numthreads(x,y,z); gate
    # on stage for the same reason.
    if stage_name in ("Compute", "Mesh", "Task"):
        dtd = getattr(refl, "dispatchThreadsDimension", None)
        if dtd is not None:
            xyz = _xyz(dtd)
            if xyz not in ("?", "(0, 0, 0)"):
                lines.append("- dispatch_threads_dimension: {}".format(xyz))

    # ---------- debug info ----------
    dbg = getattr(refl, "debugInfo", None)
    if dbg is not None:
        compiler = _enum_str(getattr(dbg, "compiler", "?"))
        if compiler and compiler != "?":
            lines.append("- compiler: {}".format(compiler))
        esn = getattr(dbg, "entrySourceName", "")
        if esn:
            lines.append("- entry_source_name: `{}`".format(esn))
        lines.append("- source_debug_information: {}".format(
            getattr(dbg, "sourceDebugInformation", "?")))
        lines.append("- debuggable: {}".format(getattr(dbg, "debuggable", "?")))
        dstatus = getattr(dbg, "debugStatus", "")
        if dstatus:
            lines.append("- debug_status: `{}`".format(dstatus))
    lines.append("")

    # ---------- compile flags / defines ----------
    cflags = []
    if dbg is not None:
        cf_obj = getattr(dbg, "compileFlags", None)
        if cf_obj is not None:
            cflags = list(getattr(cf_obj, "flags", []) or [])

    lines.append("## compile flags ({} entries)".format(len(cflags)))
    lines.append("")
    lines.append("Macros and command-line args the shader was compiled with. "
                 "For Unity / Unreal builds the keyword/variant defines live "
                 "here (look for `@cmdline` or per-keyword entries).")
    lines.append("")
    if cflags:
        cmdline_value = None
        lines.append("| name | value |")
        lines.append("|------|-------|")
        for f in cflags:
            n = str(getattr(f, "name", "?"))
            v = str(getattr(f, "value", ""))
            if n == "@cmdline":
                cmdline_value = v
            v_disp = v.replace("|", "\\|").replace("\n", " ")
            if len(v_disp) > 400:
                v_disp = v_disp[:400] + " ... [{} chars total]".format(len(v))
            lines.append("| `{}` | `{}` |".format(n, v_disp))
        lines.append("")

        # explode /D defines from @cmdline so they're greppable
        if cmdline_value:
            defines = _extract_defines(cmdline_value)
            if defines:
                lines.append("### /D defines extracted from `@cmdline`")
                lines.append("")
                lines.append("| define | value |")
                lines.append("|--------|-------|")
                for k, vv in defines:
                    lines.append("| `{}` | `{}` |".format(k, vv))
                lines.append("")

    # ---------- source files ----------
    files = []
    if dbg is not None:
        files = list(getattr(dbg, "files", []) or [])
    lines.append("## source files ({})".format(len(files)))
    lines.append("")
    if files:
        lines.append("If `has_contents` is yes the original source is dumped "
                     "verbatim under `original_source/` next to this file.")
        lines.append("")
        lines.append("| index | filename | bytes | has_contents |")
        lines.append("|-------|----------|-------|--------------|")
        for i, sf in enumerate(files):
            fn = getattr(sf, "filename", "?")
            c = getattr(sf, "contents", "") or ""
            lines.append("| {} | `{}` | {} | {} |".format(
                i, fn, len(c), "yes" if c else "no"))
        lines.append("")

    cbs = getattr(refl, "constantBlocks", []) or []
    lines.append("## constant buffers ({} declared)".format(len(cbs)))
    lines.append("")
    if cbs:
        lines.append("| index | name | byte_size | n_vars |")
        lines.append("|-------|------|-----------|--------|")
        for i, cb in enumerate(cbs):
            lines.append("| {} | `{}` | {} | {} |".format(
                i, getattr(cb, "name", "?"),
                getattr(cb, "byteSize", "?"),
                len(getattr(cb, "variables", []) or [])))
        lines.append("")

    samps = getattr(refl, "samplers", []) or []
    lines.append("## declared samplers ({})".format(len(samps)))
    lines.append("")
    if samps:
        lines.append("| index | name |")
        lines.append("|-------|------|")
        for i, s in enumerate(samps):
            lines.append("| {} | `{}` |".format(i, getattr(s, "name", "?")))
        lines.append("")

    ros = getattr(refl, "readOnlyResources", []) or []
    lines.append("## read-only resources / SRVs ({})".format(len(ros)))
    lines.append("")
    if ros:
        lines.append("| index | name | type | element_size |")
        lines.append("|-------|------|------|--------------|")
        for i, r in enumerate(ros):
            d = getattr(r, "descriptorType", None)
            lines.append("| {} | `{}` | {} | {} |".format(
                i, getattr(r, "name", "?"),
                _enum_str(d) if d is not None else "?",
                getattr(r, "elementByteSize", "?")))
        lines.append("")

    rws = getattr(refl, "readWriteResources", []) or []
    lines.append("## read-write resources / UAVs ({})".format(len(rws)))
    lines.append("")
    if rws:
        lines.append("| index | name | type | element_size |")
        lines.append("|-------|------|------|--------------|")
        for i, r in enumerate(rws):
            d = getattr(r, "descriptorType", None)
            lines.append("| {} | `{}` | {} | {} |".format(
                i, getattr(r, "name", "?"),
                _enum_str(d) if d is not None else "?",
                getattr(r, "elementByteSize", "?")))
        lines.append("")

    _write_md(target, lines)
    return True


def _write_io_md(stage_dir, refl, errors, stage_short):
    target = stage_dir / "io_signatures.md"
    title = STAGE_TITLE.get(stage_short, stage_short)
    if refl is None:
        _write_failure_md(target, "{} I/O signatures".format(title),
                          "no reflection")
        errors.add("io", stage_short, "no reflection")
        return False

    lines = ["# {} -- I/O signatures".format(title), ""]
    lines.append("Hardware-level input and output register layout. Each row is "
                 "one register channel as the shader sees it.")
    lines.append("")
    for label, attr in (("inputs", "inputSignature"), ("outputs", "outputSignature")):
        sig = getattr(refl, attr, []) or []
        lines.append("## {} ({} entries)".format(label, len(sig)))
        lines.append("")
        if sig:
            lines.append("| index | semantic | semantic_idx | reg | type | mask | system_value |")
            lines.append("|-------|----------|--------------|-----|------|------|--------------|")
            for i, e in enumerate(sig):
                lines.append("| {} | `{}` | {} | {} | {} | 0x{:X} | {} |".format(
                    i,
                    getattr(e, "semanticName", "?"),
                    getattr(e, "semanticIndex", "?"),
                    getattr(e, "regIndex", "?"),
                    _enum_str(getattr(e, "varType", "?")),
                    getattr(e, "regChannelMask", 0) or 0,
                    _enum_str(getattr(e, "systemValue", "?"))))
            lines.append("")
    _write_md(target, lines)
    return True


# ===========================================================================
# bindings.md  -- per-stage texture / sampler pairing extracted from HLSL
# ===========================================================================

# HLSL declaration / call patterns. Verified against SPIRV-Cross output
# (Endfield / Unity HGRP) -- the typical decompile target. Other Sample*
# variants (SampleBias / SampleLevel / SampleGrad / SampleCmp / etc.) all
# share the convention "first arg is the sampler", so a single regex
# covers them; .Load / .Gather use the same form for the texture side
# but Load takes no sampler.
_BIND_TEX_DECL_RE = re.compile(
    r'(?:Texture(?:1D|2D|3D|Cube|2DArray|CubeArray|2DMS|2DMSArray)|'
    r'RWTexture\w*|Buffer|StructuredBuffer|RWBuffer|RWStructuredBuffer|'
    r'ByteAddressBuffer|RWByteAddressBuffer)'
    r'(?:<[^>]+>)?\s+'
    r'(\w+)\s*:\s*register\(\s*([tu]\d+)\s*\)\s*;'
)
_BIND_SAMP_DECL_RE = re.compile(
    r'(?:SamplerState|SamplerComparisonState)\s+'
    r'(\w+)\s*:\s*register\(\s*(s\d+)\s*\)\s*;'
)
_BIND_SAMPLE_CALL_RE = re.compile(r'(\w+)\.(Sample\w*|Gather\w*)\(\s*(\w+)')
_BIND_LOAD_CALL_RE = re.compile(r'(\w+)\.Load\s*\(')


def _write_bindings_md(stage_dir, stage_short):
    """Emit bindings.md showing texture <-> sampler pairs for each
    sample call in shader.hlsl. Silent no-op if HLSL is missing /
    failed / has no texture declarations."""
    from collections import defaultdict
    hlsl_path = stage_dir / "shader.hlsl"
    md_path = stage_dir / "bindings.md"
    if not hlsl_path.exists():
        return
    try:
        text = hlsl_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    if text.startswith("// STATUS: FAILED"):
        return

    tex_reg = {m.group(1): m.group(2)
               for m in _BIND_TEX_DECL_RE.finditer(text)}
    samp_reg = {m.group(1): m.group(2)
                for m in _BIND_SAMP_DECL_RE.finditer(text)}
    if not tex_reg:
        return

    per_tex_samps = defaultdict(lambda: defaultdict(int))
    per_tex_methods = defaultdict(lambda: defaultdict(int))
    for m in _BIND_SAMPLE_CALL_RE.finditer(text):
        tname, method, sname = m.group(1), m.group(2), m.group(3)
        treg = tex_reg.get(tname)
        if not treg:
            continue
        per_tex_samps[treg][samp_reg.get(sname, "?")] += 1
        per_tex_methods[treg][method] += 1
    for m in _BIND_LOAD_CALL_RE.finditer(text):
        treg = tex_reg.get(m.group(1))
        if not treg:
            continue
        per_tex_methods[treg]["Load"] += 1

    title = STAGE_TITLE.get(stage_short, stage_short)
    lines = [
        "# {} -- texture / sampler bindings".format(title),
        "",
        "Resolved by parsing `shader.hlsl` for `Texture* / Sampler*` "
        "register declarations and the `.Sample* / .Gather / .Load` "
        "calls that reference them. Use this to know which sampler is "
        "paired with which texture without grepping HLSL yourself.",
        "",
        "## per-texture summary",
        "",
        "| register | sampler(s) | calls |",
        "|----------|-----------|-------|",
    ]
    sampled = sorted(set(per_tex_samps) | set(per_tex_methods))
    for treg in sampled:
        samp_str = ", ".join("`{}` (x{})".format(s, n)
                             for s, n in sorted(per_tex_samps[treg].items())
                             ) or "(none)"
        method_str = ", ".join("{} x{}".format(m, n)
                               for m, n in sorted(per_tex_methods[treg].items())
                               ) or "(unused)"
        lines.append("| `{}` | {} | {} |".format(treg, samp_str, method_str))
    if not sampled:
        lines.append("| _(no sample / load calls found in this shader)_ | | |")
    lines.append("")

    declared = set(tex_reg.values())
    dead = sorted(declared - set(per_tex_samps) - set(per_tex_methods))
    if dead:
        lines.append("## declared but never sampled / loaded")
        lines.append("")
        lines.append("These registers appear in the HLSL declarations "
                     "but have no `.Sample* / .Gather / .Load` call "
                     "referencing them in the decompile. Likely either "
                     "dead-stripped binds or accessed via index "
                     "operators (`tex[xy]`) the parser doesn't track.")
        lines.append("")
        for treg in dead:
            lines.append("- `{}`".format(treg))
        lines.append("")

    _write_md(md_path, lines)


# ===========================================================================
# original shader source dump
# ===========================================================================

def _sanitize_source_filename(name, fallback_index):
    """Strip directory components and replace path-unsafe chars."""
    import re
    if not name:
        return "file_{}".format(fallback_index)
    # take basename only -- some compilers embed full paths
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return name or "file_{}".format(fallback_index)


def _export_original_source(stage_dir, refl, errors, stage_short, counts):
    """Dump debugInfo.files[i].contents -- the original HLSL/GLSL the
    developer wrote, when the compiler embedded it (typical for /Zs and
    Unity-shipped shader debug info)."""
    counts.setdefault("orig_src_expected", 0)
    counts.setdefault("orig_src_ok", 0)
    if refl is None:
        return
    dbg = getattr(refl, "debugInfo", None)
    if dbg is None:
        return
    files = list(getattr(dbg, "files", []) or [])
    if not files:
        return
    # only files that actually have contents count toward the expected/ok
    # tally; filename-only entries are still listed in reflection.md.
    src_dir = stage_dir / "original_source"
    used_names = set()
    for i, sf in enumerate(files):
        contents = getattr(sf, "contents", "") or ""
        if not contents:
            continue
        counts["orig_src_expected"] += 1
        fname = _sanitize_source_filename(getattr(sf, "filename", ""), i)
        # dedupe by prefixing with index when name collides
        if fname in used_names:
            fname = "{:02d}_{}".format(i, fname)
        used_names.add(fname)
        try:
            src_dir.mkdir(parents=True, exist_ok=True)
            (src_dir / fname).write_text(contents, encoding="utf-8")
            counts["orig_src_ok"] += 1
        except Exception as e:
            errors.add("original_source", "{}/{}".format(stage_short, fname),
                       "write failed", e)


# ===========================================================================
# descriptor accesses -> per-stage bindings
# ===========================================================================

DESC_CB     = ("ConstantBuffer",)
DESC_SAMP   = ("Sampler",)
DESC_IMGSAMP= ("ImageSampler",)
DESC_SRV    = ("Image", "Buffer", "TypedBuffer")
DESC_UAV    = ("ReadWriteImage", "ReadWriteTypedBuffer", "ReadWriteBuffer")


def _desc_kind(t):
    s = _enum_str(t)
    if s in DESC_CB:      return "cb"
    if s in DESC_SAMP:    return "sampler"
    if s in DESC_IMGSAMP: return "imagesampler"
    if s in DESC_SRV:     return "srv"
    if s in DESC_UAV:     return "uav"
    return "other"


def _stage_index_to_short(stage_value):
    for _, short, val, _t in STAGES:
        if val == stage_value:
            return short
    return None


def _texture_lookup(controller):
    try:
        return {tex.resourceId: tex for tex in controller.GetTextures()}
    except Exception:
        return {}


def _buffer_lookup(controller):
    try:
        return {buf.resourceId: buf for buf in controller.GetBuffers()}
    except Exception:
        return {}


def _resource_lookup(controller):
    """Build {resourceId: ResourceDescription} for the whole capture.
    ResourceDescription.name carries the engine-side debug name (e.g.
    "Shader xyz pass 3", "GBuffer0 RT") set by the application via
    SetPrivateData / SetDebugName / vkDebugMarkerSetObjectName."""
    try:
        return {r.resourceId: r for r in controller.GetResources()}
    except Exception:
        return {}


def _resource_name(res_lookup, rid, default="?"):
    if rid is None or _is_null_id(rid):
        return default
    r = res_lookup.get(rid)
    if r is None:
        return default
    n = getattr(r, "name", None)
    return str(n) if n else default


def _extract_defines(cmdline):
    """Pull (name, value) pairs out of a shader-compiler command line.
    Handles `-Dfoo=bar`, `-Dfoo`, `/Dfoo=bar`, `/Dfoo`, with optional
    quoting around the value. Order-preserving."""
    import re
    out = []
    # both `-D` and `/D` prefixes, value optionally `=...`. The lookahead
    # stops at whitespace, but we also accept quoted values.
    pattern = re.compile(
        r"""(?:^|\s)[-/]D
            ([A-Za-z_][A-Za-z0-9_]*)
            (?:=(?:"([^"]*)"|'([^']*)'|(\S+)))?""",
        re.VERBOSE)
    for m in pattern.finditer(cmdline):
        name = m.group(1)
        val = m.group(2) if m.group(2) is not None else (
            m.group(3) if m.group(3) is not None else m.group(4))
        out.append((name, val if val is not None else "1"))
    return out


def _export_bindings(out, controller, gfxcap, pipe_resource_id, refls_by_stage,
                     bound_stages, errors, counts, res_lookup=None):
    try:
        accesses = list(controller.GetDescriptorAccess())
    except Exception as e:
        errors.add("bindings", "GetDescriptorAccess", "failed", e)
        return

    tex_lookup = _texture_lookup(controller)
    buf_lookup = _buffer_lookup(controller)
    if res_lookup is None:
        res_lookup = {}
    bound_set = set(bound_stages)

    for acc in accesses:
        stage_short = _stage_index_to_short(int(getattr(acc, "stage", -1)))
        if stage_short is None:
            continue
        if stage_short not in bound_set:
            continue
        kind = _desc_kind(getattr(acc, "type", None))
        slot = int(getattr(acc, "index", 0))
        array_elem = int(getattr(acc, "arrayElement", 0))

        stage_dir = out / stage_short
        stage_dir.mkdir(parents=True, exist_ok=True)

        if kind == "sampler":
            _export_sampler(stage_dir, controller, gfxcap, acc, errors,
                            stage_short, slot, array_elem, counts)
        elif kind == "cb":
            _export_cbuffer(stage_dir, controller, gfxcap, acc,
                            refls_by_stage.get(stage_short),
                            errors, stage_short, slot, counts, res_lookup)
        elif kind == "srv":
            _export_srv(stage_dir, controller, gfxcap, acc,
                        tex_lookup, buf_lookup, errors,
                        stage_short, slot, array_elem, counts, res_lookup)
        elif kind == "uav":
            _export_uav(stage_dir, controller, gfxcap, acc,
                        tex_lookup, buf_lookup, errors,
                        stage_short, slot, array_elem, counts, res_lookup)
        elif kind == "imagesampler":
            _export_srv(stage_dir, controller, gfxcap, acc,
                        tex_lookup, buf_lookup, errors,
                        stage_short, slot, array_elem, counts, res_lookup)


def _fetch_one_descriptor(controller, gfxcap, acc, sampler=False):
    try:
        DR = gfxcap.DescriptorRange()
    except Exception:
        return None
    try:
        DR.offset = int(acc.byteOffset)
        DR.descriptorSize = int(acc.byteSize)
        DR.count = 1
    except Exception:
        return None
    try:
        if sampler:
            arr = controller.GetSamplerDescriptors(acc.descriptorStore, [DR])
        else:
            arr = controller.GetDescriptors(acc.descriptorStore, [DR])
        if not arr:
            return None
        return arr[0]
    except Exception:
        return None


def _export_cbuffer(stage_dir, controller, gfxcap, acc, refl, errors,
                    stage_short, slot, counts, res_lookup=None):
    counts.setdefault("cb_expected", 0)
    counts.setdefault("cb_ok", 0)
    counts["cb_expected"] += 1

    target = stage_dir / "constant_buffer_b{}.md".format(slot)
    desc = _fetch_one_descriptor(controller, gfxcap, acc, sampler=False)
    buffer_id = None
    byte_offset = 0
    byte_size = 0
    if desc is not None:
        buffer_id = getattr(desc, "resource", None)
        byte_offset = int(getattr(desc, "byteOffset", 0) or 0)
        byte_size = int(getattr(desc, "byteSize", 0) or 0)
    if res_lookup is None:
        res_lookup = {}

    cb_meta_name = "(unknown)"
    cb_byte_size_decl = "?"
    cb_index = -1
    cb_decl_vars = []
    if refl is not None:
        cbs = getattr(refl, "constantBlocks", []) or []
        if 0 <= slot < len(cbs):
            cb = cbs[slot]
            cb_meta_name = getattr(cb, "name", cb_meta_name)
            cb_byte_size_decl = getattr(cb, "byteSize", "?")
            cb_index = slot
            cb_decl_vars = list(getattr(cb, "variables", []) or [])

    raw = b""
    if buffer_id is not None and not _is_null_id(buffer_id) and byte_size > 0:
        try:
            raw = bytes(controller.GetBufferData(buffer_id, byte_offset, byte_size))
        except Exception as e:
            errors.add("cbuffer", "{}/b{}".format(stage_short, slot),
                       "GetBufferData failed", e)

    var_values = []
    if cb_index >= 0 and refl is not None:
        try:
            stage_value = int(getattr(refl, "stage", 0))
            var_values = list(controller.GetCBufferVariableContents(
                getattr(refl, "resourceId", None) or gfxcap.ResourceId(),
                refl.resourceId,
                stage_value,
                refl.entryPoint,
                cb_index,
                buffer_id if buffer_id is not None else gfxcap.ResourceId(),
                byte_offset,
                byte_size if byte_size > 0 else 0))
        except Exception as e:
            errors.add("cbuffer_decode", "{}/b{}".format(stage_short, slot),
                       "GetCBufferVariableContents failed", e)

    # Raw bytes always go to a sibling .bin file. We used to inline a hex
    # dump in the .md, which made the file 600+ KB for engines that pack
    # everything into one giant globals cbuffer (Unity "$Globals" etc.).
    raw_bin = stage_dir / "constant_buffer_b{}.bin".format(slot)
    if raw:
        try:
            raw_bin.write_bytes(raw)
        except Exception as e:
            errors.add("cbuffer_bin", "{}/b{}".format(stage_short, slot),
                       "write failed", e)

    # When a cbuffer has more variables than CB_VAR_INLINE_CAP, the full
    # variable list goes to a sibling .tsv (tab-separated; trivial to grep
    # and parse) and the .md keeps only a head/tail preview plus a pointer.
    CB_VAR_INLINE_CAP = 128
    var_count = len(var_values)
    split_vars = var_count > CB_VAR_INLINE_CAP
    vars_tsv = stage_dir / "constant_buffer_b{}_vars.tsv".format(slot)

    title = STAGE_TITLE.get(stage_short, stage_short)
    lines = [
        "# {} -- constant buffer at register b{}".format(title, slot),
        "",
        "Decoded constant buffer values for the GPU's view of register `b{}` "
        "at this draw. Raw bytes live alongside in `constant_buffer_b{}.bin`."
        .format(slot, slot),
        "",
        "- declared_name: `{}`".format(cb_meta_name),
        "- declared_size: {}".format(cb_byte_size_decl),
        "- bound_size: {}".format(byte_size),
        "- byte_offset_in_buffer: {}".format(byte_offset),
        "- backing_buffer_id: `{}`".format(buffer_id),
        "- backing_buffer_name: `{}`".format(
            _resource_name(res_lookup, buffer_id, default="(unnamed)")),
        "- raw_bytes_size: {}".format(len(raw)),
        "- raw_bytes_file: `constant_buffer_b{}.bin`".format(slot)
            if raw else "- raw_bytes_file: (empty)",
        "",
    ]

    def _var_row(i, v):
        n = getattr(v, "name", "?")
        decl = cb_decl_vars[i] if i < len(cb_decl_vars) else None
        if decl is not None:
            o = int(getattr(decl, "byteOffset", 0) or 0)
            return (n, "{}".format(o), "0x{:X}".format(o),
                    _shader_var_size(v), _shader_var_type(v),
                    _shader_var_value_inline(v))
        return (n, "?", "?", _shader_var_size(v),
                _shader_var_type(v), _shader_var_value_inline(v))

    if not var_values:
        lines.append("## variables")
        lines.append("")
        lines.append("(none decoded)")
        lines.append("")
    elif split_vars:
        # full table → tsv
        try:
            with vars_tsv.open("w", encoding="utf-8") as fp:
                fp.write("name\toffset\toffset_hex\tsize\ttype\tvalue\n")
                for i, v in enumerate(var_values):
                    fp.write("\t".join(str(c) for c in _var_row(i, v)) + "\n")
        except Exception as e:
            errors.add("cbuffer_vars_tsv", "{}/b{}".format(stage_short, slot),
                       "write failed", e)
        lines.append("## variables ({} entries)".format(var_count))
        lines.append("")
        lines.append("This cbuffer carries {} variables -- well above the "
                     "{}-entry inline cap, so the full list is in "
                     "`constant_buffer_b{}_vars.tsv` (tab-separated). The "
                     "preview below is the first 32 and last 8 entries.".format(
                         var_count, CB_VAR_INLINE_CAP, slot))
        lines.append("")
        lines.append("| name | offset | offset_hex | size | type | value |")
        lines.append("|------|--------|------------|------|------|-------|")
        head_n, tail_n = 32, 8
        for i, v in enumerate(var_values[:head_n]):
            r = _var_row(i, v)
            lines.append("| `{}` | {} | {} | {} | {} | {} |".format(*r))
        if var_count > head_n + tail_n:
            lines.append("| _... {} more entries; see TSV ..._ | | | | | |".format(
                var_count - head_n - tail_n))
        for i, v in enumerate(var_values[-tail_n:], start=var_count - tail_n):
            r = _var_row(i, v)
            lines.append("| `{}` | {} | {} | {} | {} | {} |".format(*r))
        lines.append("")
    else:
        lines.append("## variables ({} entries)".format(var_count))
        lines.append("")
        lines.append("| name | offset | offset_hex | size | type | value |")
        lines.append("|------|--------|------------|------|------|-------|")
        for i, v in enumerate(var_values):
            r = _var_row(i, v)
            lines.append("| `{}` | {} | {} | {} | {} | {} |".format(*r))
        lines.append("")
        for v in var_values:
            block = _shader_var_value_expanded(v)
            if block:
                lines.append("### `{}`".format(getattr(v, "name", "?")))
                lines.extend(block)
                lines.append("")

    _write_md(target, lines)
    counts["cb_ok"] += 1


def _shader_var_type(v):
    try:
        t = v.type
        if t is None:
            return "?"
        return "{}{}x{}".format(_enum_str(t),
                                getattr(v, "rows", "?"),
                                getattr(v, "columns", "?"))
    except Exception:
        return "?"


def _shader_var_size(v):
    rows = int(getattr(v, "rows", 0) or 0)
    cols = int(getattr(v, "columns", 0) or 0)
    if rows and cols:
        return rows * cols * 4
    return "?"


def _shader_var_value_inline(v):
    try:
        members = getattr(v, "members", None) or []
        if members:
            return "(struct, {} members)".format(len(members))
        rows = int(getattr(v, "rows", 0) or 0)
        cols = int(getattr(v, "columns", 0) or 0)
        if rows == 0 or cols == 0:
            return "(empty)"
        val = getattr(v, "value", None)
        if val is None:
            return "?"
        fvs = getattr(val, "f32v", None) or getattr(val, "fv", None)
        u32 = getattr(val, "u32v", None) or getattr(val, "uv", None)
        n = rows * cols
        if rows == 1:
            arr = list(fvs[:n]) if fvs is not None else (list(u32[:n]) if u32 is not None else [])
            return "(" + ", ".join("{}".format(x) for x in arr) + ")"
        return "({}x{} matrix, see expanded block)".format(rows, cols)
    except Exception:
        return "?"


def _shader_var_value_expanded(v):
    try:
        rows = int(getattr(v, "rows", 0) or 0)
        cols = int(getattr(v, "columns", 0) or 0)
        if rows <= 1:
            return []
        val = getattr(v, "value", None)
        if val is None:
            return []
        fvs = getattr(val, "f32v", None) or getattr(val, "fv", None)
        if fvs is None:
            return []
        out = []
        for r in range(rows):
            row_vals = []
            for c in range(cols):
                idx = r * cols + c
                try:
                    row_vals.append("{}".format(fvs[idx]))
                except Exception:
                    row_vals.append("?")
            out.append("[{},*] = {}".format(r, "  ".join(row_vals)))
        return out
    except Exception:
        return []


# ---------- SRV / UAV ----------

def _export_srv(stage_dir, controller, gfxcap, acc, tex_lookup, buf_lookup,
                errors, stage_short, slot, array_elem, counts, res_lookup=None):
    counts.setdefault("srv_expected", 0)
    counts.setdefault("srv_ok", 0)
    counts["srv_expected"] += 1
    if res_lookup is None:
        res_lookup = {}

    desc = _fetch_one_descriptor(controller, gfxcap, acc, sampler=False)
    if desc is None:
        target = stage_dir / "srv_t{}.md".format(slot)
        _write_failure_md(target, "SRV t{}".format(slot), "could not fetch Descriptor")
        errors.add("srv", "{}/t{}".format(stage_short, slot), "GetDescriptors failed")
        return

    resource_id = getattr(desc, "resource", None)
    type_str = _enum_str(getattr(desc, "type", None))

    if resource_id in tex_lookup:
        _export_texture(stage_dir, controller, gfxcap, desc, tex_lookup[resource_id],
                        errors, stage_short, slot, "srv", array_elem, res_lookup)
        counts["srv_ok"] += 1
    elif resource_id in buf_lookup:
        _export_srv_buffer(stage_dir, controller, gfxcap, desc, buf_lookup[resource_id],
                           errors, stage_short, slot, res_lookup)
        counts["srv_ok"] += 1
    else:
        target = stage_dir / "srv_t{}.md".format(slot)
        _write_md(target, _md_header("SRV t{}".format(slot), [
            ("type", type_str),
            ("resource_id", resource_id),
            ("resource_name", _resource_name(res_lookup, resource_id, default="(unnamed)")),
            ("status", "FAILED -- resource not found in capture"),
        ]))
        errors.add("srv", "{}/t{}".format(stage_short, slot),
                   "resource id not found")


def _export_uav(stage_dir, controller, gfxcap, acc, tex_lookup, buf_lookup,
                errors, stage_short, slot, array_elem, counts, res_lookup=None):
    counts.setdefault("uav_expected", 0)
    counts.setdefault("uav_ok", 0)
    counts["uav_expected"] += 1
    if res_lookup is None:
        res_lookup = {}

    desc = _fetch_one_descriptor(controller, gfxcap, acc, sampler=False)
    if desc is None:
        target = stage_dir / "uav_u{}.md".format(slot)
        _write_failure_md(target, "UAV u{}".format(slot), "could not fetch Descriptor")
        errors.add("uav", "{}/u{}".format(stage_short, slot), "GetDescriptors failed")
        return

    resource_id = getattr(desc, "resource", None)
    type_str = _enum_str(getattr(desc, "type", None))

    if resource_id in tex_lookup:
        _export_texture(stage_dir, controller, gfxcap, desc, tex_lookup[resource_id],
                        errors, stage_short, slot, "uav", array_elem, res_lookup)
        counts["uav_ok"] += 1
    elif resource_id in buf_lookup:
        _export_uav_buffer(stage_dir, controller, gfxcap, desc, buf_lookup[resource_id],
                           errors, stage_short, slot, res_lookup)
        counts["uav_ok"] += 1
    else:
        target = stage_dir / "uav_u{}.md".format(slot)
        _write_md(target, _md_header("UAV u{}".format(slot), [
            ("type", type_str),
            ("resource_id", resource_id),
            ("resource_name", _resource_name(res_lookup, resource_id, default="(unnamed)")),
            ("status", "FAILED -- resource not found"),
        ]))
        errors.add("uav", "{}/u{}".format(stage_short, slot), "resource id not found")


def _export_srv_buffer(stage_dir, controller, gfxcap, desc, buf_desc, errors,
                       stage_short, slot, res_lookup=None):
    if res_lookup is None:
        res_lookup = {}
    md = stage_dir / "buffer_t{}.md".format(slot)
    bin_ = stage_dir / "buffer_t{}.bin".format(slot)
    rid = getattr(desc, "resource", None)
    offset = int(getattr(desc, "byteOffset", 0) or 0)
    size = int(getattr(desc, "byteSize", 0) or 0)
    if size == 0:
        size = int(getattr(buf_desc, "length", 0) or 0)

    fields = [
        ("kind", "shader resource view (buffer)"),
        ("register", "t{}".format(slot)),
        ("resource_id", rid),
        ("resource_name", _resource_name(res_lookup, rid, default="(unnamed)")),
        ("byte_offset_in_buffer", offset),
        ("byte_size", _byte_size_field(size)),
        ("element_size", getattr(desc, "elementByteSize", "?")),
        ("format", _enum_str(getattr(getattr(desc, "format", None), "type", "?"))),
        ("buffer_total_length", getattr(buf_desc, "length", "?")),
    ]

    try:
        data = bytes(controller.GetBufferData(rid, offset, size))
        bin_.write_bytes(data)
        fields.append(("bin", _bin_annotation(
            "buffer_t{}.bin".format(slot), len(data), size, offset)))
    except Exception as e:
        bin_.write_bytes(b"")
        fields.append(("bin", "FAILED: {}".format(e)))
        errors.add("buffer_srv", "{}/t{}".format(stage_short, slot), "GetBufferData failed", e)

    _write_md(md, _md_header("SRV buffer at register t{}".format(slot), fields))


def _export_uav_buffer(stage_dir, controller, gfxcap, desc, buf_desc, errors,
                       stage_short, slot, res_lookup=None):
    if res_lookup is None:
        res_lookup = {}
    md = stage_dir / "uav_u{}.md".format(slot)
    bin_ = stage_dir / "uav_u{}.bin".format(slot)
    rid = getattr(desc, "resource", None)
    offset = int(getattr(desc, "byteOffset", 0) or 0)
    size = int(getattr(desc, "byteSize", 0) or 0)
    if size == 0:
        size = int(getattr(buf_desc, "length", 0) or 0)

    fields = [
        ("kind", "unordered access view (buffer)"),
        ("register", "u{}".format(slot)),
        ("resource_id", rid),
        ("resource_name", _resource_name(res_lookup, rid, default="(unnamed)")),
        ("byte_offset_in_buffer", offset),
        ("byte_size", _byte_size_field(size)),
        ("element_size", getattr(desc, "elementByteSize", "?")),
        ("format", _enum_str(getattr(getattr(desc, "format", None), "type", "?"))),
        ("buffer_total_length", getattr(buf_desc, "length", "?")),
        ("counter_byte_offset", getattr(desc, "counterByteOffset", "?")),
        ("buffer_struct_count", getattr(desc, "bufferStructCount", "?")),
    ]

    try:
        data = bytes(controller.GetBufferData(rid, offset, size))
        bin_.write_bytes(data)
        fields.append(("bin", _bin_annotation(
            "uav_u{}.bin".format(slot), len(data), size, offset)))
    except Exception as e:
        bin_.write_bytes(b"")
        fields.append(("bin", "FAILED: {}".format(e)))
        errors.add("buffer_uav", "{}/u{}".format(stage_short, slot), "GetBufferData failed", e)

    _write_md(md, _md_header("UAV buffer at register u{}".format(slot), fields))


def _export_texture(stage_dir, controller, gfxcap, desc, tex_desc,
                    errors, stage_short, slot, kind, array_elem, res_lookup=None):
    """kind = 'srv' | 'uav'."""
    if kind == "srv":
        prefix = "texture_t"
        register = "t{}".format(slot)
        title = "shader resource view texture at register {}".format(register)
        kind_label = "shader resource view (texture)"
    else:
        prefix = "uav_u"
        register = "u{}".format(slot)
        title = "unordered access view texture at register {}".format(register)
        kind_label = "unordered access view (texture)"

    md = stage_dir / "{}{}.md".format(prefix, slot)
    exr = stage_dir / "{}{}.exr".format(prefix, slot)
    png = stage_dir / "{}{}.png".format(prefix, slot)

    if res_lookup is None:
        res_lookup = {}
    rid = getattr(desc, "resource", None)
    fields = [
        ("kind", kind_label),
        ("register", register),
        ("resource_id", rid),
        ("resource_name", _resource_name(res_lookup, rid, default="(unnamed)")),
        ("dimensions", "{}x{}x{}".format(
            getattr(tex_desc, "width", "?"),
            getattr(tex_desc, "height", "?"),
            getattr(tex_desc, "depth", "?"))),
        ("array_size", getattr(tex_desc, "arraysize", "?")),
        ("mips", getattr(tex_desc, "mips", "?")),
        ("samples", getattr(tex_desc, "msSamp", "?")),
        ("format", _format_str(getattr(tex_desc, "format", None))),
        ("texture_type", _enum_str(getattr(tex_desc, "type", "?"))),
        ("byte_size", getattr(tex_desc, "byteSize", "?")),
        ("first_slice", getattr(desc, "firstSlice", "?")),
        ("num_slices", getattr(desc, "numSlices", "?")),
        ("first_mip", getattr(desc, "firstMip", "?")),
        ("num_mips", getattr(desc, "numMips", "?")),
        ("array_element_seen", array_elem),
    ]

    # EXR for analysis / DCC re-import / asset extraction; PNG sibling
    # for quick eyeball. See DESIGN.md "Texture output: 3-file rule".
    type_cast = _view_type_cast(desc, gfxcap)
    exr_ok = _save_texture(controller, gfxcap, rid, exr, "EXR", errors,
                           "texture_exr", "{}/{}{}".format(stage_short, prefix, slot),
                           type_cast=type_cast)
    png_ok = _save_texture(controller, gfxcap, rid, png, "PNG", errors,
                           "texture_png", "{}/{}{}".format(stage_short, prefix, slot),
                           type_cast=type_cast)
    fields.append(("exr", "OK" if exr_ok else "FAILED"))
    fields.append(("png", "OK" if png_ok else "FAILED"))
    _append_format_caveats(fields, _format_str(getattr(tex_desc, "format", None)))

    _write_md(md, _md_header(title, fields))


def _format_str(fmt):
    if fmt is None:
        return "?"
    try:
        if hasattr(fmt, "Name"):
            return fmt.Name()
    except Exception:
        pass
    parts = []
    try:
        parts.append(_enum_str(fmt.type))
    except Exception:
        pass
    try:
        parts.append(_enum_str(fmt.compType))
    except Exception:
        pass
    return "/".join(p for p in parts if p) or "?"


def _png_color_space(format_str):
    """How the PNG bytes should be interpreted, given the source GPU
    format. Empirically verified on a BC7_SRGB sample (predictions vs
    EXR ground truth match within 8-bit quantization noise -- linear
    hypothesis is correct, sRGB-encoded hypothesis is 12-13x off):

    For SRGB-suffixed source formats (BC7_SRGB / R8G8B8A8_SRGB / etc.)
    RenderDoc samples the texture (decoding sRGB to linear), then
    downcasts to RGBA8 and writes the linear values to PNG without an
    sRGB / gAMA chunk. Naive importers (Unity TextureImporter with
    sRGB=ON, Photoshop default) double-decode and produce a 2-3x dark
    image. Set the importer's sRGB flag to OFF for these PNGs.

    For non-SRGB formats the bytes pass through; interpretation depends
    on what the original shader author intended."""
    if not format_str:
        return None
    if format_str.endswith("_SRGB"):
        return ("linear (RenderDoc sampled the SRGB source which decoded "
                "to linear, then quantized to 8-bit; PNG has no sRGB "
                "chunk -- set Unity TextureImporter sRGB=OFF to avoid "
                "double-decode)")
    return ("as_stored (PNG bytes pass through unchanged from the source "
            "GPU values; sRGB-flag in your importer depends on author "
            "intent -- typically OFF for normal maps and data textures)")


def _append_format_caveats(fields, format_str):
    """Append png_color_space + exr_caveat fields when applicable.
    Centralized so the texture and OM-target writers stay in sync."""
    cs = _png_color_space(format_str)
    if cs:
        fields.append(("png_color_space", cs))
    sn = _exr_snorm_caveat(format_str)
    if sn:
        fields.append(("exr_caveat", sn))


def _exr_snorm_caveat(format_str):
    """Return a one-line note iff the source format will lose its
    signed-ness through RenderDoc's downcast on EXR save.

    The downcast in replay_controller.cpp:787-818 sends BC1-5 / 8-bit
    SNORM through the RGBA8 UNORM path (sourceHDR=false branch), which
    biases the [-1, 1] range into [0, 1]. 16-bit SNORM and BC6 take
    the RGBA32 path and are preserved -- no caveat needed there.

    Returns "" when no caveat applies."""
    if not format_str or "SNORM" not in format_str:
        return ""
    affected = ("BC4", "BC5", "R8")
    if any(format_str.startswith(p) for p in affected):
        return ("BC4/BC5/R8 SNORM is downcast to RGBA8 UNORM by RenderDoc "
                "before EXR save -- the [-1,1] range becomes [0,1]. "
                "Remap as `value*2 - 1` when consuming.")
    return ""


def _view_type_cast(view_or_desc, gfxcap):
    """Extract a non-typeless CompType from a view / descriptor's format
    for use as SaveTexture.typeCast.

    RenderDoc's SaveTexture defaults typeCast to Typeless, which makes
    it interpret TYPELESS texture data as UNORM. That silently destroys
    any TYPELESS texture the shader was reading as half-float / float
    (LUTs, HDR buffers): an R16G16B16A16_TYPELESS half-float (1.0, 0,
    0, 1) gets reinterpreted as four UNORM16s and dequantized to
    (0.234, 0, 0, 0.234). Forwarding the *view's* compType -- which
    IS the type the shader was reading -- fixes this without affecting
    typed textures (the field is ignored when the source format isn't
    typeless).

    Returns None when no useful hint is available, in which case the
    caller leaves typeCast at the RenderDoc default.
    """
    fmt = getattr(view_or_desc, "format", None)
    if fmt is None:
        return None
    ct = getattr(fmt, "compType", None)
    if ct is None:
        return None
    CT = getattr(gfxcap, "CompType", None)
    if CT is not None and ct == getattr(CT, "Typeless", None):
        return None
    return ct


def _save_texture(controller, gfxcap, resource_id, target_path, file_type,
                  errors, group, target_name, type_cast=None):
    if resource_id is None or _is_null_id(resource_id):
        errors.add(group, target_name, "null resource id")
        return False
    try:
        TS = gfxcap.TextureSave()
        TS.resourceId = resource_id
        if type_cast is not None:
            TS.typeCast = type_cast
        ft = getattr(gfxcap, "FileType", None)
        if ft is not None:
            mapped = getattr(ft, file_type, None)
            if mapped is not None:
                TS.destType = mapped
        result = controller.SaveTexture(TS, str(target_path))
        if hasattr(result, "Succeeded"):
            if not result.Succeeded():
                errors.add(group, target_name, "SaveTexture: {}".format(result))
                return False
        elif hasattr(result, "code") and result.code != 0:
            errors.add(group, target_name, "SaveTexture: {}".format(result))
            return False
        try:
            return target_path.stat().st_size > 0
        except OSError:
            return False
    except Exception as e:
        errors.add(group, target_name, "SaveTexture exception", e)
        return False


# ---------- Sampler ----------

def _export_sampler(stage_dir, controller, gfxcap, acc, errors,
                    stage_short, slot, array_elem, counts):
    counts.setdefault("sampler_expected", 0)
    counts.setdefault("sampler_ok", 0)
    counts["sampler_expected"] += 1

    target = stage_dir / "sampler_s{}.md".format(slot)
    s = _fetch_one_descriptor(controller, gfxcap, acc, sampler=True)
    if s is None:
        _write_failure_md(target, "sampler s{}".format(slot),
                          "could not fetch SamplerDescriptor")
        errors.add("sampler", "{}/s{}".format(stage_short, slot),
                   "GetSamplerDescriptors failed")
        return

    fields = [
        ("register", "s{}".format(slot)),
        ("array_element", array_elem),
        ("filter", _filter_str(getattr(s, "filter", None))),
        ("address_u", _enum_str(getattr(s, "addressU", "?"))),
        ("address_v", _enum_str(getattr(s, "addressV", "?"))),
        ("address_w", _enum_str(getattr(s, "addressW", "?"))),
        ("compare_function", _enum_str(getattr(s, "compareFunction", "?"))),
        ("max_anisotropy", getattr(s, "maxAnisotropy", "?")),
        ("mip_lod_bias", getattr(s, "mipBias", "?")),
        ("min_lod", getattr(s, "minLOD", "?")),
        ("max_lod", getattr(s, "maxLOD", "?")),
        ("border_color_type", _enum_str(getattr(s, "borderColorType", "?"))),
        ("seamless_cubemaps", getattr(s, "seamlessCubemaps", "?")),
        ("unnormalized", getattr(s, "unnormalized", "?")),
    ]
    _write_md(target, _md_header("sampler at register s{}".format(slot), fields))
    counts["sampler_ok"] += 1


def _filter_str(f):
    if f is None:
        return "?"
    parts = []
    for k in ("minify", "magnify", "mip", "filter"):
        try:
            v = getattr(f, k, None)
            if v is not None:
                parts.append("{}={}".format(k, _enum_str(v)))
        except Exception:
            pass
    return ", ".join(parts) if parts else "?"


# ===========================================================================
# Input Assembly stage (input_assembly/)
# ===========================================================================

# ---------------------------------------------------------------------------
# Generic vertex format decoder. Used by the mesh extractor to walk
# arbitrary vertex layouts. Engine-agnostic by design -- we never assume
# what a "normal" or "color" attribute is supposed to look like; we just
# decode the bytes per the format string and emit the result. The
# consumer (LLM / human) maps semantic to meaning.
#
# Coverage: ~30 standard DXGI / Vulkan formats. R10G10B10A2_{UNORM,UINT}
# is special-cased as a packed bitfield. R11G11B10_FLOAT and other
# non-uniform packed formats return None (caller emits raw bytes).
# ---------------------------------------------------------------------------

_VERTEX_FORMAT_RE = re.compile(
    r"^R(\d+)(?:G(\d+))?(?:B(\d+))?(?:A(\d+))?_([A-Z]+)$"
)
_VERTEX_STRUCT_CHARS = {
    (32, "FLOAT"): "f", (32, "UINT"): "I", (32, "SINT"): "i",
    (16, "FLOAT"): "e", (16, "UINT"): "H", (16, "SINT"): "h",
    (8, "UINT"): "B", (8, "SINT"): "b",
}
_VERTEX_NORM_CHARS = {8: "B", 16: "H", 32: "I"}     # for *_UNORM
_VERTEX_SNORM_CHARS = {8: "b", 16: "h", 32: "i"}    # for *_SNORM


def _decode_vertex_attribute(data, byte_offset, format_str):
    """Decode one vertex's components from `data` at `byte_offset`.
    Returns a list of Python float / int per the format's compType, or
    None if the format is unsupported (caller falls back to raw hex).
    Engine-agnostic: never interprets semantic meaning."""
    if not format_str:
        return None
    if format_str == "R10G10B10A2_UNORM":
        v = struct.unpack_from("<I", data, byte_offset)[0]
        return [(v & 0x3FF) / 1023.0,
                ((v >> 10) & 0x3FF) / 1023.0,
                ((v >> 20) & 0x3FF) / 1023.0,
                ((v >> 30) & 0x3) / 3.0]
    if format_str == "R10G10B10A2_UINT":
        v = struct.unpack_from("<I", data, byte_offset)[0]
        return [v & 0x3FF, (v >> 10) & 0x3FF,
                (v >> 20) & 0x3FF, (v >> 30) & 0x3]
    m = _VERTEX_FORMAT_RE.match(format_str)
    if not m:
        return None
    comp_bits = [int(x) for x in m.groups()[:4] if x]
    type_str = m.group(5)
    if not comp_bits or not all(b == comp_bits[0] for b in comp_bits):
        return None  # non-uniform packed, e.g. R11G11B10_FLOAT
    bits = comp_bits[0]
    n = len(comp_bits)
    if type_str in ("FLOAT", "UINT", "SINT"):
        ch = _VERTEX_STRUCT_CHARS.get((bits, type_str))
        if not ch:
            return None
        try:
            return list(struct.unpack_from("<{}{}".format(n, ch),
                                           data, byte_offset))
        except struct.error:
            return None
    if type_str == "UNORM":
        ch = _VERTEX_NORM_CHARS.get(bits)
        if not ch:
            return None
        max_val = float((1 << bits) - 1)
        try:
            raw = struct.unpack_from("<{}{}".format(n, ch),
                                     data, byte_offset)
        except struct.error:
            return None
        return [v / max_val for v in raw]
    if type_str == "SNORM":
        ch = _VERTEX_SNORM_CHARS.get(bits)
        if not ch:
            return None
        max_val = float((1 << (bits - 1)) - 1)
        try:
            raw = struct.unpack_from("<{}{}".format(n, ch),
                                     data, byte_offset)
        except struct.error:
            return None
        return [max(v / max_val, -1.0) for v in raw]
    return None


# ---------------------------------------------------------------------------
# Mesh extractor. Reads the vertex / index .bin files we just wrote,
# scopes to the draw's actual vertex range using action.numIndices /
# indexOffset / baseVertex, decodes every per-vertex attribute, and
# emits 4 files:
#   mesh.obj            POSITION + NORMAL + TEXCOORD0 + face -- standard
#                       universal mesh format. Only emits a channel when
#                       the source format produces the expected component
#                       count (3 for POSITION/NORMAL, 2 for TEXCOORD0).
#   mesh_vertices.tsv   per-vertex, ALL attributes decoded by format.
#                       Color, tangent, blend weights / indices, every UV
#                       set -- the complete data the GPU saw.
#   mesh_triangles.tsv  triangle list reconstructed from the index buffer.
#   mesh.md             vertex / triangle counts, bbox from POSITION,
#                       per-attribute decode status, and explicit notes
#                       about anything that couldn't be put into OBJ.
# ---------------------------------------------------------------------------

# Index buffer index width (bytes) -> struct format
_IB_FORMAT = {2: "<H", 4: "<I"}


def _extract_mesh(ia_dir, ia, action, errors, counts):
    counts.setdefault("mesh_expected", 0)
    counts.setdefault("mesh_ok", 0)
    if ia is None or action is None:
        return
    layouts = list(getattr(ia, "layouts", []) or [])
    if not layouts:
        return
    counts["mesh_expected"] = 1

    per_vertex_layouts = [l for l in layouts
                          if not getattr(l, "perInstance", False)]
    n_per_instance_skipped = len(layouts) - len(per_vertex_layouts)
    if not per_vertex_layouts:
        return

    topology = _enum_str(getattr(ia, "topology", "?"))
    num_indices = int(getattr(action, "numIndices", 0) or 0)
    index_offset = int(getattr(action, "indexOffset", 0) or 0)
    base_vertex = int(getattr(action, "baseVertex", 0) or 0)

    if num_indices <= 0:
        # Indirect / no-vertex draw -- nothing to extract
        return

    # Index buffer: only present when the action is an indexed draw and
    # the IA carries an index buffer binding.
    ib = getattr(ia, "indexBuffer", None)
    ib_stride = int(getattr(ib, "byteStride", 0) or 0) if ib is not None else 0
    ib_path = ia_dir / "index_buffer.bin"
    ib_data = None
    if ib_stride in _IB_FORMAT and ib_path.exists() and ib_path.stat().st_size > 0:
        try:
            ib_data = ib_path.read_bytes()
        except OSError:
            ib_data = None

    triangles = []
    vertex_indices = []  # in-order, with duplicates -- mirrors the IB walk
    if ib_data is not None:
        fmt = _IB_FORMAT[ib_stride]
        start = index_offset * ib_stride
        end = start + num_indices * ib_stride
        if end <= len(ib_data):
            for i in range(num_indices):
                idx = struct.unpack_from(fmt, ib_data, start + i * ib_stride)[0]
                vertex_indices.append(idx + base_vertex)
        else:
            errors.add("mesh", "index_buffer",
                       "draw range [{}, {}) exceeds index_buffer.bin "
                       "({} bytes)".format(start, end, len(ib_data)))
            return
    else:
        # Non-indexed draw: vertex range is [0, numIndices) in the bound
        # vertex buffers (the .bin we wrote already starts at the bind
        # offset).
        for i in range(num_indices):
            vertex_indices.append(i)

    if topology == "TriangleList":
        for i in range(0, len(vertex_indices) - 2, 3):
            triangles.append((vertex_indices[i],
                              vertex_indices[i + 1],
                              vertex_indices[i + 2]))
    # TriangleStrip / other topologies: leave triangles empty, mesh.md
    # will note the limitation. mesh_vertices.tsv still gets all verts.

    unique_verts = sorted(set(vertex_indices))

    # Per-slot vertex buffer data + stride
    vbs = list(getattr(ia, "vertexBuffers", []) or [])
    vb_data = {}
    vb_stride = {}
    for slot in range(len(vbs)):
        vbp = ia_dir / "vertex_buffer_{}.bin".format(slot)
        if vbp.exists() and vbp.stat().st_size > 0:
            try:
                vb_data[slot] = vbp.read_bytes()
            except OSError:
                pass
        vb_stride[slot] = int(getattr(vbs[slot], "byteStride", 0) or 0)

    # decoded[vert_idx] is a dict {column_name: value}. The TSV writer
    # collects the column union across all vertices; using a dict from
    # the start lets layouts with different decode paths (component vs
    # raw_hex) coexist without dropping cells, and removes the four
    # dict(...) rebuilds per vertex the writer otherwise needs.
    decoded = {vi: {} for vi in unique_verts}
    decode_status = []  # (semantic, idx, format, n_decoded, note)
    comp_letters = ("x", "y", "z", "w")
    for l in per_vertex_layouts:
        slot = int(getattr(l, "inputSlot", 0))
        attr_off = int(getattr(l, "byteOffset", 0))
        sem = str(getattr(l, "semanticName", "?"))
        sem_idx = int(getattr(l, "semanticIndex", 0))
        fmt = _format_str(getattr(l, "format", None))
        col_prefix = "{}_{}".format(sem, sem_idx)
        notes = []
        if slot not in vb_data:
            decode_status.append((sem, sem_idx, fmt, 0,
                                  "vertex buffer slot {} missing".format(slot)))
            continue

        data = vb_data[slot]
        stride = vb_stride.get(slot, 0)
        # stride 0 means the GPU reads the same bytes for every vertex
        # (broadcast). Resolve pos to attr_off; every vertex_index row
        # in the TSV gets that single value.
        broadcast = (stride == 0)
        if broadcast:
            notes.append("stride 0 -- value broadcast to every vertex")

        # Probe vertex 0 once to commit this layout to a single decode
        # path: component-decode for the whole vertex set if the format
        # is supported, raw-hex for the whole set otherwise. This
        # guarantees a consistent TSV column set across all rows for
        # this attribute.
        probe_pos = (0 if broadcast else unique_verts[0] * stride) + attr_off
        probe = _decode_vertex_attribute(data, probe_pos, fmt)
        if probe is not None:
            n_decoded = len(probe)
            col_keys = ["{}_{}".format(col_prefix, c)
                        for c in comp_letters[:n_decoded]]
            for vi in unique_verts:
                pos = (0 if broadcast else vi * stride) + attr_off
                comps = _decode_vertex_attribute(data, pos, fmt)
                if comps is None:
                    for k in col_keys:
                        decoded[vi][k] = "?"
                else:
                    for k, c in zip(col_keys, comps):
                        decoded[vi][k] = c
        else:
            n_decoded = 0
            notes.append(
                "format `{}` not in decoder coverage; raw hex emitted".format(fmt))
            raw_key = col_prefix + "_raw_hex"
            for vi in unique_verts:
                pos = (0 if broadcast else vi * stride) + attr_off
                raw = data[pos:pos + 16] if pos < len(data) else b""
                decoded[vi][raw_key] = raw.hex() if raw else "?"
        decode_status.append((sem, sem_idx, fmt, n_decoded, "; ".join(notes)))

    _write_mesh_outputs(ia_dir, topology, decoded, unique_verts, triangles,
                         decode_status, ib_stride, num_indices,
                         n_per_instance_skipped, errors)
    counts["mesh_ok"] = 1


def _write_mesh_outputs(ia_dir, topology, decoded, unique_verts, triangles,
                         decode_status, ib_stride, num_indices,
                         n_per_instance_skipped, errors):
    # ---- vertices TSV: every attribute decoded, every used vertex ----
    # Column union across all vertex dicts -- catches layouts whose
    # decode path differed per vertex (rare; mostly belt-and-suspenders
    # since the extractor now commits one path per layout).
    if not unique_verts:
        return
    col_order = []
    seen_cols = set()
    for vi in unique_verts:
        for k in decoded[vi]:
            if k not in seen_cols:
                seen_cols.add(k)
                col_order.append(k)
    cols = ["vertex_index"] + col_order
    vtsv = ia_dir / "mesh_vertices.tsv"
    try:
        with vtsv.open("w", encoding="utf-8") as fp:
            fp.write("\t".join(cols) + "\n")
            for vi in unique_verts:
                vmap = decoded[vi]
                row = [str(vi)]
                for col in col_order:
                    v = vmap.get(col, "")
                    if isinstance(v, float):
                        row.append(_tsv_escape("{:.6g}".format(v)))
                    else:
                        row.append(_tsv_escape(v))
                fp.write("\t".join(row) + "\n")
    except OSError as e:
        errors.add("mesh", "mesh_vertices.tsv", "write failed", e)

    # ---- triangles TSV ----
    ttsv = ia_dir / "mesh_triangles.tsv"
    try:
        with ttsv.open("w", encoding="utf-8") as fp:
            fp.write("triangle_index\tv0\tv1\tv2\n")
            for ti, (a, b, c) in enumerate(triangles):
                fp.write("{}\t{}\t{}\t{}\n".format(ti, a, b, c))
    except OSError as e:
        errors.add("mesh", "mesh_triangles.tsv", "write failed", e)

    # ---- pick channels for OBJ ----
    # OBJ supports POSITION (v), NORMAL (vn), TEXCOORD (vt) only.
    # We emit a channel only when the source format gave us the standard
    # component count: POSITION=3, NORMAL=3, TEXCOORD=2. 4-comp POSITION
    # also accepted (drops w). Anything else -> skip that OBJ channel,
    # log in mesh.md.
    obj_pos_cols = None      # list of "POSITION_0_x/y/z"
    obj_normal_cols = None
    obj_uv_cols = None
    pos_status = ""
    nor_status = ""
    uv_status = ""
    for sem, idx, fmt, n_decoded, note in decode_status:
        if sem == "POSITION" and idx == 0:
            if n_decoded >= 3:
                obj_pos_cols = ["POSITION_0_x", "POSITION_0_y", "POSITION_0_z"]
                pos_status = "OK ({} components, used xyz)".format(n_decoded)
            else:
                pos_status = "SKIPPED ({} components from `{}`; OBJ requires 3)".format(
                    n_decoded, fmt)
        elif sem == "NORMAL" and idx == 0:
            if n_decoded >= 3:
                obj_normal_cols = ["NORMAL_0_x", "NORMAL_0_y", "NORMAL_0_z"]
                nor_status = "OK ({} components, used xyz)".format(n_decoded)
            else:
                nor_status = ("SKIPPED ({} components from `{}`; "
                              "OBJ vn requires 3 -- raw value in TSV)").format(
                    n_decoded, fmt)
        elif sem == "TEXCOORD" and idx == 0:
            if n_decoded >= 2:
                obj_uv_cols = ["TEXCOORD_0_x", "TEXCOORD_0_y"]
                uv_status = "OK ({} components, used xy)".format(n_decoded)
            else:
                uv_status = "SKIPPED ({} components from `{}`; OBJ vt requires 2)".format(
                    n_decoded, fmt)

    # ---- OBJ ----
    # The per-corner format string is keyed by (has_uv, has_normal) --
    # OBJ syntax is "v/vt/vn", with empties dropped: "v" / "v/vt" /
    # "v//vn" / "v/vt/vn". Table-driven so the four formats sit side by
    # side rather than spread across nested ifs.
    _OBJ_CORNER_FMT = {
        (False, False): "{0}",
        (True,  False): "{0}/{0}",
        (False, True):  "{0}//{0}",
        (True,  True):  "{0}/{0}/{0}",
    }
    obj_path = ia_dir / "mesh.obj"
    if obj_pos_cols is not None and triangles:
        try:
            obj_idx_of = {vi: i + 1 for i, vi in enumerate(unique_verts)}
            with obj_path.open("w", encoding="utf-8") as fp:
                fp.write("# Extracted by gfxcli from input_assembly/. "
                         "See mesh.md for caveats.\n")
                for vi in unique_verts:
                    vmap = decoded[vi]
                    fp.write("v {:.6g} {:.6g} {:.6g}\n".format(
                        vmap.get(obj_pos_cols[0], 0.0),
                        vmap.get(obj_pos_cols[1], 0.0),
                        vmap.get(obj_pos_cols[2], 0.0)))
                if obj_uv_cols is not None:
                    for vi in unique_verts:
                        vmap = decoded[vi]
                        fp.write("vt {:.6g} {:.6g}\n".format(
                            vmap.get(obj_uv_cols[0], 0.0),
                            vmap.get(obj_uv_cols[1], 0.0)))
                if obj_normal_cols is not None:
                    for vi in unique_verts:
                        vmap = decoded[vi]
                        fp.write("vn {:.6g} {:.6g} {:.6g}\n".format(
                            vmap.get(obj_normal_cols[0], 0.0),
                            vmap.get(obj_normal_cols[1], 0.0),
                            vmap.get(obj_normal_cols[2], 0.0)))
                corner_fmt = _OBJ_CORNER_FMT[(obj_uv_cols is not None,
                                              obj_normal_cols is not None)]
                for a, b, c in triangles:
                    if a not in obj_idx_of or b not in obj_idx_of or c not in obj_idx_of:
                        continue
                    fp.write("f {} {} {}\n".format(
                        corner_fmt.format(obj_idx_of[a]),
                        corner_fmt.format(obj_idx_of[b]),
                        corner_fmt.format(obj_idx_of[c])))
        except OSError as e:
            errors.add("mesh", "mesh.obj", "write failed", e)

    # ---- bbox from POSITION ----
    bbox_min = bbox_max = None
    if obj_pos_cols is not None:
        for vi in unique_verts:
            vmap = decoded[vi]
            try:
                p = (float(vmap[obj_pos_cols[0]]),
                     float(vmap[obj_pos_cols[1]]),
                     float(vmap[obj_pos_cols[2]]))
            except (KeyError, TypeError, ValueError):
                continue
            if bbox_min is None:
                bbox_min = list(p)
                bbox_max = list(p)
            else:
                for i in range(3):
                    if p[i] < bbox_min[i]: bbox_min[i] = p[i]
                    if p[i] > bbox_max[i]: bbox_max[i] = p[i]

    # ---- mesh.md ----
    md_lines = [
        "# Mesh -- extracted from input_assembly",
        "",
        "Reconstructed by `gfxcli` from `vertex_buffer_*.bin` + "
        "`index_buffer.bin` + `input_layout.md`. The vertex range is "
        "scoped to this draw via the action's `numIndices` / "
        "`indexOffset` / `baseVertex` -- not the full bound buffer.",
        "",
        "- topology: {}".format(topology),
        "- index_count: {}".format(num_indices),
        "- index_format: {}".format(
            "R{}_UINT".format(ib_stride * 8) if ib_stride else "(non-indexed)"),
        "- unique_vertex_count: {}".format(len(unique_verts)),
        "- triangle_count: {}".format(len(triangles)),
        "- per_instance_attributes_skipped: {}".format(n_per_instance_skipped),
    ]
    if bbox_min is not None:
        md_lines.append("- bbox_min: ({:.6g}, {:.6g}, {:.6g})".format(*bbox_min))
        md_lines.append("- bbox_max: ({:.6g}, {:.6g}, {:.6g})".format(*bbox_max))
    md_lines.append("")
    md_lines.append("## attribute decode status")
    md_lines.append("")
    md_lines.append("| semantic | idx | format | components decoded | note |")
    md_lines.append("|----------|-----|--------|--------------------|------|")
    for sem, idx, fmt, n_decoded, note in decode_status:
        md_lines.append("| `{}` | {} | {} | {} | {} |".format(
            sem, idx, fmt, n_decoded, note or "OK"))
    md_lines.append("")
    md_lines.append("## OBJ channel inclusion")
    md_lines.append("")
    md_lines.append("- `v` (POSITION_0): {}".format(pos_status or "absent"))
    md_lines.append("- `vt` (TEXCOORD_0): {}".format(uv_status or "absent"))
    md_lines.append("- `vn` (NORMAL_0): {}".format(nor_status or "absent"))
    md_lines.append("")
    if topology != "TriangleList":
        md_lines.append("Topology is `{}`; mesh.obj face emission only "
                        "supports TriangleList for now. mesh_vertices.tsv "
                        "is still complete.".format(topology))
        md_lines.append("")
    md_lines.append("## files")
    md_lines.append("")
    md_lines.append("- `mesh.obj` -- POSITION (+ NORMAL / TEXCOORD0 when "
                    "format yields the standard component count) + face. "
                    "Universal mesh format; opens in Blender / Unity / "
                    "Maya / MeshLab.")
    md_lines.append("- `mesh_vertices.tsv` -- per-vertex, every attribute "
                    "from input_layout decoded by its DXGI format. "
                    "Includes COLOR / TANGENT / multiple TEXCOORD sets / "
                    "BLENDWEIGHTS / BLENDINDICES, and any others present.")
    md_lines.append("- `mesh_triangles.tsv` -- triangle list from the "
                    "index buffer (post-`baseVertex` adjustment).")
    md_lines.append("")
    _write_md(ia_dir / "mesh.md", md_lines)


def _byte_size_field(size):
    """Render the API-reported byte_size in a way that doesn't look
    like a bug when the API didn't expose it.

    D3D11 commonly reports byteSize = 0 for VB / IB / SRV buffer
    bindings (the format gives byte stride + offset but defers size
    to the resource). Showing the literal 0 alongside a multi-MB
    `.bin` file (which we read with the "size=0 means full
    remainder" fallback) reads like a contradiction. Render as
    `unknown` instead; the `bin:` annotation explains the coverage.
    """
    return "unknown" if size == 0 else size


def _bin_annotation(bin_name, written, api_size, source_offset):
    """One-line annotation for the `bin:` field. Always names the
    source-buffer byte range the .bin covers so the reader doesn't
    have to compute it. Distinguishes 'API gave a size' from 'API
    didn't, we read to end of buffer' explicitly so neither side
    contradicts the other."""
    if api_size > 0:
        return "{} ({} B; covers source_buffer[{}, {}))".format(
            bin_name, written, source_offset, source_offset + written)
    return ("{} ({} B; covers source_buffer[{}, {}) -- API byteSize was 0, "
            "bin reads to end of buffer)".format(
                bin_name, written, source_offset, source_offset + written))


def _export_input_assembly(out, controller, pipe, errors, counts,
                           res_lookup=None, action=None):
    counts.setdefault("ia_expected", 1)
    counts.setdefault("ia_ok", 0)
    if res_lookup is None:
        res_lookup = {}

    ia_dir = out / "input_assembly"
    ia_dir.mkdir(parents=True, exist_ok=True)

    ia = getattr(pipe, "inputAssembly", None)
    layout_md = ia_dir / "input_layout.md"
    if ia is None:
        _write_failure_md(layout_md, "input layout", "no IA on pipeline")
        errors.add("input_assembly", "inputAssembly", "missing")
        return

    lines = ["# Input Assembly -- vertex layout", ""]
    lines.append("Mapping from vertex buffer bytes to per-vertex shader input "
                 "registers.")
    lines.append("")
    try:
        topo = _enum_str(getattr(ia, "topology", "?"))
        lines.append("- topology: {}".format(topo))
    except Exception:
        pass
    layouts = getattr(ia, "layouts", []) or []
    lines.append("- layout_entries: {}".format(len(layouts)))
    lines.append("")
    if layouts:
        lines.append("| index | semantic | semantic_idx | format | input_slot | byte_offset | per_instance |")
        lines.append("|-------|----------|--------------|--------|------------|-------------|--------------|")
        for i, l in enumerate(layouts):
            lines.append("| {} | `{}` | {} | {} | {} | {} | {} |".format(
                i,
                getattr(l, "semanticName", "?"),
                getattr(l, "semanticIndex", "?"),
                _format_str(getattr(l, "format", None)),
                getattr(l, "inputSlot", "?"),
                getattr(l, "byteOffset", "?"),
                getattr(l, "perInstance", "?")))
    _write_md(layout_md, lines)

    vbs = getattr(ia, "vertexBuffers", []) or []
    for i, vb in enumerate(vbs):
        rid = getattr(vb, "resourceId", None)
        if rid is None or _is_null_id(rid):
            continue
        bin_ = ia_dir / "vertex_buffer_{}.bin".format(i)
        md   = ia_dir / "vertex_buffer_{}.md".format(i)
        offset = int(getattr(vb, "byteOffset", 0) or 0)
        stride = int(getattr(vb, "byteStride", 0) or 0)
        size = int(getattr(vb, "byteSize", 0) or 0)
        fields = [
            ("slot", i),
            ("resource_id", rid),
            ("resource_name", _resource_name(res_lookup, rid, default="(unnamed)")),
            ("byte_offset_in_buffer", offset),
            ("byte_stride", stride),
            ("byte_size", _byte_size_field(size)),
        ]
        try:
            # size == 0 -> "from offset to end of source buffer" (D3D11
            # common case where the binding doesn't carry a byte count).
            data = bytes(controller.GetBufferData(rid, offset, size if size > 0 else 0))
            if data:
                bin_.write_bytes(data)
                fields.append(("bin", _bin_annotation(
                    "vertex_buffer_{}.bin".format(i), len(data), size, offset)))
            else:
                fields.append(("bin", "(empty)"))
        except Exception as e:
            bin_.write_bytes(b"")
            fields.append(("bin", "FAILED: {}".format(e)))
            errors.add("vertex_buffer", "vb{}".format(i), "GetBufferData", e)
        _write_md(md, _md_header("vertex buffer at slot {}".format(i), fields))

    ib = getattr(ia, "indexBuffer", None)
    bin_ = ia_dir / "index_buffer.bin"
    md   = ia_dir / "index_buffer.md"
    if ib is not None:
        rid = getattr(ib, "resourceId", None)
        offset = int(getattr(ib, "byteOffset", 0) or 0)
        size = int(getattr(ib, "byteSize", 0) or 0)
        stride = int(getattr(ib, "byteStride", 0) or 0)
        fields = [
            ("resource_id", rid),
            ("resource_name", _resource_name(res_lookup, rid, default="(unnamed)")),
            ("byte_offset_in_buffer", offset),
            ("byte_size", _byte_size_field(size)),
            ("byte_stride", stride),
        ]
        if rid is not None and not _is_null_id(rid):
            # D3D11 frequently reports IB byte_size as 0; fetch with
            # size=0 in that case to get the full remainder of the
            # source buffer. Without this fallback the .bin never gets
            # written and downstream mesh extraction has no indices.
            try:
                data = bytes(controller.GetBufferData(rid, offset,
                                                     size if size > 0 else 0))
                bin_.write_bytes(data)
                fields.append(("bin", _bin_annotation(
                    "index_buffer.bin", len(data), size, offset)))
            except Exception as e:
                bin_.write_bytes(b"")
                fields.append(("bin", "FAILED: {}".format(e)))
                errors.add("index_buffer", "ib", "GetBufferData", e)
        else:
            fields.append(("bin", "(unbound)"))
        _write_md(md, _md_header("index buffer", fields))
    else:
        _write_md(md, _md_header("index buffer", [("status", "unbound")]))
        bin_.write_bytes(b"")

    counts["ia_ok"] = 1

    # Mesh extraction runs after all .bin files are on disk so it can
    # reuse them without re-fetching from the controller. Silently
    # no-ops on draws with no IA / no vertex layout / num_indices == 0
    # (compute / clear / copy / indirect-without-readable-args).
    _extract_mesh(ia_dir, ia, action, errors, counts)


# ===========================================================================
# Output Merger (output_merger/)
# ===========================================================================

def _export_output_merger(out, controller, gfxcap, pipe, errors, counts,
                          res_lookup=None):
    if res_lookup is None:
        res_lookup = {}
    om_dir = out / "output_merger"
    om_dir.mkdir(parents=True, exist_ok=True)
    om = getattr(pipe, "outputMerger", None)
    if om is None:
        _write_failure_md(om_dir / "output_merger.md", "output merger",
                          "no OM on pipeline")
        errors.add("output_merger", "outputMerger", "missing")
        return

    rts = getattr(om, "renderTargets", []) or []
    counts.setdefault("rt_expected", 0)
    counts.setdefault("rt_ok", 0)
    for i, rt in enumerate(rts):
        rid = getattr(rt, "resource", None)
        view = getattr(rt, "view", None)
        if (_is_null_id(rid) if rid is not None else True) and (
                view is None or _is_null_id(view)):
            continue
        counts["rt_expected"] += 1
        ok = _export_om_target(om_dir, controller, gfxcap, rt, errors,
                               "render_target_{}".format(i),
                               "render target at slot {}".format(i),
                               "render_target", res_lookup)
        if ok:
            counts["rt_ok"] += 1

    depth = getattr(om, "depthTarget", None)
    counts.setdefault("depth_expected", 0)
    counts.setdefault("depth_ok", 0)
    if depth is not None:
        rid = getattr(depth, "resource", None)
        view = getattr(depth, "view", None)
        if not (_is_null_id(rid) if rid is not None else True) or (
                view is not None and not _is_null_id(view)):
            counts["depth_expected"] = 1
            ok = _export_om_target(om_dir, controller, gfxcap, depth, errors,
                                   "depth_target",
                                   "depth/stencil target",
                                   "depth_target", res_lookup)
            if ok:
                counts["depth_ok"] = 1

    bs = getattr(om, "blendState", None)
    bs_md = om_dir / "blend_state.md"
    if bs is None:
        _write_failure_md(bs_md, "blend state", "no blend state on pipeline")
        errors.add("output_merger", "blend_state", "missing")
    else:
        fields = [
            ("alpha_to_coverage", getattr(bs, "alphaToCoverage", "?")),
            ("independent_blend", getattr(bs, "independentBlend", "?")),
            ("sample_mask", getattr(bs, "sampleMask", "?")),
        ]
        bf = getattr(bs, "blendFactor", None)
        if bf is not None:
            try:
                fields.append(("blend_factor", "({}, {}, {}, {})".format(*bf)))
            except Exception:
                pass
        _write_md(bs_md, _md_header("blend state", fields))
        blends = getattr(bs, "blends", []) or []
        if blends:
            with bs_md.open("a", encoding="utf-8") as fp:
                fp.write("## per-rt blend ({} entries)\n\n".format(len(blends)))
                fp.write("| index | enabled | logic_op | write_mask |\n")
                fp.write("|-------|---------|----------|------------|\n")
                for i, b in enumerate(blends):
                    fp.write("| {} | {} | {} | 0x{:X} |\n".format(
                        i, getattr(b, "enabled", "?"),
                        _enum_str(getattr(b, "logicOperation", "?")),
                        getattr(b, "writeMask", 0) or 0))

    ds = getattr(om, "depthStencilState", None)
    ds_md = om_dir / "depth_stencil_state.md"
    if ds is None:
        _write_failure_md(ds_md, "depth-stencil state", "missing")
        errors.add("output_merger", "depth_stencil_state", "missing")
    else:
        front = getattr(ds, "frontFace", None)
        fields = [
            ("depth_enable",     getattr(ds, "depthEnable", "?")),
            ("depth_write",      getattr(ds, "depthWrites", "?")),
            ("depth_function",   _enum_str(getattr(ds, "depthFunction", "?"))),
            ("stencil_enable",   getattr(ds, "stencilEnable", "?")),
            ("stencil_read_mask", getattr(front, "compareMask", "?") if front else "?"),
            ("stencil_write_mask", getattr(front, "writeMask", "?") if front else "?"),
        ]
        _write_md(ds_md, _md_header("depth-stencil state", fields))


def _export_om_target(om_dir, controller, gfxcap, view, errors, name, label,
                      prefix, res_lookup=None):
    if res_lookup is None:
        res_lookup = {}
    md = om_dir / "{}.md".format(name)
    exr = om_dir / "{}.exr".format(name)
    png = om_dir / "{}.png".format(name)
    rid = getattr(view, "resource", None)
    fields = [
        ("kind", label),
        ("slot_name", name),
        ("resource_id", rid),
        ("resource_name", _resource_name(res_lookup, rid, default="(unnamed)")),
        ("view_id", getattr(view, "view", "?")),
        ("type", _enum_str(getattr(view, "type", "?"))),
        ("format", _format_str(getattr(view, "format", None))),
        ("first_slice", getattr(view, "firstSlice", "?")),
        ("num_slices", getattr(view, "numSlices", "?")),
        ("first_mip", getattr(view, "firstMip", "?")),
        ("num_mips", getattr(view, "numMips", "?")),
    ]
    # EXR for analysis / DCC re-import / asset extraction; PNG sibling
    # for quick eyeball. See DESIGN.md "Texture output: 3-file rule".
    type_cast = _view_type_cast(view, gfxcap)
    exr_ok = _save_texture(controller, gfxcap, rid, exr, "EXR", errors,
                           prefix + "_exr", name, type_cast=type_cast)
    png_ok = _save_texture(controller, gfxcap, rid, png, "PNG", errors,
                           prefix + "_png", name, type_cast=type_cast)
    fields.append(("exr", "OK" if exr_ok else "FAILED"))
    fields.append(("png", "OK" if png_ok else "FAILED"))
    _append_format_caveats(fields, _format_str(getattr(view, "format", None)))
    _write_md(md, _md_header(label, fields))
    return exr_ok or png_ok


# ===========================================================================
# Rasterizer (rasterizer/)
# ===========================================================================

def _export_rasterizer(out, controller, pipe, errors, counts):
    rs_dir = out / "rasterizer"
    rs_dir.mkdir(parents=True, exist_ok=True)
    md = rs_dir / "rasterizer_state.md"

    rs = getattr(pipe, "rasterizer", None)
    if rs is None:
        _write_failure_md(md, "rasterizer state", "missing")
        errors.add("rasterizer", "rasterizer", "missing")
        return

    state = getattr(rs, "state", None)
    fields = []
    if state is not None:
        fields.extend([
            ("fill_mode",          _enum_str(getattr(state, "fillMode", "?"))),
            ("cull_mode",          _enum_str(getattr(state, "cullMode", "?"))),
            ("front_ccw",          getattr(state, "frontCCW", "?")),
            ("depth_bias",         getattr(state, "depthBias", "?")),
            ("depth_bias_clamp",   getattr(state, "depthBiasClamp", "?")),
            ("slope_scaled_bias",  getattr(state, "slopeScaledDepthBias", "?")),
            ("depth_clip",         getattr(state, "depthClip", "?")),
            ("scissor_enable",     getattr(state, "scissorEnable", "?")),
            ("multisample_enable", getattr(state, "multisampleEnable", "?")),
            ("antialias_lines",    getattr(state, "antialiasedLines", "?")),
            ("forced_sample_count", getattr(state, "forcedSampleCount", "?")),
        ])
    vps = getattr(rs, "viewports", []) or []
    fields.append(("viewports_count", len(vps)))
    scs = getattr(rs, "scissors", []) or []
    fields.append(("scissors_count", len(scs)))

    lines = _md_header("rasterizer state", fields)
    if vps:
        lines.append("## viewports")
        lines.append("")
        lines.append("| index | x | y | width | height | min_depth | max_depth |")
        lines.append("|-------|---|---|-------|--------|-----------|-----------|")
        for i, v in enumerate(vps):
            lines.append("| {} | {} | {} | {} | {} | {} | {} |".format(
                i, getattr(v, "x", "?"), getattr(v, "y", "?"),
                getattr(v, "width", "?"), getattr(v, "height", "?"),
                getattr(v, "minDepth", "?"), getattr(v, "maxDepth", "?")))
        lines.append("")
    if scs:
        lines.append("## scissors")
        lines.append("")
        lines.append("| index | x | y | width | height | enabled |")
        lines.append("|-------|---|---|-------|--------|---------|")
        for i, s in enumerate(scs):
            lines.append("| {} | {} | {} | {} | {} | {} |".format(
                i, getattr(s, "x", "?"), getattr(s, "y", "?"),
                getattr(s, "width", "?"), getattr(s, "height", "?"),
                getattr(s, "enabled", "?")))
        lines.append("")
    counts.setdefault("rs_expected", 1)
    counts["rs_ok"] = 1
    _write_md(md, lines)


# ===========================================================================
# README -- single entry point. Carries metadata, draw call args, coverage,
# navigation (every file enumerated), and the full error log.
# ===========================================================================

# File-name-prefix -> 1-line description shown in the navigation list.
NAV_DESCRIPTIONS = [
    # exact-name matches first
    ("shader.dxbc",                "raw shader bytecode (binary)"),
    ("shader.asm",                 "DXBC disassembly (text)"),
    ("shader.hlsl",                "decompiled HLSL (text)"),
    ("reflection.md",              "shader reflection: cbuffer / SRV / sampler / UAV layout"),
    ("io_signatures.md",           "input + output register signatures"),
    ("bindings.md",                "texture <-> sampler pairing (parsed from shader.hlsl Sample/Load calls)"),
    ("input_layout.md",            "vertex layout: how vertex buffer bytes map to vs inputs"),
    ("index_buffer.md",            "index buffer metadata"),
    ("index_buffer.bin",           "index buffer raw bytes"),
    ("mesh.obj",                   "extracted mesh in universal OBJ format (POSITION + NORMAL + TEXCOORD0 + face)"),
    ("mesh.md",                    "mesh extraction summary: vertex/triangle counts, bbox, attribute decode status"),
    ("mesh_vertices.tsv",          "per-vertex full attribute dump (every semantic decoded by format)"),
    ("mesh_triangles.tsv",         "triangle list (vertex indices) reconstructed from index buffer"),
    ("blend_state.md",             "blend / per-rt blend / sample mask"),
    ("depth_stencil_state.md",     "depth and stencil test state"),
    ("rasterizer_state.md",        "fill / cull / depth bias / viewports / scissors"),
    ("depth_target.md",            "depth/stencil target metadata + dump status"),
    ("depth_target.exr",           "depth/stencil target as EXR (linear float)"),
    ("depth_target.png",           "depth/stencil target as PNG (where convertible)"),
]

NAV_PREFIX_DESCRIPTIONS = [
    # (file-name prefix, register-letter, description-template "{}" gets the register / slot label)
    ("constant_buffer_b", "b", "constant buffer at register {} -- decoded values + raw bytes"),
    ("texture_t",         "t", "SRV texture at register {} -- EXR + PNG + metadata"),
    ("buffer_t",          "t", "SRV buffer at register {} -- raw bin + metadata"),
    ("uav_u",             "u", "UAV at register {} -- bin/exr + metadata"),
    ("sampler_s",         "s", "sampler at register {}"),
    ("vertex_buffer_",    "",  "vertex buffer at slot {}"),
    ("render_target_",    "",  "render target at slot {} -- EXR + PNG + metadata"),
    ("srv_t",             "t", "SRV at register {} -- export FAILED, see md"),
]


def _describe_cbuffer_sidecar(name):
    """constant_buffer_b3.bin / constant_buffer_b3_vars.tsv -- variant
    descriptions for the cbuffer sidecar files."""
    if name.endswith("_vars.tsv") and name.startswith("constant_buffer_b"):
        reg = name[len("constant_buffer_b"):-len("_vars.tsv")]
        return "full variable list for constant buffer b{} (TSV)".format(reg)
    if name.endswith(".bin") and name.startswith("constant_buffer_b"):
        reg = name[len("constant_buffer_b"):-len(".bin")]
        return "raw bytes for constant buffer b{}".format(reg)
    return None


def _describe_file(name):
    """Return a one-line description of a file by name."""
    for exact, desc in NAV_DESCRIPTIONS:
        if name == exact:
            return desc
    cb_side = _describe_cbuffer_sidecar(name)
    if cb_side:
        return cb_side
    stem = name.rsplit(".", 1)[0]
    for prefix, reg_letter, template in NAV_PREFIX_DESCRIPTIONS:
        if stem.startswith(prefix):
            slot = stem[len(prefix):]
            # constant_buffer_b1_vars uses underscore in stem; handled above
            if "_" in slot:
                continue
            label = "{}{}".format(reg_letter, slot) if reg_letter else slot
            return template.format(label)
    # texture/uav binary forms
    _IMAGE_EXTS = (".exr", ".png")
    if name.startswith("texture_t") and name.endswith(_IMAGE_EXTS):
        return "SRV texture (binary)"
    if name.startswith("uav_u") and name.endswith(".bin"):
        return "UAV buffer raw bytes"
    if name.startswith("uav_u") and name.endswith(_IMAGE_EXTS):
        return "UAV texture (binary)"
    if name.startswith("vertex_buffer_") and name.endswith(".bin"):
        return "vertex buffer raw bytes"
    if name.startswith("buffer_t") and name.endswith(".bin"):
        return "SRV buffer raw bytes"
    if name.startswith("render_target_") and name.endswith(_IMAGE_EXTS):
        return "render target (binary)"
    return ""


# Top-level directory ordering + descriptions in the README's navigation.
DIR_INTROS = [
    ("input_assembly", "Input Assembly stage",
     "Vertex layout, bound vertex buffers, and the index buffer for this draw."),
    ("vertex_shader",  "Vertex Shader",
     "Vertex shader binary + disassembly + decompiled HLSL, plus its bound "
     "constant buffers, SRVs, samplers, and UAVs at this draw."),
    ("hull_shader",    "Hull Shader",
     "Hull (tessellation control) shader and its bindings."),
    ("domain_shader",  "Domain Shader",
     "Domain (tessellation evaluation) shader and its bindings."),
    ("geometry_shader","Geometry Shader",
     "Geometry shader and its bindings."),
    ("pixel_shader",   "Pixel Shader",
     "Pixel/fragment shader binary + disassembly + decompiled HLSL, plus its "
     "bound constant buffers, SRVs, samplers, and UAVs at this draw."),
    ("compute_shader", "Compute Shader",
     "Compute shader and its bindings (only present for Dispatch events)."),
    ("rasterizer",     "Rasterizer stage",
     "Fill / cull / depth bias / viewports / scissors."),
    ("output_merger",  "Output Merger stage",
     "Render targets, depth/stencil target, blend state, depth-stencil state."),
]


def _build_metadata_section(rdc, controller, action, eid, action_parents=None):
    api = _api_name(controller)
    frame_num = "?"
    capture_time = "?"
    try:
        finfo = controller.GetFrameInfo()
        frame_num = getattr(finfo, "frameNumber", "?")
        capture_time = getattr(finfo, "captureTime", "?")
    except Exception:
        pass
    try:
        size_bytes = rdc.stat().st_size
    except OSError:
        size_bytes = "?"

    marker_path = _marker_path(action_parents or [])

    lines = [
        "## metadata",
        "",
        "Identifies the capture file, the frame within it, and the specific "
        "GPU event (the EID) we exported. `marker_path` is the breadcrumb of "
        "user-inserted debug markers (PushMarker / vkCmdBeginDebugUtilsLabel) "
        "between the frame root and this draw -- often the most direct hint "
        "at what rendering pass this draw belongs to.",
        "",
        "| field | value |",
        "|-------|-------|",
        "| capture path | `{}` |".format(rdc),
        "| capture size (bytes) | {} |".format(size_bytes),
        "| capture mtime | {} |".format(_ts(rdc)),
        "| api | {} |".format(api),
        "| frame number | {} |".format(frame_num),
        "| capture time | {} |".format(capture_time),
        "| eid | {} |".format(eid),
        "| action name | `{}` |".format(_action_name(action, controller)),
        "| action flags | `{}` |".format(str(getattr(action, "flags", "?"))),
        "| marker_path | {} |".format(
            "`{}`".format(marker_path) if marker_path else "(no markers)"),
        "",
    ]
    return lines


def _build_shader_summary_section(out, bound_stages, res_lookup):
    """A 'where to find what' table so a first-time LLM can locate the
    shader name and the compile-flag/variant data without scanning every
    file. Lives near the top of the README (after the action header).

    For each bound stage:
      - shader_name (from GetResources via res_lookup)
      - resource_id
      - reflection.md path (where compile flags / @cmdline live)
      - shader.dxbc / .asm / .hlsl paths
      - original_source/ if any source files were embedded
    """
    if not bound_stages:
        return []
    lines = [
        "## shaders at a glance",
        "",
        "One row per bound shader stage. The shader's engine-side name (when "
        "the app set one via `SetPrivateData` / `SetDebugName` / "
        "`vkDebugMarkerSetObjectName`) is the quickest identification handle. "
        "For the full compile flags / keyword variants (Unity / Unreal "
        "defines, `@cmdline` etc.) drill into the linked `reflection.md`.",
        "",
        "| stage | shader_name | reflection (compile flags & variants) | binary | disasm | hlsl | original source |",
        "|-------|-------------|---------------------------------------|--------|--------|------|-----------------|",
    ]
    for short in bound_stages:
        stage_dir = out / short
        refl = stage_dir / "reflection.md"
        dxbc = stage_dir / "shader.dxbc"
        asm = stage_dir / "shader.asm"
        hlsl = stage_dir / "shader.hlsl"
        orig = stage_dir / "original_source"

        # pull resource_name back out of reflection.md if it was written --
        # the writer put it in the second-or-third line ("- resource_name: ")
        shader_name = "?"
        try:
            if refl.exists():
                for ln in refl.read_text(encoding="utf-8").splitlines()[:30]:
                    if ln.startswith("- resource_name:"):
                        shader_name = ln.split(":", 1)[1].strip()
                        break
        except Exception:
            pass
        # Markdown tables: escape pipes and collapse newlines, otherwise
        # engine names like "HGRP/Lit ... | KEYWORD_FOO" break the columns.
        shader_name_cell = (shader_name.replace("|", "\\|")
                                       .replace("\n", " "))

        def _link(p):
            return "`{}/{}`".format(short, p.name) if p.exists() else "_(missing)_"

        if orig.is_dir() and any(orig.iterdir()):
            orig_cell = "`{}/original_source/`".format(short)
        else:
            orig_cell = "_(none)_"

        lines.append("| {} | {} | {} | {} | {} | {} | {} |".format(
            short, shader_name_cell,
            _link(refl), _link(dxbc), _link(asm), _link(hlsl), orig_cell))
    lines.append("")
    return lines


def _build_draw_call_section(action):
    if action is None:
        return ["## draw call arguments", "", "(no action)", ""]
    flags = str(getattr(action, "flags", "?"))
    lines = [
        "## draw call arguments",
        "",
        "The arguments that were issued to the GPU for this event. For draw "
        "calls these are the index/vertex/instance counts; for compute "
        "dispatches the workgroup dimensions.",
        "",
        "| field | value |",
        "|-------|-------|",
        "| eid | {} |".format(getattr(action, "eventId", "?")),
        "| action_id | {} |".format(getattr(action, "actionId", "?")),
        "| flags | `{}` |".format(flags),
        "| draw_index | {} |".format(getattr(action, "drawIndex", "?")),
        "| num_indices | {} |".format(getattr(action, "numIndices", "?")),
        "| num_instances | {} |".format(getattr(action, "numInstances", "?")),
        "| index_offset | {} |".format(getattr(action, "indexOffset", "?")),
        "| base_vertex | {} |".format(getattr(action, "baseVertex", "?")),
        "| instance_offset | {} |".format(getattr(action, "instanceOffset", "?")),
        "| dispatch_dimension | {} |".format(_xyz(getattr(action, "dispatchDimension", None))),
        "| dispatch_threads_dimension | {} |".format(_xyz(getattr(action, "dispatchThreadsDimension", None))),
        "| indirect_offset | {} |".format(getattr(action, "indirectOffset", "?")),
        "",
    ]
    return lines


def _xyz(v):
    if v is None:
        return "?"
    try:
        return "({}, {}, {})".format(v[0], v[1], v[2])
    except Exception:
        try:
            return "({}, {}, {})".format(getattr(v, "x", "?"),
                                         getattr(v, "y", "?"),
                                         getattr(v, "z", "?"))
        except Exception:
            return "?"


def _build_coverage_section(counts, error_count):
    lines = [
        "## coverage",
        "",
        "Per-export-target tally: how many were expected (based on what "
        "was bound), how many succeeded, how many failed. Any non-zero "
        "**failed** value means the corresponding `.md` file in the output "
        "carries a `STATUS: FAILED` marker -- the file is always written so "
        "the failure cannot go unnoticed.",
        "",
        "- total_failures: **{}**".format(error_count),
        "",
        "| group           | expected | exported | failed |",
        "|-----------------|----------|----------|--------|",
    ]

    def row(label, exp_key, ok_key):
        exp = counts.get(exp_key, 0)
        ok  = counts.get(ok_key, 0)
        lines.append("| {:<15s} | {:>8d} | {:>8d} | {:>6d} |".format(
            label, exp, ok, max(exp - ok, 0)))

    row("stages",           "stage_expected",   "stage_ok")
    row("dxbc",             "dxbc_expected",    "dxbc_ok")
    row("asm",              "asm_expected",     "asm_ok")
    row("hlsl",             "hlsl_expected",    "hlsl_ok")
    row("original_source",  "orig_src_expected", "orig_src_ok")
    row("constant_buffers", "cb_expected",      "cb_ok")
    row("srvs",             "srv_expected",     "srv_ok")
    row("uavs",             "uav_expected",     "uav_ok")
    row("samplers",         "sampler_expected", "sampler_ok")
    row("render_targets",   "rt_expected",      "rt_ok")
    row("depth_target",     "depth_expected",   "depth_ok")
    row("input_assembly",   "ia_expected",      "ia_ok")
    row("mesh",             "mesh_expected",    "mesh_ok")
    row("rasterizer",       "rs_expected",      "rs_ok")
    lines.append("")
    return lines


def _build_navigation_section(out):
    lines = [
        "## navigation",
        "",
        "Every file in this export, grouped by directory, with a one-line "
        "description so an LLM can decide where to drill in.",
        "",
    ]
    for dirname, title, intro in DIR_INTROS:
        d = out / dirname
        if not d.exists():
            continue
        files = sorted(p.name for p in d.iterdir() if p.is_file())
        subdirs = sorted(p for p in d.iterdir() if p.is_dir())
        if not files and not subdirs:
            continue
        lines.append("### `{}/` -- {}".format(dirname, title))
        lines.append("")
        lines.append(intro)
        lines.append("")
        for f in files:
            desc = _describe_file(f)
            if desc:
                lines.append("- `{}/{}` -- {}".format(dirname, f, desc))
            else:
                lines.append("- `{}/{}`".format(dirname, f))
        for sd in subdirs:
            sd_files = sorted(p.name for p in sd.iterdir() if p.is_file())
            if not sd_files:
                continue
            if sd.name == "original_source":
                desc = ("original HLSL/GLSL the developer wrote, recovered "
                        "from the shader's embedded debug info "
                        "({} file(s))".format(len(sd_files)))
            else:
                desc = "{} file(s)".format(len(sd_files))
            lines.append("- `{}/{}/` -- {}".format(dirname, sd.name, desc))
            for f in sd_files:
                lines.append("  - `{}/{}/{}`".format(dirname, sd.name, f))
        lines.append("")
    return lines


def _build_errors_section(errors):
    if not errors.has_errors():
        return ["## errors", "",
                "Per-export-target failures (e.g. one texture's PNG conversion "
                "failed because of an unsupported format). Empty here means "
                "every export succeeded.",
                "",
                "(no errors)",
                ""]
    lines = [
        "## errors",
        "",
        "Per-export-target failures. Each entry tells you the group it came "
        "from, the specific target, and the reason. The corresponding `.md` "
        "file in the output also has a `STATUS: FAILED` header so you can "
        "find it from either direction.",
        "",
    ]
    for grp, items in sorted(errors.by_group().items()):
        lines.append("### {}".format(grp))
        lines.append("")
        for e in items:
            lines.append("- **{target}**: {reason}".format(**e))
            tb = e.get("traceback")
            if tb:
                lines.append("  - `{}`".format(tb))
        lines.append("")
    return lines


def _write_readme(out, rdc, controller, action, eid, counts, errors, bound_stages,
                  action_parents=None, res_lookup=None):
    if res_lookup is None:
        res_lookup = {}
    md = out / "README.md"
    name = _action_name(action, controller)
    flags = str(getattr(action, "flags", "?"))
    n_err = len(errors.entries)

    lines = [
        "# gfxcli dump -- EID {}".format(eid),
        "",
        "Output of `gfxcli dump` for a single GPU event from a gfxcap "
        "(RenderDoc-format) capture. Everything below is GPU-side state for "
        "this one event; nothing about other events in the frame is here.",
        "",
        "- **action**: `{}`".format(name),
        "- **action_flags**: `{}`".format(flags),
        "- **error_count**: {}".format(n_err),
        "",
        "## quick start for a first-time LLM",
        "",
        "Read in this order:",
        "",
        "1. **`## metadata`** below -- the `marker_path` row tells you which "
        "rendering pass this draw belongs to (e.g. `Frame > Opaque > GBuffer`).",
        "2. **`## shaders at a glance`** -- the `shader_name` column is the "
        "engine-side debug name of each bound shader. Click into the linked "
        "`reflection.md` to see compile flags (Unity / Unreal keyword "
        "variants live there, in the `@cmdline` row and the `## /D defines "
        "extracted from @cmdline` table below it).",
        "3. **`## draw call arguments`** -- index / instance / dispatch counts.",
        "4. **`## coverage`** -- per-target tally; if anything failed the "
        "`## errors` section at the bottom names the file with details.",
        "5. **`## navigation`** -- full file index when you need to drill in.",
        "",
        "Looking for a specific resource (a texture, render target, vertex "
        "buffer)? The per-file `.md` next to each `.bin` / `.exr` carries a "
        "`resource_name` field with the engine-side name when one was set.",
        "",
    ]
    lines.extend(_build_metadata_section(rdc, controller, action, eid,
                                         action_parents=action_parents))
    lines.extend(_build_shader_summary_section(out, bound_stages, res_lookup))
    lines.extend(_build_draw_call_section(action))
    lines.extend(_build_coverage_section(counts, n_err))
    lines.extend(_build_navigation_section(out))
    lines.extend(_build_errors_section(errors))

    _write_md(md, lines)


# ===========================================================================
# verb: dump
# ===========================================================================

def cmd_dump(args):
    _normalize_args_paths(args)
    _check_rdc(args.rdc)
    out = args.out if args.out is not None else _default_out(args.rdc, args.eid)

    # Clean the output dir if we're using the default location, so stale files
    # from a prior run can't pollute the new dump. User-specified --out is
    # left alone (might be inside something else they care about).
    if args.out is None and out.exists():
        try:
            shutil.rmtree(str(out))
        except OSError:
            pass

    out.mkdir(parents=True, exist_ok=True)

    bundle_root = _bootstrap_gfxcap_module()
    gfxcap = _import_gfxcap()

    if not args.quiet:
        print("[gfxcli] gfxcap module imported")
        print("[gfxcli] rdc:  {}".format(args.rdc))
        print("[gfxcli] eid:  {}".format(args.eid))
        print("[gfxcli] out:  {}".format(out))

    errors = ErrorCollector()
    counts = {}

    try:
        capfile, controller = _open_capture(gfxcap, args.rdc)
    except Exception as e:
        print("fatal: cannot open capture: {}".format(e), file=sys.stderr)
        return 2

    try:
        roots = controller.GetRootActions()
        action, action_parents = _find_action(roots, args.eid)
    except Exception as e:
        print("fatal: failed to enumerate actions: {}".format(e), file=sys.stderr)
        return 3

    if action is None:
        print("fatal: EID {} not found in capture".format(args.eid), file=sys.stderr)
        return 3

    try:
        controller.SetFrameEvent(args.eid, True)
    except Exception as e:
        errors.add("replay", "set_frame_event", "SetFrameEvent failed", e)

    # Resource-id -> ResourceDescription, used by every per-target writer
    # to surface the engine-side debug name (shader name, "GBuffer0 RT",
    # "MeshFilter Foo VB", etc.).
    res_lookup = _resource_lookup(controller)

    api, pipe = _get_pipe(controller, errors)
    pipe_resource_id = getattr(pipe, "resourceId", None) if pipe is not None else None

    bound_stages = []
    refls_by_stage = {}
    counts["stage_expected"] = 0
    counts["stage_ok"]       = 0
    counts["dxbc_expected"]  = 0
    counts["dxbc_ok"]        = 0
    counts["asm_expected"]   = 0
    counts["asm_ok"]         = 0
    counts["hlsl_expected"]  = 0
    counts["hlsl_ok"]        = 0

    if pipe is not None:
        action_flags = int(getattr(action, "flags", 0) or 0)
        ACT_DRAW     = 0x0002
        ACT_DISPATCH = 0x0004
        ACT_MESH     = 0x0008
        ACT_DRAW_RAY = 0x4000
        graphics_stages = {"vertex_shader", "hull_shader", "domain_shader",
                           "geometry_shader", "pixel_shader"}
        compute_stages  = {"compute_shader"}
        if action_flags & ACT_DISPATCH:
            relevant_stages = compute_stages
        elif action_flags & (ACT_DRAW | ACT_MESH | ACT_DRAW_RAY):
            relevant_stages = graphics_stages
        else:
            relevant_stages = set()

        for attr, short, _val, _title in STAGES:
            if short not in relevant_stages:
                continue
            shader = getattr(pipe, attr, None)
            if not _is_shader_bound(shader):
                continue
            bound_stages.append(short)
            counts["stage_expected"] += 1
            counts["dxbc_expected"]  += 1
            counts["asm_expected"]   += 1
            counts["hlsl_expected"]  += 1
            stage_dir = out / short
            stage_dir.mkdir(parents=True, exist_ok=True)

            res = _export_shader_files(stage_dir, controller, gfxcap,
                                       pipe_resource_id, shader, errors,
                                       short, bundle_root)
            if res.get("dxbc"):  counts["dxbc_ok"]  += 1
            if res.get("asm"):   counts["asm_ok"]   += 1
            if res.get("hlsl"):  counts["hlsl_ok"]  += 1
            counts["stage_ok"] += 1

            refl = getattr(shader, "reflection", None)
            refls_by_stage[short] = refl
            _write_reflection_md(stage_dir, refl, errors, short,
                                 shader=shader, res_lookup=res_lookup)
            _write_io_md(stage_dir, refl, errors, short)
            _export_original_source(stage_dir, refl, errors, short, counts)
            _write_bindings_md(stage_dir, short)

        _export_bindings(out, controller, gfxcap, pipe_resource_id,
                         refls_by_stage, bound_stages, errors, counts,
                         res_lookup=res_lookup)

        _export_input_assembly(out, controller, pipe, errors, counts,
                               res_lookup=res_lookup, action=action)
        _export_output_merger(out, controller, gfxcap, pipe, errors, counts,
                              res_lookup=res_lookup)
        _export_rasterizer(out, controller, pipe, errors, counts)

    _write_readme(out, args.rdc, controller, action, args.eid, counts,
                  errors, bound_stages,
                  action_parents=action_parents, res_lookup=res_lookup)

    # Folder thumbnail: copy the first available output image to the
    # root so Windows Explorer's "Large icons" view shows a hero image
    # per dump. Intentionally not listed in README navigation -- it's
    # for human Explorer browsing, not LLM consumption.
    for src in (out / "output_merger" / "render_target_0.png",
                out / "output_merger" / "depth_target.png"):
        if src.exists() and src.stat().st_size > 0:
            try:
                shutil.copy2(str(src), str(out / "preview.png"))
            except OSError:
                pass
            break

    try:
        controller.Shutdown()
    except Exception:
        pass
    try:
        capfile.Shutdown()
    except Exception:
        pass

    if not args.quiet:
        print("[gfxcli] export written to: {}".format(out))
        if errors.has_errors():
            print("[gfxcli] {} non-fatal error(s); see README.md ## errors".format(
                len(errors.entries)))

    return 1 if errors.has_errors() else 0


# ===========================================================================
# verb: list  -- index every event in a capture so an LLM (or a human with
# grep) can find an EID without opening the GUI. Authoritative output is
# events.tsv; everything else is convenience structure built around it.
# ===========================================================================

# ActionFlags bitmask values, mirrored from
# renderdoc/api/replay/replay_enums.h:5093. Kept here so we don't depend
# on the gfxcap module exposing the enum at module-level.
_AF_CLEAR        = 0x000001
_AF_DRAWCALL     = 0x000002
_AF_DISPATCH     = 0x000004
_AF_MESH         = 0x000008
_AF_CMDLIST      = 0x000010
_AF_SETMARKER    = 0x000020
_AF_PUSHMARKER   = 0x000040
_AF_POPMARKER    = 0x000080
_AF_PRESENT      = 0x000100
_AF_MULTI        = 0x000200
_AF_COPY         = 0x000400
_AF_RESOLVE      = 0x000800
_AF_GENMIPS      = 0x001000
_AF_PASSBOUND    = 0x002000
_AF_DISPATCH_RAY = 0x004000
_AF_BUILD_AS     = 0x008000
_AF_INDEXED      = 0x010000
_AF_INSTANCED    = 0x020000
_AF_AUTO         = 0x040000
_AF_INDIRECT     = 0x080000
_AF_CLEAR_COL    = 0x100000
_AF_CLEAR_DS     = 0x200000
_AF_BEGINPASS    = 0x400000
_AF_ENDPASS      = 0x800000

# "Work" classes -- a single primary label per event. Marker / pass-boundary
# / present aren't real work and are excluded from events.tsv (they live in
# marker_path / markers.md).
_CLASS_BITS = [
    (_AF_DRAWCALL,     "draw"),
    (_AF_DISPATCH,     "dispatch"),
    (_AF_MESH,         "mesh_dispatch"),
    (_AF_DISPATCH_RAY, "dispatch_ray"),
    (_AF_BUILD_AS,     "build_accstruct"),
    (_AF_CLEAR,        "clear"),
    (_AF_COPY,         "copy"),
    (_AF_RESOLVE,      "resolve"),
    (_AF_GENMIPS,      "gen_mips"),
]


def _action_class(flags_int):
    for bit, name in _CLASS_BITS:
        if flags_int & bit:
            return name
    return "other"


def _action_modifiers(flags_int):
    """Comma-joined list of secondary flag names (indexed, instanced, ...).
    Empty string when none apply."""
    mods = []
    if flags_int & _AF_INDEXED:   mods.append("indexed")
    if flags_int & _AF_INSTANCED: mods.append("instanced")
    if flags_int & _AF_INDIRECT:  mods.append("indirect")
    if flags_int & _AF_AUTO:      mods.append("auto")
    if flags_int & _AF_CLEAR_COL: mods.append("color")
    if flags_int & _AF_CLEAR_DS:  mods.append("depthstencil")
    if flags_int & _AF_PASSBOUND: mods.append("passboundary")
    return ",".join(mods)


# An "indexable" action is one we want as a row in events.tsv -- i.e. it
# does GPU work. Marker pushes / pops are skipped at the row level but
# still walked so their customName becomes the marker_path of their
# children.
def _is_indexable(flags_int):
    return bool(flags_int & (
        _AF_DRAWCALL | _AF_DISPATCH | _AF_MESH | _AF_DISPATCH_RAY |
        _AF_BUILD_AS | _AF_CLEAR | _AF_COPY | _AF_RESOLVE | _AF_GENMIPS))


def _walk_actions(actions, parents=None):
    """Generator: yields (action, parents_list) for every action in the
    tree. parents_list is the chain root->...->immediate_parent."""
    if parents is None:
        parents = []
    for a in actions:
        yield a, parents
        children = getattr(a, "children", None)
        if children:
            for sub in _walk_actions(children, parents + [a]):
                yield sub


# ---------- per-event pipeline-state enrichment -----------------------------

def _shader_names_for_pipe(pipe, res_lookup):
    """Map of stage_short -> resource_name (str). Empty string when the
    stage isn't bound or the resource has no name."""
    out = {}
    if pipe is None:
        return out
    for attr, short, _val, _title in STAGES:
        sh = getattr(pipe, attr, None)
        if sh is None:
            continue
        rid = getattr(sh, "resourceId", None)
        if rid is None or _is_null_id(rid):
            continue
        out[short] = _resource_name(res_lookup, rid, default="")
    return out


def _rt_info(pipe, res_lookup):
    """(rt0_name, rt0_size_str, rt0_format_str, n_rts, dsv_name,
        rt_resource_ids_sorted_tuple, dsv_resource_id_or_None)."""
    rt0_name = ""
    rt0_size = ""
    rt0_format = ""
    n_rts = 0
    dsv_name = ""
    rt_ids = []
    dsv_id = None
    if pipe is None:
        return rt0_name, rt0_size, rt0_format, n_rts, dsv_name, tuple(), dsv_id
    om = getattr(pipe, "outputMerger", None)
    if om is None:
        return rt0_name, rt0_size, rt0_format, n_rts, dsv_name, tuple(), dsv_id
    rts = getattr(om, "renderTargets", []) or []
    for i, rt in enumerate(rts):
        rid = getattr(rt, "resource", None)
        if rid is None or _is_null_id(rid):
            continue
        n_rts += 1
        rt_ids.append(rid)
        if not rt0_name:
            rt0_name = _resource_name(res_lookup, rid, default="")
            rt0_format = _format_str(getattr(rt, "format", None))
            tex = None  # size needs a TextureDescription lookup
    depth = getattr(om, "depthTarget", None)
    if depth is not None:
        rid = getattr(depth, "resource", None)
        if rid is not None and not _is_null_id(rid):
            dsv_name = _resource_name(res_lookup, rid, default="")
            dsv_id = rid
    return rt0_name, rt0_size, rt0_format, n_rts, dsv_name, tuple(rt_ids), dsv_id


def _rt_size_from_textures(rid, tex_lookup):
    if rid is None or rid not in tex_lookup:
        return ""
    t = tex_lookup[rid]
    w = getattr(t, "width", 0) or 0
    h = getattr(t, "height", 0) or 0
    d = getattr(t, "depth", 0) or 0
    if d > 1:
        return "{}x{}x{}".format(w, h, d)
    return "{}x{}".format(w, h)


def _srv_ids_for_pipe(controller, gfxcap):
    """Cheap collection of the bound SRV resource IDs across all stages,
    pulled from descriptor access. Used to build a per-event bind
    fingerprint. Returns a tuple of stringified ResourceIds, sorted."""
    try:
        accesses = list(controller.GetDescriptorAccess())
    except Exception:
        return tuple()
    ids = set()
    for acc in accesses:
        kind = _desc_kind(getattr(acc, "type", None))
        if kind not in ("srv", "imagesampler", "uav"):
            continue
        # We need the resource id, which lives on the descriptor itself.
        desc = _fetch_one_descriptor(controller, gfxcap, acc, sampler=False)
        if desc is None:
            continue
        rid = getattr(desc, "resource", None)
        if rid is not None and not _is_null_id(rid):
            ids.add(str(rid))
    return tuple(sorted(ids))


def _bind_fingerprint(shader_names, rt0_id, srv_ids):
    """8-hex hash so that 'same kind of draw' events cluster.

    Inputs intentionally include shader names (carry Unity-style keywords)
    and the rt0 resource id (so the same shader rendering to two different
    targets clusters separately). SRV ids cover the input texture set."""
    h = hashlib.sha1()
    for k in sorted(shader_names.keys()):
        h.update(("{}={}\n".format(k, shader_names[k])).encode("utf-8"))
    h.update(("rt0={}\n".format(rt0_id)).encode("utf-8"))
    for s in srv_ids:
        h.update(("srv={}\n".format(s)).encode("utf-8"))
    return h.hexdigest()[:8]


def _classify_hint(class_label, modifiers, num_indices, num_instances,
                   n_rts, has_dsv, dispatch_xyz):
    """Cheap heuristic hints. Designed to almost-never produce false
    positives -- when in doubt, emit nothing rather than a wrong tag."""
    if class_label == "clear":   return "clear"
    if class_label == "copy":    return "copy"
    if class_label == "resolve": return "resolve"
    if class_label == "gen_mips":return "gen_mips"
    if class_label in ("dispatch", "mesh_dispatch", "dispatch_ray"):
        return "compute" if n_rts == 0 else ""
    # graphics-only hints
    if class_label == "draw":
        # fullscreen-coverage triangle / quad: small index count, no
        # depth read/write, one instance. Very common in post-processing.
        if (num_instances <= 1 and num_indices in (3, 4, 6) and
                not has_dsv):
            return "fullscreen"
        if num_instances >= 100:
            return "instanced_batch"
        if "indirect" in modifiers.split(","):
            return "indirect"
    return ""


def _blend_expr(b):
    """Compact 'src+op*dst' rendering of a BlendEquation. '' when None."""
    if b is None:
        return ""
    return "{}+{}*{}".format(
        _enum_str(getattr(b, "source", "?")),
        _enum_str(getattr(b, "operation", "?")),
        _enum_str(getattr(b, "destination", "?")))


def _pipeline_state_brief(pipe):
    """Extract compact pipeline state for inclusion in events.tsv.

    Returns a dict with stable keys; values default to "" when state
    isn't accessible (different APIs expose these via different attr
    paths -- D3D11/D3D12 use outputMerger.blendState etc.; Vulkan/GL
    don't, in which case the row carries empty cells rather than
    crashing). Used by `gfxcli list` so LLM consumers can grep
    cross-EID for blend / depth / stencil / cull patterns without
    exploding every EID via `gfxcli dump`.

    Compact value forms:
        rt0_blend_color   "off" | "Src+Add*InvSrcAlpha" form
        rt0_blend_alpha   "off" | "One+Add*Zero" form
        rt0_blend_mask    hex digits, e.g. "F" / "7" / "0"
        depth_test        "y" / "n"
        depth_write       "y" / "n"
        depth_func        short enum (e.g. "Less", "GreaterEqual");
                          empty when depth_test=n (the API field still
                          carries the previous draw's value when the
                          test is disabled, which would cause false
                          positives in awk queries like $23=="Less")
        stencil           "y" / "n"
        stencil_ref       int as string; empty when stencil=n (same
                          rationale: API field is stale when off)
        stencil_func      short enum (front face); empty when stencil=n
        cull              "back" / "front" / "none" (lower-cased)
    """
    rec = {
        "rt0_blend_color": "", "rt0_blend_alpha": "", "rt0_blend_mask": "",
        "depth_test": "", "depth_write": "", "depth_func": "",
        "stencil": "", "stencil_ref": "", "stencil_func": "",
        "cull": "",
    }
    if pipe is None:
        return rec
    om = getattr(pipe, "outputMerger", None)
    if om is not None:
        bs = getattr(om, "blendState", None)
        if bs is not None:
            blends = getattr(bs, "blends", []) or []
            if blends:
                b0 = blends[0]
                if bool(getattr(b0, "enabled", False)):
                    rec["rt0_blend_color"] = _blend_expr(getattr(b0, "colorBlend", None))
                    rec["rt0_blend_alpha"] = _blend_expr(getattr(b0, "alphaBlend", None))
                else:
                    rec["rt0_blend_color"] = "off"
                    rec["rt0_blend_alpha"] = "off"
                rec["rt0_blend_mask"] = "{:X}".format(int(getattr(b0, "writeMask", 0) or 0))
        ds = getattr(om, "depthStencilState", None)
        if ds is not None:
            depth_on = bool(getattr(ds, "depthEnable", False))
            rec["depth_test"]  = "y" if depth_on else "n"
            rec["depth_write"] = "y" if bool(getattr(ds, "depthWrites", False)) else "n"
            # depth_func is only emitted when depth_test=y -- the API
            # keeps the field's create-time value alive even when the
            # test is disabled, so emitting it unconditionally would
            # make `$23=="Less"` match draws with depth-test off but a
            # stale Less left in the state object.
            if depth_on:
                df = _enum_str(getattr(ds, "depthFunction", ""))
                if df and df != "?":
                    rec["depth_func"] = df
            stencil_on = bool(getattr(ds, "stencilEnable", False))
            rec["stencil"] = "y" if stencil_on else "n"
            # stencil_ref / stencil_func: same gating rationale -- the
            # front-face struct holds reference / function values even
            # when stencilEnable=False, so leave them blank in that case.
            if stencil_on:
                front = getattr(ds, "frontFace", None)
                if front is not None:
                    rec["stencil_ref"] = str(int(getattr(front, "reference", 0) or 0))
                    sf = _enum_str(getattr(front, "function", ""))
                    if sf and sf != "?":
                        rec["stencil_func"] = sf
    rast = getattr(pipe, "rasterizer", None)
    if rast is not None:
        rs = getattr(rast, "state", None)
        if rs is not None:
            cull = _enum_str(getattr(rs, "cullMode", ""))
            if cull and cull != "?":
                rec["cull"] = cull.lower()
    return rec


# ---------- record assembly -------------------------------------------------

def _make_event_record(a, parents, controller, gfxcap, res_lookup,
                       tex_lookup, shallow):
    """Build the dict that becomes a single events.tsv row."""
    flags_int = int(getattr(a, "flags", 0) or 0)
    cls = _action_class(flags_int)
    mods = _action_modifiers(flags_int)

    api_call = ""
    try:
        sdf = controller.GetStructuredFile()
        if sdf is not None and hasattr(a, "GetName"):
            api_call = str(a.GetName(sdf))
    except Exception:
        pass

    rec = {
        "eid": int(getattr(a, "eventId", 0) or 0),
        "action_id": int(getattr(a, "actionId", 0) or 0),
        "class": cls,
        "flags": mods,
        "api_call": api_call,
        "marker_path": _marker_path(parents),
        "vs_name": "", "ps_name": "", "gs_name": "", "cs_name": "",
        "hs_name": "", "ds_name": "",
        "rt0_name": "", "rt0_size": "", "rt0_format": "",
        "n_rts": 0, "dsv_name": "",
        # pipeline state -- populated only in enriched mode (see below)
        "rt0_blend_color": "", "rt0_blend_alpha": "", "rt0_blend_mask": "",
        "depth_test": "", "depth_write": "", "depth_func": "",
        "stencil": "", "stencil_ref": "", "stencil_func": "",
        "cull": "",
        "num_indices":   int(getattr(a, "numIndices", 0) or 0),
        "num_instances": int(getattr(a, "numInstances", 0) or 0),
        "dispatch_xyz": "",
        "indirect": "yes" if (flags_int & _AF_INDIRECT) else "",
        "bind_fp": "",
        "hint": "",
        # internal-only fields (not written to TSV but used by md writers)
        "_rt_ids": tuple(),
        "_dsv_id": None,
    }

    # dispatch dimensions
    dd = getattr(a, "dispatchDimension", None)
    if dd is not None:
        xyz = _xyz(dd)
        if xyz not in ("?", "(0, 0, 0)"):
            rec["dispatch_xyz"] = xyz

    if shallow:
        rec["hint"] = _classify_hint(cls, mods, rec["num_indices"],
                                     rec["num_instances"], 0, False,
                                     rec["dispatch_xyz"])
        return rec

    # ---- enriched: pull pipeline state for this event ----
    try:
        controller.SetFrameEvent(rec["eid"], True)
    except Exception:
        return rec
    _api, pipe = _get_pipe(controller, ErrorCollector())  # discard errors
    if pipe is None:
        rec["hint"] = _classify_hint(cls, mods, rec["num_indices"],
                                     rec["num_instances"], 0, False,
                                     rec["dispatch_xyz"])
        return rec

    snames = _shader_names_for_pipe(pipe, res_lookup)
    rec["vs_name"] = snames.get("vertex_shader", "")
    rec["ps_name"] = snames.get("pixel_shader", "")
    rec["gs_name"] = snames.get("geometry_shader", "")
    rec["cs_name"] = snames.get("compute_shader", "")
    rec["hs_name"] = snames.get("hull_shader", "")
    rec["ds_name"] = snames.get("domain_shader", "")

    rt0_name, rt0_size, rt0_format, n_rts, dsv_name, rt_ids, dsv_id = (
        _rt_info(pipe, res_lookup))
    # fill RT size from the texture description -- _rt_info doesn't have
    # access to tex_lookup
    if rt_ids:
        rt0_size = _rt_size_from_textures(rt_ids[0], tex_lookup)
    rec["rt0_name"]   = rt0_name
    rec["rt0_size"]   = rt0_size
    rec["rt0_format"] = rt0_format
    rec["n_rts"]      = n_rts
    rec["dsv_name"]   = dsv_name
    rec["_rt_ids"]    = rt_ids
    rec["_dsv_id"]    = dsv_id

    srv_ids = _srv_ids_for_pipe(controller, gfxcap)
    rt0_id = rt_ids[0] if rt_ids else None
    rec["bind_fp"] = _bind_fingerprint(snames, rt0_id, srv_ids)

    # Cross-EID grep affordances: blend / depth / stencil / cull on rt0.
    rec.update(_pipeline_state_brief(pipe))

    rec["hint"] = _classify_hint(cls, mods, rec["num_indices"],
                                 rec["num_instances"], n_rts,
                                 bool(dsv_id), rec["dispatch_xyz"])
    return rec


# ---------- TSV / Markdown writers ------------------------------------------

_EVENTS_TSV_COLUMNS = [
    "eid", "action_id", "class", "flags", "api_call", "marker_path",
    "vs_name", "ps_name", "gs_name", "cs_name", "hs_name", "ds_name",
    "rt0_name", "rt0_size", "rt0_format", "n_rts", "dsv_name",
    "rt0_blend_color", "rt0_blend_alpha", "rt0_blend_mask",
    "depth_test", "depth_write", "depth_func",
    "stencil", "stencil_ref", "stencil_func", "cull",
    "num_indices", "num_instances", "dispatch_xyz", "indirect",
    "bind_fp", "hint",
]


def _tsv_escape(v):
    """Strip characters that would break a TSV row (tab/newline/CR)."""
    s = str(v) if v is not None else ""
    return s.replace("\t", " ").replace("\n", " ").replace("\r", " ")


def _write_events_tsv(out_dir, records):
    path = out_dir / "events.tsv"
    with path.open("w", encoding="utf-8") as fp:
        fp.write("\t".join(_EVENTS_TSV_COLUMNS) + "\n")
        for r in records:
            row = [_tsv_escape(r.get(c, "")) for c in _EVENTS_TSV_COLUMNS]
            fp.write("\t".join(row) + "\n")
    return path


def _write_shaders_tsv(out_dir, records):
    """Unique-shader catalogue, one row per (stage, shader_name) pair."""
    catalog = {}
    for r in records:
        for stage in ("vs", "ps", "cs", "gs", "hs", "ds"):
            name = r.get(stage + "_name", "")
            if not name:
                continue
            key = (stage, name)
            entry = catalog.setdefault(key, {"first_eid": r["eid"], "n": 0})
            entry["n"] += 1
            if r["eid"] < entry["first_eid"]:
                entry["first_eid"] = r["eid"]
    rows = sorted(
        ((stage, name, v["first_eid"], v["n"]) for (stage, name), v
         in catalog.items()),
        key=lambda t: (-t[3], t[2], t[0], t[1]))
    path = out_dir / "shaders.tsv"
    with path.open("w", encoding="utf-8") as fp:
        fp.write("stage\tshader_name\tfirst_eid\tn_uses\n")
        for stage, name, first_eid, n in rows:
            fp.write("{}\t{}\t{}\t{}\n".format(
                stage, _tsv_escape(name), first_eid, n))
    return path, catalog


def _write_render_targets_md(out_dir, records, res_lookup, tex_lookup):
    """Per-render-target lifecycle: which EIDs wrote to it, plus the
    DSV side. Built only from enriched-mode data (skips silently in
    shallow mode)."""
    rt_to_eids = {}
    rt_meta = {}
    dsv_to_eids = {}
    dsv_meta = {}
    for r in records:
        for rid in r.get("_rt_ids", ()):
            rt_to_eids.setdefault(rid, []).append(r["eid"])
            if rid not in rt_meta:
                rt_meta[rid] = {
                    "name": _resource_name(res_lookup, rid, default="(unnamed)"),
                    "size": _rt_size_from_textures(rid, tex_lookup),
                }
        did = r.get("_dsv_id")
        if did is not None:
            dsv_to_eids.setdefault(did, []).append(r["eid"])
            if did not in dsv_meta:
                dsv_meta[did] = {
                    "name": _resource_name(res_lookup, did, default="(unnamed)"),
                    "size": _rt_size_from_textures(did, tex_lookup),
                }

    lines = [
        "# render target lifecycles",
        "",
        "Every render target / depth-stencil view that was bound for "
        "writing in this capture, with the full EID list for each. Use this "
        "when you know *what* a draw renders into ('the main HDR buffer', "
        "'the shadow map') but not *which EID*. The first / last EID per RT "
        "usually maps to a `Clear` and the last write of the pass.",
        "",
    ]
    if not rt_to_eids and not dsv_to_eids:
        lines.append("(shallow mode -- no per-event RT data was collected. "
                     "Re-run without `--shallow` to populate this file.)")
        lines.append("")
        _write_md(out_dir / "render_targets.md", lines)
        return

    def _summarize_eids(eids):
        eids = sorted(eids)
        if len(eids) <= 8:
            return ", ".join(str(e) for e in eids)
        return "{}, {}, {}, ... [{} more] ..., {}, {}, {}".format(
            eids[0], eids[1], eids[2],
            len(eids) - 6,
            eids[-3], eids[-2], eids[-1])

    if rt_to_eids:
        lines.append("## render targets ({} unique)".format(len(rt_to_eids)))
        lines.append("")
        lines.append("| resource_id | name | size | n_writes | first_eid | last_eid | eids (preview) |")
        lines.append("|-------------|------|------|----------|-----------|----------|----------------|")
        for rid, eids in sorted(rt_to_eids.items(), key=lambda kv: -len(kv[1])):
            meta = rt_meta[rid]
            es = sorted(eids)
            lines.append("| `{}` | `{}` | {} | {} | {} | {} | {} |".format(
                rid, meta["name"], meta["size"] or "?", len(es),
                es[0], es[-1], _summarize_eids(es)))
        lines.append("")

    if dsv_to_eids:
        lines.append("## depth-stencil views ({} unique)".format(len(dsv_to_eids)))
        lines.append("")
        lines.append("| resource_id | name | size | n_writes | first_eid | last_eid | eids (preview) |")
        lines.append("|-------------|------|------|----------|-----------|----------|----------------|")
        for rid, eids in sorted(dsv_to_eids.items(), key=lambda kv: -len(kv[1])):
            meta = dsv_meta[rid]
            es = sorted(eids)
            lines.append("| `{}` | `{}` | {} | {} | {} | {} | {} |".format(
                rid, meta["name"], meta["size"] or "?", len(es),
                es[0], es[-1], _summarize_eids(es)))
        lines.append("")

    _write_md(out_dir / "render_targets.md", lines)


def _write_markers_md(out_dir, all_actions_with_parents):
    """Marker tree: each push-marker action becomes a nested heading; its
    EID range and immediate child events are listed under it.

    `all_actions_with_parents` is the full walk -- includes markers and
    drawables both, in source order."""
    lines = [
        "# marker tree",
        "",
        "User-inserted debug markers (PushMarker / vkCmdBeginDebugUtilsLabel) "
        "and their EID ranges. Engines like Unity / Unreal use these to "
        "delimit passes -- a name like `Opaque/GBuffer` here gets a row in "
        "events.tsv's `marker_path` column for every draw inside it. "
        "Use this file to scope your grep to one rendering pass.",
        "",
    ]
    # collect: marker_action -> (depth, first_eid, last_eid, n_children_indexable)
    markers = []  # list of (depth, customName, first_eid, last_eid, n_indexable)
    for a, parents in all_actions_with_parents:
        flags = int(getattr(a, "flags", 0) or 0)
        if not (flags & _AF_PUSHMARKER):
            continue
        cn = getattr(a, "customName", "") or ""
        # depth = number of ancestor PushMarkers
        depth = sum(1 for p in parents
                    if int(getattr(p, "flags", 0) or 0) & _AF_PUSHMARKER)
        # walk the subtree to find first/last EID + indexable count
        first_eid = None
        last_eid = None
        n_idx = 0
        for sub, _sp in _walk_actions(getattr(a, "children", []) or []):
            sf = int(getattr(sub, "flags", 0) or 0)
            if _is_indexable(sf):
                e = int(getattr(sub, "eventId", 0) or 0)
                if first_eid is None or e < first_eid:
                    first_eid = e
                if last_eid is None or e > last_eid:
                    last_eid = e
                n_idx += 1
        markers.append((depth, cn, first_eid, last_eid, n_idx))

    if not markers:
        lines.append("(this capture has no debug markers)")
        lines.append("")
    else:
        for depth, cn, f, l, n in markers:
            heading = "#" * min(6, 2 + depth)
            range_str = ("eids {}-{}".format(f, l) if f is not None
                         else "no indexable children")
            lines.append("{} `{}` -- {} ({} indexable events)".format(
                heading, cn, range_str, n))
            lines.append("")

    _write_md(out_dir / "markers.md", lines)


def _write_events_md(out_dir, records, all_actions_with_parents):
    """Hierarchical view of events keyed by marker_path. Same data as
    events.tsv but organized for a human-style read."""
    by_path = {}
    for r in records:
        by_path.setdefault(r["marker_path"], []).append(r)

    lines = [
        "# events grouped by marker_path",
        "",
        "Same data as events.tsv but bucketed by `marker_path` so you can "
        "skim the pass structure visually. For `grep`-style lookup use "
        "events.tsv instead.",
        "",
    ]
    # stable order: preserve the order paths first appeared in the records
    seen = []
    for r in records:
        if r["marker_path"] not in seen:
            seen.append(r["marker_path"])
    for path in seen:
        rs = by_path[path]
        title = path if path else "(no marker)"
        lines.append("## `{}` ({} events)".format(title, len(rs)))
        lines.append("")
        for r in rs[:64]:
            extra = []
            if r["vs_name"] or r["ps_name"] or r["cs_name"]:
                names = " / ".join(n for n in
                                   [r["vs_name"], r["ps_name"], r["cs_name"]]
                                   if n)
                extra.append("`{}`".format(names))
            if r["rt0_name"]:
                extra.append("-> `{}`".format(r["rt0_name"]))
            if r["hint"]:
                extra.append("[{}]".format(r["hint"]))
            lines.append("- eid {} ({}{}) {} {}".format(
                r["eid"],
                r["class"],
                "/" + r["flags"] if r["flags"] else "",
                r["api_call"],
                " ".join(extra)))
        if len(rs) > 64:
            lines.append("- _... {} more events in this scope; see events.tsv ..._".format(
                len(rs) - 64))
        lines.append("")
    _write_md(out_dir / "events.md", lines)


def _write_index_readme(out_dir, records, rdc, controller, shader_catalog,
                         tsv_path, shallow):
    api = _api_name(controller)
    try:
        finfo = controller.GetFrameInfo()
        frame_num = getattr(finfo, "frameNumber", "?")
    except Exception:
        frame_num = "?"

    # class breakdown
    class_counts = {}
    for r in records:
        class_counts[r["class"]] = class_counts.get(r["class"], 0) + 1

    # marker paths sorted by event count
    marker_counts = {}
    for r in records:
        marker_counts[r["marker_path"]] = marker_counts.get(r["marker_path"], 0) + 1

    # top shaders
    top_shaders = sorted(
        ((stage, name, v["first_eid"], v["n"])
         for (stage, name), v in (shader_catalog or {}).items()),
        key=lambda t: (-t[3], t[2]))[:10]

    lines = [
        "# gfxcli list -- event index",
        "",
        "Capture: `{}` ({} mode)".format(rdc, "shallow" if shallow else "enriched"),
        "",
        "- api: {}".format(api),
        "- frame: {}".format(frame_num),
        "- indexable events: {}".format(len(records)),
        "",
        "## how to use this index",
        "",
        "The authoritative artifact is **`events.tsv`** -- one row per "
        "drawable event with grep-friendly columns. The `.md` files are "
        "convenience views built from the same data.",
        "",
        "1. Skim `## frame breakdown` and `## top shaders` below to "
        "understand what's in the capture.",
        "2. Pick one of the **grep recipes** below to narrow events.tsv "
        "down to a handful of EIDs.",
        "3. `gfxcli dump -r CAPTURE -e <eid>` on a candidate to inspect "
        "pipeline state + textures + cbuffers in detail.",
        "",
        "## events.tsv schema",
        "",
        "Tab-separated, first row is a header. Columns:",
        "",
        "| column | meaning |",
        "|--------|---------|",
        "| eid | event id -- pass to `gfxcli dump -e` |",
        "| action_id | sequential action id within the frame |",
        "| class | draw / dispatch / mesh_dispatch / dispatch_ray / clear / copy / resolve / gen_mips / other |",
        "| flags | secondary modifiers: indexed,instanced,indirect,auto,color,depthstencil,passboundary |",
        "| api_call | API method name (e.g. `ID3D11DeviceContext::DrawIndexedInstanced`) |",
        "| marker_path | push-marker chain `A > B > C` |",
        "| vs_name, ps_name, gs_name, cs_name, hs_name, ds_name | engine-side shader name per stage; carries Unity / Unreal keyword variants when the engine encodes them in the resource name |",
        "| rt0_name | first bound render target's engine name |",
        "| rt0_size | first RT WxH (or WxHxD for 3D) |",
        "| rt0_format | first RT format string |",
        "| n_rts | total bound RTs |",
        "| dsv_name | depth-stencil view engine name |",
        "| rt0_blend_color | rt0 blend `src+op*dst` form, or `off` |",
        "| rt0_blend_alpha | rt0 alpha-channel blend `src+op*dst`, or `off` |",
        "| rt0_blend_mask | rt0 color write mask (hex digits) |",
        "| depth_test / depth_write | `y` / `n` |",
        "| depth_func | depth compare op (e.g. `Less`, `GreaterEqual`). Empty when `depth_test=n` (the API field carries a stale value when the test is disabled). |",
        "| stencil | `y` / `n` (stencil test enabled) |",
        "| stencil_ref | front-face stencil reference value (int). Empty when `stencil=n`. |",
        "| stencil_func | front-face stencil compare op. Empty when `stencil=n`. |",
        "| cull | `back` / `front` / `none` |",
        "| num_indices / num_instances / dispatch_xyz | draw/dispatch counts |",
        "| indirect | `yes` if indirect call |",
        "| bind_fp | 8-hex hash of (shader names + rt0 + sorted SRV ids); same bind_fp == same kind of draw |",
        "| hint | cheap heuristic tag: fullscreen / instanced_batch / compute / clear / copy / indirect |",
        "",
        "## frame breakdown",
        "",
        "| class | count |",
        "|-------|-------|",
    ]
    for cls in ("draw", "dispatch", "mesh_dispatch", "dispatch_ray",
                "build_accstruct", "clear", "copy", "resolve", "gen_mips",
                "other"):
        c = class_counts.get(cls, 0)
        if c:
            lines.append("| {} | {} |".format(cls, c))
    lines.append("")

    if top_shaders:
        lines.append("## top shaders (by use count)")
        lines.append("")
        lines.append("| n_uses | stage | first_eid | shader_name |")
        lines.append("|--------|-------|-----------|-------------|")
        for stage, name, first_eid, n in top_shaders:
            disp_name = name.replace("|", "\\|")
            lines.append("| {} | {} | {} | `{}` |".format(
                n, stage, first_eid, disp_name))
        lines.append("")

    # top marker scopes
    if any(p for p in marker_counts):
        top_markers = sorted(
            ((p, c) for p, c in marker_counts.items() if p),
            key=lambda t: -t[1])[:10]
        if top_markers:
            lines.append("## top marker scopes (by event count)")
            lines.append("")
            lines.append("| n_events | marker_path |")
            lines.append("|----------|-------------|")
            for p, c in top_markers:
                lines.append("| {} | `{}` |".format(c, p))
            lines.append("")

    lines.extend([
        "## grep recipes",
        "",
        "These run against `events.tsv`. Replace `EVENTS` with the path.",
        "",
        "**By shader name / Unity keyword variant** (most common entry "
        "point for engine reverse-engineering):",
        "",
        "```sh",
        "# all draws using the HG_ENABLE_MV variant of HGRP/Lit",
        "grep -F 'HG_ENABLE_MV' EVENTS | awk -F'\\t' '$3==\"draw\"'",
        "",
        "# every event hitting any *Shadow* shader",
        "grep -i shadow EVENTS",
        "```",
        "",
        "**By marker / pass**:",
        "",
        "```sh",
        "# draws inside the GBuffer pass",
        "grep -P '\\tGBuffer' EVENTS",
        "",
        "# everything under a specific marker subtree",
        "grep -F 'Opaque > GBuffer' EVENTS",
        "```",
        "",
        "**By render target** (see also render_targets.md):",
        "",
        "```sh",
        "# all draws into render target named MainHDR",
        "awk -F'\\t' '$13 ~ /MainHDR/' EVENTS",
        "```",
        "",
        "**By draw class / modifier**:",
        "",
        "```sh",
        "# only compute dispatches",
        "awk -F'\\t' '$3==\"dispatch\"' EVENTS",
        "",
        "# instanced draws",
        "grep -P '\\tinstanced' EVENTS",
        "```",
        "",
        "**By pipeline state** -- blend / depth / stencil / cull on rt0. "
        "Column numbers: `rt0_blend_color=18`, `rt0_blend_alpha=19`, "
        "`rt0_blend_mask=20`, `depth_test=21`, `depth_write=22`, "
        "`depth_func=23`, `stencil=24`, `stencil_ref=25`, "
        "`stencil_func=26`, `cull=27`.",
        "",
        "```sh",
        "# transparency (alpha blend, premultiplied or not)",
        "awk -F'\\t' 'NR>1 && $18 ~ /SrcAlpha|InvSrcAlpha/' EVENTS",
        "",
        "# additive blend (HDR particles, glow)",
        "awk -F'\\t' 'NR>1 && $18 ~ /One\\+Add\\*One/' EVENTS",
        "",
        "# opaque draws (blend off)",
        "awk -F'\\t' 'NR>1 && $18==\"off\"' EVENTS",
        "",
        "# depth test off (UI, sky, debug overlays)",
        "awk -F'\\t' 'NR>1 && $21==\"n\"' EVENTS",
        "",
        "# depth read but no write (transparents, decals)",
        "awk -F'\\t' 'NR>1 && $21==\"y\" && $22==\"n\"' EVENTS",
        "",
        "# stencil test with specific reference",
        "awk -F'\\t' 'NR>1 && $24==\"y\" && $25==\"128\"' EVENTS",
        "",
        "# two-sided draws (hair, foliage, particles)",
        "awk -F'\\t' 'NR>1 && $27==\"none\"' EVENTS",
        "",
        "# combine: transparent draws into a specific RT",
        "awk -F'\\t' 'NR>1 && $18 ~ /SrcAlpha/ && $13 ~ /MainHDR/' EVENTS",
        "```",
        "",
        "**Cluster: find one representative per bind-fingerprint** "
        "(deduplicates 'same kind of draw'):",
        "",
        "```sh",
        "# print first eid for each unique bind_fp",
        "awk -F'\\t' 'NR>1 && !seen[$32]++ {print $1, $32, $7, $13}' EVENTS",
        "```",
        "",
        "**Find fullscreen / post-process candidates**:",
        "",
        "```sh",
        "awk -F'\\t' '$33==\"fullscreen\"' EVENTS",
        "```",
        "",
        "## files",
        "",
        "- `events.tsv` -- main grep target",
        "- `events.md` -- same data grouped by marker_path",
        "- `shaders.tsv` -- unique shader catalogue (use to pick a "
        "representative EID for each shader)",
        "- `render_targets.md` -- per-RT EID lifecycle",
        "- `markers.md` -- marker tree with EID ranges per scope",
        "",
    ])
    _write_md(out_dir / "README.md", lines)


# ---------- cmd_list orchestration -----------------------------------------

def cmd_list(args):
    _normalize_args_paths(args)
    _check_rdc(args.rdc)
    out = args.out if args.out is not None else (
        args.rdc.parent / "{}_index".format(args.rdc.stem))
    if out.exists() and args.out is None:
        try:
            shutil.rmtree(str(out))
        except OSError:
            pass
    out.mkdir(parents=True, exist_ok=True)

    bundle_root = _bootstrap_gfxcap_module()  # noqa: F841 -- side effects only
    gfxcap = _import_gfxcap()

    quiet = getattr(args, "quiet", False)
    if not quiet:
        print("[gfxcli] mode: {}".format("shallow" if args.shallow else "enriched"))
        print("[gfxcli] rdc:  {}".format(args.rdc))
        print("[gfxcli] out:  {}".format(out))

    try:
        capfile, controller = _open_capture(gfxcap, args.rdc)
    except Exception as e:
        print("fatal: cannot open capture: {}".format(e), file=sys.stderr)
        return 2

    try:
        roots = controller.GetRootActions()
    except Exception as e:
        print("fatal: GetRootActions failed: {}".format(e), file=sys.stderr)
        return 3

    res_lookup = _resource_lookup(controller)
    tex_lookup = _texture_lookup(controller)

    # First pass: full walk so we can index markers + provide accurate
    # marker_path for every drawable.
    all_walk = list(_walk_actions(roots))
    indexable = [(a, parents) for a, parents in all_walk
                 if _is_indexable(int(getattr(a, "flags", 0) or 0))]

    # Apply filter flags
    filter_classes = None
    if args.filter_flags:
        filter_classes = set(x.strip() for x in args.filter_flags.split(",")
                             if x.strip())
    marker_prefix = (args.marker_prefix or "").strip()

    def _keep(a, parents):
        flags = int(getattr(a, "flags", 0) or 0)
        cls = _action_class(flags)
        if args.skip_clears and cls == "clear":
            return False
        if args.skip_copies and cls in ("copy", "resolve"):
            return False
        if filter_classes and cls not in filter_classes:
            return False
        if marker_prefix:
            mp = _marker_path(parents)
            if not mp.startswith(marker_prefix):
                return False
        return True

    todo = [(a, p) for a, p in indexable if _keep(a, p)]
    total = len(todo)

    if not quiet:
        print("[gfxcli] {} indexable events in capture; {} after filters"
              .format(len(indexable), total))

    records = []
    next_progress = 100
    for i, (a, parents) in enumerate(todo):
        rec = _make_event_record(a, parents, controller, gfxcap, res_lookup,
                                  tex_lookup, args.shallow)
        records.append(rec)
        if not quiet and (i + 1) >= next_progress:
            print("[gfxcli] progress: {}/{} events".format(i + 1, total))
            sys.stdout.flush()
            next_progress = (i + 1) + 100

    tsv_path = _write_events_tsv(out, records)
    _shaders_path, shader_catalog = _write_shaders_tsv(out, records)
    _write_render_targets_md(out, records, res_lookup, tex_lookup)
    _write_markers_md(out, all_walk)
    _write_events_md(out, records, all_walk)
    _write_index_readme(out, records, args.rdc, controller, shader_catalog,
                        tsv_path, args.shallow)

    try:
        controller.Shutdown()
    except Exception:
        pass
    try:
        capfile.Shutdown()
    except Exception:
        pass

    if not quiet:
        print("[gfxcli] index written to: {}".format(out))
    return 0


# ===========================================================================
# argparse
# ===========================================================================

def _build_parser():
    p = argparse.ArgumentParser(
        prog="gfxcli",
        description="gfxcap analysis CLI -- export GPU pipeline state to a "
                    "Markdown bundle for AI consumption.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="verb", metavar="<verb>")
    sub.required = True

    p_dump = sub.add_parser(
        "dump",
        help="export a single EID's pipeline state",
        description="Export full pipeline state for one event ID into a "
                    "Markdown bundle.",
    )
    p_dump.add_argument("-r", "--rdc", required=True, type=Path,
                        help="path to the .rdc / .gcap capture file")
    p_dump.add_argument("-e", "--eid", required=True, type=int,
                        help="event ID to export")
    p_dump.add_argument("--out", type=Path, default=None,
                        help="output dir (default: <rdc-dir>/<rdc-stem>_eid<eid>/)")
    p_dump.add_argument("--quiet", action="store_true",
                        help="suppress per-step progress lines")
    p_dump.set_defaults(func=cmd_dump)

    p_list = sub.add_parser(
        "list",
        help="enumerate every event in a capture into a grep-friendly TSV "
             "+ Markdown index",
        description="Walk every action in a capture and write a per-event "
                    "TSV (events.tsv), plus convenience indexes "
                    "(events.md, shaders.tsv, render_targets.md, "
                    "markers.md, README.md) for LLM-friendly browsing.",
    )
    p_list.add_argument("-r", "--rdc", required=True, type=Path,
                        help="path to the .rdc / .gcap capture file")
    p_list.add_argument("--out", type=Path, default=None,
                        help="output dir (default: <rdc-dir>/<rdc-stem>_index/)")
    p_list.add_argument("--shallow", action="store_true",
                        help="skip per-event pipeline state queries -- "
                             "leaves shader / RT / fingerprint columns blank "
                             "but runs in seconds even on huge captures.")
    p_list.add_argument("--filter-flags", default=None,
                        help="comma-list of class labels to keep: "
                             "draw,dispatch,mesh_dispatch,dispatch_ray,"
                             "build_accstruct,clear,copy,resolve,gen_mips. "
                             "Default: keep all.")
    p_list.add_argument("--marker-prefix", default=None,
                        help="only keep events whose marker_path startswith "
                             "this substring (e.g. `Frame > Opaque`).")
    p_list.add_argument("--skip-clears", action="store_true",
                        help="drop class=clear events from the index")
    p_list.add_argument("--skip-copies", action="store_true",
                        help="drop class=copy and class=resolve events")
    p_list.add_argument("--quiet", action="store_true",
                        help="suppress progress lines")
    p_list.set_defaults(func=cmd_list)

    return p


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
