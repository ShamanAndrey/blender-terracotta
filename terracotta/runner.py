"""Graph execution: run a node tree in dependency order."""

import bpy

from . import api, nodes
from .utils import _tag_redraw


_NODE_OPS = {
    "GoogleImageNode": "tripo.google_image",
    "GoogleViewsNode": "tripo.google_views",
    "TripoGenerateNode": "tripo.node_generate",
    "TripoSourceNode": "tripo.node_upload",
    "TripoPostNode": "tripo.node_process",
    "TripoRigNode": "tripo.node_rig",
    "TripoAnimateNode": "tripo.node_animate",
    "TripoExportNode": "tripo.node_export",
    "TripoImportNode": "tripo.node_import",
}


def _graph_order(tree):
    """Nodes in dependency order (topological sort).

    A node can only run once everything feeding it has finished, which is the
    whole point of the graph -- post-processing takes the upstream task id.
    """
    order, visiting, done = [], set(), set()

    def visit(node):
        if node.name in done:
            return
        if node.name in visiting:
            raise ValueError(f"Cycle in graph at '{node.name}'")
        visiting.add(node.name)
        for sock in node.inputs:
            for link in sock.links:
                visit(link.from_node)
        visiting.discard(node.name)
        done.add(node.name)
        order.append(node)

    for node in tree.nodes:
        if node.bl_idname in _NODE_OPS:
            visit(node)
    return order


def _launch(node, tree_name):
    """Start one node's operator, addressed to its own tree.

    Two guards, both money-related:
    - the operator gets the tree name, because node names repeat across trees
      (every bundled example has a "Generate 3D") and a global lookup can act
      on -- and charge for -- a different graph's node;
    - a node that already holds a result is skipped, because re-running rig /
      animate / process / export re-bills for work that is already done.
      A node's own button always forces a run.

    Returns True if an operator was invoked.
    """
    has = getattr(node, "has_result", None)
    if callable(has) and has():
        return False
    op_id = _NODE_OPS.get(node.bl_idname)
    if not op_id:
        return False
    category, op_name = op_id.split(".")
    getattr(getattr(bpy.ops, category), op_name)(
        node_name=node.name, tree_name=tree_name)
    return True


class TRIPO_OT_run_graph(bpy.types.Operator):
    bl_idname = "tripo.run_graph"
    bl_label = "Run Graph"
    bl_description = ("Run nodes that don't already have a result, in "
                      "dependency order. Stops on the first failure; a "
                      "node's own button forces a re-run")

    _timer = None
    _queue = None
    _current = None
    _running = False   # one Run Graph at a time; two would race the queue

    @classmethod
    def poll(cls, context):
        space = context.space_data
        tree = getattr(space, "edit_tree", None)
        return tree is not None and tree.bl_idname == nodes.TREE_ID

    def _quote(self, tree):
        """(order, [(name, credits, usd), ...]) for nodes that would run."""
        order = _graph_order(tree)
        lines = []
        for node in order:
            if node.bl_idname not in _NODE_OPS:
                continue
            has = getattr(node, "has_result", None)
            if callable(has) and has():
                continue
            busy = getattr(node, "is_busy", None)
            if callable(busy) and busy():
                continue
            cost = getattr(node, "cost", None)
            usd = getattr(node, "cost_usd", None)
            lines.append((node.name,
                          cost() if callable(cost) else 0,
                          usd() if callable(usd) else 0.0))
        return order, lines

    def execute(self, context):
        # Reached from the dialog's OK, or directly from scripts/tests.
        if TRIPO_OT_run_graph._running:
            self.report({"WARNING"}, "Run Graph is already running")
            return {"CANCELLED"}
        tree = bpy.data.node_groups.get(getattr(self, "_tree", "") or "") or \
            getattr(context.space_data, "edit_tree", None)
        if tree is None:
            self.report({"ERROR"}, "No Tripo node tree in view")
            return {"CANCELLED"}
        try:
            order = _graph_order(tree)
        except ValueError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        if not order:
            self.report({"WARNING"}, "Nothing to run")
            return {"CANCELLED"}

        self._queue = [n.name for n in order]
        self._current = None
        self._tree = tree.name
        wm = context.window_manager
        # Modal rather than blocking: generations take minutes and the UI must
        # stay responsive (and cancellable) throughout.
        TRIPO_OT_run_graph._running = True
        self._timer = wm.event_timer_add(0.5, window=context.window)
        wm.modal_handler_add(self)
        self.report({"INFO"}, f"Running {len(self._queue)} nodes")
        return {"RUNNING_MODAL"}

    def invoke(self, context, event):
        """Quote the whole chain before anything is billed.

        Every button prices itself, but nobody added up the chain -- and
        the chain is where surprise spend lives.
        """
        if TRIPO_OT_run_graph._running:
            self.report({"WARNING"}, "Run Graph is already running")
            return {"CANCELLED"}
        tree = context.space_data.edit_tree
        try:
            order, lines = self._quote(tree)
        except ValueError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        if not order:
            self.report({"WARNING"}, "Nothing to run")
            return {"CANCELLED"}
        if not lines:
            self.report({"INFO"},
                        "Every node already has a result -- nothing to pay "
                        "for. Node buttons force re-runs")
            return {"CANCELLED"}
        self._lines = lines
        self._tree = tree.name
        return context.window_manager.invoke_props_dialog(self, width=340)

    def draw(self, context):
        col = self.layout.column()
        col.label(text=f"{len(self._lines)} node(s) will run:")
        for name, credits, usd in self._lines:
            tags = []
            if credits:
                tags.append(f"{credits} cr")
            if usd:
                tags.append(f"${usd:.2f}")
            col.label(text=f"    {name}  -  {' + '.join(tags) or 'free'}")
        total_cr = sum(c for _, c, _ in self._lines)
        total_usd = sum(u for _, _, u in self._lines)
        parts = []
        if total_cr:
            parts.append(f"~{total_cr} credits")
        if total_usd:
            parts.append(f"~${total_usd:.2f}")
        col.separator()
        col.label(text="Total: " + (" + ".join(parts) or "free"),
                  icon="FUND")

    def _node(self, name):
        tree = bpy.data.node_groups.get(self._tree)
        return tree.nodes.get(name) if tree else None

    def modal(self, context, event):
        if event.type == "ESC":
            return self._finish(context, "Cancelled")
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        # Wait for the node currently running.
        if self._current:
            node = self._node(self._current)
            state = api.status(getattr(node, "job_id", "")).get("state")
            if state in ("done", None, "unknown"):
                self._current = None
            elif state == "error":
                return self._finish(context, f"'{self._current}' failed")
            else:
                return {"RUNNING_MODAL"}

        if not self._queue:
            return self._finish(context, "Graph complete")

        name = self._queue.pop(0)
        node = self._node(name)
        if node is None:
            return {"RUNNING_MODAL"}

        busy = getattr(node, "is_busy", None)
        if callable(busy) and busy():
            # A job started from the node's own button is already in flight;
            # wait for it instead of submitting -- and billing -- a second.
            self._current = name
            return {"RUNNING_MODAL"}

        try:
            started = _launch(node, self._tree)
        except Exception as e:
            return self._finish(context, f"'{name}': {e}")

        if started and getattr(node, "job_id", ""):
            self._current = name
        _tag_redraw()
        return {"RUNNING_MODAL"}

    def _finish(self, context, message):
        TRIPO_OT_run_graph._running = False
        wm = context.window_manager
        if self._timer:
            wm.event_timer_remove(self._timer)
            self._timer = None
        self._queue, self._current = None, None
        self.report({"INFO"}, message)
        _tag_redraw()
        return {"FINISHED"}


def _node_header(self, context):
    tree = getattr(context.space_data, "edit_tree", None)
    if tree is not None and tree.bl_idname == nodes.TREE_ID:
        row = self.layout.row(align=True)
        row.menu("TRIPO_MT_examples", text="Examples", icon="PRESET")
        row.operator("tripo.run_graph", icon="PLAY", text="Run Graph")
        row.operator("tripo.recover_tasks", icon="FILE_REFRESH", text="")
        row.operator("tripo.setup_keys", icon="KEY_HLT", text="")


class TRIPO_OT_recover_tasks(bpy.types.Operator):
    bl_idname = "tripo.recover_tasks"
    bl_label = "Recover Task IDs"
    bl_description = ("Match nodes that lost their task id against history, so "
                      "finished work isn't regenerated after a restart")

    def execute(self, context):
        entries = api.history(limit=999)
        by_job = {e["job"]: e for e in entries
                  if e.get("job") and e.get("task_id")}

        recovered = []
        for tree in bpy.data.node_groups:
            if tree.bl_idname != nodes.TREE_ID:
                continue
            for node in tree.nodes:
                if getattr(node, "last_task", "") or not hasattr(node, "last_task"):
                    continue
                # Job id only: it was stamped at launch and is saved with
                # the file, so it survives renames. Matching on display names
                # bound wrong tasks to same-named nodes -- no fallback.
                hit = by_job.get(getattr(node, "job_id", ""))
                if hit:
                    node.last_task = hit["task_id"]
                    recovered.append(f"{node.name} -> {hit['task_id'][:8]}")

        if recovered:
            self.report({"INFO"}, f"Recovered {len(recovered)}: "
                                  + ", ".join(recovered[:3]))
        else:
            self.report({"WARNING"}, "Nothing to recover")
        _tag_redraw()
        return {"FINISHED"}



classes = (
    TRIPO_OT_run_graph,
    TRIPO_OT_recover_tasks,
)
