# Roadmap

**Goal:** an AssetHub-class AI asset pipeline that lives entirely inside
Blender — concept image through finished, placed, animated asset, without
leaving the app. Our angle over web studios: **assets land where you work**,
in your scene and your asset library.

## Product direction

**Open tool, optional paid convenience.** The addon is open source
(Apache-2.0) and BYOK — anyone can use it with their own Tripo and Google
keys, forever. A hosted credit system may come later for people who don't
want to obtain keys; that backend stays closed and is *just another provider
entry* — same adapter, different base URL, user token instead of an API key.
Nothing in the addon may assume direct-to-provider access is the only mode.

Risks to design for before taking money, not retrofit: provider ToS on
resold compute, inherited support surface, abuse running under our key
(Tripo error 2008 is a content rejection), payment float.

## Done

- Node-graph product surface: Gemini images → consistent views → Tripo 3D →
  retopo / texture / segment / complete → rig → animate → export / import,
  in a bundled Generate workspace with example graphs. No second UI surface.
- Money discipline: per-button quotes from one pricing module, Run Graph
  chain-total confirmation, busy guards on every spend path, task ids
  recorded at submit, free re-imports from history.
- Current API surface: v3 routes everywhere one exists, per-model parameter
  gating (v2.5 / P1 / v3.x differ heavily), segmentation granularity,
  retopo tiers (smart 30 cr / basic 10 cr), prompted retexturing, the full
  117-preset animation catalogue, batch retargets, Mixamo/Tripo bone specs.
- Hard-won constraints encoded in the UI: Mixamo rigs take no Tripo presets,
  preset vocabulary depends on rig model, quad face caps, rig-last ordering,
  avian has no presets.
- 400+ check offline suite (mocked network, real import pipeline),
  register/unregister symmetry, sandboxed from user data.
- Asset Browser marking + previews on import; free local re-import cache.

## Next

- **Publish**: create the public repo and push (owner's call). README and
  LICENSE ready.
- **Live price verifications**, piggybacked on real runs: retarget 10 vs 20,
  extreme-texture surcharge, completion / quick_cap, v3 convert extras rule,
  P1 multiview.
- **v3 file upload** (`POST /v3/files`) — unlocks segmentation `ref_image`
  masks and image-guided texture prompts.
- **Stylize decision**: no v3 route exists; verify the legacy one or remove
  the operation.
- Payload migration to the documented `input`/`inputs` shapes when the
  legacy shape stops working.
- Packaging leftovers: `.mcp.json` hardcodes an absolute `uvx` path;
  decide whether to keep vendoring `addon.py` (dev-only, 122 KB) or fetch
  from upstream; possibly an `install.py`.

## Later

- Hosted credit backend (closed) for keyless use.
- Provider abstraction proven with exactly one second provider (Meshy or
  Rodin): `submit() / poll() / model_url() / cost()` plus a capability
  matrix, degrading gracefully in the UI. Add providers for reasons, not
  logos.
- Character kit workflows: segmentation → per-part regeneration → assembly.

## Never

- `refine_model` (deprecated upstream) - silent fallbacks of any kind -
  keys anywhere near a .blend file - a second UI surface - our own rigging
  or retopo algorithms (they exist as API calls).
