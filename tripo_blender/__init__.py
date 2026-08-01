"""Tripo for Blender -- AI asset generation as a node graph.

A fully standalone Blender addon: generate 3D models (Tripo) and concept
images (Google Gemini), chain retopology / texturing / segmentation / rigging
/ animation / export, and manage it all in the Generate workspace.

No other addon is required. The blender-mcp bridge used during development
only lets an AI assistant drive Blender remotely; nothing in this package
depends on it.
"""

bl_info = {
    "name": "Tripo for Blender",
    "author": "local",
    "version": (2, 2),
    # UILayout.progress, temp_override and wm.obj_import are all 4.0+ --
    # on 3.x the Jobs panel would crash exactly while a paid job is running.
    "blender": (4, 0, 0),
    "location": "Generate workspace (top bar)",
    "description": "AI 3D generation node graph: Tripo models, Google images, "
                   "rig, animate, export",
    "category": "3D View",
}

import bpy

from . import (api, build, costs, google_api, meshtools, nodes, operators,
               panels, previews, runner, utils, workspace)

# Re-exports: the public surface predates the module split, and tests plus any
# user scripts address these through the package. Keep them working.
from .utils import _find_node, _tag_redraw, _wrap
from .workspace import (WORKSPACE_NAME, workspace_blend, examples_blend,
                        example_names, load_example, _seed_example,
                        _workspace_menu, TRIPO_OT_add_workspace,
                        TRIPO_OT_load_example, TRIPO_MT_examples)
from .runner import (_NODE_OPS, _graph_order, _node_header,
                     TRIPO_OT_run_graph, TRIPO_OT_recover_tasks)
from .operators import (_task_enum_items, _object_menu,
                        TRIPO_OT_setup_keys, TRIPO_OT_validate_key,
                        TRIPO_OT_reimport, TRIPO_OT_clear_history,
                        TRIPO_OT_mark_asset, TRIPO_OT_node_generate,
                        TRIPO_OT_node_process, TRIPO_OT_node_import,
                        TRIPO_OT_node_upload, TRIPO_OT_node_export,
                        TRIPO_OT_google_image, TRIPO_OT_google_views,
                        TRIPO_OT_node_prerig, TRIPO_OT_node_rig,
                        TRIPO_OT_node_animate, TRIPO_OT_improve_selected,
                        TRIPO_OT_pick_task, TRIPO_OT_clear_node_task,
                        TRIPO_OT_add_reference, TRIPO_OT_remove_reference,
                        TRIPO_OT_render_reference, TRIPO_OT_optimize,
                        TRIPO_OT_view_image, TRIPO_OT_google_view_redo,
                        TRIPO_OT_anim_add, TRIPO_OT_anim_remove,
                        TRIPO_OT_frame, TRIPO_OT_refresh_balance,
                        TRIPO_OT_clear_jobs)
from .panels import TripoPanel, TRIPO_PT_jobs, TRIPO_PT_library


class TripoPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    tripo_api_key: bpy.props.StringProperty(
        name="Tripo API Key",
        description="API key from the Tripo console",
        subtype="PASSWORD",
        default="",
    )

    google_api_key: bpy.props.StringProperty(
        name="Google API Key",
        description="Gemini key for image generation. Images are billed by "
                    "Google on your own account, not in Tripo credits",
        subtype="PASSWORD",
        default="",
    )
    auto_workspace: bpy.props.BoolProperty(
        name="Set up brand-new files",
        description="Add the Generate workspace and a starter example to "
                    "newly created files. Files that already exist on disk "
                    "are never modified -- use the workspace + menu or the "
                    "sidebar button there",
        default=True,
    )

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "tripo_api_key")
        col.prop(self, "google_api_key")
        col.label(text="Tripo: 3D generation. Google: images.", icon="INFO")
        col.prop(self, "auto_workspace")
        col.operator("tripo.add_workspace", icon="WINDOW")


_balance_primed = False
_workspace_checked = False


def _should_auto_setup():
    """Auto-add the workspace and example only on a brand-new file.

    Opening someone's existing project must not modify it: injecting our
    workspace marks their file dirty and, once saved, bakes our data into it
    forever. A new unsaved file (empty filepath) is ours to furnish; anything
    already on disk gets the workspace + menu entry and the sidebar button
    instead.
    """
    if bpy.data.filepath:
        return False
    prefs = bpy.context.preferences.addons[__package__].preferences
    return bool(prefs.auto_workspace)


@bpy.app.handlers.persistent
def _on_file_load(_dummy):
    """Re-arm the workspace check when a file is opened or created.

    Workspaces live in the .blend, so a new file has none of ours. The check
    is a module global that only ran once per session, which meant the
    workspace silently stopped appearing after the first file.
    """
    global _workspace_checked
    _workspace_checked = False


def _ui_tick():
    """Keep the Jobs panel live while anything is in flight, and prime the
    credit balance once so the panel doesn't open reading zero."""
    global _balance_primed, _workspace_checked

    # Data can't be touched during register(), so set up from the timer
    # instead -- by now the file is fully loaded.
    if not _workspace_checked:
        _workspace_checked = True
        try:
            if _should_auto_setup():
                if WORKSPACE_NAME not in bpy.data.workspaces:
                    bpy.ops.tripo.add_workspace()
                _seed_example()
        except Exception as e:
            print(f"[tripo] workspace auto-add skipped: {e!r}")

    if not _balance_primed:
        _balance_primed = True

        def apply(info):
            try:
                bpy.context.scene.tripo_balance = int(info.get("balance") or 0)
                _tag_redraw()
            except Exception:
                pass   # no scene yet, or a key that doesn't validate

        api.refresh_balance_async(apply)

    # Sync finished task ids onto their nodes. The post-import hook only fires
    # for jobs that import something -- an upload or an export doesn't, so
    # those nodes would otherwise forget their task across a reload.
    _sync_node_tasks()

    if api.active_jobs():
        _tag_redraw()
    return 1.0


def _sync_node_tasks():
    """Keep each node's remembered task id current.

    Runs on the UI timer so a task id is captured as soon as its job finishes,
    including for uploads and exports, which never import anything and so
    never trigger the post-import hook.
    """
    for tree in bpy.data.node_groups:
        if tree.bl_idname != nodes.TREE_ID:
            continue
        for node in tree.nodes:
            job_id = getattr(node, "job_id", "")
            if not job_id or getattr(node, "last_task", ""):
                continue
            job = api.status(job_id)
            if job.get("state") == "done" and job.get("task_id"):
                node.last_task = job["task_id"]


def _remember_task(job_id, job):
    """Write the finished task id back onto whichever node launched it.

    In-memory jobs vanish on reload; the node graph is saved with the file, so
    without this a node forgets what it produced and shows "unknown".
    """
    task = job.get("task_id")
    if not task:
        return
    for tree in bpy.data.node_groups:
        if tree.bl_idname != nodes.TREE_ID:
            continue
        for node in tree.nodes:
            if getattr(node, "job_id", None) == job_id:
                node.last_task = task


def _launching_node(job_id):
    """The node that started this job, if any."""
    for tree in bpy.data.node_groups:
        if tree.bl_idname != nodes.TREE_ID:
            continue
        for node in tree.nodes:
            if getattr(node, "job_id", None) == job_id:
                return node
    return None


def _on_import(job_id, job):
    """Post-import placement and cleanup, driven by the launching node.

    The Import node's own settings (cursor placement, decimation, library
    marking) apply here. They used to be written to a dict nothing read while
    the removed panel's scene settings won instead -- exactly the two-surface
    drift that got the panel deleted.
    """
    _remember_task(job_id, job)
    node = _launching_node(job_id)

    objects = [bpy.data.objects[n] for n in (job.get("objects") or [])
               if n in bpy.data.objects]
    if not objects:
        return

    at_cursor = getattr(node, "at_cursor", True) if node is not None else True
    if at_cursor:
        cursor = bpy.context.scene.cursor.location
        for obj in objects:
            obj.location = (obj.location.x + cursor.x,
                            obj.location.y + cursor.y,
                            obj.location.z + cursor.z)

    target = int(getattr(node, "decimate_to", 0) or 0) if node is not None else 0
    if target > 0:
        for obj in objects:
            try:
                meshtools.decimate(obj.name, target_polys=target)
            except Exception as e:
                print(f"[tripo] decimate failed for {obj.name}: {e!r}")

    coll_name = (getattr(node, "collection_name", "") or "").strip() \
        if node is not None else ""
    if coll_name:
        coll = bpy.data.collections.get(coll_name)
        if coll is None:
            coll = bpy.data.collections.new(coll_name)
        scene_children = bpy.context.scene.collection.children
        if coll.name not in {c.name for c in scene_children}:
            scene_children.link(coll)
        for obj in objects:
            for c in list(obj.users_collection):
                c.objects.unlink(obj)
            coll.objects.link(obj)

    if node is not None and getattr(node, "mark_asset", False):
        for obj in objects:
            try:
                obj.asset_mark()
                obj.asset_generate_preview()
                if obj.asset_data:
                    obj.asset_data.tags.new("tripo")
            except Exception as e:
                print(f"[tripo] asset mark failed for {obj.name}: {e!r}")

    _tag_redraw()


classes = (
    (TripoPreferences,)
    + operators.classes
    + workspace.classes
    + runner.classes
    + panels.classes
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    # Everything else lives on nodes; the scene carries only the credit
    # display the Account node reads.
    bpy.types.Scene.tripo_balance = bpy.props.IntProperty(name="Credits",
                                                          default=0)

    previews.register()
    nodes.register()
    if _on_file_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_file_load)
    bpy.types.TOPBAR_MT_workspace_menu.append(_workspace_menu)
    bpy.types.NODE_HT_header.append(_node_header)
    bpy.types.VIEW3D_MT_object_context_menu.append(_object_menu)
    api.post_import_hook = _on_import
    api._ensure_timer()
    if not bpy.app.timers.is_registered(_ui_tick):
        bpy.app.timers.register(_ui_tick, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_ui_tick):
        bpy.app.timers.unregister(_ui_tick)
    api.stop_timer()
    if bpy.app.timers.is_registered(workspace._focus_tripo_tab):
        bpy.app.timers.unregister(workspace._focus_tripo_tab)
    api.post_import_hook = None
    for holder, fn in ((bpy.types.TOPBAR_MT_workspace_menu, _workspace_menu),
                       (bpy.types.NODE_HT_header, _node_header),
                       (bpy.types.VIEW3D_MT_object_context_menu, _object_menu)):
        try:
            holder.remove(fn)
        except Exception:
            pass
    nodes.unregister()
    if _on_file_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_file_load)
    previews.unregister()

    if hasattr(bpy.types.Scene, "tripo_balance"):
        del bpy.types.Scene.tripo_balance

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
