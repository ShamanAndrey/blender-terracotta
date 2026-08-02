"""Safely reinstall the addon into a running Blender.

Three crashes came from reloading while the UI still referenced things being
torn down. Blender does not tolerate this: unregistering a node class whose
nodes are on screen, or deleting a datablock an editor is displaying, segfaults
immediately.

The order below is the fix, and each step exists because skipping it crashed:

1. Detach node trees from every editor  -- editors must not hold a tree whose
   node classes are about to disappear.
2. Leave any custom workspace           -- its screen may reference our panels.
3. Unregister, purge modules, reinstall, re-register.
4. Restore the API key, then reattach the trees.

Run with:
    exec(open("/path/to/repo/reload_addon.py").read())
"""

import importlib
import linecache
import sys

import bpy

import os
ZIP = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "terracotta.zip")
ADDON = "terracotta"


def reload_addon(zip_path=ZIP):
    prefs = bpy.context.preferences.addons.get(ADDON)
    saved_key = prefs.preferences.tripo_api_key if prefs else ""

    # 1. Detach trees from every node editor, remembering what was shown.
    attached = []
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type != "NODE_EDITOR":
                continue
            space = area.spaces.active
            tree = getattr(space, "node_tree", None)
            if tree is not None and tree.bl_idname.startswith("Tripo"):
                attached.append((screen.name, area.spaces.active, tree.name))
                space.node_tree = None

    # 2. Step off any workspace whose screen shows our UI.
    previous_ws = bpy.context.window.workspace.name
    if previous_ws not in ("Layout",) and "Layout" in bpy.data.workspaces:
        bpy.context.window.workspace = bpy.data.workspaces["Layout"]

    # 3. Swap the code.
    try:
        stale = sys.modules.get(ADDON)
        if stale is not None:
            stale.unregister()
    except Exception as e:
        print(f"[reload] unregister said: {e!r}")

    bpy.ops.preferences.addon_install(filepath=zip_path, overwrite=True)
    for mod in [m for m in list(sys.modules) if m.startswith(ADDON)]:
        del sys.modules[mod]
    linecache.clearcache()
    importlib.invalidate_caches()

    module = importlib.import_module(ADDON)
    module.register()

    # 4. Restore state.
    prefs = bpy.context.preferences.addons.get(ADDON)
    if prefs and not prefs.preferences.tripo_api_key and saved_key:
        prefs.preferences.tripo_api_key = saved_key
    bpy.ops.wm.save_userpref()

    for _screen, space, tree_name in attached:
        tree = bpy.data.node_groups.get(tree_name)
        if tree is not None:
            space.node_tree = tree

    if previous_ws in bpy.data.workspaces:
        bpy.context.window.workspace = bpy.data.workspaces[previous_ws]

    print(f"[reload] {ADDON} reloaded; "
          f"{len(attached)} node editor(s) reattached; "
          f"key {'kept' if prefs and prefs.preferences.tripo_api_key else 'MISSING'}")
    return True


reload_addon()
