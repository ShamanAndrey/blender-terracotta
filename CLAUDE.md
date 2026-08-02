# Blender + AI asset generation

Generate 3D assets with Tripo and build scenes in a live Blender session over
MCP.

**The `terracotta/` plugin is fully standalone** — verified in a factory
Blender with nothing else installed. blender-mcp is only the bridge that lets
Claude drive Blender in these sessions; end users never need it.

Three standing rules for the plugin:

- **Never delete a shipped node class — stub it.** Blender shows unknown node
  types as Undefined and drops their settings on save, so deletion corrupts
  every user file containing one. See the "Deprecated nodes" section in
  `nodes.py`.
- **Never store API keys on nodes or scenes** — those are saved into .blend
  files and leak when files are shared. Keys live in addon preferences
  (machine-local), reachable via the Account node / `tripo.setup_keys`.
- The node graph is the **only product surface** — there is no viewport panel
  at all. Jobs and Library live in the node editor's sidebar; import placement,
  decimation and asset-marking are Import-node settings; cleanup for arbitrary
  objects is in the object right-click menu. Don't add a second surface: the
  old panel drifted twice, and its scene settings silently overrode the Import
  node's checkboxes (which were written to a dict nothing ever read). The Jobs
  and Library panels stay **display-only**: buttons that act on the data shown
  (re-import, clear) are fine, but any setting that changes behavior belongs
  on a node — a setting appearing here is the old panel growing back.

For Claude sessions: Blender must be running with **Connect to Claude** active
in the BlenderMCP sidebar, or every `mcp__blender__*` call fails — check that
first when things error.

Everything runs through `mcp__blender__execute_blender_code`:

```python
from terracotta import api, meshtools, build
```

Detailed workflow lives in `.claude/skills/tripo-blender/SKILL.md`.
Full API reference — every task type, parameter, error code, price — is in
`TRIPO_API.md`. Read it before any unfamiliar API work; it exists so nobody has
to rediscover this by spending credits.

## Conventions

These were learned the expensive way. Follow them unless the user says otherwise.

### Leave the face count on auto

Do not pass `face_limit`. Capping faces at generation was recommended after
testing it on one sofa — a big smooth shape that survives a low budget fine.
Applied to a bedroom set at `face_limit=5000` it wrecked the detailed pieces:
the bed came back at 3,026 polys with spiky artifacts where the bedding creases
should be. Hard-surface props tolerate low budgets; draped or detailed ones
don't, and you can't pick the right budget before seeing the model.

Generate at auto, look at it, then `meshtools.decimate()` if it's genuinely too
heavy. Expect ~1.4M polys per asset on v3.1.

### Build manufactured objects, generate organic ones

Doors, window frames, picture frames, trim, shelves, plain panels — build them
with `terracotta.build`. These are diffusion models and they are bad at
making things even and perfect; a generated door comes back subtly warped, and
the wobble reads as wrong precisely because the real object is machine-made.
Built geometry is exactly straight, free, and sized to the scene.

Use Tripo for what it's good at: bedding, upholstery, plants, sculpted and
decorative pieces. In a room that means walls, floors, doors and frames are
built; beds, chairs, sofas and art are generated.

### Architecture keeps its real dimensions

When a generated asset doesn't fit built geometry, **scale the asset — never
resize the architecture to fit the asset.**

Resizing an opening looks like the elegant move. It isn't. A correct 0.9 m door
opening was once widened to 1.06 m to fit a generated door; that door was later
replaced with a built one, and the room kept a 1.02 m door — 20% wider than any
real door. The error outlived the asset that caused it.

Scale on the axis that governs fit — a door by **width**, not height. Scaling by
height lets width fall out of the model's arbitrary aspect ratio. Sanity-check
against reality: interior door 0.76–0.86 × 1.98–2.04 · ceiling 2.4–2.7 · desk
0.72–0.78 high · counter 0.90 · double bed 1.4 × 1.9.

### Name generated objects with their model

Append the model version: `Sofa_v3.1`, `Chest_P1`. The addon does this at import
via `api.short_model()`. The same prompt gives very different results per model,
and once several assets share a scene the name is the only thing telling you
which produced what.

## Traps

- **Never `time.sleep()` inside `execute_blender_code` while waiting on a job.**
  It blocks the main thread, which is where the import timer runs — the job
  finishes and then sits at `importing` forever. Poll with separate tool calls
  and wait in a shell loop instead.
- **`bpy.ops` transform operators silently do nothing** in the MCP context — no
  error, no effect. Use `obj.data.transform(Matrix.Rotation(...))`.
- **`hide_viewport` does not affect renders** — use `hide_render`. A hidden
  default cube once occluded a reference render and produced a grey blob.
- **Never `addon_disable` before reinstalling the addon** — it discards the
  stored API key with no way to recover it. See SKILL.md for the reload dance.
- **Don't probe the API to discover parameters.** Unknown or badly-valued fields
  usually create a real, billed task. Only type-invalid values, omitted required
  fields, and unknown routes are free.
- **Check which wall face you're building against.** A wall spanning y −2.6…−2.5
  with the room at y > −2.5 has its interior face at −2.5.

## Layout

| Path | What |
| --- | --- |
| `terracotta/` | The addon: `api.py` (Tripo bridge), `google_api.py` (Gemini images), `nodes.py` (node graph), `operators.py`, `panels.py`, `runner.py` (graph execution), `workspace.py` (workspace + examples), `costs.py` (all pricing), `meshtools.py`, `build.py`, `utils.py`, `__init__.py` (wiring only). Package with `tools/build_zip.py` — it globs, never hand-list files |
| `.claude/skills/tripo-blender/` | Workflow skill |
| `TRIPO_API.md` | API reference |
| `ROADMAP.md` | Where this is going |
| `addon.py` | Upstream blender-mcp addon (untracked; fetch from github.com/ahujasid/blender-mcp) |

## API keys

Never commit a key. The lookup order is: the Tripo panel in Blender →
`TRIPO_API_KEY` env var → `~/.tripo_key` (chmod 600). Users add their own.
