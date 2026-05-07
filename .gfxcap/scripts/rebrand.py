"""Rebrand the working source tree: replace RenderDoc identifiers with custom brand.

Applied to a *working* tree (custom/build/src/), never to upstream (github/renderdoc/).
Idempotent: re-applying produces the same output.

Operations (in order):
    1. delete_dirs   — remove unused subtrees (Android etc.)
    2. rename_paths  — rename files/dirs containing 'renderdoc'
    3. rebrand_tree  — apply byte-level replacements in source files

The patterns are case-sensitive. Order matters: longest/most-specific first so that
shorter patterns don't pre-empt longer ones.
"""
from __future__ import annotations
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# (find, replace) — case-sensitive byte-level. Order: most-specific first.
REBRAND_PATTERNS: list[tuple[bytes, bytes]] = [
    # --- specific public API identifiers (do before RENDERDOC_ macro prefix) ---
    (b"RENDERDOC_API_VERSION", b"GFXCAP_API_VERSION"),
    (b"RENDERDOC_GetAPI", b"GFXCAP_GetAPI"),

    # --- author / branding strings ---
    (b"Baldur Karlsson", b"Anon Anonymous"),
    (b"baldurk/renderdoc", b"anon/gfxcap"),
    (b"baldurk", b"anon"),

    # --- specific RDC* class/macro names (before RDC_ prefix) ---
    (b"RDCDriver", b"GfxDriver"),
    (b"RDCASSERTEQUAL", b"GFXASSERTEQUAL"),
    (b"RDCASSERTMSG", b"GFXASSERTMSG"),
    (b"RDCASSERT", b"GFXASSERT"),
    (b"RDCCOMPILE_ASSERT", b"GFXCOMPILE_ASSERT"),
    (b"RDCERR", b"GFXERR"),
    (b"RDCLOG", b"GFXLOG"),
    (b"RDCWARN", b"GFXWARN"),
    (b"RDCFATAL", b"GFXFATAL"),
    (b"RDCDEBUG", b"GFXDEBUG"),
    (b"RDCDUMP", b"GFXDUMP"),
    (b"RDCEvent", b"GfxEvent"),
    (b"RDCFile", b"GfxFile"),
    (b"RDCFlag", b"GfxFlag"),

    # --- RENDERDOC_ macro/identifier prefix ---
    (b"RENDERDOC_", b"GFXCAP_"),

    # --- generic RDC_ macro prefix ---
    (b"RDC_", b"GFX_"),

    # --- Q-prefixed (Qt UI) variants. Must precede the bare 'renderdoc' rules
    #     so that the GUI binary becomes 'gfxcapui' (with no 'renderdoc' substring
    #     anywhere, including the executable filename), not 'qgfxcap'. The aim is
    #     to leave no token containing 'renderdoc' in the deployed bundle, since
    #     some games' anti-cheat / monitoring scans process names and window
    #     titles for that string. ---
    (b"qrenderdoc", b"gfxcapui"),
    (b"QRenderDoc", b"GfxCapUI"),

    # --- main brand (case-sensitive variants) ---
    (b"RenderDoc", b"GfxCap"),
    (b"Renderdoc", b"Gfxcap"),
    (b"renderdoc", b"gfxcap"),
    (b"RENDERDOC", b"GFXCAP"),

    # --- log prefix only (capture file extension stays as .rdc for
    # rdc-cli / upstream tooling compatibility; the AC checks DLL
    # content not capture-file extensions) ---
    (b"RDOC ", b"GCAP "),

    # --- residual lowercase rdoc (last; most other rdoc occurrences are inside renderdoc) ---
    (b"rdoc", b"gcap"),
]

# File extensions to process (text/source files). Other files are skipped.
TEXT_EXTENSIONS = {
    ".cpp", ".cxx", ".cc", ".c",
    ".h", ".hpp", ".hxx", ".inl",
    ".cmake", ".txt",
    ".rc", ".rc2",
    ".py", ".pyi",
    ".vcxproj", ".filters", ".props",
    ".sln",
    ".json", ".xml", ".plist",
    ".md", ".rst",
    ".sh", ".bat", ".ps1", ".cmd",
    ".vert", ".frag", ".comp", ".geom", ".tese", ".tesc",
    ".hlsl", ".fx", ".glsl",
    ".html", ".css", ".js",
    # SWIG interface files; they %import each other by name, so the rename
    # of e.g. renderdoc.i -> gfxcap.i must be reflected in the source text too.
    ".i", ".swg",
    # Visual Studio project files we missed: .vcxproj.filters above already
    # handled, but .pro (qmake) and .natvis (debugger visualisers) reference
    # binary names that need rewriting.
    ".pro", ".pri", ".natvis",
    # Qt: .ui (Designer XML) carries dialog/window labels that get embedded
    # into the executable via uic + the resource compiler, so 'RenderDoc'
    # text inside an <about> dialog still ships in the EXE if untouched.
    # .qrc indexes binary resources but also contains brand-suffixed paths.
    ".ui", ".qrc",
}

# Directories never traversed.
SKIP_DIRS = {".git", ".github", "build", "dist", ".vs", "node_modules", "__pycache__"}

# Subdirectories deleted entirely.
# NOTE: renderdoc/android/ contains Windows-side device discovery code that
# is compiled into the main DLL (not just an Android target). Don't delete it
# unless we also patch the vcxproj to remove the source references.
DELETE_DIRS: list[str] = [
    # Empty for now. Add only what's safe to remove (no Windows compile deps).
]

# Files that look text but should NOT be processed (e.g. binary blobs that match SUFFIX list)
SKIP_FILE_NAMES: set[str] = set()


@dataclass
class RebrandStats:
    files_scanned: int = 0
    files_modified: int = 0
    bytes_replaced: int = 0
    paths_renamed: int = 0
    dirs_deleted: int = 0
    pattern_hits: dict[str, int] = field(default_factory=dict)


def _rebrand_bytes(data: bytes, stats: RebrandStats | None = None) -> tuple[bytes, int]:
    """Apply all patterns to a byte string. Returns (new_data, total_replacements)."""
    total = 0
    for find, replace in REBRAND_PATTERNS:
        if find in data:
            n = data.count(find)
            data = data.replace(find, replace)
            total += n
            if stats is not None:
                key = find.decode("latin-1")
                stats.pattern_hits[key] = stats.pattern_hits.get(key, 0) + n
    return data, total


def _rebrand_utf16le(data: bytes, stats: RebrandStats | None) -> tuple[bytes, int]:
    """Apply patterns to UTF-16 LE-encoded data. Preserves BOM if present."""
    bom = b""
    if data.startswith(b"\xff\xfe"):
        bom = b"\xff\xfe"
        data = data[2:]
    try:
        text = data.decode("utf-16-le")
    except UnicodeDecodeError:
        return bom + data, 0
    total = 0
    for find_b, replace_b in REBRAND_PATTERNS:
        find_s = find_b.decode("latin-1")
        replace_s = replace_b.decode("latin-1")
        if find_s in text:
            n = text.count(find_s)
            text = text.replace(find_s, replace_s)
            total += n
            if stats is not None:
                key = find_s + " (utf16)"
                stats.pattern_hits[key] = stats.pattern_hits.get(key, 0) + n
    return bom + text.encode("utf-16-le"), total


def rebrand_file(path: Path, stats: RebrandStats | None = None) -> int:
    """Rebrand a single file in place. Auto-detects UTF-16 LE via BOM.

    Returns total replacement count.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return 0

    # Detect UTF-16 LE: explicit BOM, or .rc files (which are UTF-16 by convention)
    is_utf16 = data.startswith(b"\xff\xfe") or (
        path.suffix.lower() in {".rc", ".rc2"} and len(data) >= 2 and data[1] == 0x00
    )

    if is_utf16:
        new_data, n = _rebrand_utf16le(data, stats)
    else:
        new_data, n = _rebrand_bytes(data, stats)

    if n > 0:
        path.write_bytes(new_data)
    return n


def _should_process(path: Path) -> bool:
    if path.name in SKIP_FILE_NAMES:
        return False
    if path.suffix.lower() in TEXT_EXTENSIONS:
        return True
    if not path.suffix and path.name in {"CMakeLists", "Makefile"}:
        return True
    return False


def rebrand_tree(root: Path, stats: RebrandStats | None = None) -> RebrandStats:
    """Walk tree, applying rebrand to all text files."""
    if stats is None:
        stats = RebrandStats()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            if not _should_process(p):
                continue
            stats.files_scanned += 1
            n = rebrand_file(p, stats)
            if n > 0:
                stats.files_modified += 1
                stats.bytes_replaced += n
    return stats


def _rename_basename(name: str) -> str:
    """Apply rebrand to a single path component (file or dir name)."""
    new = name
    # Q-prefixed (Qt UI) variants first so the directory and exe become
    # 'gfxcapui' rather than 'qgfxcap'. See REBRAND_PATTERNS comment.
    new = new.replace("qrenderdoc", "gfxcapui")
    new = new.replace("QRenderDoc", "GfxCapUI")
    new = new.replace("RenderDoc", "GfxCap")
    new = new.replace("Renderdoc", "Gfxcap")
    new = new.replace("renderdoc", "gfxcap")
    new = new.replace("RENDERDOC", "GFXCAP")
    return new


def rename_paths(root: Path, stats: RebrandStats | None = None) -> RebrandStats:
    """Rename files and directories containing 'renderdoc' substring.

    Walk bottom-up so we rename children before their parents.
    """
    if stats is None:
        stats = RebrandStats()
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        dp = Path(dirpath)
        for fn in filenames:
            new_fn = _rename_basename(fn)
            if new_fn != fn:
                src = dp / fn
                dst = dp / new_fn
                if dst.exists():
                    dst.unlink()
                src.rename(dst)
                stats.paths_renamed += 1
        for dn in dirnames:
            new_dn = _rename_basename(dn)
            if new_dn != dn:
                src = dp / dn
                dst = dp / new_dn
                if dst.exists():
                    shutil.rmtree(dst)
                src.rename(dst)
                stats.paths_renamed += 1
    return stats


def delete_dirs(root: Path, paths: list[str], stats: RebrandStats | None = None) -> int:
    """Remove directories listed by relative path. Tolerant of missing entries."""
    if stats is None:
        stats = RebrandStats()
    for rel in paths:
        p = root / rel
        if p.exists() and p.is_dir():
            shutil.rmtree(p)
            stats.dirs_deleted += 1
    return stats.dirs_deleted


def apply_full(root: Path) -> RebrandStats:
    """Run delete_dirs -> rebrand_tree -> rename_paths in the canonical order.

    Source content is rebranded BEFORE renaming so that include paths
    (e.g. `#include "renderdoc/api/foo.h"`) are updated to reference the
    new directory name (`gfxcap/api/foo.h`) before that directory exists.
    Both orderings are correct since changes are textual; this order is
    chosen for predictable diffing.

    Toolset retarget (v140 -> v143) is NOT applied here -- call retarget_toolset
    separately. That step is universal regardless of whether rebrand is desired.
    """
    stats = RebrandStats()
    delete_dirs(root, DELETE_DIRS, stats)
    rebrand_tree(root, stats)
    rename_paths(root, stats)
    return stats


# === Toolset retarget ===
# Upstream RenderDoc vcxproj files target VS 2015 (v140). Modern MSBuild on
# VS 2017/2019/2022 needs the v140 toolset OR an explicit retarget.
# We retarget to v143 (VS 2022) at prepare time so build doesn't require
# the old toolset to be installed.

import re

# qrenderdoc_local.vcxproj uses '<PlatformToolSet>' (capital S) inconsistently
# with the rest of the tree's '<PlatformToolset>'. Match both casings.
TOOLSET_RETARGET_RE = re.compile(
    rb"<PlatformTool([Ss])et>v14[012]</PlatformTool([Ss])et>"
)


def retarget_toolset(root: Path) -> int:
    """Bump PlatformToolset to v143 in every vcxproj. Idempotent."""
    n_files = 0
    for p in root.rglob("*.vcxproj"):
        try:
            data = p.read_bytes()
        except OSError:
            continue
        new_data, n = TOOLSET_RETARGET_RE.subn(
            rb"<PlatformTool\1et>v143</PlatformTool\2et>", data
        )
        if n > 0:
            p.write_bytes(new_data)
            n_files += 1
    return n_files


# v143 toolset enables /JMC by default, which adds a `.msvcjmc` PE section
# that is rare in production builds and a useful signal for anti-cheat
# heuristics. Inject SupportJustMyCode=false into every ClCompile block in
# every vcxproj so cl.exe doesn't see /JMC at all.
#
# Project-property route (`/p:SupportJustMyCode=false` on the MSBuild command
# line) does NOT work: v143's Microsoft.Cl.Common.props sets the default to
# true unconditionally, and a ClCompile metadata value is needed to override
# it.
JMC_INSERT_RE = re.compile(
    rb"(<ItemDefinitionGroup(?:\s[^>]*)?>\s*<ClCompile>)(?![^<]*<SupportJustMyCode)",
    re.DOTALL,
)


def disable_jmc(root: Path) -> int:
    """Add <SupportJustMyCode>false</SupportJustMyCode> to ClCompile in every
    ItemDefinitionGroup of every vcxproj. Idempotent (won't duplicate)."""
    n_files = 0
    for p in root.rglob("*.vcxproj"):
        try:
            data = p.read_bytes()
        except OSError:
            continue
        new_data, n = JMC_INSERT_RE.subn(
            rb"\1\n      <SupportJustMyCode>false</SupportJustMyCode>",
            data,
        )
        if n > 0:
            p.write_bytes(new_data)
            n_files += 1
    return n_files


# CFG: insert ControlFlowGuard into ClCompile and Link blocks. The official
# v1.44 distribution build has a .gfids section, ours does not, and that
# is one of the few production-build PE-shape signals we can replicate
# without changing the toolset. Skip projects that link without a CRT
# (renderdocshim) -- enabling CFG there leaves __guard_check_icall_fptr
# unresolved at link time.
CFG_SKIP_PROJECTS = {"renderdocshim"}

CFG_CL_INSERT_RE = re.compile(
    rb"(<ItemDefinitionGroup(?:\s[^>]*)?>\s*<ClCompile>)(?![^<]*<ControlFlowGuard)",
    re.DOTALL,
)
CFG_LINK_INSERT_RE = re.compile(
    rb"(<ItemDefinitionGroup(?:\s[^>]*)?>(?:(?!</ItemDefinitionGroup>).)*?<Link>)(?![^<]*<ControlFlowGuard)",
    re.DOTALL,
)


MP_OFF_RE = re.compile(
    rb"<MultiProcessorCompilation>true</MultiProcessorCompilation>"
)


def disable_mp(root: Path) -> int:
    """Set <MultiProcessorCompilation>false</MultiProcessorCompilation> in
    every vcxproj. v140 cl.exe (14.00) races on PCH creation when /MP is on
    inside the same vcxproj while other cpp files in the same project try
    to /Yu the not-yet-finished PCH. Disabling MP serialises the cls per
    project; MSBuild's /m still parallelises across projects."""
    n_files = 0
    for p in root.rglob("*.vcxproj"):
        try:
            data = p.read_bytes()
        except OSError:
            continue
        new_data, n = MP_OFF_RE.subn(
            b"<MultiProcessorCompilation>false</MultiProcessorCompilation>",
            data,
        )
        if n > 0:
            p.write_bytes(new_data)
            n_files += 1
    return n_files


def enable_cfg(root: Path) -> int:
    """Enable Control Flow Guard project-wide (except CRT-less projects).

    Adds <ControlFlowGuard>Guard</ControlFlowGuard> under ClCompile and
    Link in every ItemDefinitionGroup. Skips vcxproj files whose stem is
    in CFG_SKIP_PROJECTS.
    """
    n_files = 0
    for p in root.rglob("*.vcxproj"):
        if p.stem in CFG_SKIP_PROJECTS:
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        modified = False
        new_data, n = CFG_CL_INSERT_RE.subn(
            rb"\1\n      <ControlFlowGuard>Guard</ControlFlowGuard>",
            data,
        )
        if n > 0:
            data = new_data
            modified = True
        new_data, n = CFG_LINK_INSERT_RE.subn(
            rb"\1\n      <ControlFlowGuard>true</ControlFlowGuard>",
            data,
        )
        if n > 0:
            data = new_data
            modified = True
        if modified:
            p.write_bytes(data)
            n_files += 1
    return n_files
