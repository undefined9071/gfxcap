"""Inject gfxcap.dll into target processes and trigger captures.

NOTE: targets whose executable manifest declares requireAdministrator (a
common case for games) cannot be launched via CreateProcess unless this
script runs elevated -- you'll see WinError 740. Use --elevate to relaunch
via UAC, or run from an already-elevated shell.

Two injection methods supported:

  (A) Standard injection [default]
      Uses dist/gfxcapcmd.exe to launch (or attach to) the target with
      capture hooks. Capture options and file paths are persisted per
      target in config/targets.json.

  (B) Proxy-DLL fallback
      Copies gfxcap.dll into the target's working directory and writes a
      libraries.txt that a sibling proxy DLL (e.g. a winhttp.dll/version.dll
      shim such as Xpl0itR/VersionShim) can chainload. Use only when (A)
      is not viable for the target.

Usage:
    python scripts/inject.py status
    python scripts/inject.py launch <target-name>
    python scripts/inject.py attach <pid> [--target <target-name>]
    python scripts/inject.py shim install <target-name>
    python scripts/inject.py shim uninstall <target-name>

Configure targets in config/targets.json (see config/targets.example.json).
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _paths import (
    DIST, CONFIG, CAPTURES, DLL_NAME, PRODUCT_BASE,
)

CMD_EXE = DIST / f"{PRODUCT_BASE}cmd.exe"
TARGETS_JSON = CONFIG / "targets.json"
TARGETS_EXAMPLE = CONFIG / "targets.example.json"


def is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_elevated() -> None:
    """Relaunch this Python script with UAC elevation. Stdout is detached."""
    import ctypes
    argv = [a for a in sys.argv if a != "--elevate"]
    params = " ".join(f'"{a}"' if " " in a else a for a in argv)
    print("[inject] re-launching elevated via UAC...")
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    if rc <= 32:
        sys.exit(f"UAC elevation failed (ShellExecute rc={rc})")
    sys.exit(0)

# === Default targets (written to config/targets.json on first run) ===
# Generic placeholder. Real targets live in the user's local config/targets.json
# (gitignored). See config/targets.example.json for the full schema.
DEFAULT_TARGETS: dict[str, dict] = {
    "example": {
        "exe": "C:/path/to/game/Game.exe",
        "working_dir": "C:/path/to/game",
        "capture_file": "captures/{name}_{ts}.gcap",
        "options": {
            # See `gfxcapcmd capture --help`. True = pass --opt-<name>.
            # int values pass --opt-<name> <value>.
            "ref-all-resources": False,
            "capture-all-cmd-lists": True,
            "hook-children": False,
            "api-validation": False,
        },
    },
}

# === VersionShim file conventions ===
LIBRARIES_TXT = "libraries.txt"
LIBRARIES_BAK = "libraries.txt.bak"
SHIM_NAMES = ["winhttp.dll", "version.dll"]


# ---- Config helpers ----

def load_config() -> dict[str, dict]:
    if TARGETS_JSON.exists():
        return json.loads(TARGETS_JSON.read_text(encoding="utf-8"))
    # First run: write defaults so user can edit
    save_config(DEFAULT_TARGETS)
    print(f"[inject] wrote default config: {TARGETS_JSON}")
    return DEFAULT_TARGETS


def save_config(cfg: dict[str, dict]) -> None:
    TARGETS_JSON.parent.mkdir(parents=True, exist_ok=True)
    TARGETS_JSON.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def get_target(cfg: dict[str, dict], name: str) -> dict:
    if name not in cfg:
        sys.exit(f"unknown target: {name!r} (configured: {list(cfg)})")
    return cfg[name]


def expand_capture_file(template: str, *, name: str) -> Path:
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    rel = template.format(name=name, ts=ts)
    p = Path(rel)
    return p if p.is_absolute() else CAPTURES.parent / p


# ---- Method A: RenderDoc standard via gfxcapcmd ----

def build_capture_args(target: dict, *, capture_file: Path) -> list[str]:
    """Build the option flags shared by `capture` and `inject` subcommands."""
    args: list[str] = []
    if "working_dir" in target:
        args += ["--working-dir", target["working_dir"]]
    args += ["--capture-file", str(capture_file)]
    for opt, val in target.get("options", {}).items():
        flag = f"--opt-{opt}"
        if val is True:
            args.append(flag)
        elif val is False:
            pass
        elif isinstance(val, (int, str)):
            args += [flag, str(val)]
    return args


def cmd_launch(name: str, *, wait: bool, dry_run: bool) -> int:
    cfg = load_config()
    target = get_target(cfg, name)

    if not CMD_EXE.exists():
        sys.exit(f"{CMD_EXE} missing -- run scripts/build.py first")
    exe = Path(target["exe"])
    if not exe.exists():
        sys.exit(f"target exe not found: {exe}")

    if not is_admin():
        print("[inject] WARNING: not running as administrator")
        print("[inject] If launch fails with WinError 740 / 'Failed to launch process',")
        print("[inject] re-run with --elevate or from an admin shell.")

    capture_file = expand_capture_file(target.get("capture_file", "captures/{name}_{ts}.gcap"), name=name)
    capture_file.parent.mkdir(parents=True, exist_ok=True)

    cmd = [str(CMD_EXE), "capture", *build_capture_args(target, capture_file=capture_file)]
    if wait:
        cmd.append("--wait-for-exit")
    cmd.append(str(exe))

    print(f"[inject] target:  {name}")
    print(f"[inject] capture: {capture_file}")
    print(f"[inject] cmd:     {' '.join(cmd)}")
    if dry_run:
        return 0
    return subprocess.run(cmd).returncode


def cmd_attach(pid: int, *, name: str | None, dry_run: bool) -> int:
    cfg = load_config()
    target = cfg.get(name) if name else {}

    if not CMD_EXE.exists():
        sys.exit(f"{CMD_EXE} missing -- run scripts/build.py first")

    capture_template = (target or {}).get("capture_file", "captures/pid{pid}_{ts}.gcap")
    capture_file = expand_capture_file(capture_template.replace("{pid}", str(pid)), name=name or "attach")
    capture_file.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(CMD_EXE), "inject",
        "--PID", str(pid),
        *build_capture_args(target, capture_file=capture_file),
    ]
    print(f"[inject] PID:     {pid}")
    print(f"[inject] capture: {capture_file}")
    print(f"[inject] cmd:     {' '.join(cmd)}")
    if dry_run:
        return 0
    return subprocess.run(cmd).returncode


# ---- Method B: VersionShim fallback ----

def find_shim(target_dir: Path) -> Path | None:
    for name in SHIM_NAMES:
        p = target_dir / name
        if p.exists():
            return p
    return None


def read_libraries(target_dir: Path) -> list[str]:
    f = target_dir / LIBRARIES_TXT
    if not f.exists():
        return []
    return f.read_text(encoding="utf-8", errors="replace").splitlines()


def write_libraries(target_dir: Path, lines: list[str], *, backup: bool = True) -> None:
    f = target_dir / LIBRARIES_TXT
    if backup and f.exists() and not (target_dir / LIBRARIES_BAK).exists():
        shutil.copy2(f, target_dir / LIBRARIES_BAK)
    f.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def shim_install(target_name: str, *, exclusive: bool) -> int:
    cfg = load_config()
    target = get_target(cfg, target_name)
    target_dir = Path(target["working_dir"])
    if not target_dir.exists():
        sys.exit(f"target dir doesn't exist: {target_dir}")

    src = DIST / DLL_NAME
    if not src.exists():
        sys.exit(f"{src} missing -- run scripts/build.py first")

    shim = find_shim(target_dir)
    if shim is None:
        print(f"WARNING: no shim DLL ({'/'.join(SHIM_NAMES)}) in {target_dir}")
        print("  VersionShim must be installed separately for inject to take effect.")
        print("  Get it from https://github.com/Xpl0itR/VersionShim")

    dst = target_dir / DLL_NAME
    print(f"[shim] copy {src.name} -> {target_dir}")
    shutil.copy2(src, dst)

    libs = read_libraries(target_dir)
    if exclusive:
        # Comment out non-comment, non-process-match entries (don't delete -- reversible)
        libs = [
            line if (not line.strip() or line.startswith("#") or line.startswith("*"))
            else f"# {line}  # disabled by gfxcap inject.py"
            for line in libs
        ]
    if DLL_NAME not in libs:
        libs.append(DLL_NAME)
    write_libraries(target_dir, libs)
    print(f"[shim] {LIBRARIES_TXT} updated")
    return 0


def shim_uninstall(target_name: str) -> int:
    cfg = load_config()
    target = get_target(cfg, target_name)
    target_dir = Path(target["working_dir"])

    libs = read_libraries(target_dir)
    new_libs = []
    for line in libs:
        if line.strip() == DLL_NAME:
            continue
        # restore commented entries that we disabled
        if line.endswith("  # disabled by gfxcap inject.py"):
            line = line.split("# ", 1)[1].split("  # disabled", 1)[0]
        new_libs.append(line)
    if new_libs != libs:
        write_libraries(target_dir, new_libs, backup=False)
        print(f"[shim] {LIBRARIES_TXT} restored")

    dll = target_dir / DLL_NAME
    if dll.exists():
        dll.unlink()
        print(f"[shim] removed {dll}")
    return 0


# ---- Status ----

def cmd_status() -> int:
    cfg = load_config()
    print(f"config:    {TARGETS_JSON}")
    print(f"gfxcapcmd: {CMD_EXE}  {'OK' if CMD_EXE.exists() else 'MISSING'}")
    print(f"gfxcap.dll: {DIST / DLL_NAME}  {'OK' if (DIST / DLL_NAME).exists() else 'MISSING'}")
    print()
    for name, target in cfg.items():
        exe = Path(target["exe"])
        target_dir = Path(target.get("working_dir", exe.parent))
        shim = find_shim(target_dir)
        libs = read_libraries(target_dir)
        active_libs = [l for l in libs if l.strip() and not l.startswith("#")]

        print(f"[{name}]")
        print(f"  exe:        {exe}  {'OK' if exe.exists() else 'NOT FOUND'}")
        print(f"  shim:       {shim.name if shim else '(none)'}")
        print(f"  libraries:  {len(active_libs)} active entries")
        for l in active_libs:
            mark = "<- ours" if l.strip() == DLL_NAME else ""
            print(f"    {l}  {mark}")
        gfxcap_present = (target_dir / DLL_NAME).exists()
        print(f"  gfxcap.dll in target dir: {'YES' if gfxcap_present else 'no'}")
    return 0


# ---- CLI ----

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--elevate", action="store_true",
                    help="Re-launch this script with UAC elevation (needed for admin-required targets)")
    sub = ap.add_subparsers(dest="action", required=True)

    sub.add_parser("status", help="show inject state for all configured targets")

    p_launch = sub.add_parser("launch", help="launch target with capture hooks (RenderDoc standard)")
    p_launch.add_argument("target", help="target name from config/targets.json")
    p_launch.add_argument("--wait", action="store_true", help="wait for target to exit")
    p_launch.add_argument("--dry-run", action="store_true")

    p_attach = sub.add_parser("attach", help="inject into already-running process by PID")
    p_attach.add_argument("pid", type=int)
    p_attach.add_argument("--target", help="apply this target's capture options")
    p_attach.add_argument("--dry-run", action="store_true")

    p_shim = sub.add_parser("shim", help="VersionShim fallback installation")
    sub_shim = p_shim.add_subparsers(dest="shim_action", required=True)
    p_si = sub_shim.add_parser("install")
    p_si.add_argument("target")
    p_si.add_argument("--no-exclusive", action="store_true",
                      help="don't disable other entries in libraries.txt")
    p_su = sub_shim.add_parser("uninstall")
    p_su.add_argument("target")

    args = ap.parse_args()

    if args.elevate and not is_admin():
        relaunch_elevated()  # never returns

    if args.action == "status":
        return cmd_status()
    if args.action == "launch":
        return cmd_launch(args.target, wait=args.wait, dry_run=args.dry_run)
    if args.action == "attach":
        return cmd_attach(args.pid, name=args.target, dry_run=args.dry_run)
    if args.action == "shim":
        if args.shim_action == "install":
            return shim_install(args.target, exclusive=not args.no_exclusive)
        if args.shim_action == "uninstall":
            return shim_uninstall(args.target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
