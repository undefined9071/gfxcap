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
    list   (planned) enumerate events / actions in a capture

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

Usage (from the bundle root, with the embed):
    analysis\\python36\\python.exe analysis\\gfxcli.py dump \\
        --rdc path\\to\\capture.rdc \\
        --eid 4302
"""
import argparse
import datetime
import os
import shutil
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
        ("byte_offset", offset),
        ("byte_size", size),
        ("element_size", getattr(desc, "elementByteSize", "?")),
        ("format", _enum_str(getattr(getattr(desc, "format", None), "type", "?"))),
        ("buffer_total_length", getattr(buf_desc, "length", "?")),
    ]

    try:
        data = bytes(controller.GetBufferData(rid, offset, size))
        bin_.write_bytes(data)
        fields.append(("bin", "buffer_t{}.bin ({} B)".format(slot, len(data))))
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
        ("byte_offset", offset),
        ("byte_size", size),
        ("element_size", getattr(desc, "elementByteSize", "?")),
        ("format", _enum_str(getattr(getattr(desc, "format", None), "type", "?"))),
        ("buffer_total_length", getattr(buf_desc, "length", "?")),
        ("counter_byte_offset", getattr(desc, "counterByteOffset", "?")),
        ("buffer_struct_count", getattr(desc, "bufferStructCount", "?")),
    ]

    try:
        data = bytes(controller.GetBufferData(rid, offset, size))
        bin_.write_bytes(data)
        fields.append(("bin", "uav_u{}.bin ({} B)".format(slot, len(data))))
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
    dds = stage_dir / "{}{}.dds".format(prefix, slot)
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

    dds_ok = _save_texture(controller, gfxcap, rid, dds, "DDS", errors,
                           "texture_dds", "{}/{}{}".format(stage_short, prefix, slot))
    png_ok = _save_texture(controller, gfxcap, rid, png, "PNG", errors,
                           "texture_png", "{}/{}{}".format(stage_short, prefix, slot))
    fields.append(("dds", "OK" if dds_ok else "FAILED"))
    fields.append(("png", "OK" if png_ok else "FAILED"))

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


def _save_texture(controller, gfxcap, resource_id, target_path, file_type,
                  errors, group, target_name):
    if resource_id is None or _is_null_id(resource_id):
        errors.add(group, target_name, "null resource id")
        return False
    try:
        TS = gfxcap.TextureSave()
        TS.resourceId = resource_id
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

def _export_input_assembly(out, controller, pipe, errors, counts, res_lookup=None):
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
            ("byte_offset", offset),
            ("byte_stride", stride),
            ("byte_size", size),
        ]
        if size > 0:
            try:
                data = bytes(controller.GetBufferData(rid, offset, size))
                bin_.write_bytes(data)
                fields.append(("bin", "vertex_buffer_{}.bin ({} B)".format(i, len(data))))
            except Exception as e:
                bin_.write_bytes(b"")
                fields.append(("bin", "FAILED: {}".format(e)))
                errors.add("vertex_buffer", "vb{}".format(i), "GetBufferData", e)
        else:
            try:
                data = bytes(controller.GetBufferData(rid, offset, 0))
                if data:
                    bin_.write_bytes(data)
                    fields.append(("bin", "vertex_buffer_{}.bin ({} B, full buffer)".format(i, len(data))))
                else:
                    fields.append(("bin", "(empty)"))
            except Exception as e:
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
            ("byte_offset", offset),
            ("byte_size", size),
            ("byte_stride", stride),
        ]
        if rid is not None and not _is_null_id(rid) and size > 0:
            try:
                data = bytes(controller.GetBufferData(rid, offset, size))
                bin_.write_bytes(data)
                fields.append(("bin", "index_buffer.bin ({} B)".format(len(data))))
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
    dds = om_dir / "{}.dds".format(name)
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
    dds_ok = _save_texture(controller, gfxcap, rid, dds, "DDS", errors,
                           prefix + "_dds", name)
    png_ok = _save_texture(controller, gfxcap, rid, png, "PNG", errors,
                           prefix + "_png", name)
    fields.append(("dds", "OK" if dds_ok else "FAILED"))
    fields.append(("png", "OK" if png_ok else "FAILED"))
    _write_md(md, _md_header(label, fields))
    return dds_ok or png_ok


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
    ("input_layout.md",            "vertex layout: how vertex buffer bytes map to vs inputs"),
    ("index_buffer.md",            "index buffer metadata"),
    ("index_buffer.bin",           "index buffer raw bytes"),
    ("blend_state.md",             "blend / per-rt blend / sample mask"),
    ("depth_stencil_state.md",     "depth and stencil test state"),
    ("rasterizer_state.md",        "fill / cull / depth bias / viewports / scissors"),
    ("depth_target.md",            "depth/stencil target metadata + dump status"),
    ("depth_target.dds",           "depth/stencil target as DDS"),
    ("depth_target.png",           "depth/stencil target as PNG (where convertible)"),
]

NAV_PREFIX_DESCRIPTIONS = [
    # (file-name prefix, register-letter, description-template "{}" gets the register / slot label)
    ("constant_buffer_b", "b", "constant buffer at register {} -- decoded values + raw bytes"),
    ("texture_t",         "t", "SRV texture at register {} -- DDS + PNG + metadata"),
    ("buffer_t",          "t", "SRV buffer at register {} -- raw bin + metadata"),
    ("uav_u",             "u", "UAV at register {} -- bin/dds + metadata"),
    ("sampler_s",         "s", "sampler at register {}"),
    ("vertex_buffer_",    "",  "vertex buffer at slot {}"),
    ("render_target_",    "",  "render target at slot {} -- DDS + PNG + metadata"),
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
    if name.startswith("texture_t") and (name.endswith(".dds") or name.endswith(".png")):
        return "SRV texture (binary)"
    if name.startswith("uav_u") and name.endswith(".bin"):
        return "UAV buffer raw bytes"
    if name.startswith("uav_u") and (name.endswith(".dds") or name.endswith(".png")):
        return "UAV texture (binary)"
    if name.startswith("vertex_buffer_") and name.endswith(".bin"):
        return "vertex buffer raw bytes"
    if name.startswith("buffer_t") and name.endswith(".bin"):
        return "SRV buffer raw bytes"
    if name.startswith("render_target_") and (name.endswith(".dds") or name.endswith(".png")):
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
        "buffer)? The per-file `.md` next to each `.bin` / `.dds` carries a "
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

        _export_bindings(out, controller, gfxcap, pipe_resource_id,
                         refls_by_stage, bound_stages, errors, counts,
                         res_lookup=res_lookup)

        _export_input_assembly(out, controller, pipe, errors, counts,
                               res_lookup=res_lookup)
        _export_output_merger(out, controller, gfxcap, pipe, errors, counts,
                              res_lookup=res_lookup)
        _export_rasterizer(out, controller, pipe, errors, counts)

    _write_readme(out, args.rdc, controller, action, args.eid, counts,
                  errors, bound_stages,
                  action_parents=action_parents, res_lookup=res_lookup)

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
# verb: list (planned)
# ===========================================================================

def cmd_list(args):
    print("error: 'list' is not implemented yet", file=sys.stderr)
    return 64


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
        help="(planned) enumerate events in a capture",
    )
    p_list.add_argument("-r", "--rdc", required=True, type=Path)
    p_list.set_defaults(func=cmd_list)

    return p


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
