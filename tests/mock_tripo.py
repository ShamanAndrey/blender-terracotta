"""Offline stand-in for the Tripo API.

Swaps the two places the addon touches the network -- `api._request` for JSON
calls and `urllib.request.urlopen` for binary downloads -- so the whole
generate -> poll -> download -> import cycle runs with no credits and no
connection.

Every request body is recorded in `calls`, which is what lets tests assert on
the exact payload sent rather than just "it didn't crash".
"""

import io
import json
import os
import tempfile
import types
import urllib.error
import urllib.parse

from tripo_blender import api

# Recorded requests: list of (url, payload). Payload is None for GETs.
calls = []

_task_counter = 0
_task_kinds = {}
riggable_result = True
_saved = {}
_glb_bytes = None
_fail_submit = False
_fail_poll = False


def sample_glb():
    """A tiny real .glb, exported from Blender once and cached.

    Using a genuine file rather than fake bytes means the import path under
    test is the real glTF importer, not a stub.
    """
    global _glb_bytes
    if _glb_bytes is not None:
        return _glb_bytes

    import bpy
    path = os.path.join(tempfile.gettempdir(), "tripo_test_asset.glb")
    if not os.path.exists(path):
        mesh = bpy.data.meshes.new("TestCube")
        verts = [(-.5, -.5, 0), (.5, -.5, 0), (.5, .5, 0), (-.5, .5, 0),
                 (-.5, -.5, 1), (.5, -.5, 1), (.5, .5, 1), (-.5, .5, 1)]
        faces = [(0, 1, 2, 3), (7, 6, 5, 4), (0, 4, 5, 1),
                 (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0)]
        mesh.from_pydata(verts, [], faces)
        mesh.update()
        obj = bpy.data.objects.new("TestCube", mesh)
        bpy.context.collection.objects.link(obj)
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.ops.export_scene.gltf(filepath=path, use_selection=True,
                                  export_format="GLB")
        bpy.data.objects.remove(obj, do_unlink=True)

    with open(path, "rb") as f:
        _glb_bytes = f.read()
    return _glb_bytes


def _fake_request(url, key, payload=None, timeout=60):
    calls.append((url, payload))

    if "/user/balance" in url:
        return {"code": 0, "data": {"balance": 1000, "frozen": 0}}

    if "/upload/sts/token" in url:
        return {"code": 0, "data": {
            "s3_host": "s3.us-west-2.amazonaws.com",
            "resource_bucket": "tripo-data",
            "resource_uri": "tcli_mock/20260728/abc/input.glb",
            "session_token": "MOCK_SESSION_TOKEN",
            "sts_ak": "ASIAMOCKACCESSKEY",
            "sts_sk": "mockSecretKeyValue1234567890",
        }}

    # Task creation: any POST with a body that isn't a poll.
    if payload is not None:
        if _fail_submit:
            raise RuntimeError("HTTP 403: You don't have enough credit to "
                               "create this task (Please purchase more credit)")
        global _task_counter
        _task_counter += 1
        tid = f"task-mock-{_task_counter:04d}"
        ttype = payload.get("type", "")
        if "animations/rig-check" in url:
            _task_kinds[tid] = "prerig"
        elif ttype in ("text_to_image", "generate_image"):
            _task_kinds[tid] = "image"
        elif ttype == "animate_prerigcheck":
            _task_kinds[tid] = "prerig"
        elif ttype in ("generate_multiview_image", "edit_multiview_image"):
            _task_kinds[tid] = "multiview"
        return {"code": 0, "data": {"task_id": tid}}

    # Poll: succeed immediately so tests don't sit through the 3s backoff.
    if _fail_poll:
        raise RuntimeError("mock poll failure (network down)")
    task_id = url.rstrip("/").rsplit("/", 1)[-1]
    kind = _task_kinds.get(task_id, "model")
    if kind == "image":
        return {"code": 0, "data": {
            "task_id": task_id, "status": "success", "progress": 100,
            "consumed_credit": 5,
            "output": {"generated_image_url": "https://mock.invalid/preview.png"},
        }}
    if kind == "prerig":
        return {"code": 0, "data": {
            "task_id": task_id, "status": "success", "progress": 100,
            "consumed_credit": 0,
            "output": {"riggable": riggable_result, "rig_type": "biped"},
        }}
    if kind == "multiview":
        return {"code": 0, "data": {
            "task_id": task_id, "status": "success", "progress": 100,
            "consumed_credit": 10,
            "output": {v + "_view_url": f"https://mock.invalid/preview_{v}.png"
                       for v in ("front", "left", "back", "right")},
        }}
    return {
        "code": 0,
        "data": {
            "task_id": task_id,
            "status": "success",
            "progress": 100,
            "consumed_credit": 20,
            "input": {"model": "v3.1-20260211"},
            # Real v3 field names -- v2 uses pbr_model / rendered_image.
            # Using the v3 spelling here is deliberate: the mock previously
            # used v2 names and hid a bug where v3 previews never cached.
            "output": {
                "model_url": "https://mock.invalid/model.glb",
                "rendered_image_url": "https://mock.invalid/preview.png",
            },
        },
    }


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


s3_puts = []


def _fake_sigv4_put(host, bucket, s3_key, data, ak, sk, token, region="us-west-2"):
    s3_puts.append({"host": host, "bucket": bucket, "key": s3_key,
                    "bytes": len(data), "ak": ak, "token": token})
    return 200


def sample_model_file():
    """A real .glb on disk, for upload tests."""
    path = os.path.join(tempfile.gettempdir(), "tripo_test_upload.glb")
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(sample_glb())
    return path


def _fake_upload(path, key):
    """Stand in for api._upload, which uses `requests` -- not urllib -- and so
    would otherwise reach the real API even with the rest mocked."""
    if not os.path.exists(path):
        raise RuntimeError(f"Image not found: {path}")
    ext = os.path.splitext(path)[1].lstrip(".").lower() or "png"
    return f"file_mock_{os.path.basename(path)}", ext


def sample_image():
    """A real 1x1 PNG on disk, for image and multiview tests."""
    path = os.path.join(tempfile.gettempdir(), "tripo_test_ref.png")
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(bytes.fromhex(
                "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
                "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
                "00000049454e44ae426082"))
    return path


def _fake_urlopen(url, timeout=None):
    target = url if isinstance(url, str) else getattr(url, "full_url", "")
    if "preview" in target:
        # 1x1 PNG -- enough for the preview cache to write a real file.
        return _FakeResponse(bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
            "00000049454e44ae426082"))
    return _FakeResponse(sample_glb())


def install():
    """Patch the network layer. Call uninstall() to restore."""
    if _saved:
        return

    # Build the sample asset NOW, on the main thread. _fake_urlopen is called
    # from api's worker thread, and invoking a bpy operator (the glTF export)
    # off the main thread crashes Blender outright.
    sample_glb()

    _saved["_request"] = api._request
    _saved["_read_key"] = api._read_key
    # Patch key lookup instead of writing a fake key into the user's real
    # addon preferences -- a saved fake key breaks later real use.
    api._read_key = lambda: "tsk_mock_key_for_tests"
    _saved["urllib"] = api.urllib
    _saved["_upload"] = api._upload
    _saved["_sigv4_put"] = api._sigv4_put
    _saved["_history_path"] = api._history_path
    _saved["thumb_dir"] = api.thumb_dir
    _saved["images_dir"] = api.images_dir

    api._request = _fake_request
    api._upload = _fake_upload
    # Never actually PUT to S3 from tests; just record that we tried.
    api._sigv4_put = _fake_sigv4_put

    # Redirect history and thumbnails into a scratch dir. Tests must never
    # write into the user's real Blender config -- earlier runs did, and the
    # leftover rows made a later assertion fail for the wrong reason.
    scratch = tempfile.mkdtemp(prefix="tripo_test_")
    api._history_path = lambda: os.path.join(scratch, "history.json")
    api.thumb_dir = lambda: scratch
    img_scratch = os.path.join(scratch, "images")
    os.makedirs(img_scratch, exist_ok=True)
    api.images_dir = lambda: img_scratch
    _saved["scratch"] = scratch
    # api calls urllib.request.urlopen(...) and catches urllib.error.HTTPError,
    # so the stub needs both attributes.
    # The stub must expose every urllib submodule api touches: request for
    # downloads, error for HTTPError, parse for extension sniffing.
    api.urllib = types.SimpleNamespace(
        request=types.SimpleNamespace(urlopen=_fake_urlopen),
        error=urllib.error,
        parse=urllib.parse,
    )
    calls.clear()


def uninstall():
    if not _saved:
        return
    # Wait for every in-flight job before unpatching: a worker thread still
    # recording through the sandboxed paths must not have them swapped back
    # to the user's real files mid-write.
    import time
    deadline = time.time() + 10
    while time.time() < deadline and api.active_jobs():
        time.sleep(0.05)
    if api.active_jobs():
        print("mock_tripo: WARNING -- jobs still active at uninstall:",
              api.active_jobs())
    api._request = _saved["_request"]
    api._read_key = _saved["_read_key"]
    api.urllib = _saved["urllib"]
    api._upload = _saved["_upload"]
    api._sigv4_put = _saved["_sigv4_put"]
    api._history_path = _saved["_history_path"]
    api.thumb_dir = _saved["thumb_dir"]
    api.images_dir = _saved["images_dir"]
    _saved.clear()
    # Reset toggles so a failed test can't poison later sections.
    global _fail_submit, _fail_poll, riggable_result
    _fail_submit = False
    _fail_poll = False
    riggable_result = True


def set_poll_failure(enabled):
    """Make every poll raise, simulating the network dying mid-generation."""
    global _fail_poll
    _fail_poll = enabled


def set_submit_failure(enabled):
    """Make the next submit fail, for testing the error path."""
    global _fail_submit
    _fail_submit = enabled


def last_body():
    """Payload of the most recent request that had one."""
    for url, payload in reversed(calls):
        if payload is not None:
            return payload
    return None


def wait_for_submit(previous_count, timeout=5.0):
    """Block until a new request body is recorded.

    Submission happens on api's worker thread, so asserting on last_body()
    immediately after start() is a race -- and a race that passes by luck is
    worse than no test.
    """
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len([c for c in calls if c[1] is not None]) > previous_count:
            return True
        time.sleep(0.02)
    return False


def submit_count():
    return len([c for c in calls if c[1] is not None])


def set_riggable(value):
    """Control what prerigcheck reports."""
    global riggable_result
    riggable_result = value


# --------------------------------------------------------------------------
# Google Gemini image API
# --------------------------------------------------------------------------

google_calls = []
_google_saved = {}


def _fake_google_request(body, key, timeout=300):
    google_calls.append(body)
    # Mirror the documented behaviour: the response can contain earlier image
    # blocks (echoed reference inputs, interim thinking images); the OUTPUT is
    # the LAST block. A mock that returns only one image cannot catch code
    # that wrongly takes the first.
    import base64
    echo = base64.b64encode(b"ECHOED-REFERENCE-INPUT" * 24).decode("ascii")
    return {"outputs": [{"content": [
        {"type": "image", "mime_type": "image/png", "data": echo},
        {"type": "image", "mime_type": "image/png", "data": _b64_png()},
    ]}]}


def _b64_png():
    import base64
    return base64.b64encode(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
        "00000049454e44ae426082") * 8).decode("ascii")


def install_google():
    from tripo_blender import google_api
    if _google_saved:
        return
    _google_saved["_request"] = google_api._request
    _google_saved["read_key"] = google_api.read_key
    google_api._request = _fake_google_request
    google_api.read_key = lambda: "MOCK_GEMINI_KEY"
    google_calls.clear()


def uninstall_google():
    from tripo_blender import google_api
    if not _google_saved:
        return
    google_api._request = _google_saved["_request"]
    google_api.read_key = _google_saved["read_key"]
    _google_saved.clear()
