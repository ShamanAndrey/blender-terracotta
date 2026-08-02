"""Small shared helpers: node lookup, redraw, panel cost maths."""

import bpy

from . import nodes




def _find_node(name, tree_name=""):
    """Locate a node in exactly the named tree.

    Node names are unique only within a tree -- every bundled example holds a
    "Generate 3D". An earlier version fell back to a global first-match
    search when the tree lookup failed, which could execute (and bill) a
    different graph's node. No fallback: unknown tree means no node.
    """
    tree = bpy.data.node_groups.get(tree_name)
    if tree is not None and tree.bl_idname == nodes.TREE_ID:
        return tree.nodes.get(name)
    return None


def _wrap(text, width):
    return [text[i:i + width] for i in range(0, min(len(text), width * 4), width)] or [""]


def _tag_redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type in {"VIEW_3D", "NODE_EDITOR"}:
                area.tag_redraw()
