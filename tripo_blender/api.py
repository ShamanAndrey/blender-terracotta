"""Tripo AI -> Blender bridge.

Everything touching the network runs on a worker thread -- including the submit
and any image upload -- so clicking Generate never freezes the UI and the MCP
socket (180s timeout) never stalls. The finished GLB is imported on the main
thread from a timer, since bpy is not thread-safe.

    from tripo_blender import api
    job = api.start(prompt="a cute cat")   # returns a job id immediately
    api.status(job)                        # poll until state == "done"
"""

import json
import os
import queue
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import bpy

# Set by the addon UI so imports can be auto-optimized. Called on the main
# thread with (job_id, job_dict); exceptions are swallowed to protect the pump.
post_import_hook = None

KEY_PATH = os.path.expanduser("~/.tripo_key")

V3_BASE = "https://openapi.tripo3d.ai/v3"
V2_BASE = "https://api.tripo3d.ai/v2/openapi"

# v3 rejects requests that omit `model` (code 1004). Known values:
# P1-20260311, v2.5-20250123, v3.0-20250812, v3.1-20260211
DEFAULT_V3_MODEL = "v3.1-20260211"

# Tripo has two API generations in circulation. We try v3 and fall back to v2,
# then remember which one answered.

_jobs = {}
_jobs_lock = threading.Lock()
_import_queue = queue.Queue()
_timer_running = False




# P1 rejects anything outside its supported list with an error, rather than
# ignoring it. These are the documented exclusions.
P1_UNSUPPORTED = {"quad", "smart_low_poly", "generate_parts", "geometry_quality"}
P1_FACE_LIMIT = (48, 20000)


def short_model(model):
    """`v3.1-20260211` -> `v3.1`, `P1-20260311` -> `P1`."""
    if not model:
        return "?"
    return str(model).split("-")[0]


def _read_key():
    """Key from the Tripo panel, then env, then ~/.tripo_key."""
    # __package__ so this keeps working whatever the installed addon is named.
    addon = bpy.context.preferences.addons.get(__package__)
    if addon:
        key = (addon.preferences.tripo_api_key or "").strip()
        if key:
            return key

    env = os.environ.get("TRIPO_API_KEY")
    if env:
        return env.strip()

    if os.path.exists(KEY_PATH):
        with open(KEY_PATH) as f:
            key = f.read().strip()
        if key:
            return key

    raise RuntimeError(
        "No Tripo API key. Use the Account node or Tripo panel > API Keys, "
        f"or write it to {KEY_PATH} (chmod 600)."
    )


def _request(url, key, payload=None, timeout=60):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Authorization", f"Bearer {key}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # Tripo puts the real reason in the body (e.g. code 2010 = out of
        # credit); the bare status line hides it.
        body = e.read().decode(errors="replace")
        try:
            parsed = json.loads(body)
            detail = parsed.get("message") or body
            suggestion = parsed.get("suggestion")
            if suggestion:
                detail = f"{detail} ({suggestion})"
        except ValueError:
            detail = body[:300]
        raise RuntimeError(f"HTTP {e.code}: {detail}") from None


def _upload(path, key):
    """Upload a local image, returning Tripo's file token.

    Only v2 exposes an upload route (v3 returns 404 for it), but tokens minted
    here are accepted by both, so this works regardless of submit flavor.
    """
    import requests  # bundled with Blender; only needed for multipart

    ext = os.path.splitext(path)[1].lstrip(".").lower() or "png"
    with open(path, "rb") as f:
        resp = requests.post(
            f"{V2_BASE}/upload",
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (os.path.basename(path), f, f"image/{ext}")},
            timeout=120,
        )
    try:
        payload = resp.json()
    except ValueError:
        raise RuntimeError(f"Upload failed (HTTP {resp.status_code}): {resp.text[:300]}")

    if payload.get("code") not in (0, None):
        raise RuntimeError(f"Upload rejected: {payload}")

    data = payload.get("data") or {}
    token = data.get("image_token") or data.get("file_token") or data.get("token")
    if not token:
        raise RuntimeError(f"No file token in upload response: {payload}")
    return token, ext


def _file_spec(image, key):
    """Build the `file` object for an image task, uploading if it's a local path."""
    if re.match(r"^https?://", image, re.IGNORECASE):
        return {"url": image}, None
    if not os.path.exists(image):
        raise RuntimeError(f"Image not found: {image}")
    token, ext = _upload(image, key)
    return {"file_token": token, "type": ext}, token


def has_key():
    """Whether a Tripo key is configured, without raising or revealing it."""
    try:
        _read_key()
        return True
    except Exception:
        return False


def validate_key(key=None):
    """Check a key works. Returns the balance, or raises with the API's reason."""
    key = key or _read_key()
    resp = _request(f"{V2_BASE}/user/balance", key)
    data = resp.get("data") or {}
    return {"balance": data.get("balance"), "frozen": data.get("frozen")}


def _submit(key, prompt=None, image=None, images=None, model=None,
            face_limit=None, quad=None, extra=None):
    """Create a task. Returns (task_id, flavor).

    face_limit caps the generated mesh directly, which is far better than
    decimating afterwards. Undocumented for text_to_model but the API accepts
    it (CreateTaskRequest["face_limit"], Integer).
    """
    file_spec = _file_spec(image, key)[0] if image else None
    # Multiview expects a fixed [front, left, back, right] order; empty slots
    # are passed as {} so the positions stay aligned.
    file_specs = (
        [_file_spec(i, key)[0] if i else {} for i in images] if images else None
    )

    extras = {}
    if face_limit:
        if face_limit < 1:
            raise ValueError("face_limit must be positive")
        extras["face_limit"] = int(face_limit)
    if quad:
        extras["quad"] = True

    # Confirmed to exist on CreateTaskRequest (via deserialization errors):
    # model_seed/texture_seed Integer, texture_quality/style/texture/pbr String,
    # auto_size/smart_low_poly Boolean. `seed` is NOT a field -- silently
    # ignored. `orientation` is real, but only on image/multiview tasks.
    for k, v in (extra or {}).items():
        if v is not None:
            extras[k] = v

    # P1 errors on unsupported parameters instead of ignoring them, so strip
    # them rather than letting the request fail.
    if str(model or DEFAULT_V3_MODEL).startswith("P1"):
        dropped = [k for k in P1_UNSUPPORTED if extras.get(k)]
        for k in P1_UNSUPPORTED:
            extras.pop(k, None)
        if dropped:
            print(f"[tripo] P1 does not support {', '.join(dropped)} -- dropped")
        fl = extras.get("face_limit")
        if fl is not None and not (P1_FACE_LIMIT[0] <= fl <= P1_FACE_LIMIT[1]):
            raise ValueError(
                f"P1 requires face_limit between {P1_FACE_LIMIT[0]} and "
                f"{P1_FACE_LIMIT[1]}, got {fl}")

    # Documented incompatibility: generate_parts cannot be combined with
    # texture, pbr or quad. Sending them together is what produced 21
    # untextured parts -- the API silently drops the texturing rather than
    # erroring, so force the valid combination here.
    if extras.get("generate_parts"):
        extras["texture"] = False
        extras["pbr"] = False
        if extras.pop("quad", None):
            raise ValueError("generate_parts cannot be combined with quad")

    # One deterministic route per input type, v3 only. No fallback: an
    # earlier version fell through to v2 on any v3 exception, including a
    # timeout after the request had already been delivered -- which can
    # create a second billed task, and masks the real failure either way.
    body = {"model": model or DEFAULT_V3_MODEL, **extras}
    if prompt:
        body["prompt"] = prompt
        url = f"{V3_BASE}/generation/text-to-model"
    elif file_specs:
        body["files"] = file_specs
        url = f"{V3_BASE}/generation/multiview-to-model"
    else:
        body["file"] = file_spec
        url = f"{V3_BASE}/generation/image-to-model"

    resp = _request(url, key, body)
    if resp.get("code") not in (0, None):
        raise RuntimeError(f"Submit failed: {resp}")
    task_id = (resp.get("data") or {}).get("task_id")
    if not task_id:
        raise RuntimeError(f"Submit failed: no task_id in {resp}")
    return task_id, "v3"



def thumb_dir():
    d = bpy.utils.user_resource("DATAFILES", path="tripo_thumbs", create=True)
    return d


def images_dir():
    """Where generated images live. Unlike thumbnails this is never
    auto-pruned: these files are paid results and chain inputs, and history
    rows falling off the cap must not silently destroy them."""
    return bpy.utils.user_resource("DATAFILES", path="tripo_images",
                                   create=True)


def _cache_thumb(task_id, url):
    """Download the preview render. Result URLs expire in ~5 minutes, so this
    has to happen while the task is fresh -- not lazily when the UI asks."""
    if not url:
        return None
    path = os.path.join(thumb_dir(), f"{task_id}.png")
    if os.path.exists(path):
        return path
    # Write to a temp name and rename: a download that dies mid-body must
    # not leave a 0-byte file that satisfies the exists() check forever.
    tmp = path + ".part"
    try:
        with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
            f.write(r.read())
        os.replace(tmp, path)
        return path
    except Exception as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        print(f"[tripo] thumbnail fetch failed: {e!r}")
        return None

def _poll_url(flavor, task_id):
    if flavor == "v3":
        return f"{V3_BASE}/tasks/{task_id}"
    return f"{V2_BASE}/task/{task_id}"



def _preview_url(out):
    """Preview render URL. v3 names it `rendered_image_url`, v2
    `rendered_image` -- looking for only one silently loses thumbnails."""
    for field in ("rendered_image_url", "rendered_image",
                  "generated_image_url", "generated_image"):
        val = (out or {}).get(field)
        if isinstance(val, str) and val:
            return val
    return None



MODEL_EXTS = {".glb": "gltf", ".gltf": "gltf", ".fbx": "fbx", ".obj": "obj"}
# Formats convert_model can emit but Blender can't re-import; they are only
# ever saved to disk, never queued for import.
SAVE_EXTS = set(MODEL_EXTS) | {".usdz", ".stl", ".3mf"}


def _ext_from_url(url, default=".glb"):
    """Pick the file extension from a result URL.

    Output isn't always glTF: quad=true forces FBX, and convert_model can
    return OBJ/USDZ/STL. Saving an FBX as .glb and handing it to the glTF
    importer fails in a confusing way.
    """
    path = urllib.parse.urlparse(url).path if url else ""
    ext = os.path.splitext(path)[1].lower()
    return ext if ext in SAVE_EXTS else default


def _model_url(data):
    out = data.get("output") or {}
    for field in ("pbr_model", "model_url", "model", "base_model"):
        val = out.get(field)
        if isinstance(val, str) and val:
            return val
    return None


def _worker(job_id, key, name, poll_timeout, prompt, image, images, model,
            face_limit, quad, extra):
    def note(**kw):
        with _jobs_lock:
            _jobs[job_id].update(kw)

    # Submit here rather than in start(): uploading a reference image is a
    # network round-trip, and doing it inline would freeze Blender's UI.
    try:
        task_id, flavor = _submit(
            key, prompt=prompt, image=image, images=images, model=model,
            face_limit=face_limit, quad=quad, extra=extra,
        )
        note(task_id=task_id, flavor=flavor, state="running")
        # The task now exists and bills regardless of what happens to this
        # thread. Record the id immediately: it is a permanent, free handle
        # to paid work, and losing it to a poll failure means paying twice.
        _record({"task_id": task_id, "job": job_id, "kind": "generate",
                 "name": name, "prompt": prompt,
                 "model": model or DEFAULT_V3_MODEL, "flavor": flavor,
                 "time": int(time.time())})
    except Exception as e:
        note(state="error", message=str(e))
        return

    deadline = time.time() + poll_timeout
    poll_failures = 0
    try:
        while True:
            if time.time() > deadline:
                note(state="error", message=f"timed out after {poll_timeout}s")
                return

            try:
                resp = _request(_poll_url(flavor, task_id), key)
                poll_failures = 0
            except Exception as e:
                # A poll is a free, idempotent read; one dropped packet must
                # not abandon a billed, still-running task.
                poll_failures += 1
                if poll_failures >= 3:
                    raise
                print(f"[tripo] poll retry {poll_failures}/3: {e!r}")
                time.sleep(3)
                continue
            data = resp.get("data") or {}
            status = (data.get("status") or "").lower()
            # v2 calls it consumed_credit, v3 calls it credits_consumed.
            out = data.get("output") or {}
            note(progress=data.get("progress"), api_status=status,
                 credits=data.get("consumed_credit") or data.get("credits_consumed"),
                 eta=data.get("running_left_time"), queued=data.get("queuing_num"),
                 preview_url=_preview_url(out))

            if status in ("success", "succeeded", "completed"):
                url = _model_url(data)
                if not url:
                    note(state="error", message=f"no model url in {data}")
                    return
                # Result URLs expire ~5 minutes after success, so fetch now.
                note(state="downloading")
                path = os.path.join(
                    tempfile.gettempdir(), f"tripo_{task_id}{_ext_from_url(url)}"
                )
                with urllib.request.urlopen(url, timeout=300) as r, open(path, "wb") as f:
                    f.write(r.read())
                thumb = _cache_thumb(task_id, _jobs[job_id].get("preview_url"))
                note(state="importing", path=path, thumb=thumb)
                _import_queue.put((job_id, path, name))
                return

            if status in ("failed", "cancelled", "canceled", "banned", "expired", "unknown"):
                note(state="error", message=f"task {status}: {data}")
                return

            time.sleep(3)
    except Exception as e:
        note(state="error", message=repr(e))


def _drain():
    """Main-thread import pump. Registered as a bpy timer."""
    while True:
        try:
            job_id, path, name = _import_queue.get_nowait()
        except queue.Empty:
            break
        try:
            before = set(bpy.data.objects)
            kind = MODEL_EXTS.get(os.path.splitext(path)[1].lower(), "gltf")
            if kind == "fbx":
                bpy.ops.import_scene.fbx(filepath=path)
            elif kind == "obj":
                bpy.ops.wm.obj_import(filepath=path)
            else:
                bpy.ops.import_scene.gltf(filepath=path)
            new = [o for o in bpy.data.objects if o not in before]
            if name and new:
                # Model version goes last, so multi-part imports read
                # SofaParts_3_v3.1 -- which model made it is what you need
                # when comparing assets side by side.
                with _jobs_lock:
                    model = _jobs.get(job_id, {}).get("model")
                # Post-processing jobs have no model version; appending "_?"
                # to a rig or conversion is noise, not information.
                suffix = f"_{short_model(model)}" if model else ""
                for i, obj in enumerate(new):
                    stem = name if i == 0 else f"{name}_{i}"
                    obj.name = f"{stem}{suffix}"
            with _jobs_lock:
                _jobs[job_id].update(state="done", objects=[o.name for o in new])
                snapshot = dict(_jobs[job_id])
            _record_job(snapshot)
        except Exception as e:
            with _jobs_lock:
                _jobs[job_id].update(state="error", message=f"import failed: {e!r}")
            continue

        if post_import_hook:
            # A failing hook must not kill the pump or lose the import.
            try:
                post_import_hook(job_id, snapshot)
            except Exception as e:
                print(f"[tripo] post-import hook failed: {e!r}")
    return 1.0


def _ensure_timer():
    global _timer_running
    if not _timer_running:
        bpy.app.timers.register(_drain, persistent=True)
        _timer_running = True


def stop_timer():
    """Tear the import pump down.

    Addon disable must leave no timers behind: an orphaned pump keeps
    importing through dead module state, and the stale _timer_running flag
    would stop a re-enabled addon from starting a fresh one.
    """
    global _timer_running
    if bpy.app.timers.is_registered(_drain):
        bpy.app.timers.unregister(_drain)
    _timer_running = False


def start(prompt=None, image=None, images=None, name=None, model=None,
          poll_timeout=900, face_limit=None, quad=None, **extra):
    """Kick off a generation. `image` may be a URL or a local path.

    Returns a job id immediately -- nothing blocks, not even the upload. Poll
    with status(job_id); the real Tripo task_id appears in the job dict once
    submitted.
    """
    if not prompt and not image and not images:
        raise ValueError("Need prompt, image, or images.")

    key = _read_key()
    _ensure_timer()

    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "job": job_id,
            "state": "submitting",
            "kind": "generate",
            "prompt": prompt,
            "image": image,
            "images": list(images) if images else None,
            "model": model or DEFAULT_V3_MODEL,
            "name": name,
            "face_limit": face_limit,
            "extra": dict(extra),
        }

    t = threading.Thread(
        target=_worker,
        args=(job_id, key, name, poll_timeout, prompt, image, images, model,
              face_limit, quad, dict(extra)),
        daemon=True,
    )
    t.start()
    return job_id



def register_job(kind, prompt=None, name=None, **extra):
    """Create a job entry for a non-Tripo provider.

    The UI reads state from one registry, so anything that generates should
    register here rather than inventing its own progress reporting.
    """
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {"state": "submitting", "kind": kind, "job": job_id,
                         "prompt": prompt, "name": name, **extra}
    return job_id


def record_job_by_id(job_id):
    """Snapshot a job under the lock and write it to history.

    For provider modules (Google) whose jobs finish outside the import pump,
    which is where Tripo jobs get recorded.
    """
    with _jobs_lock:
        snapshot = dict(_jobs.get(job_id, {}))
    _record_job(snapshot)


def update_job(job_id, **kw):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kw)


def active_jobs():
    """Job ids still in flight."""
    with _jobs_lock:
        return [
            k for k, v in _jobs.items()
            if v.get("state") in ("submitting", "running", "downloading", "importing")
        ]


def clear_finished():
    """Drop completed and failed jobs from the list."""
    with _jobs_lock:
        for k in [k for k, v in _jobs.items() if v.get("state") in ("done", "error")]:
            del _jobs[k]


def balance():
    """Remaining API credits. Only exposed on v2; v3 has no balance route."""
    resp = _request(f"{V2_BASE}/user/balance", _read_key())
    return (resp.get("data") or {}).get("balance")



def refresh_balance_async(callback=None):
    """Fetch the balance off-thread. The UI opens before any network call has
    happened, and showing a stale 0 reads as 'you have no credits'."""
    def work():
        try:
            info = validate_key()
        except Exception:
            return
        if callback:
            callback(info)
    threading.Thread(target=work, daemon=True).start()

def status(task_id=None):
    """Status of one job, or all of them."""
    with _jobs_lock:
        if task_id:
            return dict(_jobs.get(task_id, {"state": "unknown"}))
        return {k: dict(v) for k, v in _jobs.items()}


# --------------------------------------------------------------------------
# Task history -- survives Blender restarts, unlike the in-memory job list.
# --------------------------------------------------------------------------


# Job kinds, for the library UI and for knowing what can be re-imported.
GENERATION_TYPES = {"text_to_model", "image_to_model", "multiview_to_model"}


def _record_job(job):
    """Write a finished job to history.

    Previously only imports were recorded, so uploads, conversions and
    post-processing lost their task ids on restart -- and a task id is a
    permanent, free handle to work already paid for. Losing one means paying
    twice.
    """
    if job.get("reimport"):
        return
    task = job.get("task_id")
    if not task and not job.get("images"):
        return   # nothing durable to keep
    _record({
        "task_id": task,
        "job": job.get("job"),
        "kind": job.get("kind") or "generate",
        "flavor": job.get("flavor"),
        "name": job.get("name"),
        "prompt": job.get("prompt"),
        "model": job.get("model"),
        "credits": job.get("credits"),
        "cost_usd": job.get("cost_usd"),
        "thumb": job.get("thumb"),
        "images": job.get("images"),
        "source": job.get("source"),
        "path": job.get("path") if job.get("save_to") else None,
        "time": int(time.time()),
    })


def _history_path():
    d = bpy.utils.user_resource("CONFIG", path="tripo", create=True)
    return os.path.join(d, "history.json")


_history_lock = threading.Lock()


def history(limit=50):
    """Past tasks, newest first."""
    with _history_lock:
        try:
            with open(_history_path()) as f:
                return json.load(f)[:limit]
        except (OSError, ValueError):
            return []


def _record(entry):
    """Prepend one entry, under a lock and atomically.

    Jobs complete on worker threads, so two can record at once; without the
    lock one entry silently vanishes. The temp-file rename keeps a crash
    mid-write from truncating the whole file. Dedupe keys on task id when
    there is one, else on the job id (Google image jobs have no task).
    """
    key = entry.get("task_id") or entry.get("job")
    with _history_lock:
        try:
            try:
                with open(_history_path()) as f:
                    items = json.load(f)
            except (OSError, ValueError):
                items = []
            if key:
                items = [i for i in items
                         if (i.get("task_id") or i.get("job")) != key]
            items.insert(0, entry)
            tmp = _history_path() + ".tmp"
            with open(tmp, "w") as f:
                json.dump(items[:200], f, indent=1)
            os.replace(tmp, _history_path())
            _prune_thumbs(items[:200])
        except OSError as e:
            print(f"[tripo] could not write history: {e!r}")


def _prune_thumbs(items):
    """Delete cached files that no history row references any more.

    History is capped at 200 rows, but the thumbnails and generated images
    belonging to dropped rows used to stay behind forever. Anything a row
    still points at is kept; unreferenced files are only deleted once they
    are a day old, so an in-flight job that has written its images but not
    yet recorded them can't lose its output.
    """
    keep = set()
    for e in items:
        # "images" is a dict of outputs on Google rows but a list of input
        # references on Tripo rows.
        imgs = e.get("images") or {}
        vals = imgs.values() if isinstance(imgs, dict) else imgs
        paths = [e.get("thumb"), *vals]
        keep.update(os.path.basename(p) for p in paths
                    if p and isinstance(p, str))
    cutoff = time.time() - 86400
    try:
        d = thumb_dir()
        for fn in os.listdir(d):
            if fn in keep:
                continue
            path = os.path.join(d, fn)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
    except OSError as e:
        print(f"[tripo] thumbnail prune skipped: {e!r}")


def forget_history():
    try:
        os.remove(_history_path())
    except OSError:
        pass


def reimport(task_id, name=None, flavor=None):
    """Re-download and import a past task.

    Result URLs expire after ~5 minutes, so this re-polls the task to mint a
    fresh one rather than reusing the stored URL.
    """
    key = _read_key()
    _ensure_timer()
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {"job": job_id, "state": "downloading", "task_id": task_id,
                         "name": name, "prompt": "(re-import)", "reimport": True}

    def work():
        def note(**kw):
            with _jobs_lock:
                _jobs[job_id].update(kw)
        try:
            errors = []
            for f in ([flavor] if flavor else ["v3", "v2"]):
                try:
                    resp = _request(_poll_url(f, task_id), key)
                except Exception as e:
                    # Swallowing this told users a live task was dead -- and
                    # a "dead" task gets regenerated and paid for again.
                    errors.append(f"{f}: {e}")
                    continue
                data = resp.get("data") or {}
                # The task echoes its own input, so the model survives a
                # re-import even though our job dict never saw it.
                src = data.get("input") or {}
                note(model=src.get("model") or src.get("model_version"))
                url = _model_url(data)
                if url:
                    path = os.path.join(tempfile.gettempdir(),
                                        f"tripo_{task_id}{_ext_from_url(url)}")
                    with urllib.request.urlopen(url, timeout=300) as r, open(path, "wb") as fh:
                        fh.write(r.read())
                    note(state="importing", path=path)
                    _import_queue.put((job_id, path, name))
                    return
            detail = "; ".join(errors) if errors else "no model url in the task"
            note(state="error",
                 message=f"could not fetch that task: {detail}")
        except Exception as e:
            note(state="error", message=repr(e))

    threading.Thread(target=work, daemon=True).start()
    return job_id


def start_post(operation, source_task_id, name=None, face_limit=None,
               part_names=None, save_to=None, poll_timeout=1800, **extra):
    """Run a post-processing task on a previous task's output.

    Every one of these takes `original_model_task_id` -- which is what an edge
    in the node graph represents. v2 only; v3 has no route for these.
    """
    key = _read_key()
    _ensure_timer()

    body = {"type": operation}
    if source_task_id:
        body["original_model_task_id"] = source_task_id
    if face_limit:
        body["face_limit"] = int(face_limit)
    if part_names:
        body["part_names"] = list(part_names)
    for k, v in (extra or {}).items():
        if v is not None:
            body[k] = v

    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {"job": job_id, "state": "submitting", "kind": operation,
                         "prompt": operation, "name": name,
                         "source": source_task_id, "save_to": save_to}

    def work():
        def note(**kw):
            with _jobs_lock:
                _jobs[job_id].update(kw)
        try:
            resp = _request(f"{V2_BASE}/task", key, body)
            task_id = (resp.get("data") or {}).get("task_id")
            if not task_id:
                note(state="error", message=f"no task_id: {resp}")
                return
            note(task_id=task_id, flavor="v2", state="running")
            _record({"task_id": task_id, "job": job_id, "kind": operation,
                     "name": name, "flavor": "v2", "time": int(time.time())})
        except Exception as e:
            note(state="error", message=str(e))
            return
        _poll_and_import(job_id, "v2", task_id, key, name, poll_timeout, note,
                         save_to=save_to)

    threading.Thread(target=work, daemon=True).start()
    return job_id


def _poll_and_import(job_id, flavor, task_id, key, name, poll_timeout, note,
                     save_to=None):
    """Shared poll -> download -> import (or save to disk) loop.

    `save_to` writes the result somewhere the user chose instead of importing
    it -- conversion output is usually destined for another application.
    """
    deadline = time.time() + poll_timeout
    poll_failures = 0
    try:
        while True:
            if time.time() > deadline:
                note(state="error", message=f"timed out after {poll_timeout}s")
                return
            try:
                resp = _request(_poll_url(flavor, task_id), key)
                poll_failures = 0
            except Exception as e:
                # A poll is a free, idempotent read; one dropped packet must
                # not abandon a billed, still-running task.
                poll_failures += 1
                if poll_failures >= 3:
                    raise
                print(f"[tripo] poll retry {poll_failures}/3: {e!r}")
                time.sleep(3)
                continue
            data = resp.get("data") or {}
            status = (data.get("status") or "").lower()
            out = data.get("output") or {}
            note(progress=data.get("progress"), api_status=status,
                 credits=data.get("consumed_credit") or data.get("credits_consumed"),
                 preview_url=_preview_url(out))

            if status in ("success", "succeeded", "completed"):
                url = _model_url(data)
                if not url:
                    note(state="error", message=f"no model url in {data}")
                    return
                note(state="downloading")
                path = os.path.join(tempfile.gettempdir(),
                                    f"tripo_{task_id}{_ext_from_url(url)}")
                with urllib.request.urlopen(url, timeout=300) as r, open(path, "wb") as f:
                    f.write(r.read())
                thumb = _cache_thumb(task_id, _preview_url(out))

                if save_to:
                    target = save_to
                    if os.path.isdir(target):
                        base = name or task_id
                        target = os.path.join(target, base + _ext_from_url(url))
                    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                    with open(path, "rb") as src, open(target, "wb") as dst:
                        dst.write(src.read())
                    note(state="done", path=target, thumb=thumb, objects=[])
                    with _jobs_lock:
                        snapshot = dict(_jobs.get(job_id, {}))
                    _record_job(snapshot)
                    return

                note(state="importing", path=path, thumb=thumb)
                _import_queue.put((job_id, path, name))
                return

            if status in ("failed", "cancelled", "canceled", "banned", "expired",
                          "unknown"):
                note(state="error", message=f"task {status}: {data}")
                return
            time.sleep(3)
    except Exception as e:
        note(state="error", message=repr(e))


# --------------------------------------------------------------------------
# Bring-your-own-model: STS upload + import_model
#
# This is the highest-leverage endpoint in the API -- once a mesh is imported
# as a task, every post-processing operation (retopology, texture, segment,
# rig) works on it, not just on things Tripo generated.
# --------------------------------------------------------------------------

MODEL_UPLOAD_FORMATS = {"glb", "obj", "fbx", "stl"}
MAX_UPLOAD_BYTES = 150 * 1024 * 1024


def _sts_token(key, fmt):
    """Temporary S3 credentials for a single upload."""
    resp = _request(f"{V2_BASE}/upload/sts/token", key, {"format": fmt})
    if resp.get("code") not in (0, None):
        raise RuntimeError(f"STS token refused: {resp}")
    return resp.get("data") or {}


def _s3_region(host):
    """Region from an S3 host name. Raises rather than guessing: a wrong
    region produces an opaque signature 403 far from the actual cause."""
    m = re.search(r"s3[.-]([a-z0-9-]+)\.amazonaws\.com", host or "")
    if m:
        return m.group(1)
    if host == "s3.amazonaws.com":
        return "us-east-1"
    raise RuntimeError(f"Cannot determine S3 region from host '{host}'")


def _sigv4_put(host, bucket, s3_key, data, ak, sk, token, region=None):
    if region is None:
        region = _s3_region(host)
    """PUT an object to S3 with SigV4, signed by hand.

    Tripo's docs assume boto3, which Blender doesn't bundle. Rather than
    require a pip install inside Blender, sign the request directly -- it's a
    single PUT and the algorithm is stable.
    """
    import datetime
    import hashlib
    import hmac

    service = "s3"
    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(data).hexdigest()

    canonical_uri = "/" + bucket + "/" + urllib.parse.quote(s3_key, safe="/~")
    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
        f"x-amz-security-token:{token}\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date;x-amz-security-token"
    canonical_request = (
        f"PUT\n{canonical_uri}\n\n{canonical_headers}\n"
        f"{signed_headers}\n{payload_hash}"
    )

    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = (
        "AWS4-HMAC-SHA256\n"
        f"{amz_date}\n{scope}\n"
        f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
    )

    def sign(k, msg):
        return hmac.new(k, msg.encode(), hashlib.sha256).digest()

    k_date = sign(f"AWS4{sk}".encode(), datestamp)
    k_region = sign(k_date, region)
    k_service = sign(k_region, service)
    k_signing = sign(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode(),
                         hashlib.sha256).hexdigest()

    auth = (f"AWS4-HMAC-SHA256 Credential={ak}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}")

    req = urllib.request.Request(
        f"https://{host}{canonical_uri}", data=data, method="PUT")
    req.add_header("Host", host)
    req.add_header("x-amz-content-sha256", payload_hash)
    req.add_header("x-amz-date", amz_date)
    req.add_header("x-amz-security-token", token)
    req.add_header("Authorization", auth)
    with urllib.request.urlopen(req, timeout=600) as resp:
        return resp.status


def upload_model_file(path, key=None):
    """Upload a local mesh to Tripo's bucket. Returns {bucket, key}."""
    key = key or _read_key()
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    if ext not in MODEL_UPLOAD_FORMATS:
        raise ValueError(
            f"Unsupported model format '{ext}'. "
            f"Allowed: {', '.join(sorted(MODEL_UPLOAD_FORMATS))}")
    size = os.path.getsize(path)
    if size > MAX_UPLOAD_BYTES:
        raise ValueError(f"File is {size // 1024 // 1024}MB; limit is 150MB")
    # A hidden object exports to a ~130 byte glb with no geometry; Tripo
    # accepts the upload and then fails the task, which reads as a mystery.
    if size < 1024:
        raise ValueError(
            f"{os.path.basename(path)} is only {size} bytes -- it probably has "
            "no geometry (a hidden object exports empty)")

    info = _sts_token(key, ext)
    with open(path, "rb") as f:
        data = f.read()
    _sigv4_put(info["s3_host"], info["resource_bucket"], info["resource_uri"],
               data, info["sts_ak"], info["sts_sk"], info["session_token"])
    return {"bucket": info["resource_bucket"], "key": info["resource_uri"]}


def start_import(path, name=None, add_to_scene=False, poll_timeout=900):
    """Import a local mesh into Tripo so it can be post-processed.

    Free per Tripo's pricing. `add_to_scene` is off by default -- the point is
    usually to get a task id for chaining, not to re-import a mesh Blender
    already has.
    """
    key = _read_key()
    _ensure_timer()
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {"job": job_id, "state": "uploading", "kind": "import",
                         "prompt": os.path.basename(path),
                         "name": name or os.path.splitext(os.path.basename(path))[0],
                         "import_only": not add_to_scene}

    def work():
        def note(**kw):
            with _jobs_lock:
                _jobs[job_id].update(kw)
        try:
            obj = upload_model_file(path, key)
            note(state="submitting")
            resp = _request(f"{V2_BASE}/task", key,
                            {"type": "import_model", "file": {"object": obj}})
            task_id = (resp.get("data") or {}).get("task_id")
            if not task_id:
                note(state="error", message=f"no task_id: {resp}")
                return
            note(task_id=task_id, flavor="v2", state="running")
            _record({"task_id": task_id, "job": job_id, "kind": "import",
                     "name": name, "flavor": "v2", "time": int(time.time())})
        except Exception as e:
            note(state="error", message=str(e))
            return

        if add_to_scene:
            _poll_and_import(job_id, "v2", task_id, key, name, poll_timeout, note)
            return

        # Wait for success but skip the download -- we only need the task id.
        deadline = time.time() + poll_timeout
        try:
            while time.time() < deadline:
                data = (_request(_poll_url("v2", task_id), key).get("data") or {})
                status = (data.get("status") or "").lower()
                note(progress=data.get("progress"), api_status=status)
                if status in ("success", "succeeded", "completed"):
                    note(state="done", objects=[])
                    with _jobs_lock:
                        snapshot = dict(_jobs.get(job_id, {}))
                    _record_job(snapshot)
                    return
                if status in ("failed", "cancelled", "canceled", "banned",
                              "expired", "unknown"):
                    note(state="error", message=f"task {status}: {data}")
                    return
                time.sleep(2)
            note(state="error", message=f"timed out after {poll_timeout}s")
        except Exception as e:
            note(state="error", message=repr(e))

    threading.Thread(target=work, daemon=True).start()
    return job_id


# --------------------------------------------------------------------------
# Image tasks
#
# These produce images, not meshes, so they must not go through the model
# download/import path. Their outputs feed image-to-3D and multiview-to-3D,
# which is the concept-art -> 3D workflow.
# --------------------------------------------------------------------------









# --------------------------------------------------------------------------
# Rigging and animation
#
# Order matters: mesh editing (segmentation, completion, retopology, quad
# conversion) STRIPS skeleton data. Do all remeshing first, then
# prerigcheck -> rig -> retarget.
# --------------------------------------------------------------------------

RIG_TYPES = ("biped", "quadruped", "hexapod", "octopod", "avian",
             "serpentine", "aquatic")

# Tasks that destroy rig data if run on a rigged model.
RIG_DESTROYING = {"mesh_segmentation", "mesh_completion", "highpoly_to_lowpoly",
                  "convert_model"}

# Animations available on the current rig versions (v2.0+/v2.5).
PRESET_ANIMATIONS = (
    "preset:idle", "preset:walk", "preset:run", "preset:dive", "preset:climb",
    "preset:jump", "preset:slash", "preset:shoot", "preset:hurt", "preset:fall",
    "preset:turn", "preset:quadruped:walk", "preset:hexapod:walk",
    "preset:octopod:walk", "preset:serpentine:march", "preset:aquatic:march",
)

RIG_MODEL_DEFAULT = "v1.0-20240301"      # biped only, per the docs
RIG_MODEL_CURRENT = "v2.5-20260210"      # required for non-biped rigs


def start_prerig_check(source_task_id, name=None, poll_timeout=600):
    """Ask whether a model can be rigged. Free.

    Returns riggable plus a suggested rig_type. Worth running before spending
    on a rig -- though the docs note a negative result isn't absolute.
    """
    key = _read_key()
    _ensure_timer()
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {"job": job_id, "state": "submitting", "kind": "animate_prerigcheck",
                         "prompt": "prerig check", "name": name,
                         "source": source_task_id}

    def work():
        def note(**kw):
            with _jobs_lock:
                _jobs[job_id].update(kw)
        try:
            resp = _request(f"{V2_BASE}/task", key,
                            {"type": "animate_prerigcheck",
                             "original_model_task_id": source_task_id})
            task_id = (resp.get("data") or {}).get("task_id")
            if not task_id:
                note(state="error", message=f"no task_id: {resp}")
                return
            note(task_id=task_id, flavor="v2", state="running")
        except Exception as e:
            note(state="error", message=str(e))
            return

        deadline = time.time() + poll_timeout
        try:
            while time.time() < deadline:
                data = (_request(_poll_url("v2", task_id), key).get("data") or {})
                status = (data.get("status") or "").lower()
                note(progress=data.get("progress"), api_status=status)
                if status in ("success", "succeeded", "completed"):
                    out = data.get("output") or {}
                    note(state="done", objects=[],
                         riggable=bool(out.get("riggable")),
                         rig_type=out.get("rig_type") or "biped")
                    return
                if status in ("failed", "cancelled", "canceled", "banned",
                              "expired", "unknown"):
                    note(state="error", message=f"task {status}: {data}")
                    return
                time.sleep(2)
            note(state="error", message=f"timed out after {poll_timeout}s")
        except Exception as e:
            note(state="error", message=repr(e))

    threading.Thread(target=work, daemon=True).start()
    return job_id


def start_rig(source_task_id, rig_type="biped", out_format="glb",
              model_version=None, name=None, poll_timeout=1800):
    """Rig a model. 25 credits.

    The default rig model is biped-only; anything else needs the current
    version, so pick it automatically rather than letting the call fail.
    """
    if rig_type not in RIG_TYPES:
        raise ValueError(f"Unknown rig_type '{rig_type}'")
    if model_version is None:
        model_version = (RIG_MODEL_DEFAULT if rig_type == "biped"
                         else RIG_MODEL_CURRENT)
    return start_post("animate_rig", source_task_id, name=name,
                      poll_timeout=poll_timeout,
                      rig_type=rig_type, out_format=out_format,
                      model_version=model_version)


def start_retarget(rig_task_id, animations, out_format="glb",
                   bake_animation=True, export_with_geometry=True,
                   animate_in_place=False, name=None, poll_timeout=1800):
    """Apply preset animations to a rigged model. 10 credits per animation."""
    anims = [a for a in (animations or []) if a in PRESET_ANIMATIONS]
    if not anims:
        raise ValueError("Pick at least one preset animation")
    if len(anims) > 5:
        raise ValueError("At most 5 animations per retarget task")

    extra = {"out_format": out_format,
             "export_with_geometry": export_with_geometry}
    # bake_animation only applies to glb output.
    if out_format == "glb":
        extra["bake_animation"] = bake_animation
    if animate_in_place:
        extra["animate_in_place"] = True
    if len(anims) == 1:
        extra["animation"] = anims[0]
    else:
        extra["animations"] = anims

    return start_post("animate_retarget", rig_task_id, name=name,
                      poll_timeout=poll_timeout, **extra)
