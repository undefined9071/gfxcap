"""Prepare the working build tree: copy upstream and apply rebrand.

Usage:
    python scripts/prepare.py            # incremental sync + rebrand
    python scripts/prepare.py --clean    # wipe build/src/ and re-sync from scratch
    python scripts/prepare.py --no-rebrand   # vanilla copy (for sanity check)
"""
from __future__ import annotations
import argparse
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _paths import UPSTREAM_RD, WORK_SRC, PATCHES, THIRDPARTY, PRODUCT_BASE, SOURCE_OVERRIDES
import rebrand as rb
import source_edits
import shutil as _sh
import subprocess as _sp


COPY_IGNORE = shutil.ignore_patterns(
    ".git", ".github",
    # NOTE: do NOT use 'build*' here -- it would match files like build_info.h
    "build", "out", "cmake-build-*",
    ".vs", ".vscode",
    "*.user", "*.suo", "*.sln.cache",
    # Exclude our own additions when the repo IS the upstream (fork layout):
    ".gfxcap",
)


def sync_source(src: Path, dst: Path, *, clean: bool = False) -> None:
    """Copy src to dst. If clean, remove dst first; otherwise incremental update."""
    if clean and dst.exists():
        print(f"[prepare] clean: removing {dst}")
        shutil.rmtree(dst)

    if not dst.exists():
        print(f"[prepare] full copy {src} -> {dst}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, ignore=COPY_IGNORE)
        return

    print(f"[prepare] incremental sync {src} -> {dst}")
    skip = {".git", ".github", "build", ".vs", ".gfxcap"}
    for sp in src.rglob("*"):
        if any(part in skip for part in sp.relative_to(src).parts):
            continue
        if not sp.is_file():
            continue
        rel = sp.relative_to(src)
        dp = dst / rel
        if not dp.exists() or sp.stat().st_mtime > dp.stat().st_mtime:
            dp.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sp, dp)


def merge_thirdparty(work_dir: Path) -> int:
    """Copy custom/3rdparty/<lib>/ entries into <work_dir>/<PRODUCT_BASE>/3rdparty/.

    These are libraries we add to upstream RenderDoc (e.g. minhook for stealth
    hooking). Skips .git directories. Returns count of copied trees.
    """
    if not THIRDPARTY.exists():
        return 0
    dst_root = work_dir / PRODUCT_BASE / "3rdparty"
    if not dst_root.exists():
        return 0
    n = 0
    for src in THIRDPARTY.iterdir():
        if not src.is_dir():
            continue
        dst = dst_root / src.name
        if dst.exists():
            _sh.rmtree(dst)
        _sh.copytree(src, dst, ignore=_sh.ignore_patterns(".git", ".github"))
        n += 1
    return n


def apply_source_overrides(work_dir: Path) -> int:
    """Copy full-file replacements from custom/source_overrides/ over the work tree.

    Use for changes too complex for patch hunks (multiple multi-line edits in
    one file, line-ending sensitivity, etc.). Each path mirrors the work-tree
    layout, e.g. source_overrides/gfxcap/os/win32/win32_hook.cpp replaces
    build/src/gfxcap/os/win32/win32_hook.cpp.
    """
    if not SOURCE_OVERRIDES.exists():
        return 0
    n = 0
    for src in SOURCE_OVERRIDES.rglob("*"):
        if not src.is_file() or src.name.startswith("."):
            continue
        rel = src.relative_to(SOURCE_OVERRIDES)
        dst = work_dir / rel
        if not dst.parent.exists():
            continue
        _sh.copy2(src, dst)
        n += 1
    return n


def apply_patches(work_dir: Path) -> int:
    """Apply patches/*.patch in lexical order via GNU patch.

    Patches are unified diffs with `gfxcap/...` paths (rebranded tree).
    Use git-format-patch style headers (a/ b/ prefixes), -p1 strips the prefix.
    """
    if not PATCHES.exists():
        return 0
    patches = sorted(PATCHES.glob("*.patch"))
    if not patches:
        return 0

    # Prefer Git for Windows' patch (~2.7) over Strawberry Perl's older 2.5.9.
    # On the GHA windows-2025 runner Strawberry comes first on PATH and its
    # patch.exe blows up internally on our patch hunks (patch.c:354).
    patch_exe = None
    for cand in (
        r"C:\Program Files\Git\usr\bin\patch.exe",
        r"C:\Program Files (x86)\Git\usr\bin\patch.exe",
    ):
        if Path(cand).exists():
            patch_exe = cand
            break
    if not patch_exe:
        patch_exe = _sh.which("patch")
    if not patch_exe:
        sys.exit("patch.exe not found (install Git for Windows)")
    print(f"[prepare] using patch: {patch_exe}")

    n = 0
    for p in patches:
        # --forward: skip already-applied patches without erroring
        # --batch: never prompt
        # --silent: suppress non-error output (we'll report success)
        cmd = [patch_exe, "-p1", "--forward", "--batch", "-i", str(p)]
        rc = _sp.run(cmd, cwd=work_dir, capture_output=True, text=True)
        if rc.returncode == 0:
            print(f"[prepare] applied {p.name}")
            n += 1
        elif "already applied" in (rc.stdout + rc.stderr).lower() or \
             "previously applied" in (rc.stdout + rc.stderr).lower():
            print(f"[prepare] skipped {p.name} (already applied)")
        else:
            sys.stderr.write(rc.stdout + rc.stderr)
            sys.exit(f"failed to apply {p.name}")
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--clean", action="store_true", help="Wipe working tree and re-copy")
    ap.add_argument("--no-rebrand", action="store_true", help="Skip rebrand (vanilla copy)")
    ap.add_argument("--no-retarget", action="store_true",
                    help="Skip PlatformToolset retarget (keep v140 from upstream). "
                         "Requires v140 toolset (VS2015 build tools) to be installed.")
    args = ap.parse_args()

    if not UPSTREAM_RD.exists():
        sys.exit(f"upstream missing: {UPSTREAM_RD}")

    t0 = time.time()
    sync_source(UPSTREAM_RD, WORK_SRC, clean=args.clean)
    print(f"[prepare] sync done in {time.time() - t0:.1f}s")

    if args.no_retarget:
        print("[prepare] --no-retarget: keeping upstream PlatformToolset (v140)")
        # NOTE: do NOT call disable_mp / disable_jmc / enable_cfg here.
        # The upstream CI builds the vcxproj files unmodified (MP=true,
        # no JMC overrides) under v140 successfully; touching them
        # breaks PCH generation.
    else:
        # Retarget VS 2015 (v140) vcxproj files to v143 (VS 2022).
        n_retargeted = rb.retarget_toolset(WORK_SRC)
        print(f"[prepare] retargeted {n_retargeted} vcxproj to PlatformToolset=v143")

        # Disable /JMC project-wide -- only relevant for v143; v140 doesn't
        # support /JMC at all (the section won't be emitted).
        n_jmc = rb.disable_jmc(WORK_SRC)
        print(f"[prepare] disabled JMC in {n_jmc} vcxproj")

    # Enable CFG project-wide (except CRT-less projects). The .gfids
    # section it produces is part of the production-build PE shape that
    # the upstream baldurk distribution emits; without it some anti-cheat
    # layers reject the DLL.
    n_cfg = rb.enable_cfg(WORK_SRC)
    print(f"[prepare] enabled CFG in {n_cfg} vcxproj")

    if args.no_rebrand:
        print("[prepare] --no-rebrand: skipping rebrand")
        return 0

    t1 = time.time()
    print("[prepare] applying rebrand...")
    stats = rb.apply_full(WORK_SRC)
    print(f"[prepare] rebrand done in {time.time() - t1:.1f}s")

    n_3p = merge_thirdparty(WORK_SRC)
    if n_3p:
        print(f"[prepare] merged {n_3p} 3rdparty libs into work tree")

    t2 = time.time()
    n_patches = apply_patches(WORK_SRC)
    print(f"[prepare] applied {n_patches} patch(es) in {time.time() - t2:.1f}s")

    n_overrides = apply_source_overrides(WORK_SRC)
    if n_overrides:
        print(f"[prepare] applied {n_overrides} source override(s)")

    n_applied, n_skipped = source_edits.apply(WORK_SRC)
    print(f"[prepare] source_edits: {n_applied} applied, {n_skipped} skipped")
    print(f"  files scanned:    {stats.files_scanned}")
    print(f"  files modified:   {stats.files_modified}")
    print(f"  bytes replaced:   {stats.bytes_replaced}")
    print(f"  paths renamed:    {stats.paths_renamed}")
    print(f"  dirs deleted:     {stats.dirs_deleted}")
    if stats.pattern_hits:
        print("  top pattern hits:")
        top = sorted(stats.pattern_hits.items(), key=lambda kv: -kv[1])[:8]
        for pat, n in top:
            print(f"    {pat:30s} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
