# Tripo API — working reference

Sources: [platform.tripo3d.ai/docs](https://platform.tripo3d.ai/docs/introduction)
(rendered via browser — the pages are SPAs and return empty to plain fetches) and
the [official Python SDK](https://github.com/VAST-AI-Research/tripo-python-sdk),
whose client source lists fields the HTML docs omit. ✓ = measured by us.

## Official tooling that already exists

Worth knowing before building anything custom:

| Repo | What it is |
| --- | --- |
| [tripo-3d-for-blender](https://github.com/VAST-AI-Research/tripo-3d-for-blender) | **Official Blender extension.** Text/image/multiview, progress tracking, task history, balance, advanced settings |
| [tripo-mcp](https://github.com/VAST-AI-Research/tripo-mcp) | **Official MCP server.** One tool: generate + import to Blender. Requires their addon |
| [tripo-python-sdk](https://github.com/VAST-AI-Research/tripo-python-sdk) | Official Python SDK — the best API reference there is |
| [ComfyUI-Tripo](https://github.com/VAST-AI-Research/ComfyUI-Tripo) | Official ComfyUI nodes |

The official addon does **not** do post-processing, segmentation, or rigging.
Our setup keeps arbitrary Blender control via blender-mcp, which their MCP lacks.

## Endpoints

| | Base |
| --- | --- |
| v2 | `https://api.tripo3d.ai/v2/openapi` — one generic `POST /task` with a `type` field |
| v3 | `https://openapi.tripo3d.ai/v3` — per-task routes |

- **v3 requires an explicit `model`**; v2 defaults to `v2.5-20250123`. ✓
- Auth: `Authorization: Bearer <key>`. Tasks are bound to the key that created
  them — querying with a different key of the same account gives "not found".
- Balance: `GET /v2/openapi/user/balance` → `{balance, frozen}`. ✓
- Uploads: v3 `POST /v3/files` → `file_<uuid>`; v2 `POST /v2/openapi/upload` →
  `file_token` ✓. SDK prefers STS + direct S3 (`POST /upload/sts/token`).
- Response header `X-Tripo-Trace-ID` — quote it in support requests.

## Task response

```
task_id  type  status  input  output  progress  create_time  consumed_credit
```

**Field names differ between versions — this silently broke thumbnails.** ✓

| | v3 | v2 |
| --- | --- | --- |
| Model | `model_url` | `pbr_model` / `model` / `base_model` |
| Preview | `rendered_image_url` | `rendered_image` |
| Generated image | `generated_image_url` | `generated_image` |
| Credits | `credits_consumed` | `consumed_credit` |

Always accept both spellings.

- **`consumed_credit`** — actual credits used, 0 if failed. Read this instead of
  diffing balance.
- `output`: `model`, `base_model`, `pbr_model`, `rendered_image`,
  `generated_image`, and for multiview `front/left/back/right_view_url`.
- **All result URLs expire 5 minutes after success.** ✓
- Status: `queued` `running` → `success` `failed` `banned` `expired` `cancelled` `unknown`.
- Docs warn `output` may carry undocumented fields — don't depend on them.
- A streaming endpoint is recommended over polling ("results are pushed when
  tasks complete"), but the docs page for it did not render. Unresolved.

## Credits

$1.00 = 100 credits. Free tier: 300 credits for 2 weeks.

| Task | H2/H3 no tex | H2/H3 textured | P1 no tex | P1 textured |
| --- | --- | --- | --- | --- |
| Text to model | 10 | **20** ✓ | 30 | **40** ✓ |
| Image to model | 20 | **30** ✓ | 40 | 50 |
| Multiview to model | 20 | 30 | 40 | 50 |

**H2/H3 surcharges** (P1 is all-in; surcharges don't apply):

| Flag | Extra |
| --- | --- |
| `texture_quality=standard` | +10 — **already inside the quoted "textured" price** |
| `texture_quality=detailed` | +20 total, i.e. +10 over standard |
| `texture_quality=extreme` | +30 total, i.e. +20 over standard |
| `smart_low_poly=true` | +10 ✓ |
| `generate_parts=true` | +20 |
| `quad=true` | +5 |
| `geometry_quality=detailed` | **+20 ✓ measured — undocumented** |
| `face_limit` | **0** ✓ |

The "text to model 20" figure is really 10 (untextured) + 10 (standard texture).
Only the *step up* from standard is an extra on top of the quoted price.

**Post-processing:** texture_model 10 · segmentation 40 · completion 50 ·
smart low poly 30 · rig 25 · retarget 10/animation · convert 5 · prerig check free.
Image gen 5–10 · multiview image 10.

Credits are **frozen** during a task and settle on completion — mid-flight
`balance` understates your total. Read `balance + frozen`. ✓

## Concurrency (per task group, shared within group)

| Group | Limit |
| --- | --- |
| Standard model generation (non-P1 text/image/multiview) | 10 |
| P1 model generation | 5 |
| Refine | 5 |
| Animation | 10 |
| Other tasks | 10 |
| Multiview image generation / editing | 1 each |

Image upload: 10 QPS. Model upload ≤150MB, images ≤20MB.

## face_limit ceilings

| Model | geometry_quality=standard | =detailed |
| --- | --- | --- |
| v3.1 | 1,500,000 | 2,000,000 |
| v3.0 | 1,500,000 | 2,000,000 |
| v2.5 | 500,000 | 500,000 |

`quad=true` → quad count ≤150,000. For `highpoly_to_lowpoly`: 500–20,000
(500–10,000 with quad).

## text_to_model fields

```
prompt (required)     negative_prompt       model_version
face_limit            texture (def true)    pbr (def true)
image_seed            model_seed            texture_seed
texture_quality       geometry_quality      standard|detailed|extreme
auto_size (false)     quad (false)          compress -> "geometry"
generate_parts        smart_low_poly        export_uv (def true)
```

`image_to_model` adds `texture_alignment` (original_image|geometry),
`orientation` (default|align_image), `enable_image_autofix`.
**`orientation` is image-only** — sent to text_to_model it is silently ignored
and still bills you ✓ — and it only takes effect when `texture=true`.

### Constraints that bite

- **`generate_parts` is incompatible with `texture=true`, `pbr=true` and
  `quad=true`.** The API does not error; it just returns untextured parts —
  exactly what happened to us. Set `texture=false, pbr=false`. ✓
- **`quad=true` forces FBX output**, not glTF, so an importer that assumes
  `.glb` fails. With `quad=true` and no `face_limit`, it defaults to 10000.
- **`pbr=true` overrides `texture`** and forces it true.
- `prompt` max 1024 chars; `negative_prompt` max 255.
- `texture_quality` has three tiers: `standard` | `detailed` | `extreme`.
- `convert_model` `texture_size` defaults to 2048 (4096 for >= v2.0) and must be
  **smaller** than that default.

Model versions: `P1-20260311`, `Turbo-v1.0-20250506`, `v3.1-20260211`,
`v3.0-20250812`, `v2.5-20250123`, `v2.0-20240919`, `v1.4-20240625`,
`v1.3-20240522` (deprecated). **Default is `v2.5-20250123`** when unset.
Tiers are named H2/H3/P1; v3.1 bills at the H3 rate ✓. Most optional parameters
need `model_version >= v2.0-20240919`; `geometry_quality` needs `>= v3.0`.

## Post-processing (v2, chained by task_id)

| Type | Notes |
| --- | --- |
| `texture_model` | `part_names`, `texture_prompt{text,image,style_image}`, `bake`, model_version v2.5\|v3.0 |
| `mesh_segmentation` | `original_model_task_id`, `model_version v1.0-20250506`. Accepts text/image/multiview/texture/refine/import/highpoly_to_lowpoly tasks |
| `mesh_completion` | Takes a **segmentation** task id; `part_names` |
| `highpoly_to_lowpoly` | `face_limit` 500–20000, `quad`, `part_names`, `bake`. model_version `P-v2.0-20251225` |
| `convert_model` | `format` GLTF/USDZ/FBX/OBJ/STL/3MF, `texture_size` (2048, or 4096 for >=v2.0; must be smaller), `scale_factor`, `flatten_bottom`, `pivot_to_center_bottom`, `fbx_preset` blender/mixamo/3dsmax, `export_orientation` |
| `stylize_model` | `style` lego\|voxel\|voronoi\|minecraft, `block_size` |
| `refine_model` | `draft_model_task_id` |
| `animate_rig` / `animate_retarget` | `rig_type`, `spec` mixamo\|tripo, animation presets |

### Segmentation granularity
The v2 `mesh_segmentation` has **no part-count parameter**. The granularity
control the web UI exposes lives on the v3 route (from SDK source):
```
POST /v3/mesh/segment
{ "type":"mesh_segmentation", "model":"v2.0-20260430", "input":"<task_id>",
  "segmentation_granularity":"simple|balanced|detailed",
  "ref_image":"file_<uuid>", "split_by_connectivity":true }
```
Note `/mesh/segment`, **not** `/mesh/segmentation`. Untested.

## Changelog notes (from /docs/changelog)

Worth re-reading before assuming anything is stable.

- **1.9.7 (2026-06-03)** — `texture_quality=extreme` added; pricing re-tiered to
  standard +10 / detailed +20 / extreme +30. **`texture_quality` is actively
  developed, not deprecated.**
- **1.9.6 (2026-04-14)** — new `generate_multiview_image` and
  `edit_multiview_image` task types. `multiview_to_model` now accepts
  `original_task_id` to chain from them without re-uploading images;
  mutually exclusive with `files`. **Not implemented on our side.**
- **1.9.5 (2026-03-11)** — v3.1 (up to 2M polys) and P1 released. P1 "supports
  only selected generation parameters. Unsupported parameters will return an
  error."
- **1.9.4 (2026-03-09)** — credit usage added to task responses; this is where
  `consumed_credit` came from.

**Deprecated:** animate_rig `v2.0-20250506` (use `v2.5-20260210`),
`v1.3-20240522`, and multiview generation on v1.4.

## Error codes

| Code | Meaning |
| --- | --- |
| 1000/1001 | Server error — contact support with trace id |
| 1002 | Auth failed ✓ |
| 1003 | Malformed body |
| 1004 | Invalid parameter ✓ |
| 1005 | Not permitted |
| 1007 | Rate limit (too many requests) |
| 2000 | Generation limit exceeded — see `Retry-After` |
| 2001 | Task not found |
| 2002 / 2006 / 2007 | Unsupported task type / bad original task / original not succeeded |
| 2008 / 2009 | Content policy violation / invalid characters |
| 2010 | Insufficient credit ✓ |
| 2015–2017 | Deprecated version or type |
| 2018 | Model too complex to remesh |
| 2019–2022 | File not found / bad image url / >150MB / image >20MB |

## Measured results (v3.1 sofa, same prompt)

| Config | Polys | Credits |
| --- | --- | --- |
| default | ~1,433,000 | 20 |
| `texture_quality=detailed` + `geometry_quality=detailed` | ~1,890,000 | **50** ✓ |
| `face_limit=5000` | 4,788 | 20 |
| `smart_low_poly=true` | ~11,000 | 30 |
| P1 default | 4,663 | 40 |

`face_limit` is the best lever for poly count at no extra cost. Note it **is**
documented for `text_to_model` — an earlier version of this file claimed the
docs omitted it, which was wrong. We discovered it by probing only because the
docs hadn't been read, not because they were incomplete.

## Behaviours

- **Seeds pin the design, not the mesh.** With `image_seed`+`model_seed`+
  `texture_seed` fixed, two runs gave near-identical sofas but different vertex
  counts (4,519 vs 4,643) and hashes. Omitting `image_seed` gives wildly
  different results — text_to_model runs text→image→geometry. ✓
- **`generate_parts` returns untextured meshes** — 21 objects, zero materials.
  Likely fixed by a follow-up `texture_model` (10 credits, accepts `part_names`).
  Untested. ✓
- Output is unit-normalized (max dim 1.0) unless `auto_size=true`. ✓
- Orientation varies between runs; some assets arrive rotated 90°. ✓
- 3 concurrent tasks worked fine. ✓

## Probing safely

Field discovery by trial **costs real money** — we burned 60 credits learning it.

- A well-typed nonsense value (`face_limit: -5`) is **accepted** → task created.
- An **unknown** field (`seed`, `orientation` on text) is **ignored** → task created.
- Only these are free:
  1. **Type-invalid** value (dict where int expected) — fails at deserialization
     and the error names the field's Java type, proving it exists. Works on v3;
     v2 returns a generic 1004.
  2. **Omit a required field** so the request can never succeed.
  3. **Unknown routes** — 404s are free and confirm route names.
