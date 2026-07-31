"""Google Gemini image generation.

Images are generated directly against Google's API rather than through Tripo,
so image work is billed by Google and never leaves via a third party. Tripo is
used only for 3D.

The Gemini image API is an "interactions" endpoint, not a task queue: one
synchronous POST returns the image inline as base64. That is a different shape
from Tripo, so this module keeps its own request logic but reuses the addon's
job registry so the UI reports progress uniformly.
"""

import base64
import json
import os
import threading
import urllib.error
import urllib.request

import bpy

from . import api

BASE = "https://generativelanguage.googleapis.com/v1beta/interactions"
KEY_PATH = os.path.expanduser("~/.gemini_key")

# Model ids as Google names them, with their familiar names.
MODELS = [
    ("gemini-3.1-flash-image", "Nano Banana 2",
     "Best all-round. 1K-4K, up to 10 object + 4 character references"),
    ("gemini-3-pro-image", "Nano Banana Pro",
     "Highest quality. 1K-4K, 6 object + 5 character + 3 style references"),
    ("gemini-3.1-flash-lite-image", "Nano Banana 2 Lite",
     "Cheapest. 1K only, up to 14 object references"),
    ("gemini-2.5-flash-image", "Nano Banana (legacy)",
     "Retires October 2026; prefer Nano Banana 2 Lite"),
]

# Published per-image prices in USD, by model and output size.
PRICES = {
    "gemini-3.1-flash-image": {"0.5K": 0.045, "1K": 0.067, "2K": 0.101, "4K": 0.151},
    "gemini-3-pro-image": {"1K": 0.134, "2K": 0.134, "4K": 0.24},
    "gemini-3.1-flash-lite-image": {"1K": 0.0336},
    "gemini-2.5-flash-image": {"1K": 0.039},
}

# Reference image budgets, per Google's documentation.
REFERENCE_LIMITS = {
    "gemini-3.1-flash-lite-image": {"object": 14, "character": 0, "style": 0},
    "gemini-3.1-flash-image": {"object": 10, "character": 4, "style": 0},
    "gemini-3-pro-image": {"object": 6, "character": 5, "style": 3},
    "gemini-2.5-flash-image": {"object": 3, "character": 0, "style": 0},
}

SIZES = {
    "gemini-3.1-flash-image": ("0.5K", "1K", "2K", "4K"),
    "gemini-3-pro-image": ("1K", "2K", "4K"),
    "gemini-3.1-flash-lite-image": ("1K",),
    "gemini-2.5-flash-image": ("1K",),
}

ASPECT_RATIOS = ("1:1", "3:2", "2:3", "3:4", "4:3", "4:5", "5:4",
                 "9:16", "16:9", "21:9")

MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp"}


def read_key():
    """Key from addon preferences, then env, then ~/.gemini_key."""
    addon = bpy.context.preferences.addons.get(__package__)
    if addon:
        key = (getattr(addon.preferences, "google_api_key", "") or "").strip()
        if key:
            return key
    env = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if env:
        return env.strip()
    if os.path.exists(KEY_PATH):
        with open(KEY_PATH) as f:
            key = f.read().strip()
        if key:
            return key
    raise RuntimeError(
        "No Google API key. Add it in the Tripo addon preferences, set "
        f"GEMINI_API_KEY, or write it to {KEY_PATH} (chmod 600).")


def has_key():
    """Whether a Google key is configured, without raising or revealing it."""
    try:
        read_key()
        return True
    except Exception:
        return False


def price(model, size="1K"):
    table = PRICES.get(model, {})
    return table.get(size) or next(iter(table.values()), 0.0)


def max_references(model, kind="object"):
    return REFERENCE_LIMITS.get(model, {}).get(kind, 0)


def sizes_for(model):
    return SIZES.get(model, ("1K",))


def _encode_image(path):
    ext = os.path.splitext(path)[1].lower()
    mime = MIME.get(ext)
    if not mime:
        raise ValueError(f"Unsupported image type '{ext}'. "
                         f"Use {', '.join(sorted(MIME))}")
    with open(path, "rb") as f:
        return {"type": "image", "mime_type": mime,
                "data": base64.b64encode(f.read()).decode("ascii")}


def _request(body, key, timeout=300):
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE, data=data, method="POST")
    req.add_header("x-goog-api-key", key)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            err = json.loads(body).get("error", {})
            detail = err.get("message") or body
        except ValueError:
            detail = body[:300]
        raise RuntimeError(f"HTTP {e.code}: {detail}") from None


def _find_images(payload):
    """Pull every inline image out of a response.

    The response shape isn't fully documented, and convenience fields differ
    between SDKs, so walk the structure for anything that looks like inline
    image data rather than depending on one key path.
    """
    found = []

    def walk(node):
        if isinstance(node, dict):
            data = node.get("data") or node.get("inline_data") or \
                node.get("inlineData")
            mime = node.get("mime_type") or node.get("mimeType") or ""
            if isinstance(data, str) and len(data) > 256 and \
                    (str(mime).startswith("image/") or node.get("type") == "image"):
                found.append((str(mime) or "image/png", data))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found


def generate(prompt, model="gemini-3.1-flash-image", references=None,
             aspect_ratio=None, image_size=None, name=None):
    """Generate an image. Returns a job id; poll with api.status()."""
    key = read_key()
    api._ensure_timer()

    refs = [r for r in (references or []) if r]
    for path in refs:
        if not os.path.exists(path):
            raise RuntimeError(f"Reference image not found: {path}")

    job_id = api.register_job(kind="google_image", prompt=prompt, name=name,
                              model=model)

    def work():
        def note(**kw):
            api.update_job(job_id, **kw)
        try:
            note(state="running", progress=10)
            inputs = [{"type": "text", "text": prompt}]
            for path in refs:
                inputs.append(_encode_image(path))

            body = {"model": model, "input": inputs}
            fmt = {"type": "image"}
            if aspect_ratio:
                fmt["aspect_ratio"] = aspect_ratio
            if image_size:
                fmt["image_size"] = image_size
            if len(fmt) > 1:
                body["response_format"] = fmt

            note(progress=40)
            payload = _request(body, key)
            images = _find_images(payload)
            if not images:
                note(state="error",
                     message=f"No image in response: {json.dumps(payload)[:300]}")
                return

            note(progress=90)
            # Google's docs define the output as the LAST image block; the
            # response may include earlier blocks (echoed reference inputs,
            # interim "thinking" images), so index 0 can be the user's own
            # reference handed back.
            primary = len(images) - 1
            saved = {}
            for i, (mime, b64) in enumerate(images):
                ext = ".png" if "png" in mime else ".jpg"
                path = os.path.join(api.images_dir(),
                                    f"google_{job_id}_{i}{ext}")
                with open(path, "wb") as f:
                    f.write(base64.b64decode(b64))
                saved["generated_image" if i == primary else f"extra_{i}"] = path

            note(state="done", progress=100, images=saved, objects=[],
                 thumb=saved.get("generated_image"),
                 cost_usd=price(model, image_size or "1K"))
            # Persist: these images are paid results, and without a history
            # entry a restart severs every chain that depends on them.
            api.record_job_by_id(job_id)
        except Exception as e:
            note(state="error", message=str(e))

    threading.Thread(target=work, daemon=True).start()
    return job_id


VIEW_PROMPTS = {
    "front": "front view, facing the camera directly, orthographic",
    "left": "left side view, 90 degrees profile, orthographic",
    "back": "rear view, facing directly away from the camera, orthographic",
    "right": "right side view, 90 degrees profile, orthographic",
}


def generate_views(subject_prompt, reference=None,
                   model="gemini-3-pro-image", image_size=None, name=None,
                   views=("front", "left", "back", "right")):
    """Generate consistent orthographic views for multiview-to-3D.

    Google has no single multiview call, so each view is a separate request
    that carries the same reference image for consistency. Requests run
    sequentially -- the reference is what keeps them coherent, and firing them
    in parallel gains little while making failures harder to attribute.
    """
    key = read_key()
    api._ensure_timer()
    job_id = api.register_job(kind="google_views", prompt=subject_prompt,
                              name=name, model=model)

    def work():
        def note(**kw):
            api.update_job(job_id, **kw)
        try:
            note(state="running", progress=0)
            saved = {}
            for i, view in enumerate(views):
                prompt = (f"{subject_prompt}. {VIEW_PROMPTS[view]}. "
                          "Plain neutral background, whole subject in frame, "
                          "consistent proportions and colours across views.")
                inputs = [{"type": "text", "text": prompt}]
                if reference:
                    inputs.append(_encode_image(reference))
                body = {"model": model, "input": inputs}
                fmt = {"type": "image", "aspect_ratio": "1:1"}
                if image_size:
                    fmt["image_size"] = image_size
                body["response_format"] = fmt

                payload = _request(body, key)
                images = _find_images(payload)
                if not images:
                    note(state="error", message=f"No image returned for {view}")
                    return
                mime, b64 = images[-1]   # the output is the LAST block
                ext = ".png" if "png" in mime else ".jpg"
                path = os.path.join(api.images_dir(),
                                    f"google_{job_id}_{view}{ext}")
                with open(path, "wb") as f:
                    f.write(base64.b64decode(b64))
                saved[view] = path
                # Note each view as it lands: on a partial failure the
                # already-paid views stay visible instead of vanishing.
                note(progress=int((i + 1) / len(views) * 100),
                     images=dict(saved))

            note(state="done", images=saved, objects=[],
                 thumb=saved.get("front"),
                 cost_usd=price(model, image_size or "1K") * len(views))
            api.record_job_by_id(job_id)
        except Exception as e:
            note(state="error", message=str(e))

    threading.Thread(target=work, daemon=True).start()
    return job_id
