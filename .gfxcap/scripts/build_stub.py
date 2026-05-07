"""Compile the diagnostic stub DLL (stub/stub.cpp -> dist/stub_gfxcap.dll).

Uses cl.exe via vcvarsall.bat to set up the MSVC environment.
"""
from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _paths import ROOT, DIST


STUB_SRC = ROOT / "src" / "stub.cpp"
OUT_DLL = DIST / "stub_gfxcap.dll"


def find_vcvarsall() -> Path:
    pf = os.environ.get("ProgramFiles") or r"C:\Program Files"
    pf_x86 = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    for base in (pf, pf_x86):
        for ver in ("2022", "2019"):
            for ed in ("Enterprise", "Professional", "Community", "BuildTools"):
                p = Path(base) / "Microsoft Visual Studio" / ver / ed / "VC" / "Auxiliary" / "Build" / "vcvarsall.bat"
                if p.exists():
                    return p
    sys.exit("vcvarsall.bat not found - install Visual Studio 2019/2022")


def main() -> int:
    if not STUB_SRC.exists():
        sys.exit(f"stub source not found: {STUB_SRC}")

    vcvars = find_vcvarsall()
    DIST.mkdir(parents=True, exist_ok=True)

    # Compile. /LD = DLL, /MT = static CRT, no PDB to keep size minimal.
    obj_dir = ROOT / "build" / "stub"
    obj_dir.mkdir(parents=True, exist_ok=True)
    out_pdb = DIST / "stub_gfxcap.pdb"

    cl_args = (
        f'/LD /MT /nologo /O1 /GS- /Zi '
        f'/Fo"{obj_dir}\\\\" '
        f'/Fd"{out_pdb}" '
        f'"{STUB_SRC}" '
        f'/link /OUT:"{OUT_DLL}" /PDB:"{out_pdb}" /PDBALTPATH:%_PDB% kernel32.lib'
    )
    cmd = f'"{vcvars}" x64 && cl {cl_args}'
    print("[stub-build] cl args:", cl_args)
    rc = subprocess.run(cmd, shell=True, cwd=obj_dir).returncode
    if rc != 0:
        sys.exit(rc)

    if not OUT_DLL.exists():
        sys.exit(f"output not produced: {OUT_DLL}")
    print(f"[stub-build] {OUT_DLL.name} -> {OUT_DLL.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
