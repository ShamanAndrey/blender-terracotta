"""One-shot splitter: break the 2000-line __init__.py into focused modules.

Extraction is ast-driven from the real file rather than retyped, so nothing is
transcribed by hand. Every top-level name must be explicitly assigned to a
destination; an unknown name aborts the split instead of silently dropping code
-- which is how `_cached_image_for` was lost in an earlier scripted edit.
"""

import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "terracotta")
SRC = os.path.join(PKG, "__init__.py")

DEST = {
    # utils.py -- tiny shared helpers, no UI
    "MODEL_ITEMS": "utils", "COSTS": "utils", "DEFAULT_COST": "utils",
    "cost_for": "utils", "_gen_kwargs": "utils", "_extra_cost": "utils",
    "_find_node": "utils", "_wrap": "utils", "_tag_redraw": "utils",
    # operators.py
    "TRIPO_OT_generate_text": "operators", "TRIPO_OT_generate_image": "operators",
    "TRIPO_OT_render_reference": "operators", "TRIPO_OT_optimize": "operators",
    "TRIPO_OT_frame": "operators", "TRIPO_OT_refresh_balance": "operators",
    "TRIPO_OT_clear_jobs": "operators", "TRIPO_OT_generate_multiview": "operators",
    "TRIPO_OT_validate_key": "operators", "TRIPO_OT_reimport": "operators",
    "TRIPO_OT_clear_history": "operators", "TRIPO_OT_mark_asset": "operators",
    "TRIPO_OT_node_generate": "operators", "TRIPO_OT_node_process": "operators",
    "TRIPO_OT_node_import": "operators", "TRIPO_OT_node_upload": "operators",
    "TRIPO_OT_node_export": "operators", "TRIPO_OT_node_prerig": "operators",
    "TRIPO_OT_google_image": "operators", "TRIPO_OT_google_views": "operators",
    "TRIPO_OT_node_rig": "operators", "TRIPO_OT_node_animate": "operators",
    "TRIPO_OT_improve_selected": "operators", "_task_enum_cache": "operators",
    "_task_enum_items": "operators", "TRIPO_OT_pick_task": "operators",
    "TRIPO_OT_clear_node_task": "operators", "TRIPO_OT_add_reference": "operators",
    "TRIPO_OT_remove_reference": "operators", "_object_menu": "operators",
    # workspace.py -- workspace + bundled examples
    "WORKSPACE_NAME": "workspace", "workspace_blend": "workspace",
    "TRIPO_OT_add_workspace": "workspace", "_focus_tripo_tab": "workspace",
    "_workspace_menu": "workspace", "examples_blend": "workspace",
    "example_names": "workspace", "_example_items": "workspace",
    "_example_cache": "workspace", "load_example": "workspace",
    "TRIPO_OT_load_example": "workspace", "TRIPO_MT_examples": "workspace",
    "_seed_example": "workspace",
    # runner.py -- graph execution
    "_NODE_OPS": "runner", "_graph_order": "runner",
    "TRIPO_OT_run_graph": "runner", "TRIPO_OT_recover_tasks": "runner",
    "_node_header": "runner",
    # panels.py
    "TripoPanel": "panels", "TRIPO_PT_main": "panels",
    "TRIPO_PT_advanced": "panels", "TRIPO_PT_jobs": "panels",
    "TRIPO_PT_library": "panels", "TRIPO_PT_cleanup": "panels",
    # stay in __init__ (extracted, reassembled)
    "TripoPreferences": "init", "_balance_primed": "init",
    "_workspace_checked": "init", "_on_file_load": "init", "_ui_tick": "init",
    "_sync_node_tasks": "init", "_remember_task": "init", "_on_import": "init",
    "register": "init", "unregister": "init",
    # dropped and rebuilt by hand
    "classes": "drop", "bl_info": "drop",
}

HEADERS = {
    "utils": '''"""Small shared helpers: node lookup, redraw, panel cost maths."""

import bpy

from . import api, costs, nodes
''',
    "operators": '''"""Every operator: panel generation, node actions, references, history."""

import os

import bpy

from . import api, costs, google_api, meshtools, nodes
from .utils import (_extra_cost, _find_node, _gen_kwargs, _tag_redraw,
                    cost_for)
from .workspace import WORKSPACE_NAME
''',
    "workspace": '''"""The Generate workspace and the bundled example graphs.

Both are datablocks appended from .blend files shipped inside the addon --
appending prebuilt data is the crash-safe alternative to scripting layout
changes against a live session.
"""

import os

import bpy

from . import nodes
from .utils import _tag_redraw
''',
    "runner": '''"""Graph execution: run a node tree in dependency order."""

import bpy

from . import api, nodes
from .utils import _tag_redraw
''',
    "panels": '''"""Sidebar panels."""

import bpy

from . import api, previews
from .utils import _extra_cost, _wrap, cost_for
''',
}

BANNER_PREFIXES = ("# ---", "# Preferences", "# Operators", "# Panels",
                   "# Wiring", "# Graph execution", "# Bundled example")


def main():
    src = open(SRC).read()
    lines = src.split("\n")
    tree = ast.parse(src)

    segments = []      # (name, text) in source order
    prev_end = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            prev_end = node.end_lineno
            continue
        if isinstance(node, ast.Expr):          # module docstring
            prev_end = node.end_lineno
            continue

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            name = node.name
            start = node.lineno
            if node.decorator_list:
                start = min(d.lineno for d in node.decorator_list)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and \
                isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            start = node.lineno
        else:
            print(f"UNHANDLED top-level statement at line {node.lineno}")
            sys.exit(1)

        # Carry attached comments (the gap since the previous statement),
        # minus section banners, so explanations travel with their code.
        gap = [l for l in lines[prev_end:start - 1]
               if l.strip().startswith("#")
               and not any(l.strip().startswith(p) for p in BANNER_PREFIXES)]
        body = "\n".join(lines[start - 1:node.end_lineno])
        text = ("\n".join(gap) + "\n" if gap else "") + body

        if name not in DEST:
            print(f"UNASSIGNED name: {name} (line {node.lineno})")
            sys.exit(1)
        segments.append((name, text))
        prev_end = node.end_lineno

    found = {n for n, _ in segments}
    missing = set(DEST) - found - {"classes", "bl_info"}
    # bl_info/classes are Assigns and will be found; assert everything mapped
    missing = set(DEST) - found
    if missing:
        print(f"EXPECTED but not found: {sorted(missing)}")
        sys.exit(1)

    buckets = {}
    for name, text in segments:
        buckets.setdefault(DEST[name], []).append((name, text))

    # ---- targeted transforms on moved code --------------------------------
    def transform(module, name, text):
        if module == "panels" and name == "TRIPO_PT_main":
            # __name__ is "terracotta.panels" after the move; the addon
            # prefs are registered under the package name.
            text = text.replace("addons[__name__]", "addons[__package__]")
        if name == "TRIPO_OT_generate_image":
            # de-duplicate a line an earlier scripted edit doubled
            text = text.replace(
                "        scene.tripo_last_job = job\n"
                "        scene.tripo_last_job = job\n",
                "        scene.tripo_last_job = job\n")
        if name == "_gen_kwargs":
            # `style` is a stylize_model parameter, not a generation one --
            # sending it on generation is invalid (and was invisible: the
            # panel row for it was removed long ago).
            text = text.replace(
                '    if scene.tripo_style != "NONE":\n'
                '        kw["style"] = scene.tripo_style\n', "")
        return text

    # ---- write the five new modules ---------------------------------------
    for module in ("utils", "operators", "workspace", "runner", "panels"):
        parts = [HEADERS[module]]
        reg_classes = []
        for name, text in buckets.get(module, []):
            parts.append("\n\n" + transform(module, name, text))
            if text.lstrip().startswith("class ") and name != "TripoPanel":
                reg_classes.append(name)
        if reg_classes:
            joined = "\n    ".join(f"{c}," for c in reg_classes)
            parts.append(f"\n\n\nclasses = (\n    {joined}\n)")
        path = os.path.join(PKG, f"{module}.py")
        with open(path, "w") as f:
            f.write("\n".join(p.rstrip("\n") for p in parts) + "\n")
        print(f"wrote {module}.py: {len(buckets.get(module, []))} segments, "
              f"{len(reg_classes)} registered classes")

    # ---- rebuild __init__.py ----------------------------------------------
    init_seg = {n: t for n, t in buckets["init"]}

    header = '''"""Tripo for Blender -- generate 3D assets from text or images, then clean them up.

A companion to the blender-mcp addon rather than a patch to it, so updating that
addon can't wipe this. Everything here is also callable from Claude over MCP:

    from terracotta import api, meshtools
"""

bl_info = {
    "name": "Tripo for Blender",
    "author": "local",
    "version": (2, 1),
    "blender": (3, 0, 0),
    "location": "Generate workspace / View3D > Sidebar > Tripo",
    "description": "Text-to-3D and image-to-3D generation via Tripo, with mesh cleanup",
    "category": "3D View",
}

import bpy

from . import (api, build, costs, google_api, meshtools, nodes, operators,
               panels, previews, runner, utils, workspace)

# Re-exports: the public surface predates the module split, and tests plus any
# user scripts address these through the package. Keep them working.
from .utils import (MODEL_ITEMS, COSTS, DEFAULT_COST, cost_for, _extra_cost,
                    _gen_kwargs, _find_node, _tag_redraw, _wrap)
from .workspace import (WORKSPACE_NAME, workspace_blend, examples_blend,
                        example_names, load_example, _seed_example,
                        _workspace_menu, TRIPO_OT_add_workspace,
                        TRIPO_OT_load_example, TRIPO_MT_examples)
from .runner import (_NODE_OPS, _graph_order, _node_header,
                     TRIPO_OT_run_graph, TRIPO_OT_recover_tasks)
from .operators import (_task_enum_items, _object_menu,
                        TRIPO_OT_generate_text, TRIPO_OT_generate_image,
                        TRIPO_OT_generate_multiview, TRIPO_OT_validate_key,
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
                        TRIPO_OT_frame, TRIPO_OT_refresh_balance,
                        TRIPO_OT_clear_jobs)
from .panels import (TripoPanel, TRIPO_PT_main, TRIPO_PT_advanced,
                     TRIPO_PT_jobs, TRIPO_PT_library, TRIPO_PT_cleanup)
'''

    order = ["TripoPreferences", "_balance_primed", "_workspace_checked",
             "_on_file_load", "_ui_tick", "_sync_node_tasks",
             "_remember_task", "_on_import"]
    body = "\n\n\n".join(init_seg[n] for n in order)

    classes_block = '''classes = (
    (TripoPreferences,)
    + operators.classes
    + workspace.classes
    + runner.classes
    + panels.classes
)'''
    # register/unregister verbatim; they iterate `classes`, which is now a
    # concatenation of per-module tuples in the same registration order.
    tail = init_seg["register"] + "\n\n\n" + init_seg["unregister"]

    with open(SRC, "w") as f:
        f.write(header + "\n\n" + body + "\n\n\n" + classes_block
                + "\n\n\n" + tail + "\n")
    print(f"rewrote __init__.py")


if __name__ == "__main__":
    main()
