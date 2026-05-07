"""Scan a built DLL for forbidden detection strings.

Basic substring scan. A more thorough check uses IDA MCP to enumerate
exports, sections, and full string table -- that lives in skills (TBD).

Usage:
    python scripts/verify.py dist/gfxcap.dll
    python scripts/verify.py --all dist/      # scan every .dll in dir
"""
from __future__ import annotations
import argparse
import re
import sys
from pathlib import Path

# Strings that must NOT appear in a clean rebrand build.
FORBIDDEN_PATTERNS: list[bytes] = [
    # Original brand
    b"renderdoc",
    b"RenderDoc",
    b"RENDERDOC",
    b"Renderdoc",
    # Author / URLs
    b"Baldur Karlsson",
    b"baldurk",
    # Internal class/macro names
    b"RDCDriver",
    b"RDCASSERT",
    b"RDCERR",
    b"RDCLOG",
    # Log prefix
    b"RDOC ",
    # GUI / cmd binary references
    b"qrenderdoc",
    b"renderdoccmd",
    # Known patched-DLL fingerprints (third-party builds)
    b"JT_InlineHook",
    b"xenderdoc",
    b"_enderdoc",
]

# `.rdc` needs a regex anchor: substring matches against `.rdcarray` / `.rdcstr`
# (the rdc-prefixed container types we deliberately leave alone) would dwarf
# the few real `.rdc` file-extension hits. We treat `.rdc` as a hit only when
# it terminates a token (followed by a non-identifier byte or end of file).
RDC_EXT_RE = re.compile(rb"\.rdc(?=[^a-zA-Z0-9_]|$)")


def scan(dll: Path) -> list[tuple[str, int]]:
    """Return list of (pattern_str, count) for matches."""
    data = dll.read_bytes()
    hits: list[tuple[str, int]] = []
    for pat in FORBIDDEN_PATTERNS:
        c = data.count(pat)
        if c > 0:
            hits.append((pat.decode("latin-1"), c))
    rdc_hits = len(RDC_EXT_RE.findall(data))
    if rdc_hits > 0:
        hits.append((".rdc (file ext)", rdc_hits))
    return hits


def report(dll: Path) -> bool:
    """Print result for one DLL. Return True if clean."""
    if not dll.exists():
        print(f"[verify] {dll}: NOT FOUND")
        return False
    hits = scan(dll)
    if not hits:
        print(f"[verify] {dll.name}: CLEAN")
        return True
    print(f"[verify] {dll.name}: {sum(n for _, n in hits)} hits across {len(hits)} pattern(s)")
    for pat, c in sorted(hits, key=lambda kv: -kv[1]):
        print(f"  {pat:20s} x {c}")
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path, help="DLL file or directory containing DLLs")
    ap.add_argument("--all", action="store_true", help="Treat path as directory, scan all .dll within")
    args = ap.parse_args()

    if args.all or args.path.is_dir():
        all_clean = True
        for ext in ("*.dll", "*.exe"):
            for binary in sorted(args.path.glob(ext)):
                if not report(binary):
                    all_clean = False
        return 0 if all_clean else 1

    return 0 if report(args.path) else 1


if __name__ == "__main__":
    sys.exit(main())
