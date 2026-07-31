"""Cleanup utilities for AI-generated meshes.

Tripo returns very dense meshes (v3.1 routinely >1M polys), which is far more
than a working scene needs and painful on limited RAM. These helpers get them
down to a usable budget and place them sanely, without touching topology you
still want.
"""

import bpy


def _mesh(name):
    obj = bpy.data.objects[name]
    if obj.type != "MESH":
        raise ValueError(f"'{name}' is a {obj.type}, not a mesh")
    return obj


def stats(name):
    obj = _mesh(name)
    return {
        "name": obj.name,
        "polys": len(obj.data.polygons),
        "verts": len(obj.data.vertices),
        "dims": tuple(round(d, 3) for d in obj.dimensions),
        "location": tuple(round(v, 3) for v in obj.location),
        "materials": [m.name for m in obj.data.materials],
    }


def decimate(name, target_polys=50000, keep_original=False):
    """Collapse-decimate down to roughly target_polys.

    Collapse preserves UVs better than 'unsubdivide' here, which matters since
    the whole point of these assets is their baked texture.
    """
    obj = _mesh(name)
    before = len(obj.data.polygons)

    if before <= target_polys:
        return {"skipped": True, "polys": before, "reason": "already under target"}

    if keep_original:
        backup = obj.copy()
        backup.data = obj.data.copy()
        backup.name = f"{name}_orig"
        bpy.context.collection.objects.link(backup)
        backup.hide_viewport = True
        # hide_viewport does not affect renders -- without this the backup
        # z-fights the decimated copy in every render (see CLAUDE.md).
        backup.hide_render = True

    ratio = max(0.001, min(1.0, target_polys / before))

    mod = obj.modifiers.new(name="TripoDecimate", type="DECIMATE")
    mod.decimate_type = "COLLAPSE"
    mod.ratio = ratio

    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.modifier_apply(modifier=mod.name)

    # Read `ratio` from the local, not mod.ratio -- the modifier is freed by
    # modifier_apply and reports 0.0 afterwards.
    return {
        "before": before,
        "after": len(obj.data.polygons),
        "ratio": round(ratio, 4),
    }


def place(name, location=(0, 0, 0), max_size=None, on_ground=True):
    """Position an object, optionally normalizing its size and sitting it on Z=0.

    Generated assets arrive at arbitrary scale with the origin wherever the
    exporter left it, so 'put it next to that other thing' needs this first.
    """
    obj = _mesh(name)
    if not obj.data.vertices:
        raise ValueError(f"'{name}' has no vertices to place")

    if max_size:
        current = max(obj.dimensions)
        if current > 0:
            factor = max_size / current
            obj.scale = tuple(s * factor for s in obj.scale)
            bpy.context.view_layer.update()

    # Origin to the volume centre makes later transforms predictable.
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.ops.object.origin_set(type="ORIGIN_CENTER_OF_VOLUME")

    obj.location = location
    if on_ground:
        bpy.context.view_layer.update()
        lowest = min(
            (obj.matrix_world @ v.co).z for v in obj.data.vertices
        )
        obj.location.z += location[2] - lowest

    bpy.context.view_layer.update()
    return stats(name)


def frame(names=None, shading="MATERIAL"):
    """Frame one or more objects in the viewport for screenshotting.

    Pass nothing to frame every visible mesh. Deliberately not view_all(), which
    includes the camera and lights and zooms the subject down to nothing.
    """
    if isinstance(names, str):
        names = [names]
    if not names:
        names = [
            o.name for o in bpy.data.objects
            if o.type == "MESH" and not o.hide_viewport
        ]

    for area in bpy.context.screen.areas:
        if area.type != "VIEW_3D":
            continue
        win = [r for r in area.regions if r.type == "WINDOW"][0]
        with bpy.context.temp_override(area=area, region=win):
            bpy.ops.object.select_all(action="DESELECT")
            for n in names:
                obj = bpy.data.objects.get(n)
                if obj and not obj.hide_viewport:
                    obj.select_set(True)
                    bpy.context.view_layer.objects.active = obj
            bpy.ops.view3d.view_selected()
            bpy.ops.object.select_all(action="DESELECT")  # no orange outlines
            area.spaces.active.shading.type = shading
    return names


def render_reference(name, out_path=None, resolution=768, margin=1.25):
    """Render one object alone on a neutral background, for use as an
    image-to-3D reference.

    Isolates via hide_render, not hide_viewport: the latter still renders, which
    silently puts occluders in your reference image.
    """
    import os
    import tempfile

    scene = bpy.context.scene
    target = _mesh(name)
    if scene.camera is None:
        raise RuntimeError(
            "The scene has no camera -- add one to render a reference image")

    saved_render = {o.name: o.hide_render for o in bpy.data.objects}
    saved = {
        "engine": scene.render.engine,
        "res_x": scene.render.resolution_x,
        "res_y": scene.render.resolution_y,
        "filepath": scene.render.filepath,
        "film": scene.render.film_transparent,
        "fmt": scene.render.image_settings.file_format,
        "cam": scene.camera.name if scene.camera else None,
        "cam_loc": tuple(scene.camera.location) if scene.camera else None,
        "cam_rot": tuple(scene.camera.rotation_euler) if scene.camera else None,
    }

    try:
        for obj in bpy.data.objects:
            if obj.type == "MESH":
                obj.hide_render = obj.name != name

        out_path = out_path or os.path.join(tempfile.gettempdir(), f"ref_{name}.png")
        scene.render.resolution_x = scene.render.resolution_y = resolution
        scene.render.image_settings.file_format = "PNG"
        scene.render.filepath = out_path
        scene.render.film_transparent = False

        # Frame the subject from a three-quarter view at a distance that fits it.
        cam = scene.camera
        size = max(target.dimensions) or 1.0
        centre = target.matrix_world.translation
        offset = size * margin * 2.0
        cam.location = (
            centre.x + offset * 0.7,
            centre.y - offset * 0.7,
            centre.z + offset * 0.5,
        )
        direction = centre - cam.location
        cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()

        bpy.ops.render.render(write_still=True)
        return out_path
    finally:
        for obj_name, hidden in saved_render.items():
            obj = bpy.data.objects.get(obj_name)
            if obj:
                obj.hide_render = hidden
        scene.render.engine = saved["engine"]
        scene.render.resolution_x = saved["res_x"]
        scene.render.resolution_y = saved["res_y"]
        scene.render.filepath = saved["filepath"]
        scene.render.film_transparent = saved["film"]
        scene.render.image_settings.file_format = saved["fmt"]
        if saved["cam"]:
            cam = bpy.data.objects[saved["cam"]]
            cam.location = saved["cam_loc"]
            cam.rotation_euler = saved["cam_rot"]


def cleanup(name, target_polys=50000, max_size=None, location=(0, 0, 0)):
    """The usual post-import pass: decimate, scale, seat on the ground."""
    result = decimate(name, target_polys=target_polys)
    result["placed"] = place(name, location=location, max_size=max_size)
    return result
