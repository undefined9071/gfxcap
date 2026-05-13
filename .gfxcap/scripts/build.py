"""Build renderdoc.dll (or rebranded gfxcap.dll), the cmd tool, the shim, and
optionally the Qt UI.

RenderDoc on Windows uses bundled .sln/.vcxproj directly, NOT CMake.
We invoke MSBuild on individual .vcxproj files so we can opt in/out of the
Qt-dependent UI project without modifying the .sln.

Project-name-derived outputs (after rebrand):
  gfxcap/gfxcap.vcxproj                -> gfxcap.dll
  gfxcapcmd/gfxcapcmd.vcxproj          -> gfxcapcmd.exe
  gfxcapshim/gfxcapshim.vcxproj        -> gfxcapshim64.dll
  gfxcapui/gfxcapui_local.vcxproj      -> gfxcapui.exe   (Qt UI; --gui)

Usage:
    python scripts/build.py                # main + cmd + shim (no UI)
    python scripts/build.py --gui          # also build the UI
    python scripts/build.py --gui-only     # only the UI (and its prereqs)
    python scripts/build.py --shim-only    # toolchain test
    python scripts/build.py --main-only
    python scripts/build.py --config Development
"""
from __future__ import annotations
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _paths import WORK_SRC, DIST, PRODUCT_BASE, ORIGINAL_BASE


def find_msbuild() -> str:
    """Locate MSBuild.exe. PATH first, then known VS install locations."""
    p = shutil.which("MSBuild")
    if p:
        return p
    pf = os.environ.get("ProgramFiles") or r"C:\Program Files"
    pf_x86 = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    for base in (pf, pf_x86):
        for ver in ("2022", "2019"):
            for ed in ("Enterprise", "Professional", "Community", "BuildTools"):
                p = Path(base) / "Microsoft Visual Studio" / ver / ed / "MSBuild" / "Current" / "Bin" / "MSBuild.exe"
                if p.exists():
                    return str(p)
    sys.exit("MSBuild.exe not found - install Visual Studio 2019/2022")


def detect_brand() -> str:
    """Return 'gfxcap' if rebrand applied, else 'renderdoc'."""
    if (WORK_SRC / f"{PRODUCT_BASE}.sln").exists():
        return PRODUCT_BASE
    if (WORK_SRC / f"{ORIGINAL_BASE}.sln").exists():
        return ORIGINAL_BASE
    sys.exit(f"no .sln found in {WORK_SRC} - run prepare.py first")


def main_vcxproj(brand: str) -> Path:
    return WORK_SRC / brand / f"{brand}.vcxproj"


def shim_vcxproj(brand: str) -> Path:
    return WORK_SRC / f"{brand}shim" / f"{brand}shim.vcxproj"


def cmd_vcxproj(brand: str) -> Path:
    return WORK_SRC / f"{brand}cmd" / f"{brand}cmd.vcxproj"


def gui_vcxproj(brand: str) -> Path:
    """The Qt UI: 'qrenderdoc' upstream, 'gfxcapui' after rebrand."""
    if brand == PRODUCT_BASE:
        return WORK_SRC / "gfxcapui" / "gfxcapui_local.vcxproj"
    return WORK_SRC / "qrenderdoc" / "qrenderdoc_local.vcxproj"


# Prerequisites of the main project that MSBuild does NOT pick up automatically
# (listed as static-lib AdditionalDependencies, not ProjectReferences).
def main_prereqs(brand: str) -> list[Path]:
    bp = WORK_SRC / brand / "3rdparty" / "breakpad" / "client" / "windows"
    return [
        bp / "common.vcxproj",
        bp / "crash_generation" / "crash_generation_client.vcxproj",
        bp / "crash_generation" / "crash_generation_server.vcxproj",  # for cmd
        bp / "handler" / "exception_handler.vcxproj",
    ]


def make_build_env(*, trim_paths: bool = True) -> dict[str, str]:
    """Construct the env for MSBuild/cl.exe/link.exe.

    Two strip operations to keep absolute paths out of the DLL:
      - CL  /d1trimfile:<work_src>\\  -- strips __FILE__ macro prefix
      - LINK /PDBALTPATH:%_PDB%       -- strips PDB path to filename only

    NOTE -- THESE ARE LOCAL-BUILD ONLY. Do NOT mirror them into the GitHub
    Actions workflow. Two reasons, learned the hard way during v1.2.1:

      1. /d1trimfile interacts badly with the v140 toolset's PCH
         generation under MSBuild's /MP (multi-process) parallel build
         and produces a C1083 race on driver_dxgi / dxil / spirv,
         exactly like the /p: overrides documented in `msbuild_run_sln`
         below. /MP is unavoidable on CI (build time triples without
         it).

      2. /PDBALTPATH shortens the embedded PDB filename string inside
         IMAGE_DEBUG_DIRECTORY, which shifts subsequent .rdata offsets
         and changes the binary's PE shape. /RELEASE writes a non-zero
         PE Optional Header CheckSum field where prior releases had
         zero. Both are exactly the kind of PE-shape change that the
         target's AC PE-shape check has historically rejected at
         inject time -- the whole reason for the v140 pin in the
         first place.

    Net: a CI run with these env vars either fails to build at all,
    or produces a binary that fails to inject. We accept that shipped
    binaries carry the runner workspace path in __FILE__ strings and
    the IMAGE_DEBUG_DIRECTORY -- these are static string content with
    no runtime portability impact (the DLL never reads them).

    /JMC is suppressed via the MSBuild property `SupportJustMyCode=false`
    (passed in msbuild_run); putting it on the cl.exe command line as
    /JMC- gets overridden by MSBuild's own /JMC, so the property route is
    the only one that works.
    """
    env = os.environ.copy()
    if not trim_paths:
        return env

    trim = str(WORK_SRC).replace("/", "\\")
    if not trim.endswith("\\"):
        trim += "\\"

    # /D defines: silence breakpad-related deprecation warnings (v1.17 source uses
    # stdext::checked_array_iterator which is deprecated on newer MSVC).
    # NOTE: /guard:cf was attempted while bisecting a third-party AC's
    # PE-shape rejection (since the official v1.44 binary has a .gfids
    # section), but it makes renderdocshim fail to link due to its no-CRT
    # build. The right fix would be per-project, not env-wide. Re-enable
    # selectively if CFG turns out to be necessary.
    cl_extra = " ".join([
        f"/d1trimfile:{trim}",
        "/D_SILENCE_STDEXT_ARR_ITERS_DEPRECATION_WARNING",
        "/D_SILENCE_ALL_MS_EXT_DEPRECATION_WARNINGS",
    ])
    cl_existing = env.get("CL", "")
    env["CL"] = f"{cl_extra} {cl_existing}".strip()

    # /RELEASE writes the PE checksum into the Optional Header.
    # %_PDB% strips the embedded PDB path to filename only.
    # (/guard:cf removed -- see CL note above.)
    link_extra = "/RELEASE /PDBALTPATH:%_PDB%"
    link_existing = env.get("LINK", "")
    env["LINK"] = f"{link_extra} {link_existing}".strip()

    return env


def msbuild_run(msbuild: str, vcxproj: Path, *, config: str, jobs: int, verbosity: str,
                trim_paths: bool = True) -> int:
    """Invoke MSBuild on a single vcxproj. Sets SolutionDir so $(SolutionDir) resolves."""
    sln_dir = str(WORK_SRC).replace("/", "\\")
    if not sln_dir.endswith("\\"):
        sln_dir += "\\"

    cmd = [
        msbuild,
        str(vcxproj),
        f"/p:Configuration={config}",
        "/p:Platform=x64",
        f"/p:SolutionDir={sln_dir}",
        # v143 toolset enables /JMC by default, which adds a .msvcjmc section.
        # That section is unusual in production builds and is a useful signal
        # for anti-cheat heuristics. Force it off project-wide.
        "/p:SupportJustMyCode=false",
        f"/v:{verbosity}",
        "/nologo",
    ]
    cmd.append(f"/m:{jobs}" if jobs > 0 else "/m")
    env = make_build_env(trim_paths=trim_paths)
    print(f"[build] {vcxproj.name}")
    return subprocess.run(cmd, cwd=WORK_SRC, env=env).returncode


def msbuild_run_sln(msbuild: str, sln: Path, targets: list[str] | None, *, config: str,
                    jobs: int, verbosity: str, trim_paths: bool = True) -> int:
    """Invoke MSBuild on the .sln. Lets MSBuild resolve the full dependency
    graph (driver_*, breakpad, etc.) the way the upstream CI does it.
    Required when individual-vcxproj invocation breaks PCH-output directory
    creation in the v140 toolset.

    Pass `targets=None` to build the default Build target across the whole
    solution -- the upstream CI invocation. sln-level project-name targets
    have a brittle `/t:` syntax that varies by MSBuild version, so the
    safest path is to build everything and let unwanted projects fail
    silently if they can't (we ignore qrenderdoc's exit when no Qt is set).
    """
    cmd = [
        msbuild,
        str(sln),
        f"/p:Configuration={config}",
        "/p:Platform=x64",
        # Match the upstream baldurk CI invocation as closely as possible
        # for the v140 path. Extra /p: overrides we tried (SupportJustMyCode,
        # MultiProcessorCompilation, PreferredToolArchitecture) appear to
        # interact badly with v140 PCH generation under MSBuild and cause
        # C1083 races on driver_dxgi / dxil / spirv.
        f"/v:{verbosity}",
        "/nologo",
    ]
    if targets:
        cmd.extend(f"/t:{t}" for t in targets)
    cmd.append(f"/m:{jobs}" if jobs > 0 else "/m")
    env = make_build_env(trim_paths=trim_paths)
    label = ",".join(targets) if targets else "(all)"
    print(f"[build] {sln.name} targets={label}")
    return subprocess.run(cmd, cwd=WORK_SRC, env=env).returncode


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="Release", choices=["Release", "Development"])
    ap.add_argument("--main-only", action="store_true")
    ap.add_argument("--shim-only", action="store_true")
    ap.add_argument("--cmd-only", action="store_true")
    ap.add_argument("--gui", action="store_true",
                    help="Also build the Qt UI (gfxcapui.exe)")
    ap.add_argument("--gui-only", action="store_true",
                    help="Only build the Qt UI (and its prerequisites)")
    ap.add_argument("--no-cmd", action="store_true", help="Skip building gfxcapcmd.exe")
    ap.add_argument("--jobs", type=int, default=0)
    ap.add_argument("--no-copy", action="store_true")
    ap.add_argument("--no-trim", action="store_true",
                    help="Don't pass /d1trimfile (paths in __FILE__ will be absolute)")
    ap.add_argument("--verbosity", default="minimal",
                    choices=["quiet", "minimal", "normal", "detailed", "diagnostic"])
    args = ap.parse_args()

    brand = detect_brand()
    msbuild = find_msbuild()

    # Build via the .sln (the upstream CI route). Required for v140 because
    # individual-vcxproj invocation breaks PCH-output dir creation for
    # renderdoc.vcxproj's child driver projects.
    sln = WORK_SRC / f"{brand}.sln"
    if not sln.exists():
        sys.exit(f"sln missing: {sln}")

    # Build default target across the whole solution. The sln-level
    # `/t:<project>` syntax is brittle (MSB4057 across MSBuild versions);
    # building everything and post-filtering by output filename is the
    # path the upstream CI uses too.
    sln_targets: list[str] | None = None

    print(f"[build] brand:    {brand}")
    print(f"[build] config:   {args.config} x64")
    print(f"[build] msbuild:  {msbuild}")

    rc = msbuild_run_sln(msbuild, sln, sln_targets, config=args.config,
                         jobs=args.jobs, verbosity=args.verbosity,
                         trim_paths=not args.no_trim)
    if rc != 0:
        sys.exit(rc)

    if args.no_copy:
        return 0

    out_dir = WORK_SRC / "x64" / args.config
    if not out_dir.exists():
        print(f"[build] WARNING: output dir not found: {out_dir}")
        return 0

    DIST.mkdir(parents=True, exist_ok=True)
    found = 0
    # Project outputs + Qt/Python runtime DLLs deposited by qrenderdoc.vcxproj's
    # <Content CopyToOutputDirectory> entries. The Qt + python36 globs no-op
    # when the UI isn't built.
    runtime_globs = [
        f"{brand}*.dll",
        f"{brand}*.exe",
        f"{brand}*.pdb",
        "Qt5*.dll",
        "python3*.dll",
        # Python stdlib zip; Py_Initialize fails with 'No module named encodings'
        # without it, which manifests as the UI silently exiting at startup.
        "python3*.zip",
    ]
    for pat in runtime_globs:
        for p in out_dir.glob(pat):
            shutil.copy2(p, DIST / p.name)
            print(f"[build] {p.name} -> dist/")
            found += 1

    # Qt plugins (platforms/qwindows.dll, imageformats/qsvg.dll). Required at
    # runtime; Qt searches qtplugins/ relative to the executable.
    qtplugins_src = out_dir / "qtplugins"
    if qtplugins_src.is_dir():
        qtplugins_dst = DIST / "qtplugins"
        if qtplugins_dst.exists():
            shutil.rmtree(qtplugins_dst)
        shutil.copytree(qtplugins_src, qtplugins_dst)
        n_plugins = sum(1 for _ in qtplugins_dst.rglob("*.dll"))
        print(f"[build] qtplugins/ -> dist/qtplugins/ ({n_plugins} DLL(s))")
        found += n_plugins

    if found == 0:
        print(f"[build] no {brand}*.dll/.pdb found in {out_dir}")
        return 1
    print(f"[build] copied {found} file(s) to dist/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
