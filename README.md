# gfxcap

A fork of [RenderDoc](https://github.com/baldurk/renderdoc).

The fork keeps every upstream feature and adds a small set of changes that
make RenderDoc usable on D3D11 titles whose anti-cheat / debug-layer guards
get in the way of stock RenderDoc:

- Rebrands the binaries, log paths, registry keys, and file associations
  to hide the `renderdoc` string. The approach is borrowed from
  [ShiyumeMeguri/FractalMiner/Nisemono.py](https://github.com/ShiyumeMeguri/FractalMiner/blob/main/Assets/Scenes/Nisemono.py).
- Pins the v140 (VS2015) toolset in the build pipeline. For reasons we
  don't fully understand, some target processes refuse to take an
  injection unless the DLL was linked with v140.
- MinHook-based hooking with a d3d11 / dxgi allowlist instead of
  upstream's loader-wide IAT patching, so userspace anti-cheat libraries
  don't trip on modifications to unrelated import tables.
- F12 captures persist on disk under `./captures/*.rdc` next to the
  executable. They are never auto-deleted.
- The bundled HLSL Decompiler plugin is auto-registered on first launch.
- Single-event export CLI (`gfxcli`) produces an LLM-readable Markdown
  bundle describing every state input the GPU saw at one draw or
  dispatch.

## License

MIT, same as upstream RenderDoc — see [`LICENSE.md`](LICENSE.md). Bundled
third-party plugins keep their own licenses (see the directories under
`.gfxcap/3rdparty/` for the corresponding notices):

- **[MinHook](https://github.com/TsudaKageyu/minhook)** by Tsuda Kageyu —
  MIT. Vendored under `.gfxcap/3rdparty/minhook/`. Used as the inline-hook
  engine in place of upstream's IAT-patching loader hooks.
- **[HLSL-Decompiler](https://github.com/YYadorigi/HLSL-Decompiler)** by
  YYadorigi. Bundled at `.gfxcap/3rdparty/hlsl-decompiler/` and
  auto-registered as the DXBC/DXIL → HLSL decompiler plugin.

## Build

The authoritative build recipe is the CI workflow at
[`.github/workflows/gfxcap-build.yml`](.github/workflows/gfxcap-build.yml).
It pins the v140 toolset, applies the source edits in
`.gfxcap/scripts/source_edits.py`, builds the rebranded solution on the
GitHub-hosted Windows runner, verifies the resulting binaries' PE linker
version, and ships a portable bundle as a release artifact.

For a local build, install Visual Studio (any 2022 or newer) plus the
`Microsoft.VisualStudio.Component.VC.140` and
`Microsoft.VisualStudio.Component.VC.14.29.16.11.ATL` components, then run:

```powershell
python .gfxcap/scripts/prepare.py --clean --no-retarget
msbuild .gfxcap/build/src/gfxcap.sln /m /p:Configuration=Release /p:Platform=x64 /p:PlatformToolset=v140
```

## Usage

1. Download the bundle from the latest release (or build it yourself).
2. Run `gfxcapui.exe` and use **File → Launch Application** targeting the
   executable to capture.
3. Press F12 in the running application to take a frame capture. The capture
   file is written to `<exe-dir>/captures/` and the GUI auto-loads it.

### When `Launch Application` doesn't work

For titles that reject gfxcap's launch path (e.g. some Steam games),
side-load `gfxcap.dll` manually with a proxy DLL such as
[VersionShim](https://github.com/Xpl0itR/VersionShim). Captures land
in `./captures/*.rdc` next to the target exe.

## Bundled plugins

- `plugins/hlsl-decompiler/` — DXBC / DXIL → HLSL decompiler. Auto-registered
  on first launch via the bundled `HLSLDecompiler.bat`.

## CLI: `gfxcli`

`gfxcli` is shipped under `analysis/` in the portable bundle alongside an
embedded Python 3.6 (matched to `gfxcap.pyd`'s ABI). It exports the full GPU
pipeline state for one event into a self-contained Markdown directory:

```
analysis\python36\python.exe analysis\gfxcli.py dump --rdc <capture>.rdc --eid <event-id>
```

The output directory contains:

- per-stage shader bytecode + DXBC disassembly + decompiled HLSL
- per-binding constant buffer values (decoded variables alongside the raw
  bytes so an LLM can crosscheck decode against ground truth)
- bound textures (DDS + PNG) and buffers (raw `.bin`)
- input assembly, output merger, rasterizer state
- a coverage report and per-target failure log so partial failures cannot
  go unnoticed

See [`.gfxcap/src/scripts/DESIGN.md`](.gfxcap/src/scripts/DESIGN.md) for the
output layout reference and the per-file Markdown templates.

## Skill (for AI agents)

[`.gfxcap/skills/SKILL.md`](.gfxcap/skills/SKILL.md) is a skill description
so an AI agent can invoke `gfxcli dump` automatically when asked to inspect
a specific draw call from a capture.

## Repository layout

- `.gfxcap/scripts/` — prepare / rebrand / build / source-edit pipeline
- `.gfxcap/src/scripts/` — `gfxcli.py` (the analysis CLI) and its design doc
- `.gfxcap/skills/` — skill descriptions
- `.gfxcap/3rdparty/` — vendored third-party plugins and dependencies
- `.github/workflows/` — CI build pipeline
- everything else — upstream RenderDoc tree
