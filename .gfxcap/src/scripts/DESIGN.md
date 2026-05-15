# gfxcli design

CLI tool that ships in the portable gfxcap bundle. Single source file
(`gfxcli.py`) by design — no module split.

## Design principles

These rank above everything else:

1. **Information density and reliability beat disk and time.** Be
   profligate. A draw call's worth of state is small in absolute terms;
   spending an extra 50 MB of disk to give the LLM redundant signal
   (raw bytes alongside decoded values, EXR alongside PNG, asm alongside
   HLSL) is a good trade.
2. **Failures must never be silent.** If the GPU had four textures bound
   and only three exported successfully, an LLM reading the output MUST
   see that the fourth existed and that we failed on it. The worst
   possible bug is the LLM concluding "there were three textures" when
   there were four.

The second principle drives the design: every per-target export creates
a `.md` file unconditionally (success or failure), and `README.md`
carries an authoritative coverage table plus the aggregated error log.

## CLI

Subcommand-driven so future verbs can land without breaking v1 callers.

```
gfxcli dump  -r <capture>  -e <eid>  [--out DIR] [--quiet]
gfxcli list  -r <capture>            [--out DIR] [--shallow]
                                     [--filter-flags LIST]
                                     [--marker-prefix STR]
                                     [--skip-clears] [--skip-copies]
                                     [--quiet]
```

The intended workflow is `list` → grep / awk on the resulting TSV →
`dump` on the chosen EID. `list` is heavy enough that callers should
run it once per capture and cache the index; `dump` is the per-event
deep dive.

All flags are explicit (no positional args). Short forms: `-r` for
`--rdc`, `-e` for `--eid`.

`dump --out` defaults to `<rdc-dir>/<rdc-stem>_eid<eid>/`;
`list --out` defaults to `<rdc-dir>/<rdc-stem>_index/`. Both are
siblings of the capture file (NOT cwd); the capture is the obvious
anchor and we want the output co-located so it does not accumulate
in random working directories. When `--out` is left at its default
the directory is wiped before each run so stale files from prior
runs cannot mix with current output. User-supplied `--out` is left
alone.

The bundle ships no `.bat` launcher. Invoke via:

```
analysis\python36\python.exe analysis\gfxcli.py dump -r CAPTURE -e EID
```

PATH or `$env:PATH` can shorten this when needed.

### Exit codes

dump:

- `0` — all targets exported successfully
- `1` — partial success: capture/EID resolved, but at least one target
  failed (caller should read `README.md`)
- `2` — capture cannot be opened
- `3` — EID not found in the capture
- `4` — gfxcap module load failed (Python ABI mismatch, missing DLL)
- `64` — usage error (e.g. unimplemented verb)

list:

- `0` — index written
- `2` — capture cannot be opened
- `3` — action enumeration failed
- `4` — gfxcap module load failed

`1` is intentionally distinct from `0` so a caller can distinguish
"clean export" from "partial export" without parsing markdown. The LLM
itself reads README's coverage table; exit code is for shell callers.

## Output directory layout (verb: dump)

Industry-name directory and file names so an LLM doesn't have to decode
abbreviations. Stage subdirs only appear when the corresponding shader
is bound for the action being dumped.

```
<out>/
  README.md                          single entry point: metadata,
                                     draw-call args, coverage table,
                                     navigation, error log
  input_assembly/
    input_layout.md
    vertex_buffer_<i>.bin + .md
    index_buffer.bin + .md
  vertex_shader/                     bound stages only
    shader.dxbc                      raw bytecode
    shader.asm                       DXBC disassembly (always)
    shader.hlsl                      HLSL decompile (always; ERROR
                                     header on fail)
    reflection.md                    identity (resource_name with
                                     engine-side debug name + Unity /
                                     Unreal keyword variants when
                                     present) + compile flags
                                     (@cmdline + extracted /D
                                     defines) + cbuffer / SRV /
                                     sampler / UAV declared layout
    io_signatures.md                 input + output register sigs
    original_source/<file>           original HLSL / GLSL embedded
                                     in shader debug info (only when
                                     stamped by the compiler)
    constant_buffer_b<n>.md          decoded values (head/tail
                                     preview when > 128 entries)
    constant_buffer_b<n>.bin         raw constant-buffer bytes
    constant_buffer_b<n>_vars.tsv    full variable table when
                                     > 128 entries
    texture_t<n>.exr + .png + .md
    buffer_t<n>.bin + .md
    sampler_s<n>.md
    uav_u<n>.{exr | bin, png?, md}
  hull_shader/, domain_shader/,
  geometry_shader/, pixel_shader/,
  compute_shader/                    same shape, when bound
  output_merger/
    render_target_<n>.exr + .png + .md
    depth_target.exr + .png + .md
    blend_state.md
    depth_stencil_state.md
  rasterizer/
    rasterizer_state.md
```

### Stage nesting rationale

The structure mirrors how a graphics engineer would mentally walk a
draw: "what did the pixel shader see?" → `cd pixel_shader/` → look at
`shader.hlsl`, `constant_buffer_b0.md`, `texture_t0.md` together.
Type-grouped layouts (`pipeline/`, `resources/`, `constants/`) require
cross-jumping for every question.

### Resource duplication policy

If the same texture is bound to both VS (`t3`) and PS (`t3`), we write
both `vertex_shader/texture_t3.{exr,png,md}` and
`pixel_shader/texture_t3.{exr,png,md}`. Yes, this is two copies of the
same EXR. Disk is cheap; locality of analysis is not. The `.md` for
each carries the underlying `resource_id` so a deduper can be written
later if needed.

### Shader output: 3-file rule

Every bound shader stage gets all three of:

- `shader.dxbc` — raw bytecode bytes
- `shader.asm` — DXBC disassembly (controller-side disasm)
- `shader.hlsl` — HLSL Decompiler output

Always. Even when HLSL decompile fails: `shader.hlsl` still exists with
a `// STATUS: FAILED` header explaining why and pointing at
`shader.asm` as the fallback. This keeps the file inventory predictable
so an LLM can rely on the layout instead of probing.

### Texture output: 3-file rule

Every bound texture (and render target, depth target) gets `.exr` +
`.png` + `.md`:

- `.exr` — linear half-float OpenEXR. Always-linear (no sRGB
  confusion), HDR-correct (R11G11B10 / R16G16B16A16 / R32G32B32A32
  preserved as float), DCC-friendly (Blender / Nuke / Krita / Photoshop
  open natively). Float depth (D24S8 / D32_FLOAT) reads as proper
  float values. BC* compressed source decompresses to half float on
  save -- this is what asset-extraction / mod / analysis pipelines
  almost always want. Caveat: BC5_SNORM normal maps lose signed-ness
  via RenderDoc's RGBA8-UNORM downcast; consult the `format` field in
  the `.md` and remap if you hit that case.
- `.png` — 8-bit-friendly preview (when format permits); failures land
  in the `.md`
- `.md` — format / dimensions / mip / array / sample / bind point /
  resource_id, plus pointers to the binary files and any per-file
  failure status

Why EXR over DDS (which prior versions used): EXR is the right
default for the dump's primary use cases (LLM analysis, asset
extraction, DCC re-import). DDS only wins for verbatim BC-block
repackaging into the original game, which is a niche enough workflow
that we leave it to a future opt-in flag if anyone actually asks.

### Constant buffer format

Designed for LLM crosscheck: human-readability is sacrificed for
ambiguity-free decoding. Each `constant_buffer_b<n>.md` carries:

1. A header table with offset (decimal AND hex), size, type, name, and
   value for every variable.
2. Expanded value blocks for matrix-valued variables (one row per line,
   indices like `[0,*] = ...`).
3. The full raw byte dump as hex at the bottom.

The LLM can compare its understanding of (1) and (2) against the
authoritative bytes in (3). If decoded values disagree with raw bytes,
the LLM has a signal that decode went wrong.

### Cbuffer granularity split

Unity / Unreal globals cbuffers can carry thousands of variables. A
single `.md` with all of them inline becomes too large for an LLM to
read in one pass (we saw 586 KB / 8 K lines on an Endfield capture).
The split rule:

- raw bytes always go to a sibling `.bin` (never a hex dump inside
  `.md`)
- when variables > 128, the full table goes to a sibling `_vars.tsv`
  and the `.md` keeps a head-32 + tail-8 preview plus a pointer

The threshold (128) is informed by the observation that most cbuffers
in shipping titles are < 50 vars; the megacbuffers (`$Globals`,
`UnityPerFrame`) are the long-tail outliers and benefit most from
splitting.

Example:

```
# Vertex Shader -- constant buffer at register b0

- declared_name: `PerFrameConstants`
- declared_size: 256
- bound_size: 256
- byte_offset_in_buffer: 0
- backing_buffer_id: `ResourceId::395`

## variables (4 entries)
| name      | offset | offset_hex | size | type      | value             |
|-----------|--------|------------|------|-----------|-------------------|
| `view`    | 0      | 0x0        | 64   | Float4x4  | (4x4 matrix, see expanded block) |
| `proj`    | 64     | 0x40       | 64   | Float4x4  | (4x4 matrix, see expanded block) |
| `cameraPos` | 128  | 0x80       | 12   | Float1x3  | (1.0, 2.0, 3.0)   |
| `_padding`  | 140  | 0x8C       | 4    | Float1x1  | (0.0)             |

### `view`
[0,*] = 1.0  0.0  0.0  0.0
[1,*] = 0.0  1.0  0.0  0.0
[2,*] = 0.0  0.0  1.0  0.0
[3,*] = 0.0  0.0  0.0  1.0

## raw_bytes (256 B)
0x0000: 00 00 80 3F  00 00 00 00  00 00 00 00  00 00 00 00
...
```

## Output directory layout (verb: list)

```
<out>/
  README.md              frame breakdown + schema + grep recipes
                         (the LLM's first read)
  events.tsv             one row per drawable event; tab-separated.
                         Authoritative artifact -- everything else is
                         a convenience view of this data.
  events.md              same data grouped by marker_path
                         (visual skim)
  shaders.tsv            unique-shader catalogue
                         (stage, shader_name, first_eid, n_uses)
  render_targets.md      per-RT lifecycle: name, size, format, every
                         EID writing to it; per-DSV the same
  markers.md             marker tree with EID range per scope
```

### Why TSV is the primary artifact

A `list` run on a 10 K-event capture takes minutes (one
`SetFrameEvent` per event in enriched mode). Re-running to refine a
filter would burn that time again. With a TSV, the LLM does one read
and then composes arbitrary filters via grep / awk pipes, including
boolean combinations no single CLI flag could express.

The Markdown views (events.md / markers.md / render_targets.md /
README.md) are convenience layers built from the same per-event
records — they don't carry independent state. If you only need one
file, take events.tsv.

### events.tsv schema

Columns chosen to be (a) cheap to compute, (b) high-affinity for LLM
grep-style queries, (c) stable enough that grep recipes in the
README don't rot.

```
eid action_id class flags api_call marker_path
vs_name ps_name gs_name cs_name hs_name ds_name
rt0_name rt0_size rt0_format n_rts dsv_name
num_indices num_instances dispatch_xyz indirect
bind_fp hint
```

- `class` is the primary action label (`draw` / `dispatch` /
  `clear` / ...), one value per row.
- `flags` is the comma-joined set of secondary modifiers
  (`indexed,instanced,indirect,...`).
- `vs_name` ... `ds_name` carry the **engine-side resource name**
  per stage. For Unity / Unreal builds this is where keyword
  variants end up encoded, so a grep for `HG_ENABLE_MV` on the
  shader-name columns directly returns events using that variant.
- `bind_fp` is a 8-hex SHA1 prefix of (shader names + rt0 id +
  sorted SRV ids). Same `bind_fp` ≈ same kind of draw.
- `hint` is a cheap heuristic tag (`fullscreen`, `instanced_batch`,
  `compute`, `shadow`?, ...). False positives are worse than
  silence, so the rules are conservative.

### Shallow vs enriched

`--shallow` skips `SetFrameEvent` entirely. The TSV still has class,
flags, api_call, marker_path, counts, and the cheap hints — but the
shader / RT / fingerprint columns are blank. Use shallow on captures
where a first-pass triage is enough or where enriched cost is
prohibitive (>50 K events).

Enriched (default) does `SetFrameEvent` once per indexable event and
pulls bound shader / RT / SRV state via the same per-API pipe access
helpers as `dump`. The cost is ~50–100 ms per event empirically.

### Filter flags (pre-write)

`--filter-flags`, `--marker-prefix`, `--skip-clears`, `--skip-copies`
narrow the set of events the writer iterates over. They exist for
cost reasons, not as a substitute for grep: an LLM that wants
`draw AND (shader X) AND (NOT marker Y)` will compose that with
grep pipes against the full TSV, not by re-running list with new
flags.

## README.md (single entry point)

The single most important file in the output. An LLM reading it should
be able to answer:

- What did this draw bind? (counts per group)
- Did everything export successfully? If not, what failed?
- Where is each piece of state on disk?

Sections, each prefixed with a one-paragraph "what this is" intro:

1. **metadata** — capture file path, size, mtime; api; frame number;
   capture time; eid; action name; action flags.
2. **draw call arguments** — DrawIndexed / Dispatch / DrawIndirect args
   (numIndices, baseVertex, instance counts, dispatch dimensions, etc.).
3. **coverage** — per-group `expected | exported | failed` table.
4. **navigation** — every file in every directory enumerated with a
   one-line description so an LLM can decide where to drill in.
5. **errors** — per-target failures grouped by category; empty when
   every export succeeded.

## Failure handling — the contract

Every per-target writer obeys these rules:

1. **The `.md` file is created unconditionally.** A `texture_t3.md`
   exists even when EXR export failed; its body explains the failure.
2. **The collector records every failure.** This is the one place that
   feeds README's coverage table and error section, so adding-but-not-
   recording a failure means the coverage table will lie.
3. **Skips driven by user flags do NOT go into the collector.** A
   flag-skip is not a failure.
4. **Fatal errors short-circuit before anything is written.** Capture
   that cannot be opened, EID that does not resolve — exit before
   creating partial output.

## Module imports

The script runs under the bundled Python 3.6 embed. `gfxcap.pyd` is
loaded with `import gfxcap`. Required surface (subset of the upstream
RenderDoc Python module):

- `gfxcap.OpenCaptureFile()` → capture file
- `cap.OpenFile(path, "rdc", progress)` → ResultDetails
- `cap.OpenCapture(ReplayOptions, progress)` → (status, controller)
- `controller.GetRootActions()` → ActionDescription tree
- `controller.SetFrameEvent(eid, force)` → restore replay state at EID
- `controller.GetD3D11PipelineState()` (and the equivalents for D3D12 /
  Vulkan / GL) → per-stage bind tables
- `controller.GetDescriptorAccess()` → all bound CB / SRV / UAV /
  Sampler descriptors for the current event
- `controller.GetDescriptors(...)` / `GetSamplerDescriptors(...)` →
  expand a descriptor range
- `controller.DisassembleShader(...)` → DXBC asm string
- `controller.GetCBufferVariableContents(...)` → decoded ShaderVariable
  array for a cbuffer slot
- `controller.SaveTexture(TextureSave, path)` → EXR / PNG / ...
- `controller.GetBufferData(buffer_id, offset, length)` → bytes
- `controller.GetTextures()` / `GetBuffers()` → id lookup tables
