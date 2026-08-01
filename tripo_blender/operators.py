"""Every operator: panel generation, node actions, references, history."""

import os
import re

import bpy

from . import api, costs, google_api, meshtools, nodes
from .utils import _find_node, _tag_redraw
from .workspace import WORKSPACE_NAME






class TRIPO_OT_render_reference(bpy.types.Operator):
    bl_idname = "tripo.render_reference"
    bl_label = "Render Selected as Reference"
    bl_description = (
        "Render the active object alone on a neutral background and use it "
        "as the image input"
    )

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == "MESH"

    def execute(self, context):
        obj = context.active_object
        try:
            path = meshtools.render_reference(obj.name)
        except Exception as e:
            self.report({"ERROR"}, f"Render failed: {e}")
            return {"CANCELLED"}
        context.window_manager.clipboard = path
        self.report({"INFO"}, f"Rendered and copied to clipboard: {path}")
        return {"FINISHED"}


class TRIPO_OT_optimize(bpy.types.Operator):
    bl_idname = "tripo.optimize"
    bl_label = "Optimize Selected"
    bl_description = ("Decimate to a poly budget, then normalize size and "
                      "seat on the ground. Adjust values in the redo panel")
    bl_options = {"REGISTER", "UNDO"}

    target_polys: bpy.props.IntProperty(name="Poly budget", default=50000,
                                        min=500, max=2000000)
    normalize: bpy.props.BoolProperty(name="Normalize size", default=True)
    max_size: bpy.props.FloatProperty(name="Max size (m)", default=1.0,
                                      min=0.01, max=100.0)

    @classmethod
    def poll(cls, context):
        return any(o.type == "MESH" for o in context.selected_objects)

    def execute(self, context):
        done = []
        for name in [o.name for o in context.selected_objects
                     if o.type == "MESH"]:
            try:
                result = meshtools.decimate(name,
                                            target_polys=self.target_polys)
                if self.normalize:
                    meshtools.place(
                        name,
                        location=tuple(bpy.data.objects[name].location),
                        max_size=self.max_size,
                    )
                if result.get("skipped"):
                    done.append(f"{name}: already {result['polys']} polys")
                else:
                    done.append(f"{name}: {result['before']}->{result['after']}")
            except Exception as e:
                self.report({"ERROR"}, f"{name}: {e}")
                return {"CANCELLED"}
        self.report({"INFO"}, "; ".join(done))
        return {"FINISHED"}


class TRIPO_OT_frame(bpy.types.Operator):
    bl_idname = "tripo.frame"
    bl_label = "Frame Assets"
    bl_description = "Frame all visible meshes in the viewport with material preview"

    def execute(self, context):
        meshtools.frame()
        return {"FINISHED"}


class TRIPO_OT_refresh_balance(bpy.types.Operator):
    bl_idname = "tripo.refresh_balance"
    bl_label = "Refresh Credits"
    bl_description = "Query remaining Tripo credits"

    def execute(self, context):
        try:
            context.scene.tripo_balance = int(api.balance() or 0)
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        return {"FINISHED"}


class TRIPO_OT_clear_jobs(bpy.types.Operator):
    bl_idname = "tripo.clear_jobs"
    bl_label = "Clear Finished"
    bl_description = "Remove completed and failed jobs from the list"

    def execute(self, context):
        api.clear_finished()
        _tag_redraw()
        return {"FINISHED"}





class TRIPO_OT_setup_keys(bpy.types.Operator):
    bl_idname = "tripo.setup_keys"
    bl_label = "API Keys"
    bl_description = ("Enter the Tripo and Google API keys. Stored in "
                      "Blender's preferences on this machine, never in the "
                      ".blend file")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=380)

    def draw(self, context):
        prefs = context.preferences.addons[__package__].preferences
        col = self.layout.column()
        col.prop(prefs, "tripo_api_key", text="Tripo")
        col.prop(prefs, "google_api_key", text="Google")
        col.separator()
        col.label(text="Tripo: 3D generation (credits)", icon="INFO")
        col.label(text="Google: image generation (billed by Google)",
                  icon="INFO")

    def execute(self, context):
        bpy.ops.wm.save_userpref()
        self.report({"INFO"}, "Keys saved to preferences")
        return {"FINISHED"}


class TRIPO_OT_validate_key(bpy.types.Operator):
    bl_idname = "tripo.validate_key"
    bl_label = "Validate Key"
    bl_description = "Check the API key works and read the balance"

    def execute(self, context):
        try:
            info = api.validate_key()
        except Exception as e:
            self.report({"ERROR"}, f"Key rejected: {e}")
            return {"CANCELLED"}
        context.scene.tripo_balance = int(info.get("balance") or 0)
        frozen = info.get("frozen") or 0
        self.report({"INFO"}, f"Key OK -- {info.get('balance')} credits ({frozen} frozen)")
        return {"FINISHED"}


class TRIPO_OT_reimport(bpy.types.Operator):
    bl_idname = "tripo.reimport"
    bl_label = "Re-import"
    bl_description = "Download and import this past task again (free)"

    task_id: bpy.props.StringProperty()
    name: bpy.props.StringProperty()
    flavor: bpy.props.StringProperty()

    def execute(self, context):
        try:
            api.reimport(self.task_id, name=self.name or None,
                         flavor=self.flavor or None)
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        self.report({"INFO"}, "Re-importing...")
        _tag_redraw()
        return {"FINISHED"}


class TRIPO_OT_clear_history(bpy.types.Operator):
    bl_idname = "tripo.clear_history"
    bl_label = "Clear History"
    bl_description = "Forget all past tasks"

    def execute(self, context):
        api.forget_history()
        _tag_redraw()
        return {"FINISHED"}


class TRIPO_OT_mark_asset(bpy.types.Operator):
    bl_idname = "tripo.mark_asset"
    bl_label = "Add to Asset Library"
    bl_description = ("Mark selected objects as Blender assets with previews, so "
                      "they appear in the Asset Browser and can be reused in any file")

    @classmethod
    def poll(cls, context):
        return any(o.type == "MESH" for o in context.selected_objects)

    def execute(self, context):
        marked = []
        for o in context.selected_objects:
            if o.type != "MESH":
                continue
            try:
                o.asset_mark()
                o.asset_generate_preview()
                if o.asset_data:
                    o.asset_data.catalog_id = ""
                    o.asset_data.tags.new("tripo")
                marked.append(o.name)
            except Exception as e:
                self.report({"ERROR"}, f"{o.name}: {e}")
                return {"CANCELLED"}
        self.report({"INFO"}, f"Marked {len(marked)}: {', '.join(marked[:3])}")
        return {"FINISHED"}


class TRIPO_OT_node_generate(bpy.types.Operator):
    bl_idname = "tripo.node_generate"
    bl_label = "Generate"
    bl_description = "Run this generation node"

    node_name: bpy.props.StringProperty()
    tree_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(self.node_name, self.tree_name)
        if not node:
            self.report({"ERROR"}, "Node not found")
            return {"CANCELLED"}
        if node.is_busy():
            self.report({"ERROR"}, "This node already has a job running")
            return {"CANCELLED"}

        # Validate the face budget against the documented range for this
        # exact setup before anything is billed.
        if node.use_face_limit:
            lo, hi = costs.face_limit_range(
                node.model, quad=node.quad,
                smart_low_poly=node.smart_low_poly,
                geometry_quality=node.geometry_quality)
            if not (lo <= node.face_limit <= hi):
                self.report({"ERROR"},
                            f"face_limit must be {lo:,}-{hi:,} for this setup")
                return {"CANCELLED"}

        kw = dict(
            model=node.model,
            name=node.asset_name.strip() or None,
            texture=node.texture,
            pbr=node.pbr,
            face_limit=node.face_limit if node.use_face_limit else None,
        )
        # Send only what this model documents. The api layer strips as a
        # backstop, but the request should be right at the source.
        allowed = costs.caps(node.model)
        if "texture_quality" in allowed:
            kw["texture_quality"] = node.texture_quality
        if "geometry_quality" in allowed:
            kw["geometry_quality"] = node.geometry_quality
        if "auto_size" in allowed:
            kw["auto_size"] = node.auto_size or None
        if "quad" in allowed:
            kw["quad"] = node.quad or None
        if "smart_low_poly" in allowed:
            kw["smart_low_poly"] = node.smart_low_poly or None
        if "generate_parts" in allowed:
            kw["generate_parts"] = node.generate_parts or None
        if "export_uv" in allowed and not node.export_uv:
            kw["export_uv"] = False
        if "compress" in allowed and node.compress:
            kw["compress"] = "geometry"
        if node.negative.strip():
            kw["negative_prompt"] = node.negative.strip()[:255]
        if node.mode in {"IMAGE", "MULTIVIEW"}:
            kw["texture_alignment"] = node.texture_alignment
            if node.orientation != "default":
                kw["orientation"] = node.orientation
            if node.autofix:
                kw["enable_image_autofix"] = True
        if node.use_seeds:
            kw.update(image_seed=node.seed, model_seed=node.seed,
                      texture_seed=node.seed)

        try:
            if node.mode == "TEXT":
                if not node.prompt.strip():
                    self.report({"ERROR"}, "Prompt is empty")
                    return {"CANCELLED"}
                node.job_id = api.start(prompt=node.prompt.strip()[:1024], **kw)
            elif node.mode == "IMAGE":
                # An upstream image node supplies a file; Google results are
                # local files, not Tripo tasks, so there is no id to chain by.
                src = node.upstream_image()
                if not src:
                    src = bpy.path.abspath(node.image.strip()) if \
                        node.image.strip() else None
                if not src:
                    self.report({"ERROR"}, "No image set")
                    return {"CANCELLED"}
                node.job_id = api.start(image=src, **kw)
            else:
                imgs = node.upstream_images()
                if imgs.get("front"):
                    # Four views from an upstream Google node -- files again,
                    # so they upload; task-id chaining does not apply.
                    node.job_id = api.start(
                        images=[imgs.get(v) for v in
                                ("front", "left", "back", "right")], **kw)
                elif imgs:
                    self.report({"ERROR"},
                                "Upstream supplies one image, not views. "
                                "Connect a Views node or use Image mode")
                    return {"CANCELLED"}
                else:
                    views = [node.mv_front, node.mv_left, node.mv_back,
                             node.mv_right]
                    if not views[0].strip():
                        self.report({"ERROR"}, "Front view is required")
                        return {"CANCELLED"}
                    node.job_id = api.start(
                        images=[bpy.path.abspath(v.strip()) if v.strip() else None
                                for v in views], **kw)
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}

        node.last_task = ""
        self.report({"INFO"}, f"Submitted, ~{node.cost()} credits")
        _tag_redraw()
        return {"FINISHED"}


class TRIPO_OT_node_process(bpy.types.Operator):
    bl_idname = "tripo.node_process"
    bl_label = "Run"
    bl_description = "Run this post-processing step on the incoming asset"

    node_name: bpy.props.StringProperty()
    tree_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(self.node_name, self.tree_name)
        if not node:
            self.report({"ERROR"}, "Node not found")
            return {"CANCELLED"}
        if node.is_busy():
            self.report({"ERROR"}, "This node already has a job running")
            return {"CANCELLED"}
        src = node.upstream_task()
        if not src:
            self.report({"ERROR"}, "Upstream asset isn't finished yet")
            return {"CANCELLED"}
        parts = [p.strip() for p in node.part_names.split(",") if p.strip()]
        # part_names is a parameter of completion/texture/retopo only --
        # mesh_segmentation and stylize don't take it in any API version,
        # and undocumented fields on a billed task are a risk, not a no-op.
        if node.operation in ("mesh_segmentation", "stylize_model"):
            parts = []
        extra = {}
        if node.operation == "highpoly_to_lowpoly":
            if node.quad and node.face_limit > 10000:
                self.report({"ERROR"},
                            "Quad retopology caps the face limit at 10,000")
                return {"CANCELLED"}
            extra["face_limit"] = node.face_limit
            if node.quad:
                extra["quad"] = True
        elif node.operation == "texture_model":
            extra["texture_quality"] = node.texture_quality
        elif node.operation == "stylize_model":
            extra["style"] = node.style
            if node.style == "minecraft":
                extra["block_size"] = node.block_size

        try:
            if node.operation == "mesh_segmentation":
                # Deterministic v3 route: the legacy v2 task type only ever
                # ran the v1 geometry model, with no granularity control.
                node.job_id = api.start_segment(
                    src,
                    model_version=node.seg_model,
                    granularity=node.seg_granularity,
                    split_by_connectivity=node.seg_connectivity,
                    name="segment",
                )
            else:
                node.job_id = api.start_post(
                    node.operation, src,
                    name=node.operation.split("_")[0],
                    part_names=parts or None,
                    **extra,
                )
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        node.last_task = ""
        node.last_operation = node.operation
        self.report({"INFO"}, f"Submitted {node.job_id}")
        _tag_redraw()
        return {"FINISHED"}


class TRIPO_OT_node_import(bpy.types.Operator):
    bl_idname = "tripo.node_import"
    bl_label = "Import"
    bl_description = "Import the incoming asset into the scene (free)"

    node_name: bpy.props.StringProperty()
    tree_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(self.node_name, self.tree_name)
        if not node:
            self.report({"ERROR"}, "Node not found")
            return {"CANCELLED"}
        if node.is_busy():
            self.report({"ERROR"}, "This node already has a job running")
            return {"CANCELLED"}
        src = node.upstream_task()
        if not src:
            self.report({"ERROR"}, "Upstream asset isn't finished yet")
            return {"CANCELLED"}
        node.job_id = api.reimport(src, name=node.asset_name.strip() or None)
        self.report({"INFO"}, "Importing...")
        _tag_redraw()
        return {"FINISHED"}


class TRIPO_OT_node_upload(bpy.types.Operator):
    bl_idname = "tripo.node_upload"
    bl_label = "Upload"
    bl_description = ("Upload a mesh to Tripo so post-processing can run on it. "
                      "Free")

    node_name: bpy.props.StringProperty()
    tree_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(self.node_name, self.tree_name)
        if not node:
            self.report({"ERROR"}, "Node not found")
            return {"CANCELLED"}
        if node.is_busy():
            self.report({"ERROR"}, "This node already has a job running")
            return {"CANCELLED"}

        if node.source == "FILE":
            path = bpy.path.abspath(node.path.strip())
            if not path or not os.path.exists(path):
                self.report({"ERROR"}, "No such file")
                return {"CANCELLED"}
        else:
            obj = node.obj_ref or context.active_object
            if not obj or obj.type != "MESH":
                self.report({"ERROR"}, "Pick a mesh object on the node first")
                return {"CANCELLED"}
            # Capture the object on the node: a later Run Graph must not
            # push whatever happens to be selected through the paid chain.
            node.obj_ref = obj
            # Export just the selection to a temp .glb -- glb keeps materials
            # and is one of the four formats Tripo accepts.
            import tempfile
            safe = re.sub(r"[^\w.-]+", "_", obj.name)
            path = os.path.join(tempfile.gettempdir(),
                                f"tripo_upload_{safe}.glb")
            prev = [o for o in context.selected_objects]
            bpy.ops.object.select_all(action="DESELECT")
            obj.select_set(True)
            context.view_layer.objects.active = obj
            try:
                bpy.ops.export_scene.gltf(filepath=path, use_selection=True,
                                          export_format="GLB")
            except Exception as e:
                self.report({"ERROR"}, f"Export failed: {e}")
                return {"CANCELLED"}
            finally:
                for o in prev:
                    o.select_set(True)

        try:
            node.job_id = api.start_import(path, name=None)
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        node.last_task = ""
        self.report({"INFO"}, "Uploading...")
        _tag_redraw()
        return {"FINISHED"}


class TRIPO_OT_node_export(bpy.types.Operator):
    bl_idname = "tripo.node_export"
    bl_label = "Export"
    bl_description = "Convert the incoming asset and write it to disk (5 credits)"

    node_name: bpy.props.StringProperty()
    tree_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(self.node_name, self.tree_name)
        if not node:
            self.report({"ERROR"}, "Node not found")
            return {"CANCELLED"}
        if node.is_busy():
            self.report({"ERROR"}, "This node already has a job running")
            return {"CANCELLED"}
        src = node.upstream_task()
        if not src:
            self.report({"ERROR"}, "Upstream asset isn't finished yet")
            return {"CANCELLED"}

        directory = bpy.path.abspath(node.directory.strip())
        if not directory:
            self.report({"ERROR"}, "Choose an output folder")
            return {"CANCELLED"}

        # Conversion is 5 credits, +5 if ANY additional parameter is sent.
        # Only send values the user actually changed, or every export pays the
        # surcharge for defaults it never asked for.
        extra = {"format": node.fmt}
        if abs(node.scale_factor - 1.0) > 1e-6:
            extra["scale_factor"] = node.scale_factor
        if node.export_orientation != "+x":
            extra["export_orientation"] = node.export_orientation
        if node.quad:
            extra["quad"] = True
            if node.force_symmetry:
                extra["force_symmetry"] = True
        if node.use_face_limit:
            extra["face_limit"] = node.face_limit
        if node.flatten_bottom:
            extra["flatten_bottom"] = True
            extra["flatten_bottom_threshold"] = node.flatten_threshold
        if node.pivot_to_center_bottom:
            extra["pivot_to_center_bottom"] = True
        if node.fmt not in {"STL", "3MF"}:
            if node.texture_size != 2048:
                extra["texture_size"] = node.texture_size
            if node.texture_format != "JPEG":
                extra["texture_format"] = node.texture_format
        if node.fmt == "FBX":
            if node.fbx_preset != "blender":
                extra["fbx_preset"] = node.fbx_preset
            if not node.with_animation:
                extra["with_animation"] = False

        parts = [p.strip() for p in node.part_names.split(",") if p.strip()]

        try:
            node.job_id = api.start_post(
                "convert_model", src,
                name=node.filename.strip() or "asset",
                part_names=parts or None,
                save_to=directory,
                **extra,
            )
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        node.last_task = ""
        self.report({"INFO"}, f"Converting to {node.fmt}, ~{node.cost()} credits")
        _tag_redraw()
        return {"FINISHED"}


class TRIPO_OT_node_prerig(bpy.types.Operator):
    bl_idname = "tripo.node_prerig"
    bl_label = "Check"
    bl_description = "Ask whether this model can be rigged. Free"

    node_name: bpy.props.StringProperty()
    tree_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(self.node_name, self.tree_name)
        if not node:
            self.report({"ERROR"}, "Node not found")
            return {"CANCELLED"}
        src = node.upstream_task()
        if not src:
            self.report({"ERROR"}, "Upstream asset isn't finished yet")
            return {"CANCELLED"}
        try:
            node.check_job_id = api.start_prerig_check(src)
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        self.report({"INFO"}, "Checking, free")
        _tag_redraw()
        return {"FINISHED"}


class TRIPO_OT_google_image(bpy.types.Operator):
    bl_idname = "tripo.google_image"
    bl_label = "Generate Image"
    bl_description = "Generate an image with Google Gemini. Billed by Google"

    node_name: bpy.props.StringProperty()
    tree_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(self.node_name, self.tree_name)
        if not node:
            self.report({"ERROR"}, "Node not found")
            return {"CANCELLED"}
        if node.is_busy():
            self.report({"ERROR"}, "This node already has a job running")
            return {"CANCELLED"}
        if not node.prompt.strip():
            self.report({"ERROR"}, "Prompt is empty")
            return {"CANCELLED"}

        limits = google_api.REFERENCE_LIMITS.get(node.model, {})
        counts = node.role_counts()
        for role, used in counts.items():
            if used > limits.get(role, 0):
                self.report({"ERROR"},
                            f"{node.model} allows {limits.get(role, 0)} "
                            f"{role} references, got {used}")
                return {"CANCELLED"}

        try:
            node.job_id = google_api.generate(
                node.prompt.strip(),
                model=node.model,
                references=[bpy.path.abspath(p) for p in node.reference_paths()],
                aspect_ratio=node.aspect_ratio,
                image_size=node.image_size,
            )
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Generating, about ${node.cost_usd():.3f}")
        _tag_redraw()
        return {"FINISHED"}


class TRIPO_OT_google_views(bpy.types.Operator):
    bl_idname = "tripo.google_views"
    bl_label = "Generate Views"
    bl_description = ("Generate four consistent orthographic views for "
                      "multiview-to-3D. Billed by Google")

    node_name: bpy.props.StringProperty()
    tree_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(self.node_name, self.tree_name)
        if not node:
            self.report({"ERROR"}, "Node not found")
            return {"CANCELLED"}
        if node.is_busy():
            self.report({"ERROR"}, "This node already has a job running")
            return {"CANCELLED"}
        if not node.prompt.strip():
            self.report({"ERROR"}, "Describe the subject first")
            return {"CANCELLED"}
        ref = node.upstream_image() or (
            bpy.path.abspath(node.reference.strip())
            if node.reference.strip() else None)
        try:
            node.job_id = google_api.generate_views(
                node.prompt.strip(), reference=ref, model=node.model,
                image_size=node.image_size)
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Generating 4 views, about ${node.cost_usd():.2f}")
        _tag_redraw()
        return {"FINISHED"}


class TRIPO_OT_node_rig(bpy.types.Operator):
    bl_idname = "tripo.node_rig"
    bl_label = "Rig"
    bl_description = "Build a skeleton for this model (25 credits)"

    node_name: bpy.props.StringProperty()
    tree_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(self.node_name, self.tree_name)
        if not node:
            self.report({"ERROR"}, "Node not found")
            return {"CANCELLED"}
        if node.is_busy():
            self.report({"ERROR"}, "This node already has a job running")
            return {"CANCELLED"}
        src = node.upstream_task()
        if not src:
            self.report({"ERROR"}, "Upstream asset isn't finished yet")
            return {"CANCELLED"}
        try:
            node.job_id = api.start_rig(src, rig_type=node.rig_type,
                                        out_format=node.out_format,
                                        spec=node.spec,
                                        name=f"rig_{node.rig_type}")
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        node.last_task = ""
        self.report({"INFO"}, f"Rigging, {node.cost()} credits")
        _tag_redraw()
        return {"FINISHED"}


class TRIPO_OT_node_animate(bpy.types.Operator):
    bl_idname = "tripo.node_animate"
    bl_label = "Animate"
    bl_description = "Apply a preset animation to the incoming rig (10 credits)"

    node_name: bpy.props.StringProperty()
    tree_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(self.node_name, self.tree_name)
        if not node:
            self.report({"ERROR"}, "Node not found")
            return {"CANCELLED"}
        if node.is_busy():
            self.report({"ERROR"}, "This node already has a job running")
            return {"CANCELLED"}
        # An explicitly chosen rig wins; otherwise use the connected Rig node.
        rig_task = node.rig_source()
        if not rig_task:
            self.report({"ERROR"}, "Connect a finished Rig node")
            return {"CANCELLED"}
        try:
            node.job_id = api.start_retarget(
                rig_task, [node.animation], out_format=node.out_format,
                animate_in_place=node.animate_in_place,
                name=node.animation.split(":")[-1])
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        node.last_task = ""
        self.report({"INFO"}, f"Animating, {node.cost()} credits")
        _tag_redraw()
        return {"FINISHED"}


class TRIPO_OT_improve_selected(bpy.types.Operator):
    bl_idname = "tripo.improve_selected"
    bl_label = "Improve Selected in Graph"
    bl_description = ("Build a graph that uploads the selected object and runs "
                      "a Tripo operation on it. Uploading is free")

    operation: bpy.props.EnumProperty(
        name="Operation",
        items=[("texture_model", "Retexture", "Generate new textures (10 cr)"),
               ("highpoly_to_lowpoly", "Retopologise",
                "Rebuild with clean low-poly topology (30 cr)"),
               ("mesh_segmentation", "Split into parts",
                "Separate into named parts (40 cr)"),
               ("RIG", "Rig & animate",
                "Build a skeleton plus an idle animation (25 + 10 cr)"),
               ("NONE", "Just import", "Only bring it into Tripo, free")],
        default="highpoly_to_lowpoly")

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and \
            context.active_object.type == "MESH"

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=320)

    def draw(self, context):
        col = self.layout.column()
        col.label(text=f"Source: {context.active_object.name}", icon="MESH_DATA")
        col.prop(self, "operation")
        col.label(text="Upload is free; the operation is charged", icon="INFO")

    def execute(self, context):
        obj = context.active_object
        tree = None
        for ng in bpy.data.node_groups:
            if ng.bl_idname == nodes.TREE_ID:
                tree = ng
                break
        if tree is None:
            tree = bpy.data.node_groups.new("Tripo Graph", nodes.TREE_ID)

        # Offset below anything already in the tree so nodes don't overlap.
        base_y = min([n.location.y for n in tree.nodes], default=200) - 420

        src = tree.nodes.new("TripoSourceNode")
        src.location = (-700, base_y)
        src.source = "OBJECT"
        src.obj_ref = obj

        imp = tree.nodes.new("TripoImportNode")
        imp.asset_name = f"{obj.name}_improved"
        imp.at_cursor = False

        if self.operation in ("NONE",):
            imp.location = (-360, base_y)
            tree.links.new(src.outputs["Asset"], imp.inputs["Asset"])
        elif self.operation == "RIG":
            rig = tree.nodes.new("TripoRigNode")
            rig.location = (-380, base_y)
            anim = tree.nodes.new("TripoAnimateNode")
            anim.location = (-60, base_y)
            imp.location = (260, base_y)
            tree.links.new(src.outputs["Asset"], rig.inputs["Asset"])
            tree.links.new(rig.outputs["Rig"], anim.inputs["Rig"])
            tree.links.new(anim.outputs["Asset"], imp.inputs["Asset"])
        else:
            post = tree.nodes.new("TripoPostNode")
            post.location = (-380, base_y)
            post.operation = self.operation
            imp.location = (-40, base_y)
            tree.links.new(src.outputs["Asset"], post.inputs["Asset"])
            tree.links.new(post.outputs["Asset"], imp.inputs["Asset"])

        # Show the graph, and make sure the object is still the active
        # selection -- the Source node uploads whatever is selected.
        ws = bpy.data.workspaces.get(WORKSPACE_NAME)
        if ws:
            context.window.workspace = ws
            for area in ws.screens[0].areas:
                if area.type == "NODE_EDITOR":
                    area.spaces.active.node_tree = tree
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        context.view_layer.objects.active = obj

        self.report({"INFO"}, f"Graph built for {obj.name}. Press Upload, "
                              f"then run the chain")
        _tag_redraw()
        return {"FINISHED"}


# Blender can garbage-collect enum item strings built on the fly, which
# corrupts the menu. Keep a module-level reference alive.
_task_enum_cache = []


def _task_enum_items(self, context):
    """History entries, newest first, optionally filtered by kind."""
    global _task_enum_cache
    wanted = (self.kind_filter or "").strip()
    items = []
    for entry in api.history(limit=200):
        kind = str(entry.get("kind") or "generate")
        if wanted and kind != wanted:
            continue
        task = entry.get("task_id")
        if not task:
            continue
        label = entry.get("name") or (entry.get("prompt") or "")[:28] or task[:8]
        items.append((task, f"{label}  ({kind})", f"task {task}"))
    if not items:
        items = [("", "Nothing in history", "")]
    _task_enum_cache = items
    return _task_enum_cache


class TRIPO_OT_pick_task(bpy.types.Operator):
    bl_idname = "tripo.pick_task"
    bl_label = "Pick from History"
    bl_description = ("Choose a previous task instead of typing its id. "
                      "Reusing a finished task is always free")
    bl_property = "task"

    node_name: bpy.props.StringProperty()
    tree_name: bpy.props.StringProperty()
    field: bpy.props.StringProperty(default="existing_task")
    kind_filter: bpy.props.StringProperty()
    task: bpy.props.EnumProperty(items=_task_enum_items, name="Task")

    def invoke(self, context, event):
        context.window_manager.invoke_search_popup(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        node = _find_node(self.node_name, self.tree_name)
        if not node:
            self.report({"ERROR"}, "Node not found")
            return {"CANCELLED"}
        if not self.task:
            self.report({"WARNING"}, "Nothing selected")
            return {"CANCELLED"}
        setattr(node, self.field, self.task)
        self.report({"INFO"}, f"Using task {self.task[:8]}")
        _tag_redraw()
        return {"FINISHED"}


class TRIPO_OT_clear_node_task(bpy.types.Operator):
    bl_idname = "tripo.clear_node_task"
    bl_label = "Clear Reused Task"
    bl_description = "Stop reusing a previous task and run this node fresh"

    node_name: bpy.props.StringProperty()
    tree_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(self.node_name, self.tree_name)
        if not node or not hasattr(node, "existing_task"):
            self.report({"ERROR"}, "Node not found")
            return {"CANCELLED"}
        node.existing_task = ""
        _tag_redraw()
        return {"FINISHED"}


class TRIPO_OT_add_reference(bpy.types.Operator):
    bl_idname = "tripo.add_reference"
    bl_label = "Add Reference Image"
    bl_description = "Add another reference image for the model to draw on"

    node_name: bpy.props.StringProperty()
    tree_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(self.node_name, self.tree_name)
        if not node or not hasattr(node, "references"):
            self.report({"ERROR"}, "Node not found")
            return {"CANCELLED"}
        limits = google_api.REFERENCE_LIMITS.get(node.model, {})
        total = sum(limits.values())
        if len(node.references) >= total:
            self.report({"WARNING"},
                        f"{node.model} accepts at most {total} references")
            return {"CANCELLED"}
        node.references.add()
        _tag_redraw()
        return {"FINISHED"}


class TRIPO_OT_remove_reference(bpy.types.Operator):
    bl_idname = "tripo.remove_reference"
    bl_label = "Remove Reference Image"
    bl_description = "Remove this reference image"

    node_name: bpy.props.StringProperty()
    tree_name: bpy.props.StringProperty()
    index: bpy.props.IntProperty()

    def execute(self, context):
        node = _find_node(self.node_name, self.tree_name)
        if not node or not hasattr(node, "references"):
            self.report({"ERROR"}, "Node not found")
            return {"CANCELLED"}
        if 0 <= self.index < len(node.references):
            node.references.remove(self.index)
        _tag_redraw()
        return {"FINISHED"}


def _object_menu(self, context):
    obj = context.active_object
    if obj is not None and obj.type == "MESH":
        self.layout.separator()
        self.layout.operator("tripo.improve_selected", icon="SHADERFX")
        self.layout.operator("tripo.optimize", icon="MOD_DECIM")
        self.layout.operator("tripo.mark_asset", icon="ASSET_MANAGER")



class TRIPO_OT_google_view_redo(bpy.types.Operator):
    bl_idname = "tripo.google_view_redo"
    bl_label = "Regenerate View"
    bl_description = ("Regenerate just this view, keeping the other three. "
                      "One image billed by Google")

    node_name: bpy.props.StringProperty()
    tree_name: bpy.props.StringProperty()
    view: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(self.node_name, self.tree_name)
        if not node:
            self.report({"ERROR"}, "Node not found")
            return {"CANCELLED"}
        if node.is_busy():
            self.report({"ERROR"}, "This node already has a job running")
            return {"CANCELLED"}
        if self.view not in google_api.VIEW_PROMPTS:
            self.report({"ERROR"}, f"Unknown view '{self.view}'")
            return {"CANCELLED"}
        if not node.prompt.strip():
            self.report({"ERROR"}, "Describe the subject first")
            return {"CANCELLED"}
        current = dict(node.images())
        if not current:
            self.report({"ERROR"}, "Generate the views first")
            return {"CANCELLED"}
        ref = node.upstream_image() or (
            bpy.path.abspath(node.reference.strip())
            if node.reference.strip() else None)
        try:
            node.job_id = google_api.generate_views(
                node.prompt.strip(), reference=ref, model=node.model,
                image_size=node.image_size, views=(self.view,),
                base_images=current)
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Regenerating the {self.view} view")
        _tag_redraw()
        return {"FINISHED"}


class TRIPO_OT_view_image(bpy.types.Operator):
    bl_idname = "tripo.view_image"
    bl_label = "View Image"
    bl_description = ("Open this image at full resolution in an Image Editor "
                      "window. Node previews are capped at icon size; the "
                      "real file has the detail")

    path: bpy.props.StringProperty()

    def execute(self, context):
        path = bpy.path.abspath(self.path or "")
        if not path or not os.path.exists(path):
            self.report({"ERROR"}, "Image file no longer exists")
            return {"CANCELLED"}
        try:
            img = bpy.data.images.load(path, check_existing=True)
        except Exception as e:
            self.report({"ERROR"}, f"Could not load image: {e}")
            return {"CANCELLED"}
        try:
            bpy.ops.wm.window_new()
            window = context.window_manager.windows[-1]
            area = window.screen.areas[0]
            area.type = "IMAGE_EDITOR"
            area.spaces.active.image = img
        except Exception as e:
            self.report({"ERROR"}, f"Could not open a viewer window: {e}")
            return {"CANCELLED"}
        return {"FINISHED"}


classes = (
    TRIPO_OT_google_view_redo,
    TRIPO_OT_view_image,
    TRIPO_OT_render_reference,
    TRIPO_OT_optimize,
    TRIPO_OT_frame,
    TRIPO_OT_refresh_balance,
    TRIPO_OT_clear_jobs,
    TRIPO_OT_setup_keys,
    TRIPO_OT_validate_key,
    TRIPO_OT_reimport,
    TRIPO_OT_clear_history,
    TRIPO_OT_mark_asset,
    TRIPO_OT_node_generate,
    TRIPO_OT_node_process,
    TRIPO_OT_node_import,
    TRIPO_OT_node_upload,
    TRIPO_OT_node_export,
    TRIPO_OT_node_prerig,
    TRIPO_OT_google_image,
    TRIPO_OT_google_views,
    TRIPO_OT_node_rig,
    TRIPO_OT_node_animate,
    TRIPO_OT_improve_selected,
    TRIPO_OT_pick_task,
    TRIPO_OT_clear_node_task,
    TRIPO_OT_add_reference,
    TRIPO_OT_remove_reference,
)
