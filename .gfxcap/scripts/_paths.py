"""Shared paths and product names for the gfxcap rebuild pipeline.

The repository layout is:

    <REPO_ROOT>/                     # gfxcap-rebuild, a fork of baldurk/renderdoc
        renderdoc/                   # upstream library source
        qrenderdoc/                  # upstream Qt UI
        renderdoccmd/                # upstream CLI
        renderdocshim/               # upstream DLL hijack shim
        ...                          # other upstream dirs
        renderdoc.sln                # upstream solution
        .gfxcap/                     # our additions only -- merge-conflict-free zone
            scripts/                 # this directory
            patches/
            src/
            3rdparty/
            config/
            build/                   # gitignored: working copy + msbuild output
            dist/                    # gitignored: final artifacts

Setting GFXCAP_UPSTREAM env var overrides the inferred REPO_ROOT (used by CI).
"""
from __future__ import annotations
import os
from pathlib import Path

# === Layout ===
GFXCAP = Path(__file__).resolve().parent.parent     # <REPO>/.gfxcap/
REPO_ROOT = Path(os.environ.get("GFXCAP_UPSTREAM", str(GFXCAP.parent)))   # <REPO>/

UPSTREAM_RD = REPO_ROOT                              # the repo IS upstream renderdoc

WORK_SRC = GFXCAP / "build" / "src"                  # working copy: rebrand + patches applied
WORK_BUILD = GFXCAP / "build" / "cmake"              # CMake build dir (unused on Windows)
DIST = GFXCAP / "dist"
LOGS = GFXCAP / "logs"
PATCHES = GFXCAP / "patches"
CONFIG = GFXCAP / "config"
CAPTURES = GFXCAP / "captures"
THIRDPARTY = GFXCAP / "3rdparty"                     # merged into work tree's gfxcap/3rdparty/
SOURCE_OVERRIDES = GFXCAP / "source_overrides"       # rarely used; full-file replacements

# Backwards-compat alias used by inject.py and a few scripts that still reference ROOT
ROOT = GFXCAP

# === Product identity ===
ORIGINAL_BASE = "renderdoc"
PRODUCT_BASE = "gfxcap"
PRODUCT_PASCAL = "GfxCap"
PRODUCT_UPPER = "GFXCAP"

DLL_NAME = f"{PRODUCT_BASE}.dll"
SHIM_DLL_NAME = f"{PRODUCT_BASE}shim64.dll"          # x64 build appends '64'
