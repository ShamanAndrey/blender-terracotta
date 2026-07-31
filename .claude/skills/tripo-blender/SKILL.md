---
name: tripo-blender
description: Generate 3D assets with Tripo AI and place them into Blender over MCP, then clean them up (poly budget, real-world scale, ground seating, naming). Use this whenever the user wants to create, generate, or add a 3D model, prop, furniture, or asset in Blender — including phrasings like "make me a chair", "add a lamp to the scene", "generate a sofa", "I need a rock for this scene" — and also for anything touching the Tripo API, credits, model versions, mesh cleanup of generated assets, or driving Blender through the blender-mcp connection. Generation costs real money, so consult this before spending credits.
---

# Tripo + Blender

Generate assets with Tripo, import them into a live Blender session, and make
them usable. Every generation costs credits, so the goal is to get it right on
the first attempt rather than iterate on the API's dime.

## Before anything else

Blender must be running with **Connect to Claude** active in the BlenderMCP
sidebar panel, otherwise every `mcp__blender__*` call fails. If calls error,
that's the first thing to check.

Everything runs inside Blender's Python via `mcp__blender__execute_blender_code`:

```python
from tripo_blender import api, meshtools
api.balance()                                    # check before spending
job = api.start(prompt="a brass telescope", name="Telescope",
                model="v3.1-20260211")   # face count stays on auto
api.status(job)                                  # poll until state == "done"
```

`api.start()` returns immediately — submit, upload and polling all happen on a
worker thread so Blender's UI never freezes.

## The one thing that will bite you

**Never call `time.sleep()` inside `execute_blender_code` while waiting for a
job.** Sleeping blocks Blender's main thread, which is exactly where the import
timer runs. The job completes, downloads, and then sits at `state: "importing"`
forever until you stop sleeping — it looks like a hang in the code when it's
actually your own poll blocking it.

Poll with **separate tool calls** instead, and wait between them using a shell
loop rather than blocking Blender:

```bash
i=0; until [ $i -ge 12 ]; do sleep 5; i=$((i+1)); done
```

Typical generations take 60–120 seconds.

## Choosing generation settings

**Leave the face count on auto — do not pass `face_limit`.** This is a standing
preference from the user and holds until they say otherwise.

The reasoning is worth understanding, because `face_limit` looks attractive: it
costs no extra credits and caps a 1.4M-poly result at whatever you ask for.
The catch is that the right budget depends entirely on the object, and you can't
know it before seeing the model. A sofa at 5,000 faces looks fine — it's a large
smooth shape. A bed with creased bedding at 5,000 faces comes back as 3,000
polys of spiky artifacts, because there aren't enough faces to represent folds.
Hard-surface props (doors, windows, desks) tolerate low budgets; anything
organic, draped, or finely detailed does not.

So generate at auto, look at the result, and decimate afterwards if it's
genuinely too heavy:

```python
meshtools.decimate("Bed", target_polys=60000)
```

On an 8GB machine, expect ~1.4M polys per asset, so decimate props you don't
need at full detail rather than accumulating a dozen of them.

| Model | Cost (text/image) | Use when |
| --- | --- | --- |
| `v3.1-20260211` | 20 / 30 | Default. Best texture quality |
| `P1-20260311` | 40 / 50 | Rarely — `face_limit` on v3.1 gets the same poly count for half |
| `v3.0-20250812`, `v2.5-20250123` | 20 / 30 | Older, no real advantage |

Surcharges on v3.1 (P1 is all-in, no surcharges): `texture_quality=detailed` +10,
`smart_low_poly` +10, `generate_parts` +20, `quad` +5. `face_limit` is free.

Other settings worth knowing:
- `auto_size=True` — real-world scale instead of unit-normalized output.
- `negative_prompt` — steer away from unwanted features, but keep it narrow.
  `"multiple objects"` sounds sensible and actively hurts anything that
  legitimately has parts — a bed is a frame plus mattress plus duvet plus
  pillows, and telling the model to avoid multiple objects works against it.
- **Don't describe orientation in the prompt.** Words like "front facing" or
  "flat against the wall" don't steer Tripo — output orientation is arbitrary
  regardless — they just dilute the description. Rotate in Blender after import
  instead; assets frequently arrive 90° off.
- **Put the colour in the prompt.** Unspecified appearance is chosen freely by
  the model, which is how you end up with one blue sofa among cream ones.
- **Seeds pin the design, not the mesh.** Set `image_seed`, `model_seed` and
  `texture_seed` together — text-to-3D runs text→image→geometry, so leaving
  `image_seed` free makes the other two nearly useless. Two runs with identical
  seeds give the same *design* with slightly different vertex counts. Good
  enough for a matching furniture set; not bit-reproducible.
- `generate_parts=True` splits into separate objects but **returns them
  untextured** — parts or textures, not both.

## Architecture keeps its dimensions

When a generated asset doesn't fit built geometry, **scale the asset — never
resize the architecture to fit the asset.**

This is easy to get backwards because resizing an opening *looks* like the
elegant move ("procedural walls make this easy!"). It isn't. Real openings have
real sizes, and the moment you widen one to accommodate whatever proportions a
model came back with, the scene is wrong in a way that survives deleting the
asset. That happened here: a correct 0.9 × 2.05 m door opening was widened to
1.06 m to fit a generated door, the generated door was later replaced with a
built one, and the room kept a 1.02 m door — 20% wider than any real door.

The specific trap is **scaling on the wrong axis**. Scaling a door by height
lets its width fall out of the model's arbitrary aspect ratio. Scale on the axis
that governs fit — a door by width, a rug by length, a picture by whichever
dimension you actually care about.

Reference values worth checking against: interior door 0.76–0.86 × 1.98–2.04 ·
ceiling 2.4–2.7 · desk 0.72–0.78 high · counter 0.90 · double bed 1.4 × 1.9 ·
dining chair seat 0.45.

Also check which wall face you're building against. A wall spanning
y −2.6…−2.5 with the room at y > −2.5 has its interior face at **−2.5**; using
−2.6 puts trim on the outside where the camera can't see it.

## After import

Generated meshes arrive unit-normalized (max dimension 1.0), origin wherever the
exporter left it, sometimes rotated 90°. `meshtools` fixes this:

```python
meshtools.cleanup("Telescope", target_polys=50000, max_size=1.0)  # decimate + scale + ground
meshtools.place("Telescope", location=(1, 0, 0), max_size=0.4)    # position and size
meshtools.frame(["Telescope"])                                     # frame for a screenshot
meshtools.stats("Telescope")                                       # polys, dims, materials
meshtools.render_reference("Telescope")                            # isolated render for image-to-3D
```

Then `mcp__blender__get_viewport_screenshot` to see the result. Note it captures
only the 3D viewport — it cannot show sidebar panels, so don't try to verify UI
that way.

## Naming

Append the model version to every generated object: `Sofa_v3.1`, `Chest_P1`.
The addon does this automatically at import via `api.short_model()`. It matters
because the same prompt gives very different results per model, and once several
assets are in a scene the name is the only thing telling you which produced what.

## Transforms: prefer data over operators

`bpy.ops.object.transform_apply` and similar operators silently do nothing in
the MCP execution context — no error, no effect. Transform mesh data directly:

```python
obj.data.transform(mathutils.Matrix.Rotation(math.radians(90), 4, 'Z'))
```

Same for `hide_render` vs `hide_viewport`: **renders obey `hide_render`**.
Hiding an object from the viewport still puts it in your render, which will
silently ruin an image-to-3D reference.

## Spending credits responsibly

Check `api.balance()` before generating and tell the user the cost beforehand.
Credits are **frozen** while a task runs and settle on completion, so a
mid-flight balance reading understates the true total — read `balance + frozen`.

Re-importing a past asset is **free**:

```python
api.history()                                    # past tasks, newest first
api.reimport(task_id, name="Thing")              # re-downloads, costs nothing
```

Check history before regenerating something similar.

### Never probe the API to discover fields

Sending an unknown or badly-valued parameter usually **creates a real task and
charges for it**. A well-typed nonsense value (`face_limit: -5`) is accepted. An
unknown field is silently ignored and the task runs anyway. Only three things
are free:

1. A **type-invalid** value (a dict where an integer is expected) — fails at
   deserialization, and the error names the field's type, proving it exists.
2. **Omitting a required field** so the request cannot succeed.
3. **Unknown routes** — 404s cost nothing.

When in doubt, read `TRIPO_API.md` in the project root instead of experimenting.
It documents every task type, parameter, error code, rate limit and price, and
was built precisely so nobody has to rediscover this by spending money.

## Project layout

| Path | What it is |
| --- | --- |
| `tripo_blender/` | The addon — `api.py` (Tripo bridge), `meshtools.py` (cleanup), `__init__.py` (UI) |
| `TRIPO_API.md` | Full API reference — read this before any unfamiliar API work |
| `README.md` | Setup and usage |
| `addon.py` | Upstream blender-mcp addon |

### Changing the addon

Edit files in `tripo_blender/`, rezip, install, then force a clean reload —
`addon_install` alone does **not** re-run `register()`, so new properties will
be missing:

```python
saved = bpy.context.preferences.addons["tripo_blender"].preferences.tripo_api_key
import tripo_blender as stale; stale.unregister()
bpy.ops.preferences.addon_install(filepath=".../tripo_blender.zip", overwrite=True)
for m in [m for m in list(sys.modules) if m.startswith("tripo_blender")]: del sys.modules[m]
linecache.clearcache(); importlib.invalidate_caches()
import tripo_blender; tripo_blender.register()
prefs = bpy.context.preferences.addons["tripo_blender"].preferences
if not prefs.tripo_api_key: prefs.tripo_api_key = saved
bpy.ops.wm.save_userpref()
```

**Never call `addon_disable` before reinstalling** — it discards the stored API
key, and there is no way to recover it. The `saved`/restore dance above exists
because that has already happened once.
