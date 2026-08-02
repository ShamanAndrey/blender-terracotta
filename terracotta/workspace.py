"""The Generate workspace and the bundled example graphs.

Both are datablocks appended from .blend files shipped inside the addon --
appending prebuilt data is the crash-safe alternative to scripting layout
changes against a live session.
"""

import os

import bpy

from . import nodes
from .utils import _tag_redraw


WORKSPACE_NAME = "Generate"


def workspace_blend():
    return os.path.join(os.path.dirname(__file__), "workspaces.blend")


class TRIPO_OT_add_workspace(bpy.types.Operator):
    bl_idname = "tripo.add_workspace"
    bl_label = "Generate"
    bl_description = ("Add the Generate workspace: viewport, asset browser, "
                      "reference image editor and the Tripo panel")

    def execute(self, context):
        existing = bpy.data.workspaces.get(WORKSPACE_NAME)
        if existing:
            context.window.workspace = existing
            return {"FINISHED"}

        path = workspace_blend()
        if not os.path.exists(path):
            self.report({"ERROR"}, f"Bundled workspace missing: {path}")
            return {"CANCELLED"}

        # Appending a prebuilt datablock, rather than scripting area splits on
        # a live session -- the latter is fragile and can take Blender down.
        try:
            bpy.ops.workspace.append_activate(idname=WORKSPACE_NAME, filepath=path)
        except Exception as e:
            self.report({"ERROR"}, f"Could not append workspace: {e}")
            return {"CANCELLED"}

        # active_panel_category is a runtime region property and doesn't
        # survive being written to a .blend, so restore it once the regions
        # have actually been drawn.
        global _focus_attempts
        _focus_attempts = 0
        bpy.app.timers.register(_focus_tripo_tab, first_interval=0.3)
        return {"FINISHED"}


_focus_attempts = 0


def _focus_tripo_tab():
    """Point the Generate workspace's sidebar at our panels."""
    global _focus_attempts
    ws = bpy.data.workspaces.get(WORKSPACE_NAME)
    if not ws:
        return None
    for screen in ws.screens:
        for area in screen.areas:
            if area.type != "NODE_EDITOR":
                continue
            for region in area.regions:
                if region.type != "UI":
                    continue
                try:
                    with bpy.context.temp_override(screen=screen, area=area,
                                                   region=region):
                        region.active_panel_category = "AI"
                except Exception:
                    # Regions aren't drawn yet; retry briefly -- but never
                    # forever. A permanent failure must not leave a
                    # half-second timer spinning for the whole session.
                    _focus_attempts += 1
                    if _focus_attempts >= 20:
                        print("[tripo] gave up focusing the Tripo sidebar tab")
                        return None
                    return 0.5
            area.tag_redraw()
    return None


def _workspace_menu(self, context):
    self.layout.operator("tripo.add_workspace", text="Generate", icon="SHADERFX")


def examples_blend():
    return os.path.join(os.path.dirname(__file__), "examples.blend")


def example_names():
    """Names of the bundled examples, without loading them."""
    path = examples_blend()
    if not os.path.exists(path):
        return []
    try:
        with bpy.data.libraries.load(path) as (src, _dst):
            return sorted(src.node_groups)
    except Exception as e:
        print(f"[tripo] could not read examples: {e!r}")
        return []


def _example_items(self, context):
    global _example_cache
    names = example_names()
    _example_cache = [(n, n.replace("-", "\u2013"), f"Load '{n}'")
                      for n in names] or [("", "No examples bundled", "")]
    return _example_cache


_example_cache = []


def load_example(name):
    """Append one example graph, or return the copy already present."""
    existing = bpy.data.node_groups.get(name)
    if existing is not None and existing.bl_idname == nodes.TREE_ID:
        return existing

    path = examples_blend()
    before = {ng.name for ng in bpy.data.node_groups}
    with bpy.data.libraries.load(path) as (src, dst):
        if name not in src.node_groups:
            raise RuntimeError(f"No example called '{name}'")
        dst.node_groups = [name]
    new = [ng for ng in bpy.data.node_groups if ng.name not in before]
    return new[0] if new else bpy.data.node_groups.get(name)


class TRIPO_OT_load_example(bpy.types.Operator):
    bl_idname = "tripo.load_example"
    bl_label = "Load Example"
    bl_description = "Open a ready-made example graph"

    name: bpy.props.StringProperty()

    def execute(self, context):
        try:
            tree = load_example(self.name)
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        if tree is None:
            self.report({"ERROR"}, "Example could not be loaded")
            return {"CANCELLED"}

        space = context.space_data
        if space and getattr(space, "tree_type", None) == nodes.TREE_ID:
            space.node_tree = tree
        else:
            for area in context.screen.areas:
                if area.type == "NODE_EDITOR":
                    area.spaces.active.node_tree = tree
        self.report({"INFO"}, f"Loaded '{tree.name}'")
        _tag_redraw()
        return {"FINISHED"}


class TRIPO_MT_examples(bpy.types.Menu):
    bl_idname = "TRIPO_MT_examples"
    bl_label = "Examples"

    def draw(self, context):
        layout = self.layout
        names = example_names()
        if not names:
            layout.label(text="No examples bundled")
            return
        for name in names:
            op = layout.operator("tripo.load_example",
                                 text=name.split("-", 1)[-1].strip(),
                                 icon="NODETREE")
            op.name = name


def _seed_example():
    """Show a worked example instead of a blank canvas on a fresh file.

    Only when the file has no Tripo graph of its own -- never overwrite what
    someone is already working on.
    """
    if any(ng.bl_idname == nodes.TREE_ID for ng in bpy.data.node_groups):
        return
    names = example_names()
    if not names:
        return
    try:
        tree = load_example(names[0])
    except Exception as e:
        print(f"[tripo] could not seed an example: {e!r}")
        return
    ws = bpy.data.workspaces.get(WORKSPACE_NAME)
    if ws and tree:
        for screen in ws.screens:
            for area in screen.areas:
                if area.type == "NODE_EDITOR" and not area.spaces.active.node_tree:
                    area.spaces.active.node_tree = tree



classes = (
    TRIPO_OT_add_workspace,
    TRIPO_OT_load_example,
    TRIPO_MT_examples,
)
