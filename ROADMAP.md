# Roadmap

**Goal:** an AssetHub-class AI asset pipeline that lives entirely inside Blender —
multiple models and providers, generation through to finished usable asset,
without leaving the app.

[AssetHub](https://assethub.io) is the reference point: a node-based web studio
for modular game characters (concept image → AI splits into named parts →
generate each mesh → assemble), running Meshy, Rodin, Trellis, Hitem, Hunyuan,
Tripo and Nano Banana behind the scenes, plus AI retopology, UV unwrap and
texturing.

Our angle is different in one important way: **assets land where you work.**
A web studio can't put things in your Blender asset library; we can.

## Product direction

**Open tool, optional paid convenience.** The Blender-side project is open source
and free — anyone can use it with their own API keys, forever. A hosted credit
system is offered later for people who don't want to obtain keys themselves, and
the backend for that stays closed.

The honest value proposition is convenience, not lock-in: Tencent's Hunyuan is
the model case — a genuinely painful signup (mainland Tencent Cloud account) and
an integration that's broken in the upstream addon. "We did the painful part,
pay a little to skip it" is legitimate precisely *because* BYOK remains a
first-class path.

**Scope:** characters and environments both. They share more than they differ —
task chaining, retopology and texturing serve both. Divergence comes later:
segmentation and rigging lean character, asset library and procedural builders
lean environment.

### Build order consequence

**BYOK first, gateway last.** Build and prove the tool against direct provider
access; only take on a hosted service once it's demonstrably good. That keeps
the expensive, risky part (uptime, payments, support, abuse) out of the way
until there's evidence anyone wants it.

But **design the seam now**, because retrofitting it is painful. A provider
should be data, not code:

```
provider = { base_url, auth_header, capabilities[], cost_table }
```

With that, the hosted credit system is *just another provider entry* — same
adapter, different base URL and a user token instead of a Tripo key. Nothing in
the addon should assume direct-to-provider access.

### Licensing

- **Addon: Apache-2.0.** It's a client; permissive maximises adoption, which is
  the point. Also carries a patent grant, which MIT doesn't.
- **Gateway: closed.** Separate program, separate repo, communicating over HTTP.

### Risks to design for, not retrofit

1. **Provider ToS may prohibit reselling compute.** Check Tripo, Meshy and
   Hunyuan terms before taking money — this one can invalidate the model outright.
2. **You inherit the support surface** — a provider outage becomes your outage
   for paying users.
3. **Abuse runs under your key.** Tripo error 2008 is a content-policy rejection;
   enough of those is your account's problem, not the user's.
4. **Float and reconciliation** — providers are paid up front, credits burn later.

## What already exists

The provider-agnostic machinery is done and proven:

- Async submit/poll/download entirely off the main thread — the UI never freezes
- Main-thread import pump via `bpy.app.timers` (bpy is not thread-safe)
- Job state, progress, error surfacing, credit accounting
- Task history persisted across restarts, with **free** re-import
- Mesh cleanup: decimate, normalize scale, seat on ground, isolated reference render
- Procedural builders: box, cylinder, material, door, window, picture frame
- Full UI: text / image / multiview, advanced parameters, jobs, history, cleanup
- Model-version naming on import

Only three functions in `api.py` are actually Tripo-shaped: build the request,
poll, extract the model URL. Everything else already generalizes.

## Phase 1 — Post-processing chain

**Biggest capability jump, zero new integrations.** These are documented in
`TRIPO_API.md` and simply not built yet. They cover most of AssetHub's headline
features using an API we already understand.

| Feature | Task type | Cost | Notes |
| --- | --- | --- | --- |
| AI retopology | `highpoly_to_lowpoly` | 30 | `face_limit` 500–20000, `quad`, `part_names` |
| AI texturing | `texture_model` | 10 | Accepts `part_names` and a `texture_prompt` |
| Modular parts | `mesh_segmentation` | 40 | `/v3/mesh/segment` has `segmentation_granularity` |
| Part completion | `mesh_completion` | 50 | Operates on a segmentation result |
| Rigging | `animate_rig` / `animate_retarget` | 25 / 10 | Mixamo or Tripo spec |
| Export | `convert_model` | 5 | FBX/OBJ/USDZ, `blender` preset, `pivot_to_center_bottom` |

Two known problems this solves directly:
- **Untextured parts** — `generate_parts` returns 21 objects with zero materials.
  Segment, then `texture_model` with `part_names`.
- **The bed's spiky artifacts** — retopology instead of decimation.

Chaining matters here: every one of these takes an `original_model_task_id`, so
the job system needs to express "run B on the output of A".

## Phase 2 — Asset Browser + local cache

The multiplier, and the thing a web studio structurally cannot do.

- Mark generated meshes as Blender assets, write catalogs, generate previews
- Cache GLBs locally so re-use costs nothing and works offline
- Everything ever generated becomes drag-and-drop across all projects

Also needs no new API.

## Phase 3 — Provider abstraction

Refactor, not rewrite. A provider implements `submit()`, `poll()`,
`model_url()`, `cost()`; Tripo is the reference implementation.

Add exactly one second provider (Meshy or Rodin) to prove the interface is real
rather than Tripo-shaped. Include a capability matrix — not every provider does
multiview, parts, or rigging — and degrade gracefully in the UI.

## Phase 4 — More providers, as needed

Add a provider when there's a reason to use it, not for the logo. Candidates:
Meshy, Rodin/Hyper3D, Hunyuan, Trellis, plus library sources (Poly Haven,
Sketchfab — already exposed via blender-mcp) and local generation via ComfyUI.

## Not building

- **A node graph UI.** Blender has geometry nodes; a node editor is AssetHub's
  interface metaphor, not a requirement of the problem.
- **Our own rigging or retopo algorithms.** They exist as API calls.
- **A generation UI that duplicates the official Tripo addon.** We already match
  its features; the value we add is cleanup, chaining, and library integration.

## Known costs and risks

- **API drift multiplies per provider.** Tripo alone moved v2→v3, renamed
  `consumed_credit` to `credits_consumed`, and omitted `face_limit` from its own
  docs — which cost 60 credits to discover. Thin adapters and a capability matrix
  keep this survivable.
- **AssetHub's real value is its workflow, not its model list.** If modular
  characters are the goal, segmentation and part assembly matter far more than
  provider count. For scene building — what we've actually done so far — their
  workflow may be irrelevant.
- **8 GB of RAM** is a real constraint at ~1.4M polys per asset. Phase 1
  retopology helps more than it looks.

## Packaging (pending, unrelated to features)

To make this shareable so new users and agents can start from a clone:

- [x] Promote memories into `CLAUDE.md` so agents get the conventions
- [ ] `.mcp.json` hardcodes an absolute `uvx` path — make it plain `uvx`
- [ ] `SKILL.md` hardcodes `/Users/andreyturakin/Desktop/blender` — make relative
- [ ] `install.py`: detect Blender, install both addons, enable, save prefs, verify
- [ ] `.gitignore`: `*.blend`, `*.png`, `tripo_blender.zip`, `.tripo_key`
- [ ] Decide on `addon.py` — fetch from upstream rather than vendoring 122 KB
- [ ] `git init`, then consider a Claude Code plugin manifest for distribution
