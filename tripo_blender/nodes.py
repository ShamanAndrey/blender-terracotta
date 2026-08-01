"""Node-based generation graph.

Generation is naturally a graph: a prompt or image feeds a generation, whose
output feeds retopology, texturing or segmentation, which feeds an import.
Every Tripo post-processing task takes an `original_model_task_id`, so an edge
here is literally that parameter -- the graph and the API chain are one shape.

Nodes carry their own job id and read live state from the api module, so a
running generation shows progress and its preview render in place.
"""

import bpy

from . import api, costs, google_api, previews

TREE_ID = "TripoNodeTree"


def _is_tripo_tree(ntree):
    return getattr(ntree, "bl_idname", "") == TREE_ID


# --------------------------------------------------------------------------
# Tree and sockets
# --------------------------------------------------------------------------

class TripoNodeTree(bpy.types.NodeTree):
    bl_idname = TREE_ID
    bl_label = "Tripo Generator"
    bl_icon = "SHADERFX"

    # Removing a link from inside update() triggers update() again. Without
    # a guard that recurses until the stack blows -- a silent segfault.
    _validating = False

    def update(self):
        """Drop links between mismatched socket types.

        Blender allows any socket to connect to any other; type compatibility
        is the tree's job. Without this an Asset can be wired into a Rig input,
        which looks fine and then fails server-side with a confusing 400.
        """
        if _registering:
            # Blender re-validates trees while classes register; touching
            # half-registered link sockets here crashes outright (seen on
            # disable -> re-enable with a Tripo tree in the file).
            return
        cls = type(self)
        if cls._validating:
            return
        cls._validating = True
        try:
            invalid = [
                link for link in self.links
                if link.from_socket.bl_idname != link.to_socket.bl_idname
                # Reroute sockets are virtual; validating them deleted every
                # link through a reroute with no message at all.
                and link.from_node.bl_idname != "NodeReroute"
                and link.to_node.bl_idname != "NodeReroute"]
            for link in invalid:
                self.links.remove(link)
        finally:
            cls._validating = False


def _upstream_node(node):
    """The producing node behind a node's first input, following reroutes."""
    if not node.inputs:
        return None
    socket = node.inputs[0]
    for _ in range(33):   # bounded: a reroute cycle must not hang the UI
        if not socket.links:
            return None
        upstream = socket.links[0].from_node
        if upstream.bl_idname != "NodeReroute":
            return upstream
        if not upstream.inputs:
            return None
        socket = upstream.inputs[0]
    return None


class TripoAssetSocket(bpy.types.NodeSocket):
    """Carries a finished Tripo task, by id."""

    bl_idname = "TripoAssetSocket"
    bl_label = "Asset"

    def draw(self, context, layout, node, text):
        layout.label(text=text)

    def draw_color(self, context, node):
        return (0.9, 0.35, 0.35, 1.0)


class TripoRigSocket(bpy.types.NodeSocket):
    """Carries a rigged model. Distinct from Asset so a bare mesh cannot be
    wired into animation -- the API rejects that, and a socket that refuses to
    connect is better than a 400 after the fact."""

    bl_idname = "TripoRigSocket"
    bl_label = "Rig"

    def draw(self, context, layout, node, text):
        layout.label(text=text)

    def draw_color(self, context, node):
        return (0.95, 0.7, 0.25, 1.0)


class TripoImageSocket(bpy.types.NodeSocket):
    """Carries an image task (a single render, or a four-view set)."""

    bl_idname = "TripoImageSocket"
    bl_label = "Image"

    def draw(self, context, layout, node, text):
        layout.label(text=text)

    def draw_color(self, context, node):
        return (0.35, 0.6, 0.9, 1.0)


# --------------------------------------------------------------------------
# Base
# --------------------------------------------------------------------------

class TripoNode:
    job_id: bpy.props.StringProperty()
    last_task: bpy.props.StringProperty()   # survives reloads; job dict doesn't

    @classmethod
    def poll(cls, ntree):
        return _is_tripo_tree(ntree)

    def job(self):
        return api.status(self.job_id) if self.job_id else {}

    def is_busy(self):
        """True while this node's job is in flight.

        Action buttons must be disabled while busy: a second click submits a
        second task and charges for it again.
        """
        return self.job().get("state") in (
            "submitting", "uploading", "running", "downloading", "importing")

    def action_row(self, layout):
        """A row for the node's action button, disabled while busy."""
        row = layout.row()
        row.scale_y = 1.2
        row.enabled = not self.is_busy()
        return row

    def task_id(self):
        """Finished task for this node, preferring live state, then the id we
        stored -- in-memory jobs are lost on addon reload but the graph isn't."""
        job = self.job()
        if job.get("state") == "done" and job.get("task_id"):
            return job["task_id"]
        return self.last_task or None

    def upstream_task(self):
        node = _upstream_node(self)
        getter = getattr(node, "task_id", None) if node else None
        return getter() if callable(getter) else None

    def upstream_images(self):
        """Image files produced by a connected image node, if any.

        Google results are local files, not Tripo tasks, so image-fed nodes
        consume paths rather than task ids.
        """
        node = _upstream_node(self)
        getter = getattr(node, "images", None) if node else None
        if callable(getter):
            return getter() or {}
        return {}

    def has_result(self):
        """True when this node already holds a finished result.

        Run Graph skips such nodes -- re-running finished work charges for it
        again. A node's own button always forces a run.
        """
        if self.job().get("state") == "done":
            return True
        return bool(self.task_id())

    def stored_result(self):
        """The history row for this node's task, if any.

        The live job dies with the session; the history row (and its cached
        thumbnail) is what survives a restart or a task picked from history.
        """
        task = self.task_id()
        if not task:
            return None
        for entry in api.history(limit=999):
            if entry.get("task_id") == task:
                return entry
        return None

    def draw_status(self, layout):
        job = self.job()
        state = job.get("state")

        if not state or state == "unknown":
            # Job dict cleared (reload/restart) but the node may still know
            # its task -- show the persisted result instead of a bare label.
            entry = self.stored_result()
            if entry is not None:
                thumb = entry.get("thumb")
                icon = previews.icon_id(f"hist_{entry['task_id']}", thumb)                     if thumb else 0
                if icon:
                    layout.template_icon(icon_value=icon,
                                         scale=max(4.0, self.width / 20.0))
                row = layout.row()
                row.label(text="complete", icon="CHECKMARK")
                if entry.get("credits"):
                    row.label(text=f"{int(entry['credits'])}cr")
                if thumb:
                    op = row.operator("tripo.view_image", text="",
                                      icon="ZOOM_IN", emboss=False)
                    op.path = thumb
            elif self.last_task:
                row = layout.row()
                row.label(text="ready (earlier session)", icon="CHECKMARK")
            return

        if state == "error":
            box = layout.box()
            box.alert = True
            msg = str(job.get("message", "failed"))
            for i in range(0, min(len(msg), 140), 34):
                box.label(text=msg[i:i + 34])
            return

        if state == "done":
            thumb = previews.icon_id(f"node_{self.job_id}", job.get("thumb"))
            if thumb:
                # Scale from the node's width so the preview fills the body
                # edge to edge -- and resizing the node resizes the preview.
                layout.template_icon(icon_value=thumb,
                                     scale=max(4.0, self.width / 20.0))
            row = layout.row()
            row.label(text=", ".join(job.get("objects") or []) or "complete",
                      icon="CHECKMARK")
            if job.get("credits"):
                row.label(text=f"{int(job['credits'])}cr")
            if job.get("thumb"):
                op = row.operator("tripo.view_image", text="", icon="ZOOM_IN",
                                  emboss=False)
                op.path = job["thumb"]
            return

        pct = job.get("progress")
        if pct is not None:
            layout.progress(factor=pct / 100.0, text=f"{state}  {pct}%", type="BAR")
        else:
            layout.label(text=state, icon="SORTTIME")
        if job.get("eta"):
            layout.label(text=f"~{int(job['eta'])}s left")


# --------------------------------------------------------------------------
# Generate
# --------------------------------------------------------------------------

class TripoGenerateNode(TripoNode, bpy.types.Node):
    bl_idname = "TripoGenerateNode"
    bl_label = "Generate 3D"
    bl_icon = "SHADERFX"
    bl_width_default = 290

    mode: bpy.props.EnumProperty(
        name="Mode",
        items=[("TEXT", "Text", "From a description"),
               ("IMAGE", "Image", "From one reference image"),
               ("MULTIVIEW", "Views", "From up to four views")],
        default="TEXT")
    prompt: bpy.props.StringProperty(name="Prompt", description="Max 1024 characters")
    negative: bpy.props.StringProperty(name="Avoid", description="Max 255 characters")
    image: bpy.props.StringProperty(name="Image", subtype="FILE_PATH")
    mv_front: bpy.props.StringProperty(name="Front", subtype="FILE_PATH")
    mv_left: bpy.props.StringProperty(name="Left", subtype="FILE_PATH")
    mv_back: bpy.props.StringProperty(name="Back", subtype="FILE_PATH")
    mv_right: bpy.props.StringProperty(name="Right", subtype="FILE_PATH")

    model: bpy.props.EnumProperty(name="Model", items=costs.MODEL_ITEMS,
                                  default="v3.1-20260211")
    asset_name: bpy.props.StringProperty(name="Name")

    show_advanced: bpy.props.BoolProperty(name="Options", default=False)
    texture: bpy.props.BoolProperty(name="Texture", default=True)
    pbr: bpy.props.BoolProperty(name="PBR", default=True)
    texture_quality: bpy.props.EnumProperty(
        name="Texture", items=costs.TEXTURE_QUALITY_ITEMS, default="standard")
    geometry_quality: bpy.props.EnumProperty(
        name="Geometry", items=costs.GEOMETRY_QUALITY_ITEMS, default="standard")
    auto_size: bpy.props.BoolProperty(
        name="Real-world scale",
        description="Output in metres instead of unit-normalised")
    texture_alignment: bpy.props.EnumProperty(
        name="Alignment",
        items=[("original_image", "To image", "Match the source image"),
               ("geometry", "To geometry", "Match the 3D structure")],
        default="original_image")
    orientation: bpy.props.EnumProperty(
        name="Orientation",
        items=[("default", "Default", "As generated"),
               ("align_image", "Align to image",
                "Rotate to match the image. Needs texture on")],
        default="default")
    autofix: bpy.props.BoolProperty(
        name="Autofix image",
        description="Let Tripo clean up a blurry or incomplete reference")
    use_face_limit: bpy.props.BoolProperty(name="Cap faces")
    face_limit: bpy.props.IntProperty(name="Faces", default=10000, min=50,
                                      max=2000000)
    quad: bpy.props.BoolProperty(name="Quad mesh",
                                 description="Returns FBX instead of glTF")
    smart_low_poly: bpy.props.BoolProperty(name="Low poly")
    generate_parts: bpy.props.BoolProperty(
        name="Split into parts",
        description="Forces texture and PBR off -- an API restriction")
    export_uv: bpy.props.BoolProperty(
        name="Unwrap UVs", default=True,
        description="UV unwrapping during generation. Off is faster and "
                    "smaller; texturing unwraps later anyway")
    compress: bpy.props.BoolProperty(
        name="Compress (meshopt)",
        description="Meshopt geometry compression for a smaller file")
    use_seeds: bpy.props.BoolProperty(name="Fix seed")
    seed: bpy.props.IntProperty(name="Seed", default=42, min=0)
    existing_task: bpy.props.StringProperty(
        name="Existing model",
        description="Reuse a model you already generated instead of paying again")

    def init(self, context):
        self.inputs.new("TripoImageSocket", "Image")
        self.outputs.new("TripoAssetSocket", "Asset")

    def task_id(self):
        if self.existing_task.strip():
            return self.existing_task.strip()
        return super().task_id()

    def upstream_image(self):
        """A connected image node supplies files instead of local paths."""
        imgs = self.upstream_images()
        return imgs.get("generated_image") or imgs.get("front")

    def cost(self):
        kind = "text" if self.mode == "TEXT" else "image"
        return costs.total_cost(
            self.model, kind,
            texture_quality=self.texture_quality,
            geometry_quality=self.geometry_quality,
            smart_low_poly=self.smart_low_poly,
            generate_parts=self.generate_parts,
            quad=self.quad)

    def draw_buttons(self, context, layout):
        if not api.has_key():
            box = layout.box()
            box.alert = True
            box.label(text="No Tripo API key", icon="KEY_DEHLT")
            box.operator("tripo.setup_keys", text="Set API keys",
                         icon="KEY_HLT")

        p1 = costs.is_p1(self.model)

        layout.prop(self, "mode", expand=True)
        if self.mode == "TEXT":
            layout.prop(self, "prompt", text="")
        elif self.mode == "IMAGE":
            if self.upstream_image():
                layout.label(text="Using upstream image", icon="LINKED")
            else:
                layout.prop(self, "image", text="")
        else:
            up = self.upstream_images()
            if up.get("front"):
                layout.label(text="Using upstream views", icon="LINKED")
            elif up:
                box = layout.box()
                box.alert = True
                box.label(text="Upstream is a single image", icon="ERROR")
                box.label(text="Connect a Views node, or use Image mode")
            else:
                col = layout.column(align=True)
                for slot in ("mv_front", "mv_left", "mv_back", "mv_right"):
                    col.prop(self, slot)

        layout.prop(self, "model", text="")
        layout.prop(self, "asset_name", text="Name")

        layout.prop(self, "show_advanced", toggle=True,
                    icon="TRIA_DOWN" if self.show_advanced else "TRIA_RIGHT")
        if self.show_advanced:
            # Each model only sees the options it actually accepts -- the
            # docs' per-model matrix, not one UI pretending to fit all.
            allowed = costs.caps(self.model)
            box = layout.box()
            box.prop(self, "negative", text="Avoid")

            row = box.row(align=True)
            row.prop(self, "texture", toggle=True)
            row.prop(self, "pbr", toggle=True)
            if "texture_quality" in allowed:
                box.prop(self, "texture_quality", text="")
                if self.texture_quality == "extreme":
                    box.label(text="8K -- surcharge unverified", icon="INFO")
            if "geometry_quality" in allowed:
                box.prop(self, "geometry_quality", text="")

            # Image-only parameters, per the generation docs.
            if self.mode in {"IMAGE", "MULTIVIEW"}:
                box.prop(self, "texture_alignment", text="Align")
                box.prop(self, "orientation", text="Orient")
                box.prop(self, "autofix")

            if "auto_size" in allowed:
                box.prop(self, "auto_size")

            col = box.column(align=True)
            col.prop(self, "use_face_limit")
            sub = col.row()
            sub.enabled = self.use_face_limit
            sub.prop(self, "face_limit")
            if self.use_face_limit:
                lo, hi = costs.face_limit_range(
                    self.model, quad=self.quad,
                    smart_low_poly=self.smart_low_poly,
                    geometry_quality=self.geometry_quality)
                if not (lo <= self.face_limit <= hi):
                    col.label(text=f"This setup allows {lo:,}-{hi:,}",
                              icon="ERROR")

            if "quad" in allowed:
                col = box.column(align=True)
                col.prop(self, "quad")
                if self.quad:
                    col.label(text="Returns FBX", icon="INFO")
                col.prop(self, "smart_low_poly")
                col.prop(self, "generate_parts")
                if self.generate_parts:
                    col.label(text="Forces texture off", icon="ERROR")
                    if self.quad:
                        col.label(text="Incompatible with quad", icon="CANCEL")
            elif p1:
                box.label(text="P1: quad, parts, low-poly and", icon="INFO")
                box.label(text="geometry quality not supported")
            else:
                box.label(text="v2.5 predates the advanced options",
                          icon="INFO")

            row = box.row(align=True)
            if "export_uv" in allowed:
                row.prop(self, "export_uv", toggle=True)
            if "compress" in allowed:
                row.prop(self, "compress", toggle=True)

            col = box.column(align=True)
            col.prop(self, "use_seeds")
            sub = col.row()
            sub.enabled = self.use_seeds
            sub.prop(self, "seed")

        if self.existing_task.strip():
            box = layout.box()
            box.label(text="Using an existing model", icon="LINKED")
            row = box.row(align=True)
            row.prop(self, "existing_task", text="")
            op = row.operator("tripo.pick_task", text="", icon="VIEWZOOM")
            op.node_name = self.name
            op.tree_name = self.id_data.name
            op.field = "existing_task"
            op.kind_filter = "generate"
            clr = box.operator("tripo.clear_node_task",
                               text="Generate a new one", icon="X")
            clr.node_name = self.name
            clr.tree_name = self.id_data.name
            self.draw_status(layout)
            return

        row = self.action_row(layout)
        row.scale_y = 1.3
        op = row.operator("tripo.node_generate", icon="PLAY",
                          text=f"Generate  ({self.cost()} cr)")
        op.node_name = self.name
        op.tree_name = self.id_data.name

        row = layout.row(align=True)
        row.label(text="or reuse:")
        op = row.operator("tripo.pick_task", text="From history",
                          icon="VIEWZOOM")
        op.node_name = self.name
        op.tree_name = self.id_data.name
        op.field = "existing_task"
        op.kind_filter = "generate"

        self.draw_status(layout)


class TripoSourceNode(TripoNode, bpy.types.Node):
    """Bring your own mesh in, so post-processing works on any asset.

    Once imported, retopology / texture / segment / rig treat it exactly like
    something Tripo generated. Free.
    """

    bl_idname = "TripoSourceNode"
    bl_label = "Import Asset"
    bl_icon = "MESH_DATA"
    bl_width_default = 250

    source: bpy.props.EnumProperty(
        name="Source",
        items=[("OBJECT", "Selected object", "Export the selected mesh and upload"),
               ("FILE", "File", "Upload a .glb, .obj, .fbx or .stl from disk")],
        default="OBJECT")
    path: bpy.props.StringProperty(name="File", subtype="FILE_PATH")
    obj_ref: bpy.props.PointerProperty(
        name="Object", type=bpy.types.Object,
        description="The mesh this node uploads. Captured at upload time so "
                    "a later Run Graph doesn't push whatever happens to be "
                    "selected through the paid chain")

    def init(self, context):
        self.outputs.new("TripoAssetSocket", "Asset")

    def draw_buttons(self, context, layout):
        layout.prop(self, "source", expand=True)
        if self.source == "FILE":
            layout.prop(self, "path", text="")
        else:
            layout.prop(self, "obj_ref", text="")
            fallback = self.obj_ref or context.active_object
            if not (fallback and fallback.type == "MESH"):
                layout.label(text="Pick a mesh object", icon="ERROR")

        row = self.action_row(layout)
        op = row.operator("tripo.node_upload", icon="EXPORT", text="Upload  (free)")
        op.node_name = self.name
        op.tree_name = self.id_data.name
        layout.label(text="Max 150MB", icon="INFO")
        self.draw_status(layout)


class TripoImageRefSlot(bpy.types.PropertyGroup):
    """A reference image, with the role Google assigns it."""

    path: bpy.props.StringProperty(name="Image", subtype="FILE_PATH")
    role: bpy.props.EnumProperty(
        name="Role",
        items=[("object", "Object", "Include this object faithfully"),
               ("character", "Character", "Keep this character consistent"),
               ("style", "Style", "Use this as a style reference")],
        default="object")


def _model_items(self, context):
    return [(mid, label, desc) for mid, label, desc in google_api.MODELS]


# Enum values are stored by number, so every size keeps a fixed number
# across models -- otherwise a stored index remaps to a different size (and
# price) when the model's size list changes.
_SIZE_NUMBERS = {"0.5K": 5, "1K": 10, "2K": 20, "4K": 40}


def _size_items(self, context):
    return [(s, s, "", _SIZE_NUMBERS[s])
            for s in google_api.sizes_for(self.model)]


class GoogleNodeMixin:
    """Google nodes produce image files, not Tripo tasks."""

    def images(self):
        """This node's generated images: live job first, then history.

        Google jobs have no task id, so history entries are matched by the
        job id, which the node stores and the .blend file preserves.
        """
        imgs = self.job().get("images") or {}
        if imgs:
            return imgs
        if self.job_id:
            for entry in api.history(limit=999):
                if entry.get("job") == self.job_id:
                    return entry.get("images") or {}
        return {}

    def has_result(self):
        return bool(self.images())

    def _has_live_job(self):
        """Whether the in-memory job still exists.

        status() reports state "unknown" for a job id it no longer knows --
        which is truthy, and treating it as live was exactly the bug that
        hid a paid result after a restart.
        """
        state = self.job().get("state")
        return bool(state) and state != "unknown"

    def draw_status(self, layout):
        """Live job when there is one; otherwise the persisted result.

        The in-memory job dies with the Blender session, but the images and
        the history row survive -- a paid result must not look lost after a
        restart when it is sitting right there on disk.
        """
        if self._has_live_job():
            TripoNode.draw_status(self, layout)
            return
        imgs = self.images()
        primary = imgs.get("generated_image") or imgs.get("front")
        if not primary:
            TripoNode.draw_status(self, layout)
            return
        icon = previews.icon_id(f"node_{self.job_id}", primary)
        if icon:
            layout.template_icon(icon_value=icon,
                                 scale=max(4.0, self.width / 20.0))
        row = layout.row()
        row.label(text="complete", icon="CHECKMARK")
        op = row.operator("tripo.view_image", text="", icon="ZOOM_IN",
                          emboss=False)
        op.path = primary


class GoogleImageNode(GoogleNodeMixin, TripoNode, bpy.types.Node):
    """Generate a concept image with Google Gemini.

    Images are billed by Google on your own key; Tripo is used only for 3D.
    """

    bl_idname = "GoogleImageNode"
    bl_label = "Generate Image"
    bl_icon = "IMAGE_DATA"
    bl_width_default = 290

    prompt: bpy.props.StringProperty(name="Prompt")
    model: bpy.props.EnumProperty(name="Model", items=_model_items)
    image_size: bpy.props.EnumProperty(name="Size", items=_size_items)
    aspect_ratio: bpy.props.EnumProperty(
        name="Aspect",
        items=[(r, r, "") for r in google_api.ASPECT_RATIOS], default="1:1")
    references: bpy.props.CollectionProperty(type=TripoImageRefSlot)

    def init(self, context):
        self.outputs.new("TripoImageSocket", "Image")

    def reference_paths(self):
        return [r.path.strip() for r in self.references if r.path.strip()]

    def role_counts(self):
        counts = {"object": 0, "character": 0, "style": 0}
        for r in self.references:
            if r.path.strip():
                counts[r.role] = counts.get(r.role, 0) + 1
        return counts

    def cost_usd(self):
        return google_api.price(self.model, self.image_size or "1K")

    def draw_buttons(self, context, layout):
        if not google_api.has_key():
            box = layout.box()
            box.alert = True
            box.label(text="No Google API key", icon="KEY_DEHLT")
            box.operator("tripo.setup_keys", text="Set API keys",
                         icon="KEY_HLT")

        layout.prop(self, "prompt", text="")
        layout.prop(self, "model", text="")

        row = layout.row(align=True)
        row.prop(self, "image_size", text="")
        row.prop(self, "aspect_ratio", text="")

        box = layout.box()
        limits = google_api.REFERENCE_LIMITS.get(self.model, {})
        counts = self.role_counts()
        box.label(text=f"References  {len(self.references)}", icon="IMAGE_DATA")
        for i, ref in enumerate(self.references):
            row = box.row(align=True)
            row.label(text=f"[{i + 1}]")
            row.prop(ref, "path", text="")
            row.prop(ref, "role", text="")
            rm = row.operator("tripo.remove_reference", text="", icon="X")
            rm.node_name = self.name
            rm.tree_name = self.id_data.name
            rm.index = i

        over = [k for k, v in counts.items() if v > limits.get(k, 0)]
        if over:
            box.alert = True
            for role in over:
                box.label(text=f"{counts[role]} {role} refs, max "
                               f"{limits.get(role, 0)}", icon="ERROR")
        add = box.operator("tripo.add_reference", icon="ADD",
                           text="Add reference")
        add.node_name = self.name
        add.tree_name = self.id_data.name
        allowed = ", ".join(f"{v} {k}" for k, v in limits.items() if v)
        if allowed:
            box.label(text=f"This model allows {allowed}", icon="INFO")
        if len(self.references) > 1:
            box.label(text="Refer to them as image[1], image[2]", icon="INFO")

        row = self.action_row(layout)
        row.scale_y = 1.3
        op = row.operator("tripo.google_image", icon="PLAY",
                          text=f"Generate  (${self.cost_usd():.3f})")
        op.node_name = self.name
        op.tree_name = self.id_data.name
        layout.label(text="Billed by Google, not Tripo credits", icon="INFO")
        self.draw_status(layout)


class GoogleViewsNode(GoogleNodeMixin, TripoNode, bpy.types.Node):
    """Generate four consistent orthographic views, to feed multiview-to-3D.

    Google has no single multiview call, so this issues one request per view
    carrying the same reference image, which is what keeps them coherent.
    """

    bl_idname = "GoogleViewsNode"
    bl_label = "Generate Views"
    bl_icon = "IMGDISPLAY"
    bl_width_default = 280

    prompt: bpy.props.StringProperty(name="Subject")
    reference: bpy.props.StringProperty(name="Reference", subtype="FILE_PATH")
    model: bpy.props.EnumProperty(name="Model", items=_model_items, default=1)
    image_size: bpy.props.EnumProperty(name="Size", items=_size_items)

    def init(self, context):
        self.inputs.new("TripoImageSocket", "Image")
        self.outputs.new("TripoImageSocket", "Views")

    def upstream_image(self):
        imgs = self.upstream_images()
        return imgs.get("generated_image") or imgs.get("front")

    def cost_usd(self):
        return google_api.price(self.model, self.image_size or "1K") * 4

    def draw_buttons(self, context, layout):
        if not google_api.has_key():
            box = layout.box()
            box.alert = True
            box.label(text="No Google API key", icon="KEY_DEHLT")
            box.operator("tripo.setup_keys", text="Set API keys",
                         icon="KEY_HLT")

        layout.prop(self, "prompt", text="")
        if self.upstream_image():
            layout.label(text="Using upstream image", icon="LINKED")
        else:
            layout.prop(self, "reference", text="")
        layout.prop(self, "model", text="")
        layout.prop(self, "image_size", text="")

        images = self.images()   # live job first, then the history row
        if images:
            grid = layout.grid_flow(row_major=True, columns=2, align=True)
            for view in ("front", "left", "back", "right"):
                if view in images:
                    icon = previews.icon_id(f"gv_{self.job_id}_{view}",
                                            images[view])
                    cell = grid.column(align=True)
                    if icon:
                        # Two columns share the node body: half width each.
                        cell.template_icon(icon_value=icon,
                                           scale=max(3.0, self.width / 42.0))
                    r = cell.row(align=True)
                    r.label(text=view)
                    op = r.operator("tripo.view_image", text="",
                                    icon="ZOOM_IN", emboss=False)
                    op.path = images[view]
                    if not self.is_busy():
                        op = r.operator("tripo.google_view_redo", text="",
                                        icon="FILE_REFRESH", emboss=False)
                        op.node_name = self.name
                        op.tree_name = self.id_data.name
                        op.view = view

        row = self.action_row(layout)
        op = row.operator("tripo.google_views", icon="PLAY",
                          text=f"Generate 4 views  (${self.cost_usd():.2f})")
        op.node_name = self.name
        op.tree_name = self.id_data.name
        layout.label(text="Billed by Google", icon="INFO")
        self.draw_status(layout)


# --------------------------------------------------------------------------
# Process
# --------------------------------------------------------------------------

class TripoPostNode(TripoNode, bpy.types.Node):
    """Post-processing on an upstream task: retopo, texture, segment, convert."""

    bl_idname = "TripoPostNode"
    bl_label = "Process"
    bl_icon = "MODIFIER"
    bl_width_default = 250

    operation: bpy.props.EnumProperty(
        name="Operation",
        items=[
            ("highpoly_to_lowpoly", "Retopology", "Clean low-poly rebuild"),
            ("texture_model", "Texture", "Generate textures"),
            ("mesh_segmentation", "Segment", "Split into named parts"),
            ("mesh_completion", "Complete parts", "Fill gaps in segmented parts"),
            ("stylize_model", "Stylize", "Lego, voxel, voronoi or minecraft"),
        ],
        default="highpoly_to_lowpoly")
    # Which operation made the stored result -- showing a segmentation
    # result under a "Retopology" header misleads.
    last_operation: bpy.props.StringProperty()

    seg_model: bpy.props.EnumProperty(
        name="Segmentation model",
        items=[("v2.0-20260430", "v2 semantic (Beta)",
                "Semantic labeling with granularity control -- understands "
                "boundaries like armor vs body"),
               ("v1.0-20250506", "v1 geometry",
                "Geometry-based split, no granularity control")],
        default="v2.0-20260430")
    seg_granularity: bpy.props.EnumProperty(
        name="Granularity",
        items=[("simple", "Simple", "3-6 major parts"),
               ("balanced", "Balanced", "6-15 parts"),
               ("detailed", "Detailed", "15+ parts")],
        default="balanced")
    seg_connectivity: bpy.props.BoolProperty(
        name="Split by connectivity", default=True,
        description="Also split parts that aren't physically connected")

    face_limit: bpy.props.IntProperty(name="Faces", default=4000, min=500,
                                      max=20000)
    quad: bpy.props.BoolProperty(name="Quad mesh")
    texture_quality: bpy.props.EnumProperty(
        name="Quality", items=costs.TEXTURE_QUALITY_ITEMS, default="standard")
    part_names: bpy.props.StringProperty(
        name="Parts", description="Comma-separated part names; blank means all")
    style: bpy.props.EnumProperty(
        name="Style", items=costs.STYLE_ITEMS[1:], default="lego")
    block_size: bpy.props.IntProperty(name="Block size", default=80, min=32,
                                      max=128)

    def init(self, context):
        self.inputs.new("TripoAssetSocket", "Asset")
        self.outputs.new("TripoAssetSocket", "Asset")

    def cost(self):
        base = costs.POST.get(self.operation, 0)
        if self.operation == "texture_model":
            base += costs._TEXTURE_STEP.get(self.texture_quality, 0)
        return base

    def draw_buttons(self, context, layout):
        if self.last_task and self.last_operation and \
                self.last_operation != self.operation:
            box = layout.box()
            box.alert = True
            box.label(text="Result is from a different operation",
                      icon="ERROR")
            box.label(text="Run again to replace it")
        layout.prop(self, "operation", text="")

        if self.operation == "highpoly_to_lowpoly":
            layout.prop(self, "face_limit")
            layout.prop(self, "quad")
        elif self.operation == "texture_model":
            layout.prop(self, "texture_quality", text="")
            layout.prop(self, "part_names", text="Parts")
        elif self.operation == "mesh_segmentation":
            layout.prop(self, "seg_model", text="")
            if self.seg_model.startswith("v2"):
                layout.prop(self, "seg_granularity", text="")
                layout.prop(self, "seg_connectivity")
            up = _upstream_node(self)
            if up is not None and up.bl_idname in ("TripoSourceNode",
                                                   "TripoPostNode"):
                box = layout.box()
                box.label(text="Segment takes generated models", icon="INFO")
                box.label(text="Uploads/processed meshes may be rejected")
        elif self.operation == "mesh_completion":
            layout.prop(self, "part_names", text="Parts")
        elif self.operation == "stylize_model":
            layout.prop(self, "style", text="")
            if self.style == "minecraft":
                layout.prop(self, "block_size")

        if not self.upstream_task():
            layout.label(text="Connect a finished asset", icon="ERROR")
        else:
            row = self.action_row(layout)
            op = row.operator("tripo.node_process", icon="PLAY",
                              text=f"Run  ({self.cost()} cr)")
            op.node_name = self.name
            op.tree_name = self.id_data.name

        self.draw_status(layout)


class TripoExportNode(TripoNode, bpy.types.Node):
    """Convert an asset to another format and write it to disk.

    This is how work leaves Blender -- FBX for a game engine, OBJ for a DCC,
    STL for printing. Conversion runs server-side, so it also gives access to
    remeshing and pivot options Blender would otherwise need manual work for.
    """

    bl_idname = "TripoExportNode"
    bl_label = "Export"
    bl_icon = "EXPORT"
    bl_width_default = 260

    directory: bpy.props.StringProperty(
        name="Folder", subtype="DIR_PATH",
        description="Where to write the converted file")
    filename: bpy.props.StringProperty(name="Name", default="asset")

    fmt: bpy.props.EnumProperty(
        name="Format",
        items=[("FBX", "FBX", "Game engines; keeps rigs"),
               ("GLTF", "glTF", "Web and Blender"),
               ("OBJ", "OBJ", "Widely supported; no rigs"),
               ("USDZ", "USDZ", "Apple / AR"),
               ("STL", "STL", "3D printing; geometry only"),
               ("3MF", "3MF", "3D printing; geometry only")],
        default="FBX")

    show_advanced: bpy.props.BoolProperty(name="Options", default=False)
    quad: bpy.props.BoolProperty(
        name="Quad remesh", description="Retopologise to quads during conversion")
    force_symmetry: bpy.props.BoolProperty(
        name="Force symmetry", description="Only applies with quad remeshing")
    use_face_limit: bpy.props.BoolProperty(name="Limit faces")
    face_limit: bpy.props.IntProperty(name="Faces", default=10000, min=100,
                                      max=1000000)
    flatten_bottom: bpy.props.BoolProperty(
        name="Flatten bottom", description="Useful for printing and for props "
                                           "that must sit flat")
    flatten_threshold: bpy.props.FloatProperty(name="Depth", default=0.01,
                                               min=0.0, max=1.0)
    texture_size: bpy.props.IntProperty(
        name="Texture size", default=2048, min=128, max=4096,
        description="Must be smaller than the default (2048, or 4096 for v2.0+)")
    texture_format: bpy.props.EnumProperty(
        name="Texture format",
        items=[(f, f, "") for f in ("JPEG", "PNG", "WEBP", "TARGA", "BMP",
                                    "TIFF", "HDR", "OPEN_EXR", "DPX")],
        default="JPEG")
    scale_factor: bpy.props.FloatProperty(name="Scale", default=1.0, min=0.001,
                                          max=1000.0)
    pivot_to_center_bottom: bpy.props.BoolProperty(
        name="Pivot at base",
        description="Puts the origin at the centre bottom, so it sits on the "
                    "floor without manual adjustment")
    with_animation: bpy.props.BoolProperty(name="Include animation", default=True)
    fbx_preset: bpy.props.EnumProperty(
        name="FBX preset",
        items=[("blender", "Blender", ""), ("mixamo", "Mixamo", ""),
               ("3dsmax", "3ds Max", "")],
        default="blender")
    export_orientation: bpy.props.EnumProperty(
        name="Forward axis",
        items=[("+x", "+X", ""), ("+y", "+Y", ""), ("-x", "-X", ""),
               ("-y", "-Y", "")],
        default="+x")
    part_names: bpy.props.StringProperty(
        name="Parts", description="Comma-separated; blank means all")

    def init(self, context):
        self.inputs.new("TripoAssetSocket", "Asset")

    def has_extra_params(self):
        """Anything beyond `format`, which triggers the +5 surcharge."""
        return any((
            self.quad, self.use_face_limit, self.flatten_bottom,
            self.pivot_to_center_bottom,
            abs(self.scale_factor - 1.0) > 1e-6,
            self.export_orientation != "+x",
            self.part_names.strip(),
            self.fmt not in {"STL", "3MF"} and (
                self.texture_size != 2048 or self.texture_format != "JPEG"),
            self.fmt == "FBX" and (
                self.fbx_preset != "blender" or not self.with_animation),
        ))

    def cost(self):
        return costs.convert_cost(self.has_extra_params())

    def draw_buttons(self, context, layout):
        layout.prop(self, "fmt", text="")
        layout.prop(self, "directory", text="")
        layout.prop(self, "filename", text="Name")

        layout.prop(self, "show_advanced", toggle=True,
                    icon="TRIA_DOWN" if self.show_advanced else "TRIA_RIGHT")
        if self.show_advanced:
            box = layout.box()
            col = box.column(align=True)
            col.prop(self, "quad")
            sub = col.row()
            sub.enabled = self.quad
            sub.prop(self, "force_symmetry")
            if self.quad and self.fmt in {"GLTF", "STL"}:
                col.label(text=f"{self.fmt} stores triangles only", icon="INFO")

            col = box.column(align=True)
            col.prop(self, "use_face_limit")
            sub = col.row()
            sub.enabled = self.use_face_limit
            sub.prop(self, "face_limit")

            col = box.column(align=True)
            col.prop(self, "flatten_bottom")
            sub = col.row()
            sub.enabled = self.flatten_bottom
            sub.prop(self, "flatten_threshold")

            box.prop(self, "pivot_to_center_bottom")
            box.prop(self, "scale_factor")
            box.prop(self, "export_orientation", text="Forward")

            if self.fmt not in {"STL", "3MF"}:
                box.prop(self, "texture_size")
                box.prop(self, "texture_format", text="")
            if self.fmt == "FBX":
                box.prop(self, "fbx_preset", text="Preset")
                box.prop(self, "with_animation")
            box.prop(self, "part_names", text="Parts")

        if not self.upstream_task():
            layout.label(text="Connect a finished asset", icon="ERROR")
            return
        if not self.directory.strip():
            layout.label(text="Choose an output folder", icon="ERROR")
            return

        row = self.action_row(layout)
        op = row.operator("tripo.node_export", icon="EXPORT",
                          text=f"Export  ({self.cost()} cr)")
        op.node_name = self.name
        op.tree_name = self.id_data.name
        self.draw_status(layout)


class TripoRigNode(TripoNode, bpy.types.Node):
    """Build a skeleton for a character.

    Rigging must come after any remeshing -- segmentation, completion,
    retopology and quad conversion all strip skeleton data.
    """

    bl_idname = "TripoRigNode"
    bl_label = "Rig"
    bl_icon = "ARMATURE_DATA"
    bl_width_default = 250

    rig_type: bpy.props.EnumProperty(
        name="Rig", items=costs.RIG_TYPE_ITEMS, default="biped")
    out_format: bpy.props.EnumProperty(
        name="Format", items=[("glb", "glTF", ""), ("fbx", "FBX", "")],
        default="glb")
    spec: bpy.props.EnumProperty(
        name="Bones",
        items=[("tripo", "Tripo bones", "Tripo native bone naming"),
               ("mixamo", "Mixamo bones",
                "Mixamo-compatible naming, for game pipelines and "
                "animation libraries")],
        default="tripo")
    # The free check runs on its own job slot. If it shared job_id, the
    # finished check would count as this node's result: Run Graph would skip
    # the paid rig, and Animate would chain off the check's task id.
    check_job_id: bpy.props.StringProperty()
    existing_task: bpy.props.StringProperty(
        name="Existing rig",
        description="Task id of a rig you already have. Set this to reuse a "
                    "rig instead of paying to build it again")

    def init(self, context):
        self.inputs.new("TripoAssetSocket", "Asset")
        self.outputs.new("TripoRigSocket", "Rig")

    def task_id(self):
        # An explicitly supplied rig wins: it is already paid for.
        if self.existing_task.strip():
            return self.existing_task.strip()
        return super().task_id()

    def cost(self):
        return costs.POST["animate_rig"]

    def _upstream_is_remesh(self):
        node = _upstream_node(self)
        return node is not None and \
            getattr(node, "operation", None) in api.RIG_DESTROYING

    def draw_buttons(self, context, layout):
        if self.existing_task.strip():
            box = layout.box()
            box.label(text="Using an existing rig", icon="LINKED")
            row = box.row(align=True)
            row.prop(self, "existing_task", text="")
            op = row.operator("tripo.pick_task", text="", icon="VIEWZOOM")
            op.node_name = self.name
            op.tree_name = self.id_data.name
            op.field = "existing_task"
            op.kind_filter = "animate_rig"
            clr = box.operator("tripo.clear_node_task", text="Rig a new model",
                               icon="X")
            clr.node_name = self.name
            clr.tree_name = self.id_data.name
            self.draw_status(layout)
            return

        # Offer the reuse route FIRST. It must stay reachable even with
        # nothing connected -- otherwise a node with no input is a dead end.
        row = layout.row(align=True)
        row.prop(self, "existing_task", text="Reuse")
        op = row.operator("tripo.pick_task", text="", icon="VIEWZOOM")
        op.node_name = self.name
        op.tree_name = self.id_data.name
        op.field = "existing_task"
        op.kind_filter = "animate_rig"

        layout.separator(factor=0.3)
        layout.prop(self, "rig_type", text="")
        layout.prop(self, "spec", text="")
        if self.spec == "mixamo":
            box = layout.box()
            box.label(text="Tripo presets can't retarget", icon="INFO")
            box.label(text="Mixamo rigs -- use mixamo.com")
        layout.prop(self, "out_format", text="")
        if self.rig_type != "biped":
            layout.label(text="Uses rig model v2.5", icon="INFO")

        if self._upstream_is_remesh():
            box = layout.box()
            box.alert = True
            box.label(text="Upstream remesh strips rigs", icon="ERROR")
            box.label(text="Do mesh editing before rigging")

        if not self.upstream_task():
            layout.label(text="Connect an asset, or reuse a rig above",
                         icon="INFO")
            self.draw_status(layout)
            return

        check = api.status(self.check_job_id) if self.check_job_id else {}
        if check.get("state") == "done" and "riggable" in check:
            box = layout.box()
            if check["riggable"]:
                box.label(text=f"Riggable as {check.get('rig_type', 'biped')}",
                          icon="CHECKMARK")
            else:
                box.alert = True
                box.label(text="Not riggable", icon="X")
        elif check.get("state") in ("submitting", "running"):
            layout.label(text="Checking...", icon="TIME")
        elif check.get("state") == "error":
            layout.label(text="Check failed -- see Jobs", icon="ERROR")

        row = self.action_row(layout)
        op = row.operator("tripo.node_prerig", icon="VIEWZOOM",
                          text="Check  (free)")
        op.node_name = self.name
        op.tree_name = self.id_data.name

        row = self.action_row(layout)
        op = row.operator("tripo.node_rig", icon="ARMATURE_DATA",
                          text=f"Rig  ({self.cost()} cr)")
        op.node_name = self.name
        op.tree_name = self.id_data.name

        self.draw_status(layout)


class TripoAnimateNode(TripoNode, bpy.types.Node):
    """Apply preset animations to a rigged model.

    Takes a Rig, not an Asset -- animation chains from the rig task, and
    feeding it a plain mesh is what the API rejects with a 400.
    """

    bl_idname = "TripoAnimateNode"
    bl_label = "Animate"
    bl_icon = "ANIM"
    bl_width_default = 250

    animation: bpy.props.EnumProperty(
        name="Animation", items=costs.ANIMATION_ITEMS, default="preset:idle")
    out_format: bpy.props.EnumProperty(
        name="Format", items=[("glb", "glTF", ""), ("fbx", "FBX", "")],
        default="glb")
    animate_in_place: bpy.props.BoolProperty(
        name="In place", description="Animate without moving from the origin")
    existing_task: bpy.props.StringProperty(
        name="Rig task",
        description="Animate a rig directly, without a Rig node upstream")

    def init(self, context):
        self.inputs.new("TripoRigSocket", "Rig")
        self.outputs.new("TripoAssetSocket", "Asset")

    def cost(self):
        return costs.POST["animate_retarget"]

    def rig_source(self):
        """The rig to animate: an explicit one, else whatever is connected."""
        if self.existing_task.strip():
            return self.existing_task.strip()
        return self.upstream_task()

    def draw_buttons(self, context, layout):
        row = layout.row(align=True)
        row.prop(self, "existing_task", text="Rig")
        op = row.operator("tripo.pick_task", text="", icon="VIEWZOOM")
        op.node_name = self.name
        op.tree_name = self.id_data.name
        op.field = "existing_task"
        op.kind_filter = "animate_rig"

        layout.separator(factor=0.3)
        layout.prop(self, "animation", text="")
        rig_node = _upstream_node(self)
        rt = getattr(rig_node, "rig_type", "") if rig_node is not None else ""
        if rt and not self.existing_task.strip():
            # Preset vocabulary depends on the rig: plain presets are biped;
            # species rigs only take their own preset:<species>:* walks; the
            # preset:biped:* catalogue needs the v1 (biped-only) rig model.
            species = ("quadruped", "hexapod", "octopod", "serpentine",
                       "aquatic")
            warn = None
            if getattr(rig_node, "spec", "") == "mixamo":
                # Measured (error 1004): Tripo's preset retarget refuses
                # mixamo-spec rigs outright. Undocumented.
                warn = "Presets don't work on Mixamo rigs"
            elif rt == "avian":
                warn = "No presets exist for avian rigs yet"
            elif rt == "biped":
                if any(self.animation.startswith(f"preset:{sp}:")
                       for sp in species):
                    warn = "That preset is for a different rig type"
            elif not self.animation.startswith(f"preset:{rt}:"):
                warn = f"{rt.title()} rigs only take preset:{rt}:* presets"
            if warn:
                box = layout.box()
                box.alert = True
                box.label(text=warn, icon="ERROR")
        layout.prop(self, "out_format", text="")
        layout.prop(self, "animate_in_place")

        if not self.rig_source():
            layout.label(text="Connect a Rig node, or pick a rig above",
                         icon="INFO")
            self.draw_status(layout)
            return

        row = self.action_row(layout)
        op = row.operator("tripo.node_animate", icon="ANIM",
                          text=f"Animate  ({self.cost()} cr)")
        op.node_name = self.name
        op.tree_name = self.id_data.name
        self.draw_status(layout)


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------

class TripoImportNode(TripoNode, bpy.types.Node):
    bl_idname = "TripoImportNode"
    bl_label = "Import to Scene"
    bl_icon = "IMPORT"
    bl_width_default = 220

    asset_name: bpy.props.StringProperty(name="Name")
    at_cursor: bpy.props.BoolProperty(name="At 3D cursor", default=True)
    mark_asset: bpy.props.BoolProperty(
        name="Add to library", default=False,
        description="Mark as a Blender asset so it appears in the Asset Browser")
    decimate_to: bpy.props.IntProperty(
        name="Decimate to", default=0, min=0, max=2000000,
        description="Reduce to this many faces after import. 0 leaves it alone")

    def init(self, context):
        self.inputs.new("TripoAssetSocket", "Asset")

    def draw_buttons(self, context, layout):
        layout.prop(self, "asset_name", text="Name")
        layout.prop(self, "at_cursor")
        layout.prop(self, "mark_asset")
        layout.prop(self, "decimate_to")

        if not self.upstream_task():
            layout.label(text="Connect a finished asset", icon="ERROR")
            return
        row = self.action_row(layout)
        op = row.operator("tripo.node_import", icon="IMPORT", text="Import  (free)")
        op.node_name = self.name
        op.tree_name = self.id_data.name
        self.draw_status(layout)


# --------------------------------------------------------------------------
# Add menu
# --------------------------------------------------------------------------


class TripoAccountNode(TripoNode, bpy.types.Node):
    """API keys and credits, reachable without leaving the graph.

    Deliberately stores NOTHING. Node settings are written into the .blend
    file, so a key kept on a node would travel with every shared file. Keys
    live in the addon preferences (machine-local); this node shows status and
    opens the dialog that edits them.
    """

    bl_idname = "TripoAccountNode"
    bl_label = "Account"
    bl_icon = "USER"
    bl_width_default = 240

    def draw_buttons(self, context, layout):
        col = layout.column(align=True)
        col.label(text="Tripo key",
                  icon="CHECKMARK" if api.has_key() else "X")
        col.label(text="Google key",
                  icon="CHECKMARK" if google_api.has_key() else "X")

        bal = getattr(context.scene, "tripo_balance", 0)
        if bal:
            col.label(text=f"{bal} Tripo credits", icon="FUND")

        row = layout.row()
        row.scale_y = 1.2
        row.operator("tripo.setup_keys", icon="KEY_HLT", text="Set API keys")
        layout.operator("tripo.refresh_balance", icon="FILE_REFRESH",
                        text="Check balance")
        layout.label(text="Keys stay on this machine,", icon="INFO")
        layout.label(text="never inside the .blend file")


# --------------------------------------------------------------------------
# Deprecated nodes
#
# Policy: a node class that ever shipped is never deleted, only stubbed.
# Blender shows unknown node types as "Undefined" and drops their settings on
# the next save -- deleting a class therefore corrupts every user file that
# contains one. Stubs keep old files loadable and point at the replacement.
# --------------------------------------------------------------------------

class _DeprecatedNode(TripoNode):
    replacement = ""

    def init(self, context):
        self.outputs.new("TripoImageSocket", "Image")

    def draw_buttons(self, context, layout):
        box = layout.box()
        box.alert = True
        box.label(text="Deprecated node", icon="ERROR")
        box.label(text=f"Use '{self.replacement}' instead")


class TripoImageNode(_DeprecatedNode, bpy.types.Node):
    bl_idname = "TripoImageNode"
    bl_label = "Generate Image (deprecated)"
    bl_icon = "IMAGE_DATA"
    replacement = "Generate Image (Google)"


class TripoMultiviewNode(_DeprecatedNode, bpy.types.Node):
    bl_idname = "TripoMultiviewNode"
    bl_label = "Multiview Images (deprecated)"
    bl_icon = "IMGDISPLAY"
    replacement = "Generate Views (Google)"


def _add_menu(self, context):
    if not _is_tripo_tree(getattr(context.space_data, "edit_tree", None)):
        return
    layout = self.layout
    layout.separator()
    layout.label(text="Tripo")
    for idname, label, icon in (("GoogleImageNode", "Generate Image", "IMAGE_DATA"),
                                ("GoogleViewsNode", "Generate Views", "IMGDISPLAY"),
                                ("TripoGenerateNode", "Generate 3D", "SHADERFX"),
                                ("TripoSourceNode", "Import Asset", "MESH_DATA"),
                                ("TripoPostNode", "Process", "MODIFIER"),
                                ("TripoRigNode", "Rig", "ARMATURE_DATA"),
                                ("TripoAnimateNode", "Animate", "ANIM"),
                                ("TripoExportNode", "Export", "EXPORT"),
                                ("TripoImportNode", "Import to Scene", "IMPORT"),
                                ("TripoAccountNode", "Account / API Keys", "USER")):
        op = layout.operator("node.add_node", text=label, icon=icon)
        op.type = idname
        op.use_transform = True


classes = (
    TripoImageRefSlot,
    TripoNodeTree,
    TripoAssetSocket,
    TripoImageSocket,
    TripoRigSocket,
    GoogleImageNode,
    GoogleViewsNode,
    TripoGenerateNode,
    TripoSourceNode,
    TripoPostNode,
    TripoRigNode,
    TripoAnimateNode,
    TripoExportNode,
    TripoImportNode,
    TripoAccountNode,
    TripoImageNode,
    TripoMultiviewNode,
)


_registering = False


def register():
    global _registering
    _registering = True
    try:
        for cls in classes:
            bpy.utils.register_class(cls)
    finally:
        _registering = False
    bpy.types.NODE_MT_add.append(_add_menu)


def unregister():
    global _registering
    try:
        bpy.types.NODE_MT_add.remove(_add_menu)
    except Exception:
        pass
    _registering = True
    try:
        for cls in reversed(classes):
            bpy.utils.unregister_class(cls)
    finally:
        _registering = False
