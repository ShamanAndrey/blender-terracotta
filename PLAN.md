# Implementation plan — full Tripo coverage

Goal: every Tripo capability reachable from the Blender node graph.

Ordered by unlocked value, not by API listing order. Each phase is independently
shippable. Every item follows the same rule: **read the docs, write mocked tests,
then spend credits once to confirm the live contract.**

Verification budget for the whole plan is roughly **200 credits**. Current
balance 930.

---

## Phase A — Finish what's half-built

Low risk, no new concepts, immediate payoff.

### A1. `import_model` — bring your own mesh ★ highest value
Upload a local `.glb/.obj/.fbx` and get a `task_id`, which makes **every**
post-processing task work on meshes we didn't generate.

- Endpoint: `type: import_model`, `file: {object: {bucket, key}}` (STS upload)
- New node: **Import Asset** — a source node with an Asset output, no input
- Blocker: import_model takes an STS `object`, not a `file_token`, so **A6 (STS
  upload) is a prerequisite**
- Limits: ≤150 MB
- Cost: free per pricing. Verify: **0 credits**

Why first: it turns retopo/texture/segment/rig from "things you can do to Tripo
output" into "things you can do to any asset in your scene". That is the single
biggest capability multiplier in the API.

### A2. Complete `convert_model` — how assets leave Blender
Currently only `format` is exposed. Add: `quad`, `force_symmetry`, `face_limit`,
`flatten_bottom` + threshold, `texture_size`, `texture_format`, `scale_factor`,
`pivot_to_center_bottom`, `with_animation`, `fbx_preset`, `export_orientation`,
`part_names`, `export_vertex_colors`.

- Also needs a **save-to-disk** path: conversion output should be written where
  the user wants, not imported into the scene
- `pivot_to_center_bottom` replaces our manual ground-seating
- Note `texture_size` must be *smaller* than the default (2048, or 4096 ≥v2.0)
- Cost: 5. Verify: **5 credits**

### A3. Generation extras
`export_uv` (off = faster, UV deferred to texturing) and `compress` (meshopt
geometry compression — note the output then needs decompressing).

- Cost: 0. Verify: folded into another run

### A4. Multiview chaining (`original_task_id`)
`multiview_to_model` accepts `original_task_id` from `generate_multiview_image`
or `edit_multiview_image`, skipping re-upload. Mutually exclusive with `files`.

- Depends on B3. Verify with B3.

### A5. Segmentation granularity
The web UI's part-count control is `segmentation_granularity`
(`simple|balanced|detailed`) on the **v3** route `POST /v3/mesh/segment`, with
`input` as the task id and `split_by_connectivity`. Our v2 path has no such
parameter.

- Requires a second code path: post-processing currently assumes v2
- Cost: 40. Verify: **40 credits**

### A6. STS upload
SDK's preferred path: `POST /upload/sts/token` → S3 credentials → upload via
boto3 → `{object: {bucket, key}}`. Needed for `import_model` and better for
large files.

- boto3 is not bundled with Blender — needs a plain `urllib` S3 signer, or fall
  back to `file_token` where the endpoint allows it
- Cost: 0. Verify: **0 credits**

---

## Phase B — Image generation (the AssetHub front end)

This is the workflow AssetHub sells: concept image → multiple views → 3D. All of
it is API-side; we just don't expose it.

### B1. `text_to_image`
Prompt → image. Feeds image-to-3D without leaving Blender.
- Cost 5. New **Image** socket type, distinct from Asset.

### B2. `generate_image`
Advanced image generation with `template` (e.g. `t-pose`, `3d-enhance`),
`t_pose`, `sketch_to_render`, and multiple model backends.
- Cost 5–10. The `t-pose` template matters for the rigging phase.

### B3. `generate_multiview_image` + `edit_multiview_image`
One image → front/left/back/right set; each view editable independently via
`prompts: [{view, prompt}]`.
- Cost 10, then 5 per edited view
- Concurrency is **1** for both — the graph must queue, not fan out

### B4. Image display in nodes
Outputs are `front/left/back/right_view_url`. Nodes need to show them, and feed
them into a multiview generate node.
- Reuse the preview cache; add an Image socket carrying a task + view index

**Phase B verify: ~25 credits**

---

## Phase C — Rigging and animation (the character half)

### C1. `animate_prerigcheck`
Returns `riggable` — is this mesh riggable at all. **Free**, so it should run
automatically before offering to rig.

### C2. `animate_rig`
`rig_type` (biped/quadruped/hexapod/octopod/avian/serpentine/aquatic/others),
`spec` (`mixamo`|`tripo`), `out_format` glb|fbx.
- Use model_version `v2.5-20260210` — `v2.0-20250506` is deprecated
- Blender side: armature import, verify bones survive glTF/FBX
- Cost 25. Verify: **25 credits**

### C3. `animate_retarget`
15 preset animations (idle, walk, run, jump, climb, quadruped_walk…), plus
`bake_animation`, `export_with_geometry`, `animate_in_place`.
- Cost 10 per animation. Verify: **10 credits**
- Blender side: actions, NLA strips, playback

**Phase C verify: ~35 credits**

---

## Phase D — Infrastructure

### D1. Graph execution
Today every node is clicked individually. A **Run graph** operator should walk
the tree in dependency order, wait for each stage, and stop on failure. This is
the difference between a node editor and a pipeline.

- Needs: topological sort, per-node state machine, a cancel path
- No credits

### D2. Streaming instead of polling
Docs recommend a streaming endpoint over polling ("results are pushed when tasks
complete"). The docs page didn't render — needs investigation. Polling works, so
this is an efficiency change, not a capability one.

### D3. `refine_model`
Legacy: explicitly **not supported for model_version ≥ v2.0**. Only useful for
old draft models. Lowest priority; possibly skip.

---

## Cross-cutting work

- **Socket types**: `Asset` (a 3D task) vs `Image` (an image task) — they should
  not be interchangeable, and the graph should refuse invalid links.
- **Concurrency awareness**: standard generation 10, P1 5, multiview image 1.
  The runner must respect per-group limits or jobs will queue invisibly.
- **Per-model parameter gating**: P1 already errors on unsupported params. Each
  new task type needs the same treatment rather than sending everything.
- **Cost quoting**: every new node must go through `costs.py`. A node that
  under-quotes is worse than one that doesn't quote.
- **Tests**: extend `tests/mock_tripo.py` with each new task type's response
  shape, using the **real field names** (v3 `*_url`, v2 bare). A mock that
  drifts from reality manufactures false confidence — it has already hidden one
  real bug.

---

## Suggested order

1. **A6 → A1** (STS upload, then import_model) — biggest unlock, free to verify
2. **A2** convert — how work leaves Blender
3. **D1** graph execution — makes the whole thing a pipeline rather than buttons
4. **B1–B4** image generation — completes the concept-to-3D flow
5. **C1–C3** rigging — the character half
6. **A5** segmentation granularity, **A3/A4** leftovers
7. **D2/D3** last, or never

Phases A and D1 alone would make this a coherent tool. B and C are what make it
match AssetHub's scope.
