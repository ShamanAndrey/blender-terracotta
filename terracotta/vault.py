"""Graph vault: sidecar backups of every Terracotta node tree.

Blender's one unfixable data-loss hole for custom nodes: open a file while
the addon isn't loaded and every node becomes Undefined; one save then
strips all their settings. No addon code can warn at that moment -- the
addon isn't running. So instead, every save of a healthy file also writes
the graphs to a JSON sidecar outside the .blend, and a stripped file can be
restored from it with one click.

The vault deliberately refuses to record damaged trees: a corrupted save
must never overwrite the last good snapshot.
"""

import hashlib
import json
import os

import bpy

from . import nodes

# Builtin node attributes handled structurally, not as serialized props.
_SKIP_PROPS = {"rna_type", "name", "label", "location", "width", "height",
               "color", "use_custom_color", "select", "parent", "mute",
               "show_options", "show_preview", "hide", "show_texture",
               "width_hidden", "warning_propagation"}


def vault_dir():
    d = bpy.utils.user_resource("CONFIG", path="tripo", create=True)
    path = os.path.join(d, "vault")
    os.makedirs(path, exist_ok=True)
    return path


def _vault_path(filepath):
    stem = os.path.splitext(os.path.basename(filepath))[0][:40]
    digest = hashlib.sha1(filepath.encode("utf-8", "replace")).hexdigest()[:16]
    return os.path.join(vault_dir(), f"{stem}.{digest}.json")


def _prop_names(node):
    """The addon-defined, writable properties of a node."""
    out = []
    for prop in node.bl_rna.properties:
        if prop.identifier in _SKIP_PROPS:
            continue
        if not prop.is_runtime:
            continue
        # Collections are always RNA-readonly (mutated, never assigned).
        if prop.is_readonly and prop.type != "COLLECTION":
            continue
        out.append((prop.identifier, prop.type))
    return out


def _serialize_node(node):
    data = {
        "bl_idname": node.bl_idname,
        "name": node.name,
        "label": node.label,
        "location": list(node.location),
        "width": node.width,
        "parent": node.parent.name if node.parent else None,
    }
    if node.bl_idname == "NodeFrame":
        data["height"] = node.height
        data["use_custom_color"] = node.use_custom_color
        data["color"] = list(node.color)
        data["text"] = node.text.as_string() if node.text else None
        return data

    props = {}
    for ident, ptype in _prop_names(node):
        try:
            value = getattr(node, ident)
        except Exception:
            continue
        if ptype in {"STRING", "ENUM", "BOOLEAN", "INT", "FLOAT"}:
            props[ident] = value
        elif ptype == "POINTER":
            props[ident] = getattr(value, "name", None)
        elif ptype == "COLLECTION":
            items = []
            for item in value:
                entry = {}
                for sub in item.bl_rna.properties:
                    if sub.is_runtime and not sub.is_readonly:
                        entry[sub.identifier] = getattr(item, sub.identifier)
                items.append(entry)
            props[ident] = items
    data["props"] = props
    return data


def _serialize_tree(tree):
    data = {"name": tree.name, "nodes": [], "links": []}
    for node in tree.nodes:
        data["nodes"].append(_serialize_node(node))
    for link in tree.links:
        data["links"].append({
            "from_node": link.from_node.name,
            "from_socket": list(link.from_node.outputs).index(link.from_socket),
            "to_node": link.to_node.name,
            "to_socket": list(link.to_node.inputs).index(link.to_socket),
        })
    return data


def _is_damaged(tree):
    """Whether this tree carries Undefined nodes (addon-was-absent save)."""
    if tree.bl_idname == "NodeTreeUndefined":
        return True
    return any(n.bl_idname in ("NodeUndefined", "") for n in tree.nodes)


def write_vault(filepath=None):
    """Snapshot every healthy Terracotta tree for this file.

    Returns the number of trees written, or None when writing was refused
    (no file path, no trees, or damage detected -- a damaged save must not
    overwrite the last good snapshot).
    """
    filepath = filepath or bpy.data.filepath
    if not filepath:
        return None
    trees = [t for t in bpy.data.node_groups if t.bl_idname == nodes.TREE_ID]
    damaged = [t for t in bpy.data.node_groups if _is_damaged(t)]
    if damaged:
        print("[terracotta] vault: damaged trees present -- keeping the "
              "previous snapshot untouched")
        return None
    if not trees:
        return None
    payload = {
        "filepath": filepath,
        "trees": [_serialize_tree(t) for t in trees],
    }
    path = _vault_path(filepath)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=1)
    os.replace(tmp, path)
    return len(trees)


def read_vault(filepath=None):
    filepath = filepath or bpy.data.filepath
    if not filepath:
        return None
    try:
        with open(_vault_path(filepath)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def damage_detected():
    """A tree in this file is Undefined and the vault knows its name."""
    entry = read_vault()
    if not entry:
        return False
    vaulted = {t["name"] for t in entry.get("trees", [])}
    return any(t.name in vaulted and _is_damaged(t)
               for t in bpy.data.node_groups)


def _restore_tree(data):
    """Rebuild one tree from its vault record. Returns (tree, skipped)."""
    name = data["name"]
    existing = bpy.data.node_groups.get(name)
    if existing is not None:
        if _is_damaged(existing):
            bpy.data.node_groups.remove(existing)
        else:
            # Never clobber a healthy tree -- restore beside it.
            name = f"{name} (vault)"

    tree = bpy.data.node_groups.new(name, nodes.TREE_ID)
    made = {}
    skipped = []

    # Frames first so other nodes can parent to them.
    ordered = sorted(data["nodes"],
                     key=lambda n: n["bl_idname"] != "NodeFrame")
    for nd in ordered:
        try:
            node = tree.nodes.new(nd["bl_idname"])
        except Exception:
            skipped.append(nd["bl_idname"])
            continue
        node.name = nd["name"]
        node.label = nd.get("label", "")
        node.location = nd.get("location", (0, 0))
        node.width = nd.get("width", node.width)
        made[nd["name"]] = node

        if nd["bl_idname"] == "NodeFrame":
            node.height = nd.get("height", node.height)
            node.use_custom_color = nd.get("use_custom_color", False)
            if nd.get("color"):
                node.color = nd["color"]
            if nd.get("text"):
                body = bpy.data.texts.new(f"{nd['name']} notes")
                body.write(nd["text"])
                node.text = body
            continue

        for ident, value in (nd.get("props") or {}).items():
            try:
                prop = node.bl_rna.properties.get(ident)
                if prop is None:
                    continue
                if prop.type == "POINTER":
                    if value:
                        setattr(node, ident, bpy.data.objects.get(value))
                elif prop.type == "COLLECTION":
                    coll = getattr(node, ident)
                    coll.clear()
                    for entry in value or []:
                        item = coll.add()
                        for k, v in entry.items():
                            setattr(item, k, v)
                else:
                    setattr(node, ident, value)
            except Exception as e:
                print(f"[terracotta] vault: {nd['name']}.{ident} "
                      f"not restored: {e!r}")

    for nd in data["nodes"]:
        if nd.get("parent") and nd["name"] in made and nd["parent"] in made:
            made[nd["name"]].parent = made[nd["parent"]]

    for ld in data.get("links", []):
        a = made.get(ld["from_node"])
        b = made.get(ld["to_node"])
        try:
            if a is not None and b is not None:
                tree.links.new(a.outputs[ld["from_socket"]],
                               b.inputs[ld["to_socket"]])
        except Exception as e:
            print(f"[terracotta] vault: link "
                  f"{ld['from_node']}->{ld['to_node']} not restored: {e!r}")
    return tree, skipped


def restore(filepath=None):
    """Restore every vaulted tree for this file. Returns (restored, skipped)."""
    entry = read_vault(filepath)
    if not entry:
        return [], []
    restored, skipped = [], []
    for data in entry.get("trees", []):
        tree, missing = _restore_tree(data)
        restored.append(tree)
        skipped.extend(missing)
    return restored, skipped


@bpy.app.handlers.persistent
def _on_save(_dummy):
    try:
        write_vault()
    except Exception as e:
        # A vault failure must never block or break a user's save.
        print(f"[terracotta] vault write failed: {e!r}")


class TRIPO_OT_vault_restore(bpy.types.Operator):
    bl_idname = "tripo.vault_restore"
    bl_label = "Restore Graphs from Vault"
    bl_description = ("Rebuild this file's Terracotta graphs from the last "
                      "healthy save's sidecar backup")

    def execute(self, context):
        restored, skipped = restore()
        if not restored:
            self.report({"ERROR"}, "No vault snapshot exists for this file")
            return {"CANCELLED"}
        msg = f"Restored {len(restored)} graph(s)"
        if skipped:
            msg += f"; unknown node types skipped: {', '.join(set(skipped))}"
        self.report({"INFO"}, msg)
        # Show the first restored tree wherever a Terracotta editor is open.
        for screen in bpy.data.screens:
            for area in screen.areas:
                if area.type == "NODE_EDITOR":
                    for space in area.spaces:
                        if space.type == "NODE_EDITOR" and \
                                space.tree_type == nodes.TREE_ID:
                            space.node_tree = restored[0]
                    area.tag_redraw()
        return {"FINISHED"}


classes = (TRIPO_OT_vault_restore,)
