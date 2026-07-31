"""Build the bundled example graphs.

Run headless -- node trees are plain datablocks and need no window, unlike
workspaces:

    Blender -b --python tools/build_examples.py

Writes tripo_blender/examples.blend. Each example is a complete, runnable graph
with prompts already filled in, so opening one and pressing a button does
something real rather than presenting an empty canvas.
"""

import os
import sys

import bpy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tripo_blender", "examples.blend")


def enable_addon():
    zip_path = os.path.join(ROOT, "tripo_blender.zip")
    if os.path.exists(zip_path):
        bpy.ops.preferences.addon_install(filepath=zip_path, overwrite=True)
    bpy.ops.preferences.addon_enable(module="tripo_blender")


def frame(tree, label, x, y, text):
    """A labelled frame so each example explains itself on the canvas."""
    note = tree.nodes.new("NodeFrame")
    note.label = label
    note.location = (x, y)
    note.width, note.height = 460, 120
    note.use_custom_color = True
    note.color = (0.18, 0.16, 0.22)
    body = bpy.data.texts.new(f"{label} notes")
    body.write(text)
    note.text = body
    return note


def new_tree(name):
    tree = bpy.data.node_groups.new(name, "TripoNodeTree")
    tree.use_fake_user = True      # keep it even with nothing pointing at it
    return tree


def example_text_to_3d():
    t = new_tree("1 - Text to 3D")
    frame(t, "Text to 3D", -700, 460,
          "Describe an object and get a mesh.\n"
          "Cheapest route: 20 credits on v3.1.\n"
          "Press Generate, wait, then Import.")
    gen = t.nodes.new("TripoGenerateNode")
    gen.location = (-660, 260)
    gen.mode = "TEXT"
    gen.prompt = "a weathered wooden barrel with iron bands"
    gen.asset_name = "Barrel"
    imp = t.nodes.new("TripoImportNode")
    imp.location = (-280, 260)
    imp.asset_name = "Barrel"
    t.links.new(gen.outputs["Asset"], imp.inputs["Asset"])
    return t


def example_reference_images():
    t = new_tree("2 - Reference images")
    frame(t, "Reference images", -700, 460,
          "Concept art before modelling. Billed by Google, not Tripo.\n"
          "Add references and cite them as image[1], image[2] in the prompt.\n"
          "Nano Banana Pro keeps characters consistent across images.")
    img = t.nodes.new("GoogleImageNode")
    img.location = (-660, 260)
    img.model = "gemini-3-pro-image"
    img.prompt = ("a stylised fantasy market stall, front elevation, "
                  "plain background, clean silhouette")
    img.aspect_ratio = "1:1"
    return t


def example_image_to_3d():
    t = new_tree("3 - Image to 3D")
    frame(t, "Image to 3D", -1040, 460,
          "Generate a concept image, then turn it into a mesh.\n"
          "The image node feeds the 3D node directly -- no file juggling.\n"
          "Image is billed by Google; the mesh costs 30 credits.")
    img = t.nodes.new("GoogleImageNode")
    img.location = (-1000, 260)
    img.prompt = ("a single ornate treasure chest, three-quarter view, "
                  "plain neutral background")
    gen = t.nodes.new("TripoGenerateNode")
    gen.location = (-640, 260)
    gen.mode = "IMAGE"
    gen.asset_name = "Chest"
    imp = t.nodes.new("TripoImportNode")
    imp.location = (-280, 260)
    imp.asset_name = "Chest"
    t.links.new(img.outputs["Image"], gen.inputs["Image"])
    t.links.new(gen.outputs["Asset"], imp.inputs["Asset"])
    return t


def example_multiview():
    t = new_tree("4 - Multiview to 3D")
    frame(t, "Multiview to 3D", -1380, 460,
          "Four consistent views give the model far more to work with\n"
          "than a single image, which mostly helps with backs and sides.\n"
          "Four Google images, then 30 credits for the mesh.")
    img = t.nodes.new("GoogleImageNode")
    img.location = (-1340, 260)
    img.prompt = "a small robot companion, plain background"
    views = t.nodes.new("GoogleViewsNode")
    views.location = (-1000, 260)
    views.prompt = "a small robot companion"
    gen = t.nodes.new("TripoGenerateNode")
    gen.location = (-640, 260)
    gen.mode = "MULTIVIEW"
    gen.asset_name = "Robot"
    imp = t.nodes.new("TripoImportNode")
    imp.location = (-280, 260)
    imp.asset_name = "Robot"
    t.links.new(img.outputs["Image"], views.inputs["Image"])
    t.links.new(views.outputs["Views"], gen.inputs["Image"])
    t.links.new(gen.outputs["Asset"], imp.inputs["Asset"])
    return t


def example_rig_animate():
    t = new_tree("5 - Rig and animate")
    frame(t, "Rig and animate", -1380, 460,
          "Characters only. Ask for a T-pose in the prompt.\n"
          "Check is free and tells you whether rigging will work.\n"
          "Rig 25 credits, each animation 10.\n"
          "Do any remeshing BEFORE rigging -- it strips skeletons.")
    gen = t.nodes.new("TripoGenerateNode")
    gen.location = (-1340, 260)
    gen.mode = "TEXT"
    gen.model = "P1-20260311"
    gen.prompt = ("a stylised humanoid character standing in T-pose, "
                  "arms straight out, simple clothing")
    gen.asset_name = "Hero"
    rig = t.nodes.new("TripoRigNode")
    rig.location = (-1000, 260)
    anim = t.nodes.new("TripoAnimateNode")
    anim.location = (-660, 260)
    anim.animation = "preset:walk"
    imp = t.nodes.new("TripoImportNode")
    imp.location = (-300, 260)
    imp.asset_name = "HeroWalk"
    t.links.new(gen.outputs["Asset"], rig.inputs["Asset"])
    t.links.new(rig.outputs["Rig"], anim.inputs["Rig"])
    t.links.new(anim.outputs["Asset"], imp.inputs["Asset"])
    return t


def example_improve_existing():
    t = new_tree("6 - Improve an existing model")
    frame(t, "Improve an existing model", -1040, 460,
          "Works on anything: your own model, a purchase, a built prop.\n"
          "Uploading is free; you pay only for the operation.\n"
          "Retopology 30, texture 10, split into parts 40.")
    src = t.nodes.new("TripoSourceNode")
    src.location = (-1000, 260)
    src.source = "OBJECT"
    post = t.nodes.new("TripoPostNode")
    post.location = (-660, 260)
    post.operation = "highpoly_to_lowpoly"
    post.face_limit = 8000
    imp = t.nodes.new("TripoImportNode")
    imp.location = (-300, 260)
    imp.asset_name = "Improved"
    t.links.new(src.outputs["Asset"], post.inputs["Asset"])
    t.links.new(post.outputs["Asset"], imp.inputs["Asset"])
    return t


def example_export():
    t = new_tree("7 - Export for a game engine")
    frame(t, "Export for a game engine", -1040, 460,
          "Conversion runs server-side, so you get quad remeshing and\n"
          "pivot control Blender would otherwise need by hand.\n"
          "5 credits, or 10 if you change any option.")
    src = t.nodes.new("TripoSourceNode")
    src.location = (-1000, 260)
    exp = t.nodes.new("TripoExportNode")
    exp.location = (-640, 260)
    exp.fmt = "FBX"
    exp.filename = "asset"
    exp.pivot_to_center_bottom = True
    t.links.new(src.outputs["Asset"], exp.inputs["Asset"])
    return t


BUILDERS = (
    example_text_to_3d,
    example_reference_images,
    example_image_to_3d,
    example_multiview,
    example_rig_animate,
    example_improve_existing,
    example_export,
)


def main():
    enable_addon()
    trees = set()
    for build in BUILDERS:
        tree = build()
        trees.add(tree)
        print(f"  built '{tree.name}' with {len(tree.nodes)} nodes")

    bpy.data.libraries.write(OUT, trees, fake_user=True, compress=True)
    print(f"wrote {OUT} ({os.path.getsize(OUT)} bytes, {len(trees)} examples)")


if __name__ == "__main__":
    main()
