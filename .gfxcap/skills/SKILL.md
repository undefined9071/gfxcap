---
name: gfxcli
description: >
  Use this skill to inspect GPU work inside a gfxcap (or upstream RenderDoc)
  capture. Two verbs:

  - `gfxcli list` walks every event in the capture and writes a grep-friendly
    TSV index plus Markdown views. Search by shader name (Unity / Unreal
    keyword variants surface verbatim in the resource name), by debug
    marker / pass, by render target, by bind-fingerprint cluster, by
    pipeline state (blend / depth / stencil / cull on rt0), or by cheap
    heuristics like fullscreen / compute / shadow. **Every per-event
    grep-needable property is a column in events.tsv** -- the LLM should
    never need to write a Python probe to survey state across EIDs.
  - `gfxcli dump` exports a single EID's full pipeline state — shaders
    (DXBC + asm + decompiled HLSL + embedded original source if present),
    textures, cbuffers, IA, OM, rasterizer — into a Markdown bundle that
    a LLM can drop into its context.

  Trigger phrases for list: "find the draw that uses X", "which EID
  renders to Y", "list events", "search the capture", "events matching",
  "show me all draws / dispatches", "compile flags", "shader variants",
  "which variants are used", "what passes are in this frame", "which
  draws have additive blend / alpha blend", "find draws with depth test
  off", "stencil ref = N", "draws with cull = front", "post-process
  draws", "which EIDs read / write resource X", "what does this draw
  read", "who consumes this buffer / texture", "trace the dataflow
  between passes", "producer / consumer of a resource".

  Trigger phrases for dump: "export this draw", "dump EID", "extract
  shader", "decompile this draw", "renderdoc export", "gfxcap export",
  "what's bound at EID N", "show me the inputs to draw N".
---

# gfxcli Skill

## Two-step workflow

For most LLM tasks the workflow is:

```
1.  gfxcli list -r CAPTURE                  → events.tsv + index/
2.  grep / awk on events.tsv                → candidate EID(s)
3.  gfxcli dump -r CAPTURE -e EID           → full bundle for that draw
```

Step 1 is run once per capture; the index is cheap to keep around and
can be referenced for many follow-up questions. Step 3 is the deep
dive that pulls binary assets (textures, cbuffer bytes, decompiled
HLSL) for a chosen EID.

## Invocation

The bundle ships a self-contained Python 3.6 embed that matches the ABI
of the `gfxcap.pyd` SWIG module. From the bundle root:

```
analysis\python36\python.exe analysis\gfxcli.py <verb> <flags>
```

To shorten the command, prepend the bundle's `analysis\python36\` to PATH:

```powershell
$env:PATH = "C:\path\to\bundle\analysis\python36;$env:PATH"
python.exe C:\path\to\bundle\analysis\gfxcli.py list -r CAP.rdc
```

---

## Verb: `list`

Walk every action in the capture and write a TSV index plus Markdown
convenience views. Designed so the LLM does **one read of the TSV** and
then uses standard text tools (grep, awk) for arbitrary boolean
filtering — no need to re-run the tool to refine a query.

### Flags

```
gfxcli list
    -r, --rdc PATH         capture file (.rdc / .gcap)
    --out DIR              output dir (default: <rdc-dir>/<rdc-stem>_index/)
    --shallow              skip per-event pipeline-state queries — runs
                           in seconds even on huge captures but leaves
                           shader / RT / fingerprint columns blank
    --filter-flags LIST    comma-list of class labels to keep
                           (draw, dispatch, mesh_dispatch, dispatch_ray,
                           build_accstruct, clear, copy, resolve, gen_mips)
    --marker-prefix STR    keep only events whose marker_path starts with
                           this prefix (e.g. "Frame > Opaque")
    --skip-clears          drop class=clear events
    --skip-copies          drop class=copy and class=resolve events
    --quiet                suppress progress lines
```

`--shallow` exists for huge captures (>50k events). Default (enriched)
mode does SetFrameEvent per event and pulls bound shader / RT / SRV
state; expect ~minutes for 10k-event captures.

### Output layout

```
<out>/
  README.md              ← read this first. Frame breakdown stats,
                           top shaders, top markers, schema reference,
                           ready-to-run grep recipes.
  events.tsv             ← the authoritative artifact. One row per
                           drawable event. Tab-separated, header in
                           row 1. Run grep / awk against this.
  resource_io.tsv        ← dataflow join table. One row per
                           (event, bound resource): direction
                           (read/write/readwrite), kind, resource id,
                           name, dims. Grep this to answer "which EIDs
                           read/write resource X" (both directions) and
                           "what is the full I/O of EID N" -- the
                           producer/consumer questions events.tsv's
                           single rt0 column can't.
  events.md              ← same data as events.tsv, grouped by
                           marker_path. Use for skimming pass
                           structure visually.
  shaders.tsv            ← unique-shader catalogue
                           (stage, shader_name, first_eid, n_uses).
                           Sorted by n_uses desc. Pick one row to
                           find a representative EID for a shader.
  render_targets.md      ← per-RT lifecycle: name, size, format,
                           all EIDs writing to it. Use this when you
                           know what a draw renders to (the main HDR
                           buffer, a shadow map) but not which EID.
  markers.md             ← marker tree with EID range per scope.
                           Empty when the engine emits no debug
                           markers (common for shipping Unity / Unreal
                           builds with marker streams compiled out).
```

### events.tsv columns

| column | meaning |
|--------|---------|
| `eid` | event id — pass to `gfxcli dump -e` |
| `action_id` | sequential action id within the frame |
| `class` | one of: `draw`, `dispatch`, `mesh_dispatch`, `dispatch_ray`, `build_accstruct`, `clear`, `copy`, `resolve`, `gen_mips`, `other` |
| `flags` | secondary modifiers, comma-joined: `indexed`, `instanced`, `indirect`, `auto`, `color`, `depthstencil`, `passboundary` |
| `api_call` | API method name (`ID3D11DeviceContext::DrawIndexedInstanced` etc.) |
| `marker_path` | push-marker chain `A > B > C` (empty if no markers) |
| `vs_name`, `ps_name`, `gs_name`, `cs_name`, `hs_name`, `ds_name` | engine-side shader resource name per stage. **Carries Unity / Unreal keyword variants verbatim** when the engine encodes them in the resource name (Unity does, for example: `HGRP/Lit(ShaderLOD: 600 PassName: ) \| HG_ENABLE_MV SRP_INSTANCING_ON _PARALLAX_MAP`). Blank in shallow mode. |
| `rt0_name` | first bound render target's engine name |
| `rt0_size` | first RT `WxH` (or `WxHxD` for 3D) |
| `rt0_format` | first RT format string |
| `n_rts` | total bound render targets |
| `dsv_name` | depth-stencil view engine name |
| `rt0_blend_color` | rt0 color-channel blend as `src+op*dst` (e.g. `Src+Add*InvSrc`), or `off` when blending is disabled. |
| `rt0_blend_alpha` | rt0 alpha-channel blend, same form. |
| `rt0_blend_mask` | rt0 color write mask, hex digits (`F` = RGBA, `7` = RGB, `0` = nothing). |
| `depth_test` | `y` / `n` — depth-test enabled. |
| `depth_write` | `y` / `n` — depth-write enabled. |
| `depth_func` | depth compare op (`Less`, `LessEqual`, `Greater`, `Equal`, `Always`, ...). **Empty when `depth_test=n`** — the API field is stale (carries the create-time value) when depth-test is disabled, so it is intentionally blanked here to keep `$23=="Less"`-style queries free of false positives. |
| `stencil` | `y` / `n` — stencil test enabled. |
| `stencil_ref` | front-face stencil reference value, integer. **Empty when `stencil=n`** (same gating rationale as `depth_func`). |
| `stencil_func` | front-face stencil compare op (`Always`, `Equal`, `Less`, ...). **Empty when `stencil=n`**. |
| `cull` | lower-cased cull mode: `back` / `front` / `nocull` / `frontandback` (the raw enum name; note it is `nocull`, not `none`). |
| `num_indices`, `num_instances`, `dispatch_xyz` | draw / dispatch counts |
| `indirect` | `yes` if indirect call |
| `bind_fp` | 8-hex hash of (all shader names + rt0 id + sorted SRV ids). **Same `bind_fp` = same kind of draw**. Cluster all draws by this column to dedupe a frame. |
| `hint` | cheap heuristic: `fullscreen` (3/4/6 indices, 1 instance, no DSV), `instanced_batch` (>=100 instances), `compute` (dispatch with no RT), `clear`, `copy`, `resolve`, `gen_mips`, `indirect`. Empty when no rule fires. |

Pipeline-state columns (`rt0_blend_*` through `cull`) are populated only
in enriched mode **and only for the rasterizing classes** (`draw`,
`mesh_dispatch`). For `dispatch` / `clear` / `copy` / etc. they are
blank, because the bound blend / depth / cull state doesn't describe
what those events actually do. `--shallow` leaves them blank for every
class.

### Grep recipes

Replace `EVENTS` with your `events.tsv` path. These mirror what's in
the index's own README.

**By shader name / keyword variant** (most useful for engine RE):

```sh
# all draws using a specific Unity keyword combination
grep -F 'HG_ENABLE_MV' EVENTS | awk -F'\t' '$3=="draw"'

# any event hitting a *Shadow* shader (case-insensitive)
grep -i shadow EVENTS

# all variants of a given shader by name prefix
grep -F 'HGRP/Lit' EVENTS | awk -F'\t' 'NR>1 && !seen[$7]++ {print $7}'
```

**By marker / pass**:

```sh
# everything under the GBuffer marker scope
grep -P '\tGBuffer' EVENTS

# nested marker subtree
grep -F 'Opaque > GBuffer' EVENTS
```

**By render target** (cross-reference `render_targets.md` for size/format):

```sh
# all draws into a render target whose name contains MainHDR
awk -F'\t' 'NR>1 && $13 ~ /MainHDR/' EVENTS

# all 1024x1024 RTs — usually shadow maps
awk -F'\t' 'NR>1 && $14=="1024x1024"' EVENTS
```

**By draw class / modifier**:

```sh
awk -F'\t' '$3=="dispatch"' EVENTS
grep -P '\tinstanced' EVENTS
grep -P '\tindirect' EVENTS
```

**By pipeline state** -- blend / depth / stencil / cull. These are the
queries that historically required a Python probe; they're now plain
awk against the columns. Column numbers: `rt0_blend_color=18`,
`rt0_blend_alpha=19`, `rt0_blend_mask=20`, `depth_test=21`,
`depth_write=22`, `depth_func=23`, `stencil=24`, `stencil_ref=25`,
`stencil_func=26`, `cull=27`.

```sh
# all transparency draws (alpha blend, premultiplied or not)
awk -F'\t' 'NR>1 && $18 ~ /SrcAlpha|InvSrcAlpha/' EVENTS

# additive blend (HDR particles, glow, fire)
awk -F'\t' 'NR>1 && $18 ~ /One\+Add\*One/' EVENTS

# opaque-only (blend disabled)
awk -F'\t' 'NR>1 && $18=="off"' EVENTS

# depth-test disabled (UI, debug, sky)
awk -F'\t' 'NR>1 && $21=="n"' EVENTS

# depth-test on but no depth write (transparents, decals)
awk -F'\t' 'NR>1 && $21=="y" && $22=="n"' EVENTS

# stencil test with a specific reference value (e.g. character mask = 128)
awk -F'\t' 'NR>1 && $24=="y" && $25=="128"' EVENTS

# stencil compare op "Equal" (mask read pass)
awk -F'\t' 'NR>1 && $26=="Equal"' EVENTS

# two-sided draws (no culling -- hair, foliage, particles)
awk -F'\t' 'NR>1 && $27=="nocull"' EVENTS

# back-face culled (standard opaque geometry)
awk -F'\t' 'NR>1 && $27=="back"' EVENTS

# combine pipeline-state filters with shader / RT filters
awk -F'\t' 'NR>1 && $18 ~ /SrcAlpha/ && $13 ~ /MainHDR/' EVENTS
```

**Cluster: one representative per bind-fingerprint** (deduplicate the
"same kind of draw"):

```sh
awk -F'\t' 'NR>1 && !seen[$32]++ {print $1, $32, $7, $13}' EVENTS
```

**Fullscreen / post-process candidates**:

```sh
awk -F'\t' '$33=="fullscreen"' EVENTS
```

### resource_io.tsv -- dataflow (who reads / writes what)

`events.tsv` carries only the first render target (`rt0_*`). To trace
the **full** input/output set of an event, or to answer the reverse
question "which events touch resource X", use `resource_io.tsv`: a
long-format join table with one row per (event, bound resource).

Columns: `eid`, `direction` (`read` = SRV, `write` = RT/DSV,
`readwrite` = UAV), `kind` (`srv`/`uav`/`rt`/`dsv`), `resource_id`,
`resource_name`, `dims` (`WxH` for textures, blank otherwise).

```sh
# IO = your resource_io.tsv path

# CONSUMERS: every event that reads resource 'ResourceId::28255' as SRV
awk -F'\t' '$4=="ResourceId::28255" && $2=="read"' IO

# PRODUCERS: every event that writes a given render target
awk -F'\t' '$4=="ResourceId::7496" && $2=="write"' IO

# FULL I/O of one event (what EID 1505 reads and writes)
awk -F'\t' '$1=="1505"' IO

# trace a buffer's whole lifetime (reads + writes + UAV), any direction
awk -F'\t' '$4=="ResourceId::18904"' IO

# find a resource id by name first, then pivot to its EIDs
grep -F 'AnimationTexture' IO | cut -f4 | sort -u
```

Typical dataflow trace: find the EID that *writes* a buffer
(producer), then grep its id with `$2=="read"` to find every
*consumer* downstream -- the dependency edge between two passes.

### Exit codes

- `0` index written
- `2` capture cannot be opened
- `3` action enumeration failed
- `4` gfxcap module load failure (Python ABI mismatch)

---

## Verb: `dump`

Export full pipeline state for one event ID into a Markdown bundle.

### Flags

```
gfxcli dump
    -r, --rdc PATH         capture file (.rdc / .gcap)
    -e, --eid INT          event ID to export
    --out DIR              output dir (default: <rdc-dir>/<rdc-stem>_eid<eid>/)
    --quiet                suppress per-step progress
```

By default the export goes to a sibling of the capture file:
`<capture-dir>/<capture-stem>_eid<EID>/`. The default location is
**wiped** before each dump so stale files cannot mix with current
output. User-supplied `--out` is left alone.

### Output layout

```
<out>/
  README.md                           ← READ THIS FIRST.
                                       Metadata + marker_path + a
                                       "quick start for first-time
                                       LLM" section + a "shaders at
                                       a glance" table with the
                                       engine-side shader name per
                                       stage + draw call args +
                                       coverage table + navigation +
                                       errors.
  input_assembly/
    input_layout.md
    vertex_buffer_<i>.bin + .md
    index_buffer.bin + .md
  vertex_shader/                      ← one dir per bound stage
    shader.dxbc                       raw bytecode
    shader.asm                        DXBC disassembly
    shader.hlsl                       decompiled HLSL
    reflection.md                     identity (resource_name including
                                      keyword variants) + compile_flags
                                      (`@cmdline` and extracted `/D`
                                      defines) + cbuffer / SRV / sampler
                                      / UAV declared layout
    io_signatures.md                  input + output register sigs
    original_source/<file>            original HLSL / GLSL source files
                                      embedded in the shader's debug
                                      info (only when compiler stamped
                                      it in; absent otherwise)
    constant_buffer_b<n>.md           decoded values (head/tail preview
                                      when > 128 entries)
    constant_buffer_b<n>.bin          raw constant-buffer bytes
    constant_buffer_b<n>_vars.tsv     full variable table when > 128
                                      entries (TSV, grep-friendly)
    texture_t<n>.exr + .png + .md
    texture_t<n>_slice<k>.png            per-array-slice PNGs, only when
                                      the bound view spans >1 slice
                                      (texture arrays / cubemaps); capped
                                      at 16 slices
    buffer_t<n>.bin + .md
    sampler_s<n>.md
    uav_u<n>.{exr | bin, png?, md}
  hull_shader/, domain_shader/,
  geometry_shader/, pixel_shader/,
  compute_shader/                     same shape, when bound
  output_merger/
    render_target_<n>.exr + .png + .md
    depth_target.exr + .png + .md
    blend_state.md
    depth_stencil_state.md
  rasterizer/
    rasterizer_state.md
```

### Where to look for what

| You want… | Read |
|-----------|------|
| Shader name + keyword variants | `README.md` "shaders at a glance" table, or `<stage>/reflection.md` first lines |
| Compile defines (`/D` macros) | `<stage>/reflection.md` "compile flags" section |
| Which pass this draw belongs to | `README.md` metadata `marker_path` row |
| Resource identity of any RT / VB / texture | the `resource_name` field in that file's `.md` |
| Which sampler is paired with each texture | `<stage>/bindings.md` |
| Value range of a texture / RT (HDR, LUT, depth) | `value_min_rgba` / `value_max_rgba` fields in that texture's `.md` (GetMinMax over mip0/slice0; absent when the format doesn't support it) |
| Individual slices of a texture array / cubemap | `texture_t<n>_slice<k>.png` next to the `.md` (see `array_slices_exported` field) |
| PNG color space for Unity import | `png_color_space` field in each texture / RT `.md` |
| Vertex / index buffer raw bytes layout | the `byte_offset_in_buffer` field + the `bin:` source-coverage annotation in `input_assembly/*.md` |
| Original (un-compiled) shader source | `<stage>/original_source/` (only when present) |
| What failed in the export | `README.md` `## errors` section and any `STATUS: FAILED` headers |

### Notes for downstream re-import (Unity / Blender / etc.)

**Texture import in Unity:**
- For PNGs: read the `png_color_space` field in the texture's `.md`.
  When it says `linear (...)` set `Texture Importer -> sRGB (Color
  Texture) = OFF` -- the PNG bytes are already linear and Unity's
  default sRGB import would double-decode (2-3x darker output). When it
  says `as_stored (...)` interpret per author intent (typically OFF for
  normal maps and data textures, ON for color textures).
- DDS is NOT supported by Unity's standard `TextureImporter`. Use the
  `.png` (color / 8-bit) or `.exr` (HDR / depth / float). KTX2 in
  Unity 6+ is an option for repacking BC blocks but the dump doesn't
  emit it directly.

**Mesh extraction:**
- `dump` runs a built-in mesh extractor that emits these into
  `input_assembly/`:
  - `mesh.obj` -- universal POSITION + NORMAL + TEXCOORD0 + face
  - `mesh_vertices.tsv` -- every per-vertex attribute decoded by its
    DXGI format (POSITION, NORMAL, TANGENT, COLOR, all TEXCOORD sets,
    BLENDWEIGHTS, BLENDINDICES -- whatever the layout declares)
  - `mesh_triangles.tsv` -- triangle list from index buffer
  - `mesh.md` -- vertex / triangle counts, bbox, per-attribute decode
    status, OBJ channel inclusion notes
- Vertex range is scoped to **this draw** via `action.numIndices /
  indexOffset / baseVertex` -- not the full bound buffer.
- Extractor is engine-agnostic: decodes by DXGI format, never guesses
  semantic meaning. Unusual packings (e.g. `R32_FLOAT` for an
  octahedral normal) land verbatim in `mesh_vertices.tsv`, and the
  OBJ channel is omitted with a note in `mesh.md`. Consumer decides
  how to interpret.
- Underlying `vertex_buffer_<i>.bin` and `index_buffer.bin` are
  **pre-sliced** -- byte 0 of the `.bin` corresponds to
  `byte_offset_in_buffer` of the source buffer. The `bin:` annotation
  in each buffer `.md` spells the source-buffer range explicitly,
  e.g. `vertex_buffer_0.bin (10970400 B; covers
  source_buffer[5806816, 16777216))`. When D3D11 doesn't expose the
  binding size the field shows `byte_size: unknown` and the
  annotation says "API byteSize was 0, bin reads to end of buffer".

### Failure-handling contract

Failures are **never** silent. Any per-target export failure leaves a
marker `.md` with a `STATUS: FAILED` header **and** an entry in
`README.md`'s `## coverage` table (failed > 0) and `## errors` section.
An LLM reading the output cannot mistake "missing in output" for "did
not exist on the GPU".

### Exit codes

- `0` all targets exported
- `1` partial export — capture + EID OK, some per-target export failed
  (read `README.md`'s coverage table and errors section)
- `2` capture cannot be opened
- `3` EID not in capture
- `4` gfxcap module load failure (Python ABI mismatch)

---

## When to use which verb

| Scenario | Verb |
|----------|------|
| "Find the EID for [shader / variant / RT / pass]" | `list`, then grep |
| "Give me everything about EID N" | `dump` |
| "What variants of shader X exist in this frame?" | `list`, then `shaders.tsv` |
| "Pick a representative draw for each cluster" | `list`, then `bind_fp` dedupe (see README recipe) |
| "Which draws use additive / alpha blend / no blend" | `list`, then awk on `rt0_blend_color` |
| "Find draws with depth test off / no depth write" | `list`, then awk on `depth_test` / `depth_write` |
| "Stencil test = N / specific compare op" | `list`, then awk on `stencil_ref` / `stencil_func` |
| "Two-sided / no-cull draws" | `list`, then awk on `cull` |
| "Which EIDs read / write resource X" | `list`, then awk on `resource_io.tsv` |
| "Full input/output set of one draw" | `list` + `resource_io.tsv` (`$1==EID`), or `dump` for the deep version |
| "Trace producer → consumer between passes" | `list`, then `resource_io.tsv` (write side → read side of same id) |
| "Value range / slices of a texture" | `dump`, read `value_*_rgba` + `*_slice<k>.png` |
| "I need the textures and cbuffer values" | `dump` |
| "Survey what's in this capture" | `list` (read its README first) |

## Notes

- Capture must be opened with a matching `gfxcap` build. The bundle
  ships its own `gfxcap.pyd`; on a foreign install the format-version
  must line up.
- HLSL decompile uses the bundled `plugins/hlsl-decompiler/` next to
  the `gfxcap` binaries. Decompile failure is non-fatal; `shader.asm`
  is always written as a fallback.
- Texture export writes both EXR (linear half-float, HDR-correct,
  DCC-friendly -- main RTs / G-buffer / depth read as proper float)
  and PNG (8-bit LLM-visible preview, when format permits). PNG
  conversion failure is recorded in the texture's `.md`. EXR
  decompresses BC* blocks; for typical asset-extraction and analysis
  this is preferable to keeping BC blocks. The one caveat is BC5_SNORM
  normal maps: RenderDoc's downcast remaps to UNORM for EXR, losing
  signed-ness. Consult the `format` field in the texture's `.md` and
  remap manually for SNORM cases.
- `list --shallow` is the fast triage mode: blanks shader / RT
  columns but still gives you `class`, `marker_path`, `api_call`,
  counts, and the `hint` column. Use it to confirm there's something
  to look at before paying the enriched-mode cost.

See `DESIGN.md` (next to `gfxcli.py` in the bundle) for the design
rationale.
