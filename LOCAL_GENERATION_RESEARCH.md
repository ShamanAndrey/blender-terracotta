# Local (no-API) generation research — handoff document

Researched 2026-08-02 on the Mac side of the project. This file is a
self-contained briefing for a Claude Code session on the Windows PC.

## Context for the reading session

- The project is **Terracotta** (repo: https://github.com/ShamanAndrey/blender-terracotta,
  GPL-3.0-or-later) — a standalone Blender addon doing AI 3D asset generation
  as a node graph. Current providers are cloud APIs: **Tripo** (3D, billed in
  credits) and **Google Gemini** (concept images + consistent 4-view
  reference sheets that feed multiview-to-3D).
- Goal of this research: can small local models replace or supplement the
  APIs — 3D generation and image generation — with zero per-asset cost?
- **Target machine: the PC this file is being read on — NVIDIA RTX 5070 Ti,
  16 GB VRAM, Blackwell (sm_120).** The Mac (M2, 8 GB unified) is a
  secondary footnote at the end.
- The repo's ROADMAP.md already plans a **provider abstraction**: a provider
  is data (`base_url, auth, capabilities[], cost_table`), not code. Local
  generation should eventually be *just another provider entry*.

## Verdict

- **3D on the 5070 Ti: yes.** Best open models reach roughly **Tripo
  v2.5–v3.0 quality** — production-usable geometry with PBR, behind hosted
  APIs on texture resolution, topology cleanliness, and thin structures.
  Hosted stays ahead because vendors keep flagship weights closed (hosted
  Hunyuan 3.x, Tripo 3.1/P1, Sparc3D are not downloadable).
- **Images on the 5070 Ti: yes**, including the hard part — consistent
  multi-view character turnarounds — via 12–14 GB edit-model pipelines.
- **Mac 8 GB: draft-tier only.** No full shape+texture pipeline fits beside
  macOS + Blender.

## 3D generation on 16 GB Blackwell — ranked

| # | Model | Fit on 16 GB | Time (5070 Ti-class) | Output | Notes |
|---|---|---|---|---|---|
| 1 | **TRELLIS.2** (Microsoft, Dec 2025, 4B, MIT) | 512³ tier = exactly 16 GB (1024³ wants 40 GB) | ~2–4 min full run | 400K+ vertex mesh + baked PBR | Best open quality; worst Blackwell story (see gotchas) |
| 2 | **Hunyuan3D-2.1** full PBR | Defaults ~29 GB — must reduce: `max_num_view=6`, 512px textures via community wrappers → 12–16 GB | shape 15–40 s; +PBR texture ~5–10 min | PBR (metal/rough), open training code | Use **YanWenKun/Hunyuan3D-2-WinPortable v4-cu129** — prebuilt Blackwell binaries, no compile |
| 3 | **Hunyuan3D-2.0 via Hunyuan3D-2GP** | Whole pipeline 6–9 GB with offload profiles — comfortable | shape 5–20 s (turbo); +RGB texture 1–3 min | RGB textures (not PBR) | The zero-drama option; supports 2.0/2mini/2mv/turbo |
| 4 | **TripoSG** (1.5B, shape-only) + **PartCrafter** | >8 GB — fine | fast | Excellent geometry, no texture stage | PartCrafter (built on TripoSG VAE) outputs **part-segmented** meshes — interesting vs Terracotta's segmentation feature |
| 5 | **Step1X-3D** (4.8B geo+texture) | Borderline, offloading for texture | — | Hunyuan-2.x class | Small ecosystem; lower priority |
| — | Draft tier: TripoSR, Stable Fast 3D, SPAR3D | trivial (<7 GB) | <1–2 s | blobby, weak backs | placeholder/blockout only |
| — | Geometry-only extras: Hi3DGen, Direct3D-S2, TRELLIS 1 | fit | — | high-res geometry, no texture | |

Not available locally at all: hosted Hunyuan 2.5/3.0/3.1 (closed; GitHub
issue asking about 2.5 weights went unanswered), Sparc3D (hosted platform),
Tripo v3.x/P1, Meshy.

### Blackwell (sm_120) gotchas — all real, all solved

- **PyTorch must be cu128/cu129, ≥2.7** — older pins throw "no kernel image
  available". Override any repo pinning torch ≤2.6.
  https://github.com/pytorch/pytorch/issues/159207
- **Hunyuan custom ops** (`custom_rasterizer`, `differentiable_renderer`)
  need source build with `TORCH_CUDA_ARCH_LIST` including `12.0`; pip can
  exit 0 on a silently failed build — verify
  `hy3dpaint/custom_rasterizer/lib/` is non-empty. 50-series thread:
  https://github.com/Tencent-Hunyuan/Hunyuan3D-2/issues/329 ·
  guide: https://www.qwe.edu.pl/ai-tools/hunyuan3d-2-1-install-guide/
  Or skip entirely via WinPortable v4-cu129.
- **flash-attention**: source build on Windows for sm_120
  (https://github.com/Dao-AILab/flash-attention/issues/2535); most
  pipelines fall back to SDPA if absent.
- **TRELLIS.2 on Blackwell**: `spconv` lacks bf16 kernels →
  `KeyError: torch.bfloat16` when the pipeline auto-picks bf16. Force
  fp16/fp32, use patched forks, or the one-click installers.
  https://github.com/visualbruno/ComfyUI-Trellis2/issues/157 ·
  https://github.com/microsoft/TRELLIS/issues/243
- **ComfyUI-3D-Pack** is the most fragile install; prefer model-specific
  wrappers (kijai's ComfyUI-Hunyuan3DWrapper, visualbruno/ComfyUI-Trellis2).

### Key 3D links

- https://github.com/microsoft/TRELLIS (+ trellis2.app/blog/trellis-2-comfyui, /trellis-2-low-vram)
- https://github.com/tencent-hunyuan/hunyuan3d-2.1 · https://github.com/Tencent-Hunyuan/Hunyuan3D-2
- https://github.com/YanWenKun/Hunyuan3D-2-WinPortable
- https://github.com/deepbeepmeep/Hunyuan3D-2GP
- https://github.com/VAST-AI-Research/TripoSG · https://github.com/wgsxm/PartCrafter
- https://github.com/stepfun-ai/Step1X-3D
- https://github.com/Stability-AI/stable-fast-3d · https://github.com/Stability-AI/stable-point-aware-3d
- Quality/ELO context: https://www.3daistudio.com/blog/hitem3d-vs-meshy-vs-tripo-comparison

## Image generation on 16 GB Blackwell

- **Single concept art**: easy. FLUX.1-schnell/dev (fp8), FLUX.2 klein
  4B/9B, SD 3.5, Z-Image Turbo (Alibaba, 6B, Apache-2.0 — near-FLUX quality,
  tiny footprint) all fit. Seconds per image.
- **Consistent orthographic turnarounds (the pipeline-critical part)**:
  - **Qwen-Image-Edit 2511 + "Multiple-Angles" LoRA** — currently the best
    local turnaround tool: 96 camera angles from one reference; Q4 GGUF
    ~12–14 GB → fits. https://dev.to/gary_yan_86eb77d35e0070f5/qwen-image-edit-2511-multiple-angles-lora-complete-guide-to-multi-angle-ai-image-generation-1g5f
  - **FLUX Kontext turnaround LoRA** (12B base, fp8 fits 16 GB):
    https://www.runcomfy.com/comfyui-workflows/flux-kontext-character-turnaround-sheet-lora
  - **MV-Adapter** (ICCV 2025, ~14 GB image-to-multiview):
    https://github.com/huanngzh/MV-Adapter
  - FLUX.2 klein multi-reference editing (geometry-preserving edits):
    https://blog.comfy.org/p/flux2-klein-4b-fast-local-image-editing
- Quality gap vs Gemini/Nano Banana Pro: real but now *incremental*, not
  categorical — NB Pro still wins on reference handling (up to 14 refs),
  text rendering, and conversational iteration.
- **Watch item**: **Z-Image-Edit** (6B, announced Nov 2025, unreleased) —
  explicitly demos character rotation; would be the first small-machine
  turnaround model. https://learn.rundiffusion.com/z-image/

## Suggested plan of attack on the PC

1. **Hunyuan3D-2 WinPortable v4-cu129** first — prebuilt Blackwell, least
   fight. Generate a few assets from existing Terracotta reference images;
   compare against the Tripo history side by side.
2. If quality justifies the dependency battle, stand up **TRELLIS.2**
   (one-click installer or ComfyUI-Trellis2, fp16 forced).
3. For turnarounds: ComfyUI + Qwen-Image-Edit Q4 + Multiple-Angles LoRA.
4. **Integration architecture** (ties into ROADMAP's provider seam): run
   the local stack as an HTTP server on the PC (ComfyUI API or a small
   wrapper). Terracotta — on any machine, including the Mac over LAN —
   treats it as a provider entry (`base_url: http://<pc>:8188`, cost 0).
   Free local tier (~v2.5 quality) + Tripo as the quality tier, one node UI
   over both. Nothing in the addon needs restructuring for this; that seam
   was designed in.

## Mac 8 GB footnote (secondary)

Only realistic on the M2/8GB: TripoSR / SF3D in CPU mode (draft-tier
geometry), possibly INT4 Hunyuan3D-2.1 shape-only via
https://github.com/dgrauet/Hunyuan3D-2.1-mlx (~4 GB peak; 16 GB machines
recommended even for that). For images: **Z-Image Turbo 6-bit via Draw
Things** genuinely works (~4 GB, minutes per image) for single concept art.
No multi-view path exists under 16 GB. macOS + Blender already consume
3–6 GB of the pool; treat every "runs on 8 GB" claim accordingly.
