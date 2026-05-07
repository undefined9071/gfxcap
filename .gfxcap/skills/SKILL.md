---
name: gfxcli
description: >
  Use this skill to dump a single GPU draw call's complete pipeline state
  from a gfxcap (or upstream RenderDoc) capture into a Markdown bundle for
  AI analysis. Useful when reverse-engineering shaders, comparing rendering
  paths between captures, or feeding draw context to an LLM.
  Trigger phrases: "export this draw", "dump EID", "extract shader",
  "decompile this draw", "renderdoc export", "gfxcap export",
  "gfxcli dump".
---

# gfxcli Skill

## Purpose

Given a `.rdc` / `.gcap` capture and an event ID (EID), produce a directory
containing every piece of state the GPU saw at that draw, formatted for
parsing by an LLM:

- per-stage shader bytecode + DXBC disassembly + decompiled HLSL (via the
  bundled HLSL Decompiler plugin) — all three written unconditionally
- input / output register signatures and shader reflection
- bound resources (textures as DDS + PNG, buffers as raw bin)
- samplers
- constant buffers with decoded values AND raw hex (LLM can crosscheck)
- input assembly (vertex layout, vertex / index buffers)
- output merger (render targets, depth, blend / depth-stencil state)
- rasterizer state
- draw call arguments

Failures are never silent: any per-target export failure leaves a marker
`.md` with `STATUS: FAILED` and is summarised in `README.md`'s coverage
table and errors section.

## Invocation

The bundle ships a self-contained Python 3.6 embed that matches the ABI
of the gfxcap SWIG module. From the bundle root:

```
analysis\python36\python.exe analysis\gfxcli.py dump -r <capture> -e <EID>
```

Subcommand form (`dump` is the only verb today; `list` is reserved for
future EID enumeration).

`--out` is optional. By default the export goes to a sibling directory of
the capture file:

```
<capture-dir>\<capture-stem>_eid<EID>\
```

The default output directory is wiped before each dump so stale files
from prior runs cannot mix with current output. User-supplied `--out` is
left alone.

To shorten the command, prepend the bundle's `analysis\python36\` to PATH
in your shell:

```
$env:PATH = "C:\path\to\bundle\analysis\python36;$env:PATH"
python.exe C:\path\to\bundle\analysis\gfxcli.py dump -r CAP.rdc -e 4302
```

## Flags

```
gfxcli dump
    -r, --rdc PATH         capture file (.rdc / .gcap)
    -e, --eid INT          event ID to export
    --out DIR              output dir (default: <rdc-dir>/<rdc-stem>_eid<eid>/)
    --quiet                suppress per-step progress
```

## Output layout

```
<out>/
  README.md                          single entry point — read this first.
                                     Contains metadata, draw-call args,
                                     coverage table, navigation, errors.
  input_assembly/
    input_layout.md
    vertex_buffer_<i>.bin + .md
    index_buffer.bin + .md
  vertex_shader/                     bound stages only
    shader.dxbc                      raw bytecode
    shader.asm                       DXBC disassembly
    shader.hlsl                      decompiled HLSL (header notes
                                     any failure)
    reflection.md                    cbuffer / SRV / sampler / UAV
                                     declared layout
    io_signatures.md                 input + output register sigs
    constant_buffer_b<n>.md          decoded values + raw bytes
    texture_t<n>.dds + .png + .md
    buffer_t<n>.bin + .md
    sampler_s<n>.md
    uav_u<n>.{dds | bin, png?, md}
  hull_shader/, domain_shader/,
  geometry_shader/, pixel_shader/,
  compute_shader/                    same shape, when bound
  output_merger/
    render_target_<n>.dds + .png + .md
    depth_target.dds + .png + .md
    blend_state.md
    depth_stencil_state.md
  rasterizer/
    rasterizer_state.md
```

See `DESIGN.md` (next to `gfxcli.py` in the bundle) for the design
rationale and the per-file Markdown templates.

## Exit codes

- `0` clean export
- `1` partial export — capture + EID OK, some per-target export failed
  (read `README.md`'s coverage table and errors section)
- `2` capture cannot be opened
- `3` EID not in capture
- `4` `gfxcap` module load failure (Python ABI mismatch)

## When to use

- The user asks "give the AI everything about EID N in capture X"
- Shader reverse-engineering tasks
- Per-frame regression analysis between two captures
- Building training data for a graphics-aware LLM

## Notes

- Capture must be opened with the matching gfxcap version. The bundle
  ships its own `gfxcap.pyd`; on a foreign install the format-version
  must line up.
- Texture export writes both DDS (raw layout, all formats) and PNG
  (8-bit-friendly preview, when format permits). PNG conversion failure
  is recorded in the texture's `.md`.
- HLSL decompile uses the bundled `plugins/hlsl-decompiler/` next to
  the gfxcap binaries. Decompile failure is non-fatal; `shader.asm` is
  always written as a fallback.
