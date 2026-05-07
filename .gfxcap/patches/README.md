# Patches

Unified-diff patches applied by `prepare.py` after rebrand and 3rdparty merge,
before `scripts/source_edits.py` runs. Use this directory for line-numbered
structural changes (e.g. additions to a `.vcxproj`); for text-level edits
that need to survive upstream version drift, prefer `scripts/source_edits.py`.

## Naming

`NNNN-description.patch` -- the 4-digit prefix sets application order.

## When to use this vs `source_edits.py`

| Change type                            | Patch file | source_edits.py |
|----------------------------------------|:----------:|:---------------:|
| String find/replace                    |            | yes             |
| Adding new files (via vendored dirs)   | yes        |                 |
| `.vcxproj` ItemGroup additions         | yes        |                 |
| Modifying upstream function bodies     |            | yes (preferred) |
| Adding new C/C++ files via vcxproj     | yes        |                 |
| Behavioural toggles requiring conditional compilation | yes |          |

`source_edits.py` is preferred when the surrounding context can be matched as
a stable string -- it tolerates upstream line-number drift. Reach for a
unified-diff patch when the change is small but inherently positional
(e.g. inserting an XML element).

## Current series

- `0001-minhook-vcxproj.patch` -- adds MinHook source files to the main
  project's compile list with per-file PCH/ForcedInclude/WarnAsError disabled.
