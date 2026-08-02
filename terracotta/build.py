"""Procedural builders for manufactured objects.

Real doors, frames and trim are dead straight and symmetrical. Generative 3D
gives them an organic wobble that reads as wrong, so anything you could describe
with a handful of numbers is better built here -- exact, free, and sized to the
scene instead of the scene being sized around it.

Geometry is built from mesh data rather than bpy.ops, which behaves
unpredictably in the MCP execution context.
"""

import math

import bpy


# --------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------

def box(name, p0, p1, mat=None, collection=None):
    """Axis-aligned box from two opposite corners."""
    (x0, y0, z0), (x1, y1, z1) = p0, p1
    verts = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
             (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    faces = [(0, 1, 2, 3), (7, 6, 5, 4), (0, 4, 5, 1),
             (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0)]
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.update()
    ob = bpy.data.objects.new(name, me)
    (collection or bpy.context.collection).objects.link(ob)
    if mat:
        ob.data.materials.append(mat)
    return ob


def cylinder(name, centre, radius, length, axis="Z", segments=24, mat=None,
             collection=None):
    """Closed cylinder centred on `centre`, running along `axis`."""
    cx, cy, cz = centre
    verts, faces = [], []
    for end in (-length / 2, length / 2):
        for i in range(segments):
            a = 2 * math.pi * i / segments
            u, v = radius * math.cos(a), radius * math.sin(a)
            if axis == "Y":
                verts.append((cx + u, cy + end, cz + v))
            elif axis == "X":
                verts.append((cx + end, cy + u, cz + v))
            else:
                verts.append((cx + u, cy + v, cz + end))
    for i in range(segments):
        j = (i + 1) % segments
        faces.append((i, j, segments + j, segments + i))
    faces.append(tuple(range(segments)))
    faces.append(tuple(range(2 * segments - 1, segments - 1, -1)))
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.update()
    ob = bpy.data.objects.new(name, me)
    (collection or bpy.context.collection).objects.link(ob)
    if mat:
        ob.data.materials.append(mat)
    return ob


def material(name, rgb, roughness=0.5, metallic=0.0, alpha=1.0, transmission=0.0):
    """Principled material, reusing one of the same name if it exists."""
    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*rgb, 1.0)
    b.inputs["Roughness"].default_value = roughness
    b.inputs["Metallic"].default_value = metallic
    if transmission and "Transmission Weight" in b.inputs:
        b.inputs["Transmission Weight"].default_value = transmission
    if alpha < 1.0:
        b.inputs["Alpha"].default_value = alpha
        m.blend_method = "BLEND"
    return m


def join(names, result_name):
    """Merge objects into one, so a built door behaves as a single asset."""
    objs = [bpy.data.objects[n] for n in names if n in bpy.data.objects]
    if not objs:
        return None
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    bpy.ops.object.join()
    objs[0].name = result_name
    return objs[0]


# --------------------------------------------------------------------------
# Assemblies
# --------------------------------------------------------------------------

def door(name="Door", width=1.02, height=2.03, thickness=0.042,
         centre=(0.0, -2.55), wall_face=-2.6, casing=True,
         panel_mat=None, metal_mat=None):
    """Panelled interior door with a lever handle, standing on z=0.

    `centre` is (x, y) of the slab; `wall_face` is the y of the wall face the
    architrave sits against.
    """
    panel_mat = panel_mat or material("PaintWhite", (0.93, 0.93, 0.92), 0.35)
    metal_mat = metal_mat or material("Chrome", (0.8, 0.8, 0.82), 0.15, metallic=1.0)

    cx, cy = centre
    y0, y1 = cy - thickness / 2, cy + thickness / 2
    x0, x1 = cx - width / 2, cx + width / 2
    made = []

    made.append(box(f"{name}_Slab", (x0, y0, 0.004), (x1, y1, height), panel_mat))

    # Recessed panels read as a door far more than a bare slab does.
    rail, relief = 0.105, 0.010
    for i, (pz0, pz1) in enumerate([(0.14, height * 0.50),
                                    (height * 0.555, height - 0.14)]):
        made.append(box(f"{name}_PanelF{i}", (x0 + rail, y0 - relief, pz0),
                        (x1 - rail, y0, pz1), panel_mat))
        made.append(box(f"{name}_PanelB{i}", (x0 + rail, y1, pz0),
                        (x1 - rail, y1 + relief, pz1), panel_mat))

    # Handle on both faces.
    hx, hz = x1 - 0.085, 1.05
    for tag, base, d in (("F", y0, -1), ("B", y1, 1)):
        made.append(cylinder(f"{name}_Rose{tag}", (hx, base + 0.012 * d, hz),
                             0.030, 0.024, axis="Y", mat=metal_mat))
        made.append(cylinder(f"{name}_Stem{tag}", (hx, base + 0.032 * d, hz),
                             0.011, 0.050, axis="Y", mat=metal_mat))
        made.append(box(f"{name}_Lever{tag}",
                        (hx - 0.125, base + 0.045 * d - 0.011, hz - 0.012),
                        (hx + 0.012, base + 0.045 * d + 0.011, hz + 0.012),
                        metal_mat))

    if casing:
        ow, oh, tw, tp = width + 0.04, height + 0.03, 0.06, 0.018
        fy = wall_face
        made.append(box(f"{name}_CasingL", (cx - ow / 2 - tw, fy, 0),
                        (cx - ow / 2, fy + tp, oh + tw), panel_mat))
        made.append(box(f"{name}_CasingR", (cx + ow / 2, fy, 0),
                        (cx + ow / 2 + tw, fy + tp, oh + tw), panel_mat))
        made.append(box(f"{name}_CasingT", (cx - ow / 2 - tw, fy, oh),
                        (cx + ow / 2 + tw, fy + tp, oh + tw), panel_mat))

    return join([o.name for o in made], name)


def window(name="Window", width=0.90, height=1.22, sill_z=0.90,
           y_face=2.5, depth=0.10, cols=2, rows=3,
           frame_mat=None, glass_mat=None):
    """Casement window with glazing bars, filling an opening in a wall.

    `y_face` is the inner wall face; the frame spans `depth` through the wall.
    """
    frame_mat = frame_mat or material("PaintWhite", (0.93, 0.93, 0.92), 0.35)
    glass_mat = glass_mat or material("Glass", (0.85, 0.9, 0.92), 0.05,
                                      alpha=0.15, transmission=0.9)

    x0, x1 = -width / 2, width / 2
    z0, z1 = sill_z, sill_z + height
    ya, yb = y_face, y_face + depth
    f = 0.055                      # frame member width
    made = []

    made.append(box(f"{name}_FrameL", (x0, ya, z0), (x0 + f, yb, z1), frame_mat))
    made.append(box(f"{name}_FrameR", (x1 - f, ya, z0), (x1, yb, z1), frame_mat))
    made.append(box(f"{name}_FrameB", (x0, ya, z0), (x1, yb, z0 + f), frame_mat))
    made.append(box(f"{name}_FrameT", (x0, ya, z1 - f), (x1, yb, z1), frame_mat))

    # Glazing bars, thinner and set on the inner face like real ones.
    b, ym = 0.022, (ya + yb) / 2
    iw0, iw1, ih0, ih1 = x0 + f, x1 - f, z0 + f, z1 - f
    for c in range(1, cols):
        cx = iw0 + (iw1 - iw0) * c / cols
        made.append(box(f"{name}_BarV{c}", (cx - b / 2, ym - 0.012, ih0),
                        (cx + b / 2, ym + 0.012, ih1), frame_mat))
    for r in range(1, rows):
        cz = ih0 + (ih1 - ih0) * r / rows
        made.append(box(f"{name}_BarH{r}", (iw0, ym - 0.012, cz - b / 2),
                        (iw1, ym + 0.012, cz + b / 2), frame_mat))

    made.append(box(f"{name}_Glass", (iw0, ym - 0.003, ih0),
                    (iw1, ym + 0.003, ih1), glass_mat))

    # Interior sill, slightly proud of the wall.
    made.append(box(f"{name}_Sill", (x0 - 0.05, ya - 0.045, z0 - 0.035),
                    (x1 + 0.05, ya + 0.01, z0), frame_mat))

    return join([o.name for o in made], name)


def picture_frame(name="Frame", width=0.50, height=0.70, depth=0.035,
                  border=0.035, centre=(0.0, 0.0, 1.55), facing="-X",
                  frame_mat=None, canvas_mat=None):
    """Flat framed canvas hanging on a wall."""
    frame_mat = frame_mat or material("FrameBlack", (0.05, 0.05, 0.05), 0.4)
    canvas_mat = canvas_mat or material("Canvas", (0.9, 0.89, 0.86), 0.9)

    made = []
    w, h = width / 2, height / 2
    # Build facing -Y, then rotate into place.
    for tag, p0, p1 in (
        ("L", (-w, 0, -h), (-w + border, depth, h)),
        ("R", (w - border, 0, -h), (w, depth, h)),
        ("B", (-w, 0, -h), (w, depth, -h + border)),
        ("T", (-w, 0, h - border), (w, depth, h)),
    ):
        made.append(box(f"{name}_{tag}", p0, p1, frame_mat))
    made.append(box(f"{name}_Canvas", (-w + border, depth * 0.45, -h + border),
                    (w - border, depth * 0.6, h - border), canvas_mat))

    ob = join([o.name for o in made], name)
    rot = {"-Y": 0, "+X": 90, "+Y": 180, "-X": 270}[facing]
    if rot:
        import mathutils
        ob.data.transform(mathutils.Matrix.Rotation(math.radians(rot), 4, "Z"))
        ob.data.update()
    ob.location = centre
    bpy.context.view_layer.update()
    return ob
