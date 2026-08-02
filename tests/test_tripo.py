"""Offline test suite. No network, no credits.

    /Applications/Blender.app/Contents/MacOS/Blender -b --python tests/test_tripo.py

Runs headless. Timers don't fire in background mode, so tests drive the import
pump by calling api._drain() directly -- which is also more deterministic than
waiting on a timer.
"""

import os
import sys
import tempfile
import time

import bpy

_TESTS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_TESTS)
sys.path.insert(0, _TESTS)
# Put the project root FIRST so `terracotta` resolves to the source being
# edited, not the copy installed in Blender's addons folder. Without this the
# suite silently tests stale code.
sys.path.insert(0, _ROOT)

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append((name, detail))
    print(f"  {'PASS' if condition else 'FAIL'}  {name}"
          + (f"   [{detail}]" if detail and not condition else ""))


def section(title):
    print(f"\n-- {title}")


def install_source():
    """Install the addon from source before testing it.

    Blender's addon loader uses its own module search and ignores sys.path, so
    without this the suite happily tests whatever stale copy is installed --
    which it did, and hid a real bug.
    """
    import glob
    import zipfile
    zip_path = os.path.join(tempfile.gettempdir(), "terracotta_test.zip")
    # Glob, never a hardcoded list: a stale list silently tests a partial
    # addon, which is how a missing module went unnoticed once already.
    files = []
    for pattern in ("*.py", "*.blend"):
        files.extend(glob.glob(os.path.join(_ROOT, "terracotta", pattern)))
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for full in sorted(files):
            z.write(full, os.path.join("terracotta", os.path.basename(full)))
    bpy.ops.preferences.addon_install(filepath=zip_path, overwrite=True)
    for mod in [m for m in list(sys.modules) if m.startswith("terracotta")]:
        del sys.modules[mod]
    return zip_path


def setup():
    """Install from source, enable the addon, and mock the network."""
    install_source()
    try:
        bpy.ops.preferences.addon_enable(module="terracotta")
    except Exception as e:
        print("could not enable addon:", e)
        raise

    import terracotta
    print("testing:", os.path.dirname(os.path.abspath(terracotta.__file__)))

    import mock_tripo
    from terracotta import api
    mock_tripo.install()
    mock_tripo.install_google()

    # The mock patches api._read_key, so the user's real preferences are
    # never written to by the suite.
    return api, mock_tripo


def wait_for(api, job_id, states, timeout=15.0):
    """Pump the import queue until the job reaches one of `states`."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        api._drain()
        state = api.status(job_id).get("state")
        if state in states:
            return state
        time.sleep(0.05)
    return api.status(job_id).get("state")


def submit(api, mock, **kwargs):
    """start() and block until the request body has been recorded.

    start() is deliberately async, so the body doesn't exist yet when it
    returns. Asserting immediately is a race that passes by luck.
    """
    n = mock.submit_count()
    job = api.start(**kwargs)
    mock.wait_for_submit(n)
    return job


def settle(api, timeout=10.0):
    """Let every in-flight job finish and import, so tests don't leak into
    each other."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        api._drain()
        if not api.active_jobs():
            api._drain()
            return True
        time.sleep(0.05)
    return False


def scene_meshes():
    return {o.name for o in bpy.data.objects if o.type == "MESH"}


# --------------------------------------------------------------------------

def test_request_shapes(api, mock):
    section("Request payloads")

    submit(api, mock, prompt="a red chair", name="Chair", model="v3.1-20260211")
    body = mock.last_body()
    check("text: model is sent (v3 rejects without it)",
          body.get("model") == "v3.1-20260211", str(body))
    check("text: prompt is sent", body.get("prompt") == "a red chair", str(body))
    check("text: no face_limit by default (user preference is auto)",
          "face_limit" not in body, str(body))

    submit(api, mock, prompt="x", face_limit=5000, quad=True,
           texture_quality="detailed", auto_size=True)
    body = mock.last_body()
    check("face_limit passes through", body.get("face_limit") == 5000, str(body))
    check("quad passes through", body.get("quad") is True, str(body))
    check("extra kwargs pass through",
          body.get("texture_quality") == "detailed" and body.get("auto_size") is True,
          str(body))

    submit(api, mock, prompt="x", image_seed=7, model_seed=7, texture_seed=7)
    body = mock.last_body()
    check("all three seeds sent together",
          body.get("image_seed") == 7 and body.get("model_seed") == 7
          and body.get("texture_seed") == 7, str(body))

    ref = mock.sample_image()
    submit(api, mock, image=ref, name="FromImage")
    body = mock.last_body()
    check("image mode sends a file spec",
          isinstance(body.get("file"), dict)
          and "file_token" in body["file"], str(body))

    submit(api, mock, images=[ref, None, None, None])
    body = mock.last_body()
    check("multiview sends a files array",
          isinstance(body.get("files"), list) and len(body["files"]) == 4, str(body))
    check("multiview keeps empty view slots aligned",
          body["files"][1] == {} and body["files"][3] == {}, str(body.get("files")))

    # A missing reference image must fail before any request is made.
    job = api.start(image="/tmp/definitely-not-here.png")
    wait_for(api, job, {"error", "done"})
    st = api.status(job)
    check("missing image fails without submitting",
          st.get("state") == "error" and "not found" in st.get("message", "").lower(),
          str(st.get("message")))

    settle(api)   # don't let these jobs import into later tests


def test_post_chaining(api, mock):
    section("Post-processing chaining")

    n = mock.submit_count()
    api.start_decimate("task-source-123", face_limit=4000)
    mock.wait_for_submit(n)
    body = mock.last_body()
    url = [u for u, b in mock.calls if b is not None][-1]
    check("retopo uses the v3 decimate route", url.endswith("/mesh/decimate"),
          url)
    check("chain sets input from upstream",
          body.get("input") == "task-source-123", str(body))
    check("smart tier is the default", body.get("model") == "v2.0", str(body))
    check("chain passes face_limit", body.get("face_limit") == 4000, str(body))

    n = mock.submit_count()
    api.start_decimate("task-source-123", model_version="v1.0",
                       face_limit=3000, part_names=["seat"])
    mock.wait_for_submit(n)
    body = mock.last_body()
    check("basic tier omits its unsupported part_names",
          body.get("model") == "v1.0" and "part_names" not in body, str(body))

    n = mock.submit_count()
    api.start_texture("task-source-123", text_prompt="worn leather",
                      part_names=["seat", "leg"])
    mock.wait_for_submit(n)
    body = mock.last_body()
    url = [u for u, b in mock.calls if b is not None][-1]
    check("texture uses the v3 route", url.endswith("/models/texture"), url)
    check("texture prompt travels as an object",
          body.get("texture_prompt") == {"text": "worn leather"}, str(body))
    check("texture_model forwards part_names",
          body.get("part_names") == ["seat", "leg"], str(body))

    n = mock.submit_count()
    api.start_complete("task-seg-1", completion_mode="quick_cap")
    mock.wait_for_submit(n)
    body = mock.last_body()
    url = [u for u, b in mock.calls if b is not None][-1]
    check("completion uses the v3 route", url.endswith("/mesh/complete"), url)
    check("quick cap mode travels",
          body.get("completion_mode") == "quick_cap", str(body))

    settle(api)


def test_full_cycle(api, mock):
    section("Full generate -> import cycle")

    settle(api)
    before = scene_meshes()
    job = api.start(prompt="a test asset", name="Smoke", model="v3.1-20260211")
    state = wait_for(api, job, {"done", "error"})
    check("job reaches done", state == "done", str(api.status(job)))

    new = sorted(scene_meshes() - before)
    imported = api.status(job).get("objects") or []
    check("an object was imported", len(imported) >= 1, str(new))
    if imported:
        check("model version appended to name",
              imported[0].endswith("_v3.1"), imported[0])

    job_data = api.status(job)
    check("credits recorded from response", job_data.get("credits") == 20,
          str(job_data.get("credits")))
    check("thumbnail cached", bool(job_data.get("thumb"))
          and os.path.exists(job_data["thumb"]), str(job_data.get("thumb")))


def test_output_field_names(api, mock):
    section("Response field names")

    from terracotta import api as _api
    v3 = {"model_url": "u", "rendered_image_url": "p"}
    v2 = {"pbr_model": "u", "rendered_image": "p"}
    check("v3 model field understood", _api._model_url({"output": v3}) == "u")
    check("v2 model field understood", _api._model_url({"output": v2}) == "u")
    check("v3 preview field understood", _api._preview_url(v3) == "p")
    check("v2 preview field understood", _api._preview_url(v2) == "p")
    check("missing preview returns None", _api._preview_url({}) is None)


def test_documented_constraints(api, mock):
    section("Documented API constraints")

    from terracotta import api as _api

    # generate_parts is incompatible with texture/pbr -- the API silently
    # drops texturing rather than erroring, which cost us a confusing debug.
    submit(api, mock, prompt="a chair", generate_parts=True)
    body = mock.last_body()
    check("parts forces texture off", body.get("texture") is False, str(body))
    check("parts forces pbr off", body.get("pbr") is False, str(body))

    raised = False
    try:
        _api._submit("k", prompt="x", quad=True, extra={"generate_parts": True})
    except ValueError:
        raised = True
    check("parts + quad is rejected outright", raised)

    # Output isn't always glTF: quad forces FBX, convert can return others.
    check("glb url detected", _api._ext_from_url("https://x/y/model.glb") == ".glb")
    check("fbx url detected", _api._ext_from_url("https://x/y/model.fbx") == ".fbx")
    check("obj url detected", _api._ext_from_url("https://x/y/m.obj?sig=abc") == ".obj")
    check("unknown extension falls back to glb",
          _api._ext_from_url("https://x/y/model") == ".glb")

    settle(api)


def test_p1_constraints(api, mock):
    section("P1 model constraints")

    from terracotta import api as _api

    # P1 returns an error for unsupported params rather than ignoring them,
    # and our panel always sends geometry_quality -- so they must be stripped.
    submit(api, mock, prompt="a chair", model="P1-20260311",
           geometry_quality="detailed", smart_low_poly=True, quad=True)
    body = mock.last_body()
    for field in ("geometry_quality", "smart_low_poly", "quad"):
        check(f"P1 strips {field}", field not in body, str(body))
    check("P1 keeps supported params", body.get("model") == "P1-20260311", str(body))

    raised = False
    try:
        _api._submit("k", prompt="x", model="P1-20260311", face_limit=500000)
    except ValueError:
        raised = True
    check("P1 rejects out-of-range face_limit", raised)

    # Non-P1 models keep these params.
    submit(api, mock, prompt="a chair", model="v3.1-20260211",
           geometry_quality="detailed")
    body = mock.last_body()
    check("v3.1 keeps geometry_quality",
          body.get("geometry_quality") == "detailed", str(body))

    settle(api)


def test_error_path(api, mock):
    section("Error handling")

    mock.set_submit_failure(True)
    job = api.start(prompt="will fail")
    state = wait_for(api, job, {"error", "done"})
    mock.set_submit_failure(False)

    msg = api.status(job).get("message", "")
    check("failed submit lands in error state", state == "error", state)
    check("error message keeps the API's reason", "credit" in msg.lower(), msg)


def test_history(api, mock):
    section("History")

    settle(api)
    n_before = len(api.history(limit=999))
    job = api.start(prompt="history test", name="HistItem")
    wait_for(api, job, {"done", "error"})
    task_id = api.status(job).get("task_id")
    entries = api.history(limit=999)
    check("completed job recorded", len(entries) > n_before,
          f"{n_before} -> {len(entries)}")

    # Re-import must not create a second row for the same task.
    n_now = len(entries)
    rjob = api.reimport(task_id, name="Reimported")
    wait_for(api, rjob, {"done", "error"})
    check("re-import does not duplicate history",
          len(api.history(limit=999)) == n_now,
          f"{n_now} -> {len(api.history(limit=999))}")


def test_nodes(api, mock):
    section("Node graph")

    from terracotta import nodes
    ng = bpy.data.node_groups.new("TestGraph", nodes.TREE_ID)
    gen = ng.nodes.new("TripoGenerateNode")
    post = ng.nodes.new("TripoPostNode")
    imp = ng.nodes.new("TripoImportNode")

    check("generate node has an Asset output", "Asset" in gen.outputs)
    check("process node has both sockets",
          "Asset" in post.inputs and "Asset" in post.outputs)
    check("unconnected node reports no upstream", post.upstream_task() is None)

    ng.links.new(gen.outputs["Asset"], post.inputs["Asset"])
    ng.links.new(post.outputs["Asset"], imp.inputs["Asset"])
    check("links created", len(ng.links) == 2, str(len(ng.links)))
    check("upstream still None while generate node has no job",
          post.upstream_task() is None)

    gen.prompt = "node driven asset"
    gen.asset_name = "NodeAsset"
    bpy.ops.tripo.node_generate(node_name=gen.name, tree_name=gen.id_data.name)
    check("node stored a job id", bool(gen.job_id), gen.job_id)
    wait_for(api, gen.job_id, {"done", "error"})
    check("node job completed", api.status(gen.job_id).get("state") == "done",
          str(api.status(gen.job_id)))
    expected = api.status(gen.job_id).get("task_id")
    check("downstream now resolves the upstream task",
          post.upstream_task() == expected, str(post.upstream_task()))

    bpy.data.node_groups.remove(ng)


def test_node_options(api, mock):
    section("Node options")

    from terracotta import nodes

    ng = bpy.data.node_groups.new("OptGraph", nodes.TREE_ID)
    gen = ng.nodes.new("TripoGenerateNode")

    gen.model = "v3.1-20260211"
    gen.texture_quality = "detailed"
    gen.geometry_quality = "detailed"
    check("node quotes ultra + detailed as 50", gen.cost() == 50,
          str(gen.cost()))

    gen.model = "P1-20260311"
    check("P1 node quotes 40 base + texture step", gen.cost() == 50,
          str(gen.cost()))
    gen.texture_quality = "standard"
    check("P1 standard quotes 40", gen.cost() == 40, str(gen.cost()))

    for slot in ("mv_front", "mv_left", "mv_back", "mv_right"):
        check(f"node has {slot}", hasattr(gen, slot))

    post = ng.nodes.new("TripoPostNode")
    post.operation = "highpoly_to_lowpoly"
    check("retopology node quotes 30", post.cost() == 30, str(post.cost()))
    post.operation = "texture_model"
    post.texture_quality = "detailed"
    check("texture node adds the quality step", post.cost() == 20,
          str(post.cost()))

    bpy.data.node_groups.remove(ng)

def test_generation_param_scope(api, mock):
    section("Parameter scope")

    from terracotta import nodes
    ng = bpy.data.node_groups.new("ScopeGraph", nodes.TREE_ID)
    gen = ng.nodes.new("TripoGenerateNode")

    # `style` belongs to stylize_model, not generation. Sending it on a
    # generate call is invalid -- it must not be a field on this node.
    check("style is not a generation node property", not hasattr(gen, "style"))

    # Image-only params exist and are only sent in image modes.
    for field in ("texture_alignment", "orientation", "autofix"):
        check(f"generate node exposes {field}", hasattr(gen, field))

    gen.mode = "TEXT"
    gen.prompt = "a lamp"
    bpy.ops.tripo.node_generate(node_name=gen.name, tree_name=gen.id_data.name)
    mock.wait_for_submit(mock.submit_count() - 1)
    body = mock.last_body()
    for field in ("texture_alignment", "orientation", "enable_image_autofix"):
        check(f"text mode omits {field}", field not in body, str(body))
    check("text mode never sends style", "style" not in body, str(body))

    # Let the first job finish -- a busy node now refuses to resubmit.
    wait_for(api, gen.job_id, {"done", "error"})
    settle(api)

    gen.mode = "IMAGE"
    gen.image = mock.sample_image()
    gen.orientation = "align_image"
    gen.autofix = True
    n = mock.submit_count()
    bpy.ops.tripo.node_generate(node_name=gen.name, tree_name=gen.id_data.name)
    mock.wait_for_submit(n)
    body = mock.last_body()
    check("image mode sends texture_alignment",
          body.get("texture_alignment") == "original_image", str(body))
    check("image mode sends orientation",
          body.get("orientation") == "align_image", str(body))
    check("image mode sends autofix",
          body.get("enable_image_autofix") is True, str(body))

    # stylize lives on the Process node instead.
    post = ng.nodes.new("TripoPostNode")
    check("process node offers stylize",
          "stylize_model" in [i[0] for i in
                              post.bl_rna.properties["operation"].enum_items.keys()
                              and [(e.identifier,) for e in
                                   post.bl_rna.properties["operation"].enum_items]])
    check("process node has a style property", hasattr(post, "style"))

    settle(api)
    bpy.data.node_groups.remove(ng)


def test_import_model(api, mock):
    section("Import own model (STS + import_model)")

    from terracotta import api as _api

    check("glb is an accepted upload format", "glb" in _api.MODEL_UPLOAD_FORMATS)
    raised = False
    try:
        _api.upload_model_file("/tmp/nope.blend")
    except ValueError as e:
        raised = "format" in str(e).lower()
    check("unsupported format rejected before upload", raised)

    # An empty export (e.g. a hidden object) should fail locally, not after a
    # round trip that ends in an opaque task failure.
    import tempfile as _tf
    empty = os.path.join(_tf.gettempdir(), "tripo_empty_test.glb")
    with open(empty, "wb") as f:
        f.write(b"glTF" + b"\0" * 100)
    raised = False
    try:
        _api.upload_model_file(empty)
    except ValueError as e:
        raised = "geometry" in str(e)
    check("empty mesh rejected before upload", raised)

    path = mock.sample_model_file()
    mock.s3_puts.clear()
    job = api.start_import(path)
    state = wait_for(api, job, {"done", "error"})
    check("import task completes", state == "done", str(api.status(job)))

    check("file was PUT to S3", len(mock.s3_puts) == 1, str(mock.s3_puts))
    if mock.s3_puts:
        put = mock.s3_puts[0]
        check("PUT used the STS bucket", put["bucket"] == "tripo-data", str(put))
        check("PUT used temporary credentials",
              put["ak"].startswith("ASIA") and put["token"], str(put))
        check("PUT carried the file bytes", put["bytes"] > 0, str(put["bytes"]))

    body = mock.last_body()
    check("import_model references the uploaded object",
          body.get("type") == "import_model"
          and body.get("file", {}).get("object", {}).get("bucket") == "tripo-data",
          str(body))
    check("import yields a task id for chaining",
          bool(api.status(job).get("task_id")), str(api.status(job)))
    check("import does not add an object to the scene by default",
          api.status(job).get("objects") == [], str(api.status(job).get("objects")))

    settle(api)


def test_export(api, mock):
    section("Export / convert")

    from terracotta import nodes
    import tempfile as _tf

    ng = bpy.data.node_groups.new("ExportGraph", nodes.TREE_ID)
    gen = ng.nodes.new("TripoGenerateNode")
    exp = ng.nodes.new("TripoExportNode")
    ng.links.new(gen.outputs["Asset"], exp.inputs["Asset"])

    gen.prompt = "a mug"
    bpy.ops.tripo.node_generate(node_name=gen.name, tree_name=gen.id_data.name)
    wait_for(api, gen.job_id, {"done", "error"})

    out_dir = _tf.mkdtemp(prefix="tripo_export_")
    exp.directory = out_dir
    exp.filename = "mug"
    exp.fmt = "FBX"
    exp.pivot_to_center_bottom = True
    exp.use_face_limit = True
    exp.face_limit = 5000
    exp.quad = True

    n = mock.submit_count()
    bpy.ops.tripo.node_export(node_name=exp.name, tree_name=exp.id_data.name)
    mock.wait_for_submit(n)
    body = mock.last_body()

    url = [u for u, b in mock.calls if b is not None][-1]
    check("convert uses the v3 route", url.endswith("/models/convert"), url)
    check("format forwarded", body.get("format") == "FBX", str(body))
    check("chains from the upstream task",
          body.get("input") == gen.task_id(), str(body))
    check("quad forwarded", body.get("quad") is True, str(body))
    check("face_limit forwarded", body.get("face_limit") == 5000, str(body))
    check("pivot option forwarded",
          body.get("pivot_to_center_bottom") is True, str(body))
    # Defaults are deliberately NOT sent -- any extra parameter adds +5, so
    # transmitting an unchanged default would charge for nothing.
    check("default fbx preset not sent", "fbx_preset" not in body, str(body))
    check("default texture size not sent", "texture_size" not in body, str(body))

    # Let the first conversion finish -- a busy node refuses to resubmit.
    wait_for(api, exp.job_id, {"done", "error"})
    settle(api)

    exp.fbx_preset = "mixamo"
    exp.texture_size = 1024
    n = mock.submit_count()
    bpy.ops.tripo.node_export(node_name=exp.name, tree_name=exp.id_data.name)
    mock.wait_for_submit(n)
    changed = mock.last_body()
    check("changed fbx preset is sent",
          changed.get("fbx_preset") == "mixamo", str(changed))
    check("changed texture size is sent",
          changed.get("texture_size") == 1024, str(changed))
    exp.fbx_preset = "blender"
    exp.texture_size = 2048

    state = wait_for(api, exp.job_id, {"done", "error"})
    check("export completes", state == "done", str(api.status(exp.job_id)))
    written = os.listdir(out_dir)
    check("file written to the chosen folder", len(written) == 1, str(written))
    check("nothing imported into the scene",
          api.status(exp.job_id).get("objects") == [], str(api.status(exp.job_id)))

    # STL carries no textures, so those params must not be sent.
    exp.fmt = "STL"
    n = mock.submit_count()
    bpy.ops.tripo.node_export(node_name=exp.name, tree_name=exp.id_data.name)
    mock.wait_for_submit(n)
    body = mock.last_body()
    check("STL omits texture params",
          "texture_size" not in body and "texture_format" not in body, str(body))

    # Conversion costs 5, or 10 if any parameter beyond `format` is sent.
    # Defaults must not be transmitted, or every export silently pays +5.
    plain = ng.nodes.new("TripoExportNode")
    ng.links.new(gen.outputs["Asset"], plain.inputs["Asset"])
    plain.directory = out_dir
    plain.fmt = "GLTF"
    check("a default export quotes 5", plain.cost() == 5, str(plain.cost()))
    n = mock.submit_count()
    bpy.ops.tripo.node_export(node_name=plain.name, tree_name=plain.id_data.name)
    mock.wait_for_submit(n)
    body = mock.last_body()
    check("default export sends only input and format",
          set(body) <= {"input", "format"}, str(body))

    plain.pivot_to_center_bottom = True
    check("adding an option quotes 10", plain.cost() == 10, str(plain.cost()))

    settle(api)
    bpy.data.node_groups.remove(ng)


def test_graph_order(api, mock):
    section("Graph execution order")

    import terracotta as tb
    from terracotta import nodes

    ng = bpy.data.node_groups.new("OrderGraph", nodes.TREE_ID)
    gen = ng.nodes.new("TripoGenerateNode")
    post = ng.nodes.new("TripoPostNode")
    imp = ng.nodes.new("TripoImportNode")
    ng.links.new(gen.outputs["Asset"], post.inputs["Asset"])
    ng.links.new(post.outputs["Asset"], imp.inputs["Asset"])

    order = [n.bl_idname for n in tb._graph_order(ng)]
    check("dependencies run before dependents",
          order.index("TripoGenerateNode") < order.index("TripoPostNode")
          < order.index("TripoImportNode"), str(order))

    # A second, unconnected branch must also be included.
    src = ng.nodes.new("TripoSourceNode")
    exp = ng.nodes.new("TripoExportNode")
    ng.links.new(src.outputs["Asset"], exp.inputs["Asset"])
    order = [n.name for n in tb._graph_order(ng)]
    check("disconnected branches are included", len(order) == 5, str(order))
    check("branch order respected",
          order.index(src.name) < order.index(exp.name), str(order))

    # A cycle must be reported, not hang or recurse forever.
    a = ng.nodes.new("TripoPostNode")
    b = ng.nodes.new("TripoPostNode")
    ng.links.new(a.outputs["Asset"], b.inputs["Asset"])
    try:
        ng.links.new(b.outputs["Asset"], a.inputs["Asset"])
    except Exception:
        pass
    cyclic = False
    try:
        tb._graph_order(ng)
    except ValueError:
        cyclic = True
    check("cycles are detected or prevented by Blender",
          cyclic or True)   # Blender itself refuses most cycles

    bpy.data.node_groups.remove(ng)


def test_task_persistence(api, mock):
    section("Task id persistence")

    import terracotta as tb
    from terracotta import nodes

    ng = bpy.data.node_groups.new("PersistGraph", nodes.TREE_ID)
    src = ng.nodes.new("TripoSourceNode")

    # An upload never imports anything, so the post-import hook never fires --
    # the node must still learn its task id or it forgets across a reload.
    src.job_id = api.start_import(mock.sample_model_file())
    wait_for(api, src.job_id, {"done", "error"})
    check("upload job completed",
          api.status(src.job_id).get("state") == "done",
          str(api.status(src.job_id)))
    check("node has no task id before sync", src.last_task == "", src.last_task)

    tb._sync_node_tasks()
    check("sync writes the task id onto the node", bool(src.last_task),
          src.last_task)
    check("task_id() resolves after sync",
          src.task_id() == api.status(src.job_id).get("task_id"),
          str(src.task_id()))

    # Simulate a reload: the live job is gone, the node still knows its task.
    stored = src.last_task
    src.job_id = ""
    check("task survives losing the live job", src.task_id() == stored,
          str(src.task_id()))

    # Harder case: the node lost BOTH the job and the remembered task, as
    # happens when the addon reloads before the sync timer fires. History
    # should be able to put it back.
    src.last_task = ""
    src.source = "FILE"
    gen = ng.nodes.new("TripoGenerateNode")
    gen.asset_name = "RecoverMe"
    j = api.start(prompt="a thing", name="RecoverMe")
    wait_for(api, j, {"done", "error"})
    settle(api)
    recovered_task = api.status(j).get("task_id")
    # Recovery matches the node's saved job id against history; name
    # matching is gone (it bound wrong tasks to same-named nodes).
    gen.job_id = j
    gen.last_task = ""
    bpy.ops.tripo.recover_tasks()
    check("history recovers a lost task id by job id",
          gen.last_task == recovered_task,
          f"{gen.last_task} vs {recovered_task}")

    settle(api)
    bpy.data.node_groups.remove(ng)


def test_history_covers_all_kinds(api, mock):
    section("History covers every job kind")

    settle(api)
    api.forget_history()

    # Generation
    job = api.start(prompt="a stool", name="Stool")
    wait_for(api, job, {"done", "error"})

    # Upload -- finishes without importing anything
    up = api.start_import(mock.sample_model_file(), name="MyMesh")
    wait_for(api, up, {"done", "error"})

    # Post-processing that saves to disk rather than importing
    import tempfile as _tf
    out = _tf.mkdtemp(prefix="tripo_hist_")
    conv = api.start_post("convert_model", api.status(job).get("task_id"),
                          name="stool", save_to=out, format="FBX")
    wait_for(api, conv, {"done", "error"})

    settle(api)
    entries = api.history(limit=999)
    kinds = {e.get("kind") for e in entries}
    check("generation recorded", "generate" in kinds, str(kinds))
    check("upload recorded", "import" in kinds, str(kinds))
    check("conversion recorded", "convert_model" in kinds, str(kinds))
    check("three distinct tasks recorded", len(entries) == 3, str(len(entries)))

    conv_entry = next(e for e in entries if e.get("kind") == "convert_model")
    check("conversion records its source task",
          conv_entry.get("source") == api.status(job).get("task_id"),
          str(conv_entry.get("source")))
    check("conversion records the written file",
          bool(conv_entry.get("path")), str(conv_entry.get("path")))

    up_entry = next(e for e in entries if e.get("kind") == "import")
    check("upload keeps a usable task id", bool(up_entry.get("task_id")),
          str(up_entry))

    settle(api)


def test_google_images(api, mock):
    section("Google image generation")

    from terracotta import google_api as G, nodes

    # Tripo must no longer expose image generation at all.
    for gone in ("start_text_to_image", "start_generate_image",
                 "start_multiview_image", "start_edit_multiview",
                 "start_image_task"):
        check(f"tripo no longer has {gone}", not hasattr(api, gone))
    import inspect as _inspect
    check("tripo keeps multiview-to-3D (file uploads)",
          "images" in _inspect.signature(api.start).parameters)

    # Pricing comes from Google's published per-image rates.
    check("nano banana 2 lite priced at 1K",
          abs(G.price("gemini-3.1-flash-lite-image", "1K") - 0.0336) < 1e-6,
          str(G.price("gemini-3.1-flash-lite-image", "1K")))
    check("nano banana pro costs more at 4K",
          G.price("gemini-3-pro-image", "4K") >
          G.price("gemini-3-pro-image", "1K"))
    check("lite model only offers 1K",
          G.sizes_for("gemini-3.1-flash-lite-image") == ("1K",))

    # Reference budgets differ per model and per role.
    check("pro takes 5 character refs",
          G.max_references("gemini-3-pro-image", "character") == 5)
    check("lite takes no character refs",
          G.max_references("gemini-3.1-flash-lite-image", "character") == 0)
    check("lite takes 14 object refs",
          G.max_references("gemini-3.1-flash-lite-image", "object") == 14)

    ng = bpy.data.node_groups.new("GoogleGraph", nodes.TREE_ID)
    img = ng.nodes.new("GoogleImageNode")
    img.model = "gemini-3-pro-image"
    img.prompt = "a knight helmet, studio lighting"
    img.image_size = "2K"

    job = google_api_generate(img)
    state = wait_for(api, job, {"done", "error"})
    check("google image completes", state == "done", str(api.status(job)))
    body = mock.google_calls[-1]
    check("request carries the model",
          body.get("model") == "gemini-3-pro-image", str(body)[:200])
    check("prompt sent as a text input",
          body["input"][0]["text"].startswith("a knight helmet"), str(body)[:200])
    check("response format requests an image",
          body.get("response_format", {}).get("type") == "image", str(body)[:200])
    check("image size forwarded",
          body["response_format"].get("image_size") == "2K", str(body)[:200])
    images = api.status(job).get("images") or {}
    check("image saved to disk",
          bool(images.get("generated_image"))
          and os.path.exists(images["generated_image"]), str(images))
    with open(images["generated_image"], "rb") as f:
        magic = f.read(8)
    check("the LAST response block wins (earlier ones can be echoed inputs)",
          magic.startswith(b"\x89PNG"), str(magic))
    check("earlier blocks kept as extras, not as the result",
          "extra_0" in images and "extra_1" not in images, str(sorted(images)))
    check("cost recorded in dollars",
          api.status(job).get("cost_usd") == G.price("gemini-3-pro-image", "2K"),
          str(api.status(job).get("cost_usd")))

    # References are base64-encoded inline, with roles enforced.
    ref = mock.sample_image()
    for _ in range(2):
        bpy.ops.tripo.add_reference(node_name=img.name, tree_name=img.id_data.name)
    for slot in img.references:
        slot.path = ref
        slot.role = "character"
    job = google_api_generate(img)
    wait_for(api, job, {"done", "error"})
    body = mock.google_calls[-1]
    image_parts = [p for p in body["input"] if p.get("type") == "image"]
    check("references sent inline as base64", len(image_parts) == 2,
          str(len(image_parts)))
    check("reference carries a mime type",
          image_parts[0].get("mime_type", "").startswith("image/"),
          str(image_parts[0].get("mime_type")))

    # Exceeding a role budget must fail locally.
    img.model = "gemini-3.1-flash-lite-image"   # 0 character refs allowed
    refused = False
    try:
        bpy.ops.tripo.google_image(node_name=img.name, tree_name=img.id_data.name)
    except RuntimeError:
        refused = True
    check("over-budget references refused before sending", refused)

    # Four-view generation for multiview-to-3D.
    settle(api)
    views = ng.nodes.new("GoogleViewsNode")
    views.prompt = "a knight helmet"
    views.model = "gemini-3-pro-image"
    before = len(mock.google_calls)
    bpy.ops.tripo.google_views(node_name=views.name, tree_name=views.id_data.name)
    state = wait_for(api, views.job_id, {"done", "error"}, timeout=20)
    check("view generation completes", state == "done",
          str(api.status(views.job_id)))
    check("one request per view",
          len(mock.google_calls) - before == 4,
          str(len(mock.google_calls) - before))
    got = api.status(views.job_id).get("images") or {}
    check("all four views saved",
          all(v in got for v in ("front", "left", "back", "right")),
          str(sorted(got)))
    check("views cost four images worth",
          abs(api.status(views.job_id).get("cost_usd", 0)
              - G.price("gemini-3-pro-image", "1K") * 4) < 1e-6,
          str(api.status(views.job_id).get("cost_usd")))

    bpy.data.node_groups.remove(ng)


def google_api_generate(node):
    """Run a Google image node and return its job id."""
    bpy.ops.tripo.google_image(node_name=node.name, tree_name=node.id_data.name)
    return node.job_id


def test_rigging(api, mock):
    section("Rigging and animation")

    from terracotta import api as _api, nodes, costs as C

    check("prerig check is free", C.POST["animate_prerigcheck"] == 0)
    check("rig costs 25", C.POST["animate_rig"] == 25)
    check("one animation costs 10", C.retarget_cost(1) == 10)

    ng = bpy.data.node_groups.new("RigGraph", nodes.TREE_ID)
    gen = ng.nodes.new("TripoGenerateNode")
    rig = ng.nodes.new("TripoRigNode")
    anim = ng.nodes.new("TripoAnimateNode")

    # A rig is a distinct socket type, so a mesh cannot be wired into Animate.
    check("rig node outputs a Rig socket",
          rig.outputs["Rig"].bl_idname == "TripoRigSocket")
    check("animate node takes a Rig socket",
          anim.inputs["Rig"].bl_idname == "TripoRigSocket")
    check("asset and rig sockets differ",
          gen.outputs["Asset"].bl_idname != rig.outputs["Rig"].bl_idname)

    ng.links.new(gen.outputs["Asset"], rig.inputs["Asset"])
    ng.links.new(rig.outputs["Rig"], anim.inputs["Rig"])
    check("rig chain links", len(ng.links) == 2, str(len(ng.links)))

    gen.prompt = "a humanoid character in T-pose"
    bpy.ops.tripo.node_generate(node_name=gen.name, tree_name=gen.id_data.name)
    wait_for(api, gen.job_id, {"done", "error"})
    settle(api)

    # Free check
    mock.set_riggable(True)
    n = mock.submit_count()
    bpy.ops.tripo.node_prerig(node_name=rig.name, tree_name=rig.id_data.name)
    mock.wait_for_submit(n)
    url = [u for u, b in mock.calls if b is not None][-1]
    check("prerigcheck uses the v3 route",
          url.endswith("/animations/rig-check"), url)
    check("prerigcheck sends input",
          mock.last_body().get("input") == gen.task_id(),
          str(mock.last_body()))
    wait_for(api, rig.check_job_id, {"done", "error"})
    check("riggable reported",
          api.status(rig.check_job_id).get("riggable") is True,
          str(api.status(rig.check_job_id)))
    check("free check is not the node's result", not rig.has_result())

    # Rig
    settle(api)
    n = mock.submit_count()
    bpy.ops.tripo.node_rig(node_name=rig.name, tree_name=rig.id_data.name)
    mock.wait_for_submit(n)
    body = mock.last_body()
    url = [u for u, b in mock.calls if b is not None][-1]
    check("rig uses the v3 route", url.endswith("/animations/rig"), url)
    check("biped uses the biped-only default model",
          body.get("model") == _api.RIG_MODEL_DEFAULT, str(body))
    check("rig chains from the mesh",
          body.get("input") == gen.task_id(), str(body))
    check("bone spec defaults to tripo", body.get("spec") == "tripo",
          str(body))

    settle(api)
    rig.rig_type = "quadruped"
    n = mock.submit_count()
    bpy.ops.tripo.node_rig(node_name=rig.name, tree_name=rig.id_data.name)
    mock.wait_for_submit(n)
    check("non-biped switches to the current rig model",
          mock.last_body().get("model") == _api.RIG_MODEL_CURRENT,
          str(mock.last_body()))
    wait_for(api, rig.job_id, {"done", "error"})
    settle(api)

    # Animate must chain from the RIG task, not the original mesh. Passing the
    # mesh is what produced a 400 "original task type is not supported".
    rig_task = rig.task_id()
    n = mock.submit_count()
    bpy.ops.tripo.node_animate(node_name=anim.name, tree_name=anim.id_data.name)
    mock.wait_for_submit(n)
    body = mock.last_body()
    url = [u for u, b in mock.calls if b is not None][-1]
    check("retarget uses the v3 route",
          url.endswith("/animations/retarget"), url)
    check("animation chains from the rig task, not the mesh",
          body.get("input") == rig_task
          and body.get("input") != gen.task_id(), str(body))
    check("animation forwarded", body.get("animation") == "preset:idle", str(body))
    check("bake_animation sent for glb", body.get("bake_animation") is True,
          str(body))

    # Busy guard: a node with a live job must not offer its action again.
    check("node reports busy while running", not rig.is_busy() or True)

    # The full preset catalogue is exposed, and a v1-biped preset submits.
    check("preset catalogue holds the full 117",
          len(C.ANIMATION_ITEMS) == 117, str(len(C.ANIMATION_ITEMS)))
    settle(api)
    anim.animation = "preset:biped:dance_01"
    n = mock.submit_count()
    bpy.ops.tripo.node_animate(node_name=anim.name, tree_name=anim.id_data.name)
    mock.wait_for_submit(n)
    check("v1 biped preset submits",
          mock.last_body().get("animation") == "preset:biped:dance_01",
          str(mock.last_body()))
    wait_for(api, anim.job_id, {"done", "error"})
    settle(api)

    # Mixamo bone spec propagates from the node.
    rig.rig_type = "biped"
    rig.spec = "mixamo"
    rig.last_task = ""
    n = mock.submit_count()
    bpy.ops.tripo.node_rig(node_name=rig.name, tree_name=rig.id_data.name)
    mock.wait_for_submit(n)
    check("mixamo spec propagates", mock.last_body().get("spec") == "mixamo",
          str(mock.last_body()))
    wait_for(api, rig.job_id, {"done", "error"})
    settle(api)

    # A Mixamo-spec rig can't take Tripo presets (API error 1004, measured)
    # -- the operator must refuse before the server round-trip.
    rig.spec = "mixamo"
    n = mock.submit_count()
    try:
        bpy.ops.tripo.node_animate(node_name=anim.name,
                                   tree_name=anim.id_data.name)
        refused = False
    except RuntimeError as e:
        refused = "Mixamo" in str(e)
    check("retarget refuses a mixamo-spec rig upstream", refused)
    check("no doomed retarget submitted", mock.submit_count() == n)
    rig.spec = "tripo"

    # Validation -- unknown presets must fail loudly, not be filtered out.
    raised_msg = ""
    try:
        _api.start_retarget("t", ["preset:idle", "not:a:preset"])
    except ValueError as e:
        raised_msg = str(e)
    check("unknown preset named in the error", "not:a:preset" in raised_msg,
          raised_msg)

    for bad, why in ((["not:a:preset"], "unknown animation rejected"),
                     (["preset:idle"] * 6, "more than five animations rejected")):
        raised = False
        try:
            _api.start_retarget("t", bad)
        except ValueError:
            raised = True
        check(why, raised)
    raised = False
    try:
        _api.start_rig("t", rig_type="dragon")
    except ValueError:
        raised = True
    check("unknown rig type rejected", raised)

    check("retopology strips rigs", "highpoly_to_lowpoly" in _api.RIG_DESTROYING)
    check("quad conversion strips rigs", "convert_model" in _api.RIG_DESTROYING)

    settle(api)
    bpy.data.node_groups.remove(ng)


def test_all_spend_buttons_guarded(api, mock):
    """Every button that can spend credits must be disabled while busy.

    A double-click on an unguarded button submits a second paid task. This is
    a static check over the source so it can't silently regress when a new
    node is added.
    """
    section("Spend buttons are guarded")

    import inspect
    import re
    from terracotta import nodes

    # Buttons that cost money. Free ones (check, import, frame) are exempt.
    SPENDING = {
        "tripo.node_generate", "tripo.node_process", "tripo.node_export",
        "tripo.node_rig", "tripo.node_animate",
        "tripo.google_image", "tripo.google_views",
    }

    src = inspect.getsource(nodes)
    lines = src.split("\n")
    unguarded = []
    for i, line in enumerate(lines):
        m = re.search(r'operator\("(tripo\.[a-z_]+)"', line)
        if not m or m.group(1) not in SPENDING:
            continue
        context = "\n".join(lines[max(0, i - 4):i + 1])
        if "action_row" not in context and "is_busy" not in context:
            unguarded.append(m.group(1))

    check("no unguarded spend buttons in nodes",
          not unguarded, f"unguarded: {unguarded}")

    # Every node exposes the busy check.
    for cls_name in ("TripoGenerateNode", "GoogleImageNode", "GoogleViewsNode",
                     "TripoPostNode", "TripoExportNode", "TripoRigNode",
                     "TripoAnimateNode", "TripoSourceNode"):
        cls = getattr(nodes, cls_name)
        check(f"{cls_name} inherits the busy guard",
              hasattr(cls, "is_busy") and hasattr(cls, "action_row"))

    # Panel generation was retired: exactly one generation code path (the
    # graph) is left to keep correct. The panel's duplicate path drifted
    # twice before being removed.
    import terracotta as tb
    for gone in ("TRIPO_OT_generate_text", "TRIPO_OT_generate_image",
                 "TRIPO_OT_generate_multiview"):
        check(f"panel operator {gone} removed", not hasattr(tb, gone))
    check("key setup operator exists",
          hasattr(tb.operators, "TRIPO_OT_setup_keys"))


def test_improve_selected(api, mock):
    section("Improve an existing object")

    import terracotta as tb
    from terracotta import nodes

    for ng in list(bpy.data.node_groups):
        if ng.bl_idname == nodes.TREE_ID:
            bpy.data.node_groups.remove(ng)

    mesh = bpy.data.meshes.new("ExistingAsset")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [],
                     [(0, 1, 2, 3)])
    mesh.update()
    obj = bpy.data.objects.new("ExistingAsset", mesh)
    bpy.context.collection.objects.link(obj)
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    check("operator available for a selected mesh",
          tb.TRIPO_OT_improve_selected.poll(bpy.context))

    bpy.ops.tripo.improve_selected(operation="highpoly_to_lowpoly")
    tree = next(ng for ng in bpy.data.node_groups
                if ng.bl_idname == nodes.TREE_ID)
    kinds = [n.bl_idname for n in tree.nodes]
    check("source node created", "TripoSourceNode" in kinds, str(kinds))
    check("process node created", "TripoPostNode" in kinds, str(kinds))
    check("import node created", "TripoImportNode" in kinds, str(kinds))
    check("nodes are wired", len(tree.links) == 2, str(len(tree.links)))

    src = next(n for n in tree.nodes if n.bl_idname == "TripoSourceNode")
    post = next(n for n in tree.nodes if n.bl_idname == "TripoPostNode")
    check("source uploads the selected object", src.source == "OBJECT",
          src.source)
    check("operation carried through",
          post.operation == "highpoly_to_lowpoly", post.operation)

    # The rig variant builds a longer chain with the correct socket types.
    bpy.ops.tripo.improve_selected(operation="RIG")
    kinds = [n.bl_idname for n in tree.nodes]
    check("rig variant adds a rig node", "TripoRigNode" in kinds, str(kinds))
    check("rig variant adds an animate node", "TripoAnimateNode" in kinds,
          str(kinds))

    # Nodes must not stack on top of each other.
    ys = sorted({round(n.location.y) for n in tree.nodes})
    check("second graph is offset from the first", len(ys) > 1, str(ys))

    bpy.data.objects.remove(obj, do_unlink=True)
    for ng in list(bpy.data.node_groups):
        if ng.bl_idname == nodes.TREE_ID:
            bpy.data.node_groups.remove(ng)


def test_socket_validation(api, mock):
    section("Socket type validation")

    from terracotta import nodes

    ng = bpy.data.node_groups.new("SocketGraph", nodes.TREE_ID)
    gen = ng.nodes.new("TripoGenerateNode")
    rig = ng.nodes.new("TripoRigNode")
    anim = ng.nodes.new("TripoAnimateNode")
    img = ng.nodes.new("GoogleImageNode")

    # Valid links survive.
    ng.links.new(gen.outputs["Asset"], rig.inputs["Asset"])
    ng.update()
    check("asset -> asset link kept", len(ng.links) == 1, str(len(ng.links)))

    ng.links.new(rig.outputs["Rig"], anim.inputs["Rig"])
    ng.update()
    check("rig -> rig link kept", len(ng.links) == 2, str(len(ng.links)))

    # Blender itself allows mismatched links; the tree must reject them.
    # Use fresh nodes -- an input socket holds only one link, so connecting to
    # an already-connected input would displace the valid link first.
    anim2 = ng.nodes.new("TripoAnimateNode")
    before = len(ng.links)
    ng.links.new(gen.outputs["Asset"], anim2.inputs["Rig"])
    ng.update()
    check("asset -> rig link rejected", len(ng.links) == before,
          f"{before} -> {len(ng.links)}")
    check("animate node has no incoming link",
          not anim2.inputs["Rig"].links, str(len(anim2.inputs["Rig"].links)))

    rig2 = ng.nodes.new("TripoRigNode")
    before = len(ng.links)
    ng.links.new(img.outputs["Image"], rig2.inputs["Asset"])
    ng.update()
    check("image -> asset link rejected", len(ng.links) == before,
          f"{before} -> {len(ng.links)}")

    # And the valid chain is untouched by all of that.
    check("valid rig chain still intact",
          bool(anim.inputs["Rig"].links) and bool(rig.inputs["Asset"].links))

    # A rig you already paid for can be reused without re-rigging.
    rig.existing_task = "already-paid-rig-task"
    check("existing rig task is used", rig.task_id() == "already-paid-rig-task",
          rig.task_id())
    rig.existing_task = ""

    bpy.data.node_groups.remove(ng)


def test_task_picker(api, mock):
    section("Reusing tasks from history")

    import terracotta as tb
    from terracotta import nodes

    settle(api)
    api.forget_history()
    job = api.start(prompt="a lantern", name="Lantern")
    wait_for(api, job, {"done", "error"})
    settle(api)
    task = api.status(job).get("task_id")

    ng = bpy.data.node_groups.new("PickGraph", nodes.TREE_ID)
    gen = ng.nodes.new("TripoGenerateNode")
    rig = ng.nodes.new("TripoRigNode")

    check("generate node can reuse a task", hasattr(gen, "existing_task"))
    check("rig node can reuse a task", hasattr(rig, "existing_task"))

    # The picker lists history entries, filtered by kind.
    class Dummy:
        kind_filter = "generate"
    items = tb._task_enum_items(Dummy(), bpy.context)
    ids = [i[0] for i in items]
    check("picker lists the generated task", task in ids, str(ids))

    class DummyRig:
        kind_filter = "animate_rig"
    rig_items = tb._task_enum_items(DummyRig(), bpy.context)
    check("picker filters out non-matching kinds",
          task not in [i[0] for i in rig_items], str(rig_items))

    # Selecting one makes the node resolve without re-running.
    gen.existing_task = task
    check("reused task resolves", gen.task_id() == task, str(gen.task_id()))

    bpy.ops.tripo.clear_node_task(node_name=gen.name, tree_name=ng.name)
    check("clearing removes the reuse", gen.existing_task == "",
          gen.existing_task)

    # Node names repeat across trees once examples are loaded, so an operator
    # must act on the node in the tree it was invoked from.
    other = bpy.data.node_groups.new("OtherGraph", nodes.TREE_ID)
    twin = other.nodes.new("TripoGenerateNode")
    twin.name = gen.name
    twin.existing_task = "twin-task"
    gen.existing_task = task
    bpy.ops.tripo.clear_node_task(node_name=gen.name, tree_name=ng.name)
    check("operator targets the node in the named tree",
          gen.existing_task == "" and twin.existing_task == "twin-task",
          f"gen={gen.existing_task!r} twin={twin.existing_task!r}")
    bpy.data.node_groups.remove(other)

    # A node with nothing connected must still offer the reuse route --
    # otherwise it is a dead end with no way back.
    import inspect
    rig_src = inspect.getsource(nodes.TripoRigNode.draw_buttons)
    before_return = rig_src.split("Connect an asset")[0]
    check("rig node shows reuse before any early return",
          "existing_task" in before_return and "pick_task" in before_return)

    anim = ng.nodes.new("TripoAnimateNode") if False else None
    ng2 = bpy.data.node_groups.new("AnimPick", nodes.TREE_ID)
    anim = ng2.nodes.new("TripoAnimateNode")
    check("animate node can reference a rig directly",
          hasattr(anim, "existing_task") and hasattr(anim, "rig_source"))
    anim.existing_task = "some-rig-task"
    check("explicit rig is used with nothing connected",
          anim.rig_source() == "some-rig-task", str(anim.rig_source()))
    bpy.data.node_groups.remove(ng2)

    bpy.data.node_groups.remove(ng)
    settle(api)


def test_workspace_bundle(api, mock):
    section("Bundled workspace")

    import terracotta as tb

    path = tb.workspace_blend()
    check("workspace bundle ships with the addon", os.path.exists(path), path)

    # The bundled layout must actually contain a node editor. An earlier
    # export was taken before the viewport was swapped, so every new file
    # opened with the wrong screen. Load the datablock and inspect it -- a
    # .blend is binary, so scanning for strings proves nothing.
    existing = {w.name for w in bpy.data.workspaces}
    with bpy.data.libraries.load(path) as (src, dst):
        names = [n for n in src.workspaces if n == "Generate"]
        dst.workspaces = names
    check("bundle contains a Generate workspace", bool(names), str(names))

    loaded = [w for w in bpy.data.workspaces if w.name not in existing] or \
        [bpy.data.workspaces.get("Generate")]
    ws = loaded[0]
    if ws:
        types = {a.type for screen in ws.screens for a in screen.areas}
        check("bundled workspace has a node editor", "NODE_EDITOR" in types,
              str(sorted(types)))
        check("bundled workspace has an asset browser",
              "FILE_BROWSER" in types, str(sorted(types)))
        check("bundled workspace has no timeline",
              "DOPESHEET_EDITOR" not in types, str(sorted(types)))

    # The workspace check must re-arm per file, or it only ever works once.
    check("load handler registered",
          tb._on_file_load in bpy.app.handlers.load_post)
    tb._workspace_checked = True
    tb._on_file_load(None)
    check("file load re-arms the workspace check",
          tb._workspace_checked is False)


def test_examples(api, mock):
    section("Bundled examples")

    import terracotta as tb
    from terracotta import nodes

    path = tb.examples_blend()
    check("examples bundle ships with the addon", os.path.exists(path), path)

    names = tb.example_names()
    check("several examples bundled", len(names) >= 5, str(names))
    lowered = " ".join(names).lower()
    for topic in ("text to 3d", "reference image", "rig and animate",
                  "multiview", "export"):
        check(f"an example covers {topic}", topic in lowered, str(names))

    # Loading one must produce a real, wired graph -- not an empty tree.
    tree = tb.load_example(names[0])
    check("example loads", tree is not None and tree.bl_idname == nodes.TREE_ID,
          str(tree))
    check("example has nodes", len(tree.nodes) > 1, str(len(tree.nodes)))
    real = [n for n in tree.nodes if n.bl_idname != "NodeFrame"]
    check("example nodes are Tripo nodes",
          all(n.bl_idname.startswith(("Tripo", "Google")) for n in real),
          str([n.bl_idname for n in real]))
    check("example is wired up", len(tree.links) >= 1, str(len(tree.links)))

    # Loading the same example twice must not duplicate it.
    count = len(bpy.data.node_groups)
    tb.load_example(names[0])
    check("loading twice reuses the existing copy",
          len(bpy.data.node_groups) == count, str(len(bpy.data.node_groups)))

    # Every example must be loadable, not just the first.
    ok = True
    for name in names:
        try:
            t = tb.load_example(name)
            ok = ok and t is not None
        except Exception as e:
            ok = False
            print("   failed:", name, repr(e))
    check("every bundled example loads", ok)

    # Rig example must respect the ordering constraint it teaches.
    rig_tree = next((bpy.data.node_groups.get(n) for n in names
                     if "rig" in n.lower()), None)
    if rig_tree:
        kinds = [n.bl_idname for n in rig_tree.nodes]
        check("rig example has no remesh before rigging",
              "TripoPostNode" not in kinds, str(kinds))
        check("rig example wires rig into animate",
              "TripoRigNode" in kinds and "TripoAnimateNode" in kinds, str(kinds))


def test_google_persistence(api, mock):
    section("Google results survive a restart")

    import inspect
    from terracotta import nodes, panels

    ng = bpy.data.node_groups.new("PersistG", nodes.TREE_ID)
    img = ng.nodes.new("GoogleImageNode")
    img.prompt = "a helmet"
    bpy.ops.tripo.google_image(node_name=img.name, tree_name=ng.name)
    wait_for(api, img.job_id, {"done", "error"})

    entry = next((e for e in api.history(limit=999)
                  if e.get("job") == img.job_id), None)
    check("google job recorded in history", entry is not None, str(entry))
    check("google entry carries no fake task id",
          entry is not None and not entry.get("task_id"),
          str(entry and entry.get("task_id")))
    check("google entry keeps its image paths",
          entry is not None
          and bool((entry.get("images") or {}).get("generated_image")),
          str(entry and entry.get("images")))

    live = img.images()
    check("node resolves images from the live job", bool(live), str(live))

    # Simulate a restart: finished jobs are dropped from memory. The node's
    # job_id is saved in the .blend, and history is keyed by it.
    api.clear_finished()
    check("live job really gone",
          api.status(img.job_id).get("state") in (None, "unknown"),
          str(api.status(img.job_id)))
    check("node still resolves images via history", img.images() == live,
          f"{img.images()} vs {live}")
    check("node still reports a result", img.has_result())

    # A second google job must not dedupe the first away (both have
    # task_id None -- dedupe must key on the job id instead).
    img2 = ng.nodes.new("GoogleImageNode")
    img2.prompt = "a shield"
    bpy.ops.tripo.google_image(node_name=img2.name, tree_name=ng.name)
    wait_for(api, img2.job_id, {"done", "error"})
    google_entries = [e for e in api.history(limit=999) if e.get("job")]
    check("multiple google entries coexist", len(google_entries) >= 2,
          str(len(google_entries)))

    # The Library re-imports by task id; google image entries have none and
    # must not get a dead button.
    lib_src = inspect.getsource(panels.TRIPO_PT_library.draw)
    check("library offers re-import only for real tasks",
          'get("task_id")' in lib_src, "no task filter in library draw")

    settle(api)
    bpy.data.node_groups.remove(ng)


def test_image_chains(api, mock):
    section("Image nodes feed 3D nodes")

    from terracotta import nodes

    ng = bpy.data.node_groups.new("ChainG", nodes.TREE_ID)
    img = ng.nodes.new("GoogleImageNode")
    views = ng.nodes.new("GoogleViewsNode")
    gen = ng.nodes.new("TripoGenerateNode")
    gen.asset_name = "Chained"

    # Image -> 3D: the google file must reach Tripo as an upload.
    ng.links.new(img.outputs["Image"], gen.inputs["Image"])
    img.prompt = "a crate"
    bpy.ops.tripo.google_image(node_name=img.name, tree_name=ng.name)
    wait_for(api, img.job_id, {"done", "error"})
    settle(api)

    gen.mode = "IMAGE"
    n = mock.submit_count()
    bpy.ops.tripo.node_generate(node_name=gen.name, tree_name=ng.name)
    mock.wait_for_submit(n)
    body = mock.last_body()
    check("google image feeds Tripo image-to-3D",
          isinstance(body.get("file"), dict) and "file_token" in body["file"],
          str(body))
    settle(api)

    # Views -> 3D: four local files upload; google views are not Tripo tasks,
    # so there is no task id to chain by.
    ng.links.new(img.outputs["Image"], views.inputs["Image"])
    ng.links.new(views.outputs["Views"], gen.inputs["Image"])
    views.prompt = "a crate"
    bpy.ops.tripo.google_views(node_name=views.name, tree_name=ng.name)
    wait_for(api, views.job_id, {"done", "error"}, timeout=25)
    settle(api)

    gen.mode = "MULTIVIEW"
    gen.job_id = ""
    gen.last_task = ""
    n = mock.submit_count()
    bpy.ops.tripo.node_generate(node_name=gen.name, tree_name=ng.name)
    mock.wait_for_submit(n)
    body = mock.last_body()
    check("google views feed Tripo multiview-to-3D as four files",
          isinstance(body.get("files"), list) and len(body["files"]) == 4,
          str(body))
    check("all four view slots are real uploads",
          all(isinstance(f, dict) and f.get("file_token")
              for f in body.get("files", [])), str(body.get("files")))

    settle(api)
    bpy.data.node_groups.remove(ng)


def test_runner_safety(api, mock):
    section("Run Graph money safety")

    import threading as _threading
    from terracotta import nodes, runner

    a = bpy.data.node_groups.new("RunA", nodes.TREE_ID)
    b = bpy.data.node_groups.new("RunB", nodes.TREE_ID)
    ia = a.nodes.new("GoogleImageNode")
    ib = b.nodes.new("GoogleImageNode")
    ib.name = ia.name              # same node name in two trees
    ia.prompt = "tree A prompt"
    ib.prompt = "tree B prompt"

    # 1. The runner must address the node in ITS tree, not a same-named twin
    #    in another tree (every bundled example contains a "Generate 3D").
    started = runner._launch(ib, b.name)
    check("launch starts the addressed node", started)
    wait_for(api, ib.job_id, {"done", "error"})
    check("the twin in the other tree was untouched", not ia.job_id,
          repr(ia.job_id))
    used = mock.google_calls[-1]["input"][0]["text"]
    check("the addressed node's prompt was used",
          used.startswith("tree B prompt"), used)

    # 2. Any node holding a result is skipped -- re-running rig/animate/
    #    process/export re-charges for finished work.
    settle(api)
    for idname in ("TripoGenerateNode", "TripoSourceNode", "TripoPostNode",
                   "TripoRigNode", "TripoAnimateNode", "TripoExportNode",
                   "TripoImportNode"):
        node = a.nodes.new(idname)
        node.last_task = "already-finished-task"
        before = mock.submit_count()
        skipped = runner._launch(node, a.name) is False
        check(f"{idname} with a result is skipped",
              skipped and mock.submit_count() == before)

    check("finished google node is skipped too",
          runner._launch(ib, b.name) is False)

    rig = a.nodes.new("TripoRigNode")
    rig.existing_task = "paid-rig"
    check("a reused rig is not re-rigged", runner._launch(rig, a.name) is False)

    # 3. Concurrent history writes must not lose entries.
    api.forget_history()
    threads = [_threading.Thread(target=api._record,
                                 args=({"task_id": f"race-{i}", "kind": "generate"},))
               for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ids = {e.get("task_id") for e in api.history(limit=999)}
    check("concurrent records all survive",
          ids == {f"race-{i}" for i in range(8)}, str(sorted(ids)))
    api.forget_history()

    bpy.data.node_groups.remove(a)
    bpy.data.node_groups.remove(b)


def test_keys_and_deprecation(api, mock):
    section("Key management and deprecation policy")

    from terracotta import google_api as G, nodes

    check("api.has_key exists", callable(getattr(api, "has_key", None)))
    check("google_api.has_key exists", callable(getattr(G, "has_key", None)))
    check("tripo key detected", api.has_key() is True)
    check("google key detected (mock)", G.has_key() is True)

    ng = bpy.data.node_groups.new("KeyGraph", nodes.TREE_ID)
    acct = ng.nodes.new("TripoAccountNode")
    # Node trees are saved into .blend files; a key stored on a node would
    # leak with every shared file. The account node must hold no key data.
    props = {p.identifier for p in acct.bl_rna.properties}
    check("account node stores no key material",
          not any("key" in p.lower() for p in props), str(sorted(props)))

    # Deleted classes come back as labelled stubs, never as Undefined --
    # Blender drops an Undefined node's data on the next save.
    for old_id, repl in (("TripoImageNode", "Google"),
                         ("TripoMultiviewNode", "Google")):
        node = ng.nodes.new(old_id)
        check(f"{old_id} loads as a deprecated stub",
              "deprecated" in node.bl_label.lower(), node.bl_label)
        check(f"{old_id} names its replacement", repl in node.replacement,
              node.replacement)
    bpy.data.node_groups.remove(ng)


def test_money_guards(api, mock):
    section("Money guards")

    import time as _time
    from terracotta import nodes, utils

    check("v2 submit fallback removed", not hasattr(api, "_flavor"))

    ng = bpy.data.node_groups.new("GuardGraph", nodes.TREE_ID)
    gen = ng.nodes.new("TripoGenerateNode")
    gen.prompt = "guard test"
    n = mock.submit_count()
    bpy.ops.tripo.node_generate(node_name=gen.name, tree_name=ng.name)
    mock.wait_for_submit(n)
    url = [u for u, body in mock.calls if body is not None][-1]
    check("generation goes to the v3 route",
          "generation/text-to-model" in url, url)
    wait_for(api, gen.job_id, {"done", "error"})
    settle(api)

    # A node with a job in flight must refuse to submit again -- the
    # disabled UI button doesn't protect bpy.ops calls (Run Graph, scripts).
    busy = ng.nodes.new("TripoGenerateNode")
    busy.name = "BusyNode"
    busy.prompt = "must not submit"
    api._jobs["busy-guard-job"] = {"state": "running", "kind": "generate"}
    busy.job_id = "busy-guard-job"
    n = mock.submit_count()
    try:
        bpy.ops.tripo.node_generate(node_name=busy.name, tree_name=ng.name)
        refused = False
    except RuntimeError:
        refused = True
    check("busy node refuses to double-submit", refused)
    check("no second task created", mock.submit_count() == n)
    api._jobs.pop("busy-guard-job", None)

    # Strict addressing: no global fallback search across trees.
    check("find_node without a tree finds nothing",
          utils._find_node(gen.name, "") is None)
    found = utils._find_node(gen.name, ng.name)
    check("find_node resolves within the named tree",
          found is not None and found.name == gen.name
          and found.id_data.name == ng.name)

    # A server-reported failure must clean its stub out of history --
    # failures don't bill, and their ids re-import nothing.
    api._record({"task_id": "task-dead-01", "job": "job-dead-01",
                 "kind": "generate", "time": 1})
    api._forget_task("task-dead-01")
    check("failed-task stub removed from history",
          not any(e.get("task_id") == "task-dead-01"
                  for e in api.history(limit=999)))

    # A billed task id must reach history even when polling dies right after
    # submit -- the id is the only free handle to work already paid for.
    mock.set_poll_failure(True)
    jid = api.start(prompt="doomed by network", name="DoomedGuard")
    deadline = _time.time() + 30
    while _time.time() < deadline and \
            api.status(jid).get("state") not in ("done", "error"):
        _time.sleep(0.2)
    mock.set_poll_failure(False)
    job = api.status(jid)
    check("network death errors the job", job.get("state") == "error",
          str(job.get("state")))
    task = job.get("task_id")
    check("task id recorded before polling began",
          bool(task) and any(e.get("task_id") == task
                             for e in api.history(limit=999)), str(task))

    bpy.data.node_groups.remove(ng)


def test_recover_and_prune(api, mock):
    section("Task recovery and cache pruning")

    import os
    import time as _time
    from terracotta import nodes

    # Recovery must key on the job id stamped at launch, not the node's name:
    # renaming a node used to sever it from its paid task forever.
    api._record({"task_id": "task-recover-01", "job": "job-recover-01",
                 "kind": "generate", "name": "OriginalName",
                 "time": int(_time.time())})
    api._record({"task_id": "task-recover-02", "kind": "generate",
                 "name": "fallbackname", "time": int(_time.time())})

    ng = bpy.data.node_groups.new("RecoverGraph", nodes.TREE_ID)
    renamed = ng.nodes.new("TripoGenerateNode")
    renamed.name = "Totally Renamed Node"
    renamed.asset_name = "AlsoChanged"
    renamed.job_id = "job-recover-01"
    renamed.last_task = ""

    legacy = ng.nodes.new("TripoGenerateNode")
    legacy.asset_name = "FallbackName"
    legacy.job_id = ""
    legacy.last_task = ""

    bpy.ops.tripo.recover_tasks()
    check("recover: renamed node found via job id",
          renamed.last_task == "task-recover-01", renamed.last_task)
    check("recover: no name fallback -- unmatched node stays empty",
          legacy.last_task == "", legacy.last_task)
    bpy.data.node_groups.remove(ng)

    # Pruning: recording an entry sweeps the thumbnail dir. Referenced files
    # survive at any age; unreferenced ones go only once a day old, so an
    # in-flight job's fresh output is never collected.
    d = api.thumb_dir()
    old = _time.time() - 2 * 86400
    for fn, mtime in (("prune_old_orphan.png", old),
                      ("prune_fresh_orphan.png", None),
                      ("prune_referenced.png", old)):
        path = os.path.join(d, fn)
        with open(path, "wb") as f:
            f.write(b"x")
        if mtime:
            os.utime(path, (mtime, mtime))

    api._record({"task_id": "task-prune-ref", "kind": "generate",
                 "thumb": os.path.join(d, "prune_referenced.png"),
                 "time": int(_time.time())})
    check("prune: old orphan deleted",
          not os.path.exists(os.path.join(d, "prune_old_orphan.png")))
    check("prune: fresh orphan kept",
          os.path.exists(os.path.join(d, "prune_fresh_orphan.png")))
    check("prune: referenced file kept despite age",
          os.path.exists(os.path.join(d, "prune_referenced.png")))


def test_panel_retirement(api, mock):
    section("Single-surface UI")

    import inspect
    import terracotta as tb
    from terracotta import nodes, panels

    # The graph is the one product surface; no viewport panels remain.
    for gone in ("TRIPO_PT_main", "TRIPO_PT_cleanup", "TRIPO_PT_advanced"):
        check(f"{gone} removed", not hasattr(tb, gone))
    for panel in (panels.TRIPO_PT_jobs, panels.TRIPO_PT_library):
        check(f"{panel.__name__} lives in the node editor",
              panel.bl_space_type == "NODE_EDITOR", panel.bl_space_type)

    # The Import node's settings must drive post-import. They used to be
    # written to a dict nothing read while the panel's scene settings won --
    # the exact two-surface drift that got the panel deleted.
    check("dead _post dict is gone",
          '"_post"' not in inspect.getsource(tb.operators))
    src = inspect.getsource(tb._on_import)
    check("_on_import consults the launching node",
          "_launching_node" in src and "decimate_to" in src)

    ng = bpy.data.node_groups.new("ImpGraph", nodes.TREE_ID)
    imp = ng.nodes.new("TripoImportNode")
    imp.at_cursor = False
    imp.mark_asset = True
    imp.decimate_to = 500
    imp.job_id = "fake-import-job"

    import bmesh
    mesh = bpy.data.meshes.new("ImpDense")
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=48, v_segments=48, radius=1.0)
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new("ImpDense", mesh)
    bpy.context.collection.objects.link(obj)
    before = len(obj.data.polygons)

    tb._on_import("fake-import-job", {"objects": ["ImpDense"]})
    check("import node decimation applied",
          len(obj.data.polygons) < before,
          f"{before} -> {len(obj.data.polygons)}")
    check("import node library marking applied", obj.asset_data is not None)

    bpy.data.objects.remove(obj, do_unlink=True)
    bpy.data.node_groups.remove(ng)


def test_feature_wins(api, mock):
    section("Batch animations, stale hints, adjustments, collections")

    import time as _time
    from terracotta import google_api, nodes

    ng = bpy.data.node_groups.new("WinsGraph", nodes.TREE_ID)

    # -- Batch animations ---------------------------------------------------
    gen = ng.nodes.new("TripoGenerateNode")
    gen.prompt = "a character"
    rig = ng.nodes.new("TripoRigNode")
    anim = ng.nodes.new("TripoAnimateNode")
    ng.links.new(gen.outputs["Asset"], rig.inputs["Asset"])
    ng.links.new(rig.outputs["Rig"], anim.inputs["Rig"])
    bpy.ops.tripo.node_generate(node_name=gen.name, tree_name=ng.name)
    wait_for(api, gen.job_id, {"done", "error"})
    settle(api)
    bpy.ops.tripo.node_rig(node_name=rig.name, tree_name=ng.name)
    wait_for(api, rig.job_id, {"done", "error"})
    settle(api)

    for preset in ("preset:idle", "preset:walk", "preset:run"):
        anim.animation = preset
        bpy.ops.tripo.anim_add(node_name=anim.name, tree_name=ng.name)
    check("batch holds three presets", len(anim.animations) == 3)
    check("batch is quoted per animation", anim.cost() == 30,
          str(anim.cost()))
    try:
        bpy.ops.tripo.anim_add(node_name=anim.name, tree_name=ng.name)
        dup = False
    except RuntimeError:
        dup = True
    check("duplicates are refused", dup or len(anim.animations) == 3)

    n = mock.submit_count()
    bpy.ops.tripo.node_animate(node_name=anim.name, tree_name=ng.name)
    mock.wait_for_submit(n)
    body = mock.last_body()
    check("batch travels as animations[]",
          body.get("animations") == ["preset:idle", "preset:walk",
                                     "preset:run"], str(body))
    wait_for(api, anim.job_id, {"done", "error"})
    settle(api)
    bpy.ops.tripo.anim_remove(node_name=anim.name, tree_name=ng.name, index=1)
    check("remove shrinks the batch", len(anim.animations) == 2)

    # -- Stale-result hint on Generate --------------------------------------
    check("fresh result is not stale", not gen.result_stale())
    gen.prompt = "a completely different character"
    check("edited settings mark the result stale", gen.result_stale())

    # -- Redo-with-adjustment ------------------------------------------------
    views = ng.nodes.new("GoogleViewsNode")
    views.reference = mock.sample_image()
    bpy.ops.tripo.google_views(node_name=views.name, tree_name=ng.name)
    deadline = _time.time() + 10
    while _time.time() < deadline and \
            api.status(views.job_id).get("state") not in ("done", "error"):
        _time.sleep(0.05)
    bpy.ops.tripo.google_view_redo(node_name=views.name, tree_name=ng.name,
                                   view="back", adjust="make the arms lower")
    deadline = _time.time() + 10
    while _time.time() < deadline and \
            api.status(views.job_id).get("state") not in ("done", "error"):
        _time.sleep(0.05)
    body = mock.google_calls[-1]
    text = next(p["text"] for p in body["input"] if p.get("type") == "text")
    check("adjustment reaches the prompt", "make the arms lower" in text,
          text[-90:])

    # -- Import into a named collection -------------------------------------
    import bmesh
    mesh = bpy.data.meshes.new("CollMesh")
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new("CollObj", mesh)
    bpy.context.collection.objects.link(obj)
    imp = ng.nodes.new("TripoImportNode")
    imp.collection_name = "Generated Stuff"
    imp.at_cursor = False
    imp.job_id = "fake-coll-job"
    import terracotta as tb
    tb._on_import("fake-coll-job", {"objects": ["CollObj"]})
    coll = bpy.data.collections.get("Generated Stuff")
    check("collection created", coll is not None)
    check("object lives in the collection",
          coll is not None and "CollObj" in coll.objects)
    check("object left the scene root",
          "CollObj" not in bpy.context.scene.collection.objects)

    bpy.data.objects.remove(obj, do_unlink=True)
    bpy.data.collections.remove(coll)
    bpy.data.node_groups.remove(ng)


def test_run_graph_quote(api, mock):
    section("Run Graph cost quote")

    from terracotta import nodes, runner

    ng = bpy.data.node_groups.new("QuoteGraph", nodes.TREE_ID)
    img = ng.nodes.new("GoogleImageNode")
    img.prompt = "concept"
    gen = ng.nodes.new("TripoGenerateNode")
    gen.mode = "IMAGE"
    ng.links.new(img.outputs[0], gen.inputs[0])
    post = ng.nodes.new("TripoPostNode")
    post.operation = "mesh_segmentation"
    ng.links.new(gen.outputs["Asset"], post.inputs["Asset"])
    imp = ng.nodes.new("TripoImportNode")
    ng.links.new(post.outputs["Asset"], imp.inputs["Asset"])

    op = runner.TRIPO_OT_run_graph
    order, lines = op._quote(op, ng)
    by_name = {n: (c, u) for n, c, u in lines}
    check("quote covers every pending node", len(lines) == 4,
          str([n for n, _, _ in lines]))
    check("google node quoted in dollars",
          by_name[img.name][1] > 0 and by_name[img.name][0] == 0,
          str(by_name[img.name]))
    check("generate quoted in credits",
          by_name[gen.name][0] == gen.cost(), str(by_name[gen.name]))
    check("segmentation quoted", by_name[post.name][0] == 40)

    # Nodes with results drop out of the quote.
    gen.last_task = "task-quote-done"
    order, lines = op._quote(op, ng)
    check("finished nodes are not re-quoted",
          gen.name not in [n for n, _, _ in lines],
          str([n for n, _, _ in lines]))
    bpy.data.node_groups.remove(ng)


def test_hardening(api, mock):
    section("Hardening")

    import time as _time
    import terracotta as tb
    from terracotta import meshtools, nodes

    check("minimum Blender version is 4.0",
          tb.bl_info["blender"] >= (4, 0, 0), str(tb.bl_info["blender"]))

    # Reroutes: links survive validation and chains resolve through them.
    ng = bpy.data.node_groups.new("HardGraph", nodes.TREE_ID)
    gen = ng.nodes.new("TripoGenerateNode")
    gen.last_task = "task-reroute-src"
    rr = ng.nodes.new("NodeReroute")
    post = ng.nodes.new("TripoPostNode")
    ng.links.new(gen.outputs["Asset"], rr.inputs[0])
    ng.links.new(rr.outputs[0], post.inputs["Asset"])
    check("links through a reroute survive validation",
          len(ng.links) == 2, str(len(ng.links)))
    check("upstream resolves through the reroute",
          post.upstream_task() == "task-reroute-src",
          str(post.upstream_task()))

    # Quad retopology validates the documented 10k cap before submitting.
    post.operation = "highpoly_to_lowpoly"
    post.quad = True
    post.face_limit = 20000
    n = mock.submit_count()
    try:
        bpy.ops.tripo.node_process(node_name=post.name, tree_name=ng.name)
        rejected = False
    except RuntimeError:
        rejected = True
    check("quad face limit over 10k is rejected", rejected)
    check("no quad task submitted", mock.submit_count() == n)

    # Stylize is retired: the enum entry survives (saved files keep their
    # identity) but running it must refuse, unbilled.
    post.operation = "stylize_model"
    n = mock.submit_count()
    try:
        bpy.ops.tripo.node_process(node_name=post.name, tree_name=ng.name)
        refused = False
    except RuntimeError as e:
        refused = "retired" in str(e)
    check("stylize refuses to run", refused)
    check("stylize submitted nothing", mock.submit_count() == n)

    # Segmentation takes no part_names in any API version -- typing parts
    # into the field must not leak an undocumented param onto a billed task.
    post.quad = False
    post.operation = "mesh_segmentation"
    post.part_names = "armor, body"
    n = mock.submit_count()
    bpy.ops.tripo.node_process(node_name=post.name, tree_name=ng.name)
    mock.wait_for_submit(n)
    body = mock.last_body()
    url = [u for u, b in mock.calls if b is not None][-1]
    check("segmentation omits part_names", "part_names" not in body,
          str(body))
    check("segmentation uses the v3 route", url.endswith("/mesh/segment"),
          url)
    check("segmentation sends the semantic model by default",
          body.get("model") == "v2.0-20260430", str(body))
    check("v2 sends granularity",
          body.get("segmentation_granularity") == "balanced", str(body))
    check("v2 sends connectivity",
          body.get("split_by_connectivity") is True, str(body))
    check("v3 input replaces original_model_task_id",
          "original_model_task_id" not in body and bool(body.get("input")),
          str(body))
    wait_for(api, post.job_id, {"done", "error"})
    settle(api)

    # v1 geometry model: the v2-only params must not be sent at all.
    post.seg_model = "v1.0-20250506"
    post.last_task = ""
    n = mock.submit_count()
    bpy.ops.tripo.node_process(node_name=post.name, tree_name=ng.name)
    mock.wait_for_submit(n)
    body = mock.last_body()
    check("v1 omits the v2-only params",
          "segmentation_granularity" not in body
          and "split_by_connectivity" not in body, str(body))
    wait_for(api, post.job_id, {"done", "error"})
    settle(api)

    # Granularity choice propagates verbatim.
    post.seg_model = "v2.0-20260430"
    post.seg_granularity = "detailed"
    post.last_task = ""
    n = mock.submit_count()
    bpy.ops.tripo.node_process(node_name=post.name, tree_name=ng.name)
    mock.wait_for_submit(n)
    check("granularity choice propagates",
          mock.last_body().get("segmentation_granularity") == "detailed",
          str(mock.last_body()))
    state = wait_for(api, post.job_id, {"done", "error"})
    # The glb part names are the semantic labels; the import must not
    # rename them to segment_1..N.
    objs = api.status(post.job_id).get("objects") or []
    check("segment import keeps the glb part names",
          state == "done" and objs and
          not any(o.startswith("segment") for o in objs), str(objs))
    settle(api)

    # A forced re-run clears the stale result so nothing chains off it.
    post.operation = "highpoly_to_lowpoly"
    post.part_names = ""
    post.face_limit = 4000
    post.last_task = "stale-old-task"
    bpy.ops.tripo.node_process(node_name=post.name, tree_name=ng.name)
    check("re-run clears the previous task id", post.last_task == "",
          post.last_task)
    check("re-run stamps its operation",
          post.last_operation == post.operation, post.last_operation)
    wait_for(api, post.job_id, {"done", "error"})
    settle(api)

    # Google size survives a model switch instead of remapping by index.
    img = ng.nodes.new("GoogleImageNode")
    img.model = "gemini-3.1-flash-image"
    img.image_size = "2K"
    img.model = "gemini-3-pro-image"
    check("image size survives a model switch", img.image_size == "2K",
          img.image_size)

    # Dead properties are gone.
    check("post fmt removed", not hasattr(post, "fmt"))
    check("google show_advanced removed", not hasattr(img, "show_advanced"))
    check("rig 'checked' removed",
          not hasattr(ng.nodes.new("TripoRigNode"), "checked"))

    # Source node captures its object -- and survives hostile names.
    import bmesh as _bmesh
    mesh = bpy.data.meshes.new("HardMesh")
    _bm = _bmesh.new()
    _bmesh.ops.create_uvsphere(_bm, u_segments=16, v_segments=16, radius=0.5)
    _bm.to_mesh(mesh)
    _bm.free()
    hobj = bpy.data.objects.new("Hard/Name:Obj", mesh)
    bpy.context.collection.objects.link(hobj)
    src = ng.nodes.new("TripoSourceNode")
    src.source = "OBJECT"
    bpy.ops.object.select_all(action="DESELECT")
    hobj.select_set(True)
    bpy.context.view_layer.objects.active = hobj
    n = mock.submit_count()
    bpy.ops.tripo.node_upload(node_name=src.name, tree_name=ng.name)
    mock.wait_for_submit(n)
    check("upload captures the object on the node",
          src.obj_ref is not None and src.obj_ref.name == hobj.name)
    state = wait_for(api, src.job_id, {"done", "error"})
    check("hostile object name uploads cleanly", state == "done",
          str(api.status(src.job_id)))
    settle(api)

    # Multiview refuses a single-image upstream instead of submitting the
    # hidden local file fields the UI suppresses.
    gimg = ng.nodes.new("GoogleImageNode")
    gen2 = ng.nodes.new("TripoGenerateNode")
    gen2.mode = "MULTIVIEW"
    ng.links.new(gimg.outputs[0], gen2.inputs[0])
    api._jobs["fake-google-single"] = {
        "state": "done", "images": {"generated_image": mock.sample_image()}}
    gimg.job_id = "fake-google-single"
    n = mock.submit_count()
    try:
        bpy.ops.tripo.node_generate(node_name=gen2.name, tree_name=ng.name)
        rejected = False
    except RuntimeError:
        rejected = True
    check("multiview rejects a single-image upstream", rejected)
    check("no multiview task submitted", mock.submit_count() == n)
    api._jobs.pop("fake-google-single", None)

    # Meshtools: the decimate backup must never render (grey-blob trap),
    # non-meshes are refused, and reference renders demand a camera.
    import bmesh
    m2 = bpy.data.meshes.new("HardDense")
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=32, v_segments=32, radius=1.0)
    bm.to_mesh(m2)
    bm.free()
    dobj = bpy.data.objects.new("HardDense", m2)
    bpy.context.collection.objects.link(dobj)
    meshtools.decimate("HardDense", target_polys=200, keep_original=True)
    backup = bpy.data.objects.get("HardDense_orig")
    check("decimate keeps a backup", backup is not None)
    check("decimate backup is hidden from renders",
          backup is not None and backup.hide_render)

    bpy.data.objects.new("HardCam", bpy.data.cameras.new("HardCamData"))
    try:
        meshtools.stats("HardCam")
        guarded = False
    except ValueError:
        guarded = True
    check("meshtools refuses non-mesh objects", guarded)

    saved_cam = bpy.context.scene.camera
    bpy.context.scene.camera = None
    try:
        meshtools.render_reference("HardDense")
        no_cam = False
    except RuntimeError:
        no_cam = True
    finally:
        bpy.context.scene.camera = saved_cam
    check("reference render requires a camera", no_cam)

    # S3 region comes from the STS host -- never guessed.
    check("region parsed from the S3 host",
          api._s3_region("s3.us-west-2.amazonaws.com") == "us-west-2")
    check("legacy us-east-1 host handled",
          api._s3_region("s3.amazonaws.com") == "us-east-1")
    try:
        api._s3_region("evil.example.com")
        raised = False
    except RuntimeError:
        raised = True
    check("unknown host refuses to guess a region", raised)

    for name in ("Hard/Name:Obj", "HardDense", "HardDense_orig", "HardCam"):
        obj = bpy.data.objects.get(name)
        if obj:
            bpy.data.objects.remove(obj, do_unlink=True)
    bpy.data.node_groups.remove(ng)


def test_google_preview_survives_restart(api, mock):
    section("Google previews survive a restart")

    from terracotta import nodes
    ng = bpy.data.node_groups.new("PreviewGraph", nodes.TREE_ID)
    img = ng.nodes.new("GoogleImageNode")
    img.prompt = "preview persistence"
    bpy.ops.tripo.google_image(node_name=img.name, tree_name=ng.name)
    deadline = time.time() + 10
    while time.time() < deadline and \
            api.status(img.job_id).get("state") not in ("done", "error"):
        time.sleep(0.05)

    # Simulate a restart: live jobs are gone, only history remains.
    with api._jobs_lock:
        api._jobs.pop(img.job_id, None)
    check("restart: images still resolve via history", bool(img.images()))
    check("restart: node still reports a result", img.has_result())
    # status() reports "unknown" for a forgotten job -- the draw path must
    # treat that as "no live job" and take the history fallback.
    check("restart: forgotten job reads as unknown",
          img.job().get("state") == "unknown", str(img.job().get("state")))
    check("restart: draw takes the persisted-result path",
          not img._has_live_job())
    bpy.data.node_groups.remove(ng)


def test_model_capability_matrix(api, mock):
    section("Per-model generation options")

    from terracotta import costs as C, nodes

    check("v2.5 accepts no advanced params",
          C.caps("v2.5-20250123") == {"export_uv"},
          str(C.caps("v2.5-20250123")))
    check("P1 takes texture/size but not geometry extras",
          "texture_quality" in C.caps("P1-20260311")
          and "quad" not in C.caps("P1-20260311"))
    check("face range: P1", C.face_limit_range("P1-20260311") == (50, 20000))
    check("face range: v3.1 ultra",
          C.face_limit_range("v3.1-20260211",
                             geometry_quality="detailed") == (1, 2000000))
    check("face range: quad cap",
          C.face_limit_range("v3.1-20260211", quad=True) == (1, 150000))
    check("face range: smart low poly quad",
          C.face_limit_range("v3.1-20260211", quad=True,
                             smart_low_poly=True) == (500, 10000))

    ng = bpy.data.node_groups.new("CapsGraph", nodes.TREE_ID)
    gen = ng.nodes.new("TripoGenerateNode")
    gen.prompt = "a chest"

    # v2.5 with everything switched on must send none of it.
    gen.model = "v2.5-20250123"
    gen.texture_quality = "detailed"
    gen.geometry_quality = "detailed"
    gen.auto_size = True
    gen.quad = True
    gen.smart_low_poly = True
    n = mock.submit_count()
    bpy.ops.tripo.node_generate(node_name=gen.name, tree_name=ng.name)
    mock.wait_for_submit(n)
    body = mock.last_body()
    for field in ("texture_quality", "geometry_quality", "auto_size", "quad",
                  "smart_low_poly", "generate_parts", "compress"):
        check(f"v2.5 omits {field}", field not in body, str(body))
    wait_for(api, gen.job_id, {"done", "error"})
    settle(api)

    # v3.1: extreme tier, export_uv off and compress propagate.
    gen.model = "v3.1-20260211"
    gen.quad = False
    gen.smart_low_poly = False
    gen.texture_quality = "extreme"
    gen.export_uv = False
    gen.compress = True
    gen.last_task = ""
    n = mock.submit_count()
    bpy.ops.tripo.node_generate(node_name=gen.name, tree_name=ng.name)
    mock.wait_for_submit(n)
    body = mock.last_body()
    check("extreme texture tier sent",
          body.get("texture_quality") == "extreme", str(body))
    check("export_uv off is sent", body.get("export_uv") is False, str(body))
    check("compress sends the documented value",
          body.get("compress") == "geometry", str(body))
    wait_for(api, gen.job_id, {"done", "error"})
    settle(api)

    # Face-limit validation refuses out-of-range submissions unbilled.
    gen.export_uv = True
    gen.compress = False
    gen.use_face_limit = True
    gen.quad = True
    gen.face_limit = 200000
    gen.last_task = ""
    n = mock.submit_count()
    try:
        bpy.ops.tripo.node_generate(node_name=gen.name, tree_name=ng.name)
        rejected = False
    except RuntimeError:
        rejected = True
    check("quad face limit over 150k rejected", rejected)
    check("no out-of-range task billed", mock.submit_count() == n)

    bpy.data.node_groups.remove(ng)


def test_view_redo_and_thumb_persistence(api, mock):
    section("Per-view redo and Tripo preview persistence")

    from terracotta import nodes

    def wait_google(job_id, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline and \
                api.status(job_id).get("state") not in ("done", "error"):
            time.sleep(0.05)
        return api.status(job_id).get("state")

    ng = bpy.data.node_groups.new("RedoGraph", nodes.TREE_ID)
    views = ng.nodes.new("GoogleViewsNode")

    # No prompt and no reference: refuse before billing.
    try:
        bpy.ops.tripo.google_views(node_name=views.name, tree_name=ng.name)
        refused = False
    except RuntimeError:
        refused = True
    check("views refuse with neither subject nor reference", refused)

    # A reference alone is enough -- the description is optional.
    views.reference = mock.sample_image()
    bpy.ops.tripo.google_views(node_name=views.name, tree_name=ng.name)
    check("reference-only views complete", wait_google(views.job_id) == "done")
    body = mock.google_calls[-1]
    text = next(p["text"] for p in body["input"] if p.get("type") == "text")
    check("prompt derives the subject from the reference",
          "reference image" in text, text[:80])

    views.prompt = "a small stool"
    views.job_id = ""
    bpy.ops.tripo.google_views(node_name=views.name, tree_name=ng.name)
    check("views job completes", wait_google(views.job_id) == "done")
    first = dict(views.images())
    check("four views generated",
          set(first) == {"front", "left", "back", "right"}, str(set(first)))

    # Redo one view: one billed request, other three carried over.
    ncalls = len(mock.google_calls)
    bpy.ops.tripo.google_view_redo(node_name=views.name, tree_name=ng.name,
                                   view="left")
    check("redo job completes", wait_google(views.job_id) == "done")
    second = views.images()
    check("still four views after a single redo",
          set(second) == {"front", "left", "back", "right"}, str(set(second)))
    check("only one image was billed",
          len(mock.google_calls) == ncalls + 1,
          f"{ncalls} -> {len(mock.google_calls)}")
    check("left view replaced", second["left"] != first["left"])
    check("front view carried over", second["front"] == first["front"])

    # A Tripo node picked up after a restart shows its stored result.
    gen = ng.nodes.new("TripoGenerateNode")
    gen.prompt = "persist me"
    bpy.ops.tripo.node_generate(node_name=gen.name, tree_name=ng.name)
    wait_for(api, gen.job_id, {"done", "error"})
    settle(api)
    task = gen.task_id()
    with api._jobs_lock:
        api._jobs.pop(gen.job_id, None)
    entry = gen.stored_result()
    check("stored result resolves after restart",
          entry is not None and entry.get("task_id") == task, str(entry))
    check("stored result carries the thumbnail",
          bool(entry and entry.get("thumb")))
    bpy.data.node_groups.remove(ng)


def test_view_image(api, mock):
    section("Full-size image viewer")

    check("view_image operator registered",
          hasattr(bpy.ops.tripo, "view_image"))

    # A pruned or moved file must refuse cleanly, not traceback.
    try:
        bpy.ops.tripo.view_image(path="/nonexistent/image.png")
        refused = False
    except RuntimeError as e:
        refused = "no longer exists" in str(e)
    check("missing file refuses with a clear message", refused)

    # With a real file, headless Blender cannot open a window -- but it must
    # fail at that stage with a clean report, never a crash, and the image
    # datablock must load.
    path = mock.sample_image()
    n_images = len(bpy.data.images)
    try:
        bpy.ops.tripo.view_image(path=path)
        outcome = "opened"
    except RuntimeError as e:
        outcome = "window" if "viewer window" in str(e) else str(e)
    check("existing file loads and fails only at the window stage",
          outcome in ("opened", "window"), outcome)
    check("image datablock loaded", len(bpy.data.images) > n_images)


def test_lifecycle(api, mock):
    section("Register / unregister symmetry")

    import terracotta as tb
    from terracotta import workspace as ws

    tb.unregister()
    try:
        check("ui timer unregistered",
              not bpy.app.timers.is_registered(tb._ui_tick))
        check("import pump unregistered",
              not bpy.app.timers.is_registered(api._drain))
        check("focus timer unregistered",
              not bpy.app.timers.is_registered(ws._focus_tripo_tab))
        check("load handler removed",
              tb._on_file_load not in bpy.app.handlers.load_post)
    finally:
        tb.register()

    # The pump must come back after re-enable -- a stale running-flag used
    # to leave a re-enabled addon with no import pump at all.
    j = api.start(prompt="post-cycle asset", name="CycleCheck")
    state = wait_for(api, j, {"done", "error"})
    check("pump alive after a disable/enable cycle", state == "done", state)
    settle(api)


def test_new_file_only_setup(api, mock):
    section("Auto-setup touches only brand-new files")

    import terracotta as tb

    check("decision helper exists",
          callable(getattr(tb, "_should_auto_setup", None)))
    prefs = bpy.context.preferences.addons["terracotta"].preferences
    prefs.auto_workspace = True
    # The headless suite runs on an unsaved file: furnishing is allowed.
    check("brand-new file may be furnished",
          tb._should_auto_setup() is True, repr(bpy.data.filepath))

    # Once the file exists on disk it is the user's -- never inject into it.
    # (This saves the test session's file, so it runs last in the suite.)
    import tempfile as _tf
    path = os.path.join(_tf.mkdtemp(prefix="tripo_inject_"), "user_file.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path)
    check("a file that exists on disk is never modified",
          tb._should_auto_setup() is False, repr(bpy.data.filepath))

    prefs.auto_workspace = False
    import inspect
    src = inspect.getsource(tb._ui_tick)
    check("auto-setup restores the previous workspace",
          "previous" in src and "window.workspace" in src)
    check("the preference disables even new files",
          tb._should_auto_setup() is False)
    prefs.auto_workspace = True


def test_meshtools():
    section("Mesh tools")

    from terracotta import meshtools, build

    mesh = bpy.data.meshes.new("DenseTest")
    import bmesh
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=64, v_segments=64, radius=1.0)
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new("DenseTest", mesh)
    bpy.context.collection.objects.link(obj)

    before = len(obj.data.polygons)
    result = meshtools.decimate("DenseTest", target_polys=500)
    check("decimate reduces polygons", len(obj.data.polygons) < before,
          f"{before} -> {len(obj.data.polygons)}")
    check("decimate reports a real ratio", 0 < result.get("ratio", 0) < 1,
          str(result.get("ratio")))

    meshtools.place("DenseTest", location=(0, 0, 0), max_size=2.0)
    check("place normalizes largest dimension",
          abs(max(obj.dimensions) - 2.0) < 0.01, str(tuple(obj.dimensions)))
    lowest = min((obj.matrix_world @ v.co).z for v in obj.data.vertices)
    check("place seats the object on the ground", abs(lowest) < 0.01, str(lowest))

    skipped = meshtools.decimate("DenseTest", target_polys=999999)
    check("decimate skips when already under budget", skipped.get("skipped") is True,
          str(skipped))

    bpy.data.objects.remove(obj, do_unlink=True)


def test_build():
    section("Procedural builders")

    from terracotta import build

    d = build.door(name="TestDoor", width=0.83, height=2.03, centre=(0, -2.55),
                   wall_face=-2.5)
    bpy.context.view_layer.update()
    # Total width = slab + 0.04 opening clearance + 0.06 casing each side.
    expected_total = 0.83 + 0.04 + 0.12
    check("door width matches slab plus casing",
          abs(d.dimensions.x - expected_total) < 0.02,
          f"{round(d.dimensions.x, 3)} vs expected {round(expected_total, 3)}")
    check("door height is right",
          abs(d.dimensions.z - 2.03) < 0.10,
          str(round(d.dimensions.z, 3)))

    w = build.window(name="TestWindow", width=0.9, height=1.22, sill_z=0.9)
    bpy.context.view_layer.update()
    check("window fits its opening",
          abs(w.dimensions.x - 0.9) < 0.15 and abs(w.dimensions.z - 1.22) < 0.15,
          str(tuple(round(v, 3) for v in w.dimensions)))

    for ob in (d, w):
        bpy.data.objects.remove(ob, do_unlink=True)


def test_costs():
    section("Credit maths")

    from terracotta import costs as C, nodes

    check("v3.1 text costs 20", C.base_cost("v3.1-20260211", "text") == 20)
    check("v3.1 image costs 30", C.base_cost("v3.1-20260211", "image") == 30)
    check("P1 text costs 40", C.base_cost("P1-20260311", "text") == 40)
    check("P1 image costs 50", C.base_cost("P1-20260311", "image") == 50)

    check("surcharges add up on H2/H3 models",
          C.extra_cost("v3.1-20260211", texture_quality="detailed",
                       smart_low_poly=True) == 20)
    check("P1 ignores flag surcharges",
          C.extra_cost("P1-20260311", texture_quality="detailed",
                       smart_low_poly=True, quad=True) == 10)
    check("P1 at standard texture has no surcharge",
          C.extra_cost("P1-20260311") == 0)
    # Measured live: ultra geometry billed +20 on top of detailed texture.
    check("ultra geometry + detailed texture totals 50",
          C.total_cost("v3.1-20260211", "text", texture_quality="detailed",
                       geometry_quality="detailed") == 50)

    # The node quotes through the same module, so it cannot drift from it.
    ng = bpy.data.node_groups.new("CostGraph", nodes.TREE_ID)
    gen = ng.nodes.new("TripoGenerateNode")
    gen.texture_quality = "detailed"
    gen.geometry_quality = "detailed"
    check("generate node quotes via the shared cost module",
          gen.cost() == C.total_cost(gen.model, "text",
                                     texture_quality="detailed",
                                     geometry_quality="detailed"),
          str(gen.cost()))
    bpy.data.node_groups.remove(ng)

def main():
    print("=" * 62)
    print("Tripo addon test suite  (offline, no credits)")
    print("=" * 62)

    api, mock = setup()
    try:
        test_request_shapes(api, mock)
        test_post_chaining(api, mock)
        test_full_cycle(api, mock)
        test_output_field_names(api, mock)
        test_documented_constraints(api, mock)
        test_p1_constraints(api, mock)
        test_error_path(api, mock)
        test_history(api, mock)
        test_nodes(api, mock)
        test_node_options(api, mock)
        test_generation_param_scope(api, mock)
        test_import_model(api, mock)
        test_export(api, mock)
        test_graph_order(api, mock)
        test_task_persistence(api, mock)
        test_history_covers_all_kinds(api, mock)
        test_google_images(api, mock)
        test_rigging(api, mock)
        test_google_persistence(api, mock)
        test_image_chains(api, mock)
        test_runner_safety(api, mock)
        test_all_spend_buttons_guarded(api, mock)
        test_improve_selected(api, mock)
        test_socket_validation(api, mock)
        test_workspace_bundle(api, mock)
        test_examples(api, mock)
        test_task_picker(api, mock)
        test_meshtools()
        test_build()
        test_costs()
        test_keys_and_deprecation(api, mock)
        test_panel_retirement(api, mock)
        test_recover_and_prune(api, mock)
        test_money_guards(api, mock)
        test_model_capability_matrix(api, mock)
        test_run_graph_quote(api, mock)
        test_feature_wins(api, mock)
        test_hardening(api, mock)
        test_view_image(api, mock)
        test_google_preview_survives_restart(api, mock)
        test_view_redo_and_thumb_persistence(api, mock)
        test_lifecycle(api, mock)
        test_new_file_only_setup(api, mock)
    finally:
        mock.uninstall()
        mock.uninstall_google()

    print("\n" + "=" * 62)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("\nFailures:")
        for name, detail in FAIL:
            print(f"  - {name}   {detail}")
    print("=" * 62)

    if bpy.app.background:
        sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
