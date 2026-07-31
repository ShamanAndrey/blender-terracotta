"""Panels -- node editor sidebar only.

There is deliberately no 3D-viewport panel: the Generate workspace is the one
product surface. The old viewport panel duplicated generation UI and drifted
from the graph twice before it was removed.
"""

import bpy

from . import api, previews
from .utils import _wrap


class TripoPanel:
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "UI"
    bl_category = "Tripo"

    @classmethod
    def poll(cls, context):
        space = context.space_data
        return getattr(space, "tree_type", "") == "TripoNodeTree"


class TRIPO_PT_jobs(TripoPanel, bpy.types.Panel):
    bl_label = "Jobs"
    bl_idname = "TRIPO_PT_jobs"

    def draw(self, context):
        layout = self.layout
        jobs = api.status()
        if not jobs:
            layout.label(text="Nothing running")
            return

        for job_id, job in jobs.items():
            state = job.get("state", "?")
            box = layout.box()
            row = box.row(align=True)

            label = job.get("name") or (job.get("prompt") or "image")[:20]
            icon = {"done": "CHECKMARK", "error": "ERROR"}.get(state, "SORTTIME")
            row.label(text=label, icon=icon)
            if job.get("credits"):
                row.label(text=f"{job['credits']}cr")

            if state == "error":
                for chunk in _wrap(str(job.get("message", "")), 34):
                    box.label(text=chunk)
            elif state == "done":
                objs = job.get("objects") or []
                thumb = previews.icon_id(f"job_{job_id}", job.get("thumb"))
                if thumb:
                    box.template_icon(icon_value=thumb, scale=6.0)
                box.label(text=", ".join(objs[:3]) or "imported")
            else:
                pct = job.get("progress")
                if pct is not None:
                    box.progress(factor=pct / 100.0, text=f"{state}  {pct}%",
                                 type="BAR")
                else:
                    box.label(text=state)
                eta = job.get("eta")
                if eta:
                    box.label(text=f"~{int(eta)}s remaining")

        layout.operator("tripo.clear_jobs", icon="TRASH")


class TRIPO_PT_library(TripoPanel, bpy.types.Panel):
    bl_label = "Library"
    bl_idname = "TRIPO_PT_library"

    def draw(self, context):
        layout = self.layout
        # Only entries with a task id can be re-imported; Google image
        # entries are files, shown on their own nodes instead.
        items = [e for e in api.history(limit=48) if e.get("task_id")][:24]
        if not items:
            layout.label(text="Nothing generated yet")
            return

        layout.label(text="Click to re-import (free)", icon="INFO")

        KIND_ICON = {
            "generate": "SHADERFX",
            "import": "MESH_DATA",
            "convert_model": "EXPORT",
            "highpoly_to_lowpoly": "MOD_DECIM",
            "texture_model": "TEXTURE",
            "mesh_segmentation": "MOD_EXPLODE",
            "mesh_completion": "MOD_BUILD",
            "stylize_model": "MOD_REMESH",
        }

        grid = layout.grid_flow(row_major=True, columns=2, even_columns=True,
                                align=True)
        for item in items:
            tid = item.get("task_id") or ""
            kind = item.get("kind") or "generate"
            cell = grid.column(align=True)
            icon = previews.icon_id(f"hist_{tid}", item.get("thumb"))
            if icon:
                cell.template_icon(icon_value=icon, scale=5.0)
            label = (item.get("name") or item.get("prompt") or "?")[:14]
            op = cell.operator("tripo.reimport", text=label,
                               icon=KIND_ICON.get(kind, "DOT"))
            op.task_id = tid
            op.name = item.get("name") or ""
            op.flavor = item.get("flavor") or ""

        layout.operator("tripo.clear_history", icon="TRASH")


classes = (
    TRIPO_PT_jobs,
    TRIPO_PT_library,
)
