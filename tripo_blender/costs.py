"""Credit costs in one place.

The panel and the node graph both quote prices, and they must not drift apart --
a button that under-quotes is worse than one that doesn't quote at all.

Figures marked measured were confirmed by watching `consumed_credit` on real
jobs; the rest come from Tripo's pricing page.
"""

MODEL_ITEMS = [
    ("v3.1-20260211", "v3.1", "Newest H3-tier model. Highest detail"),
    ("P1-20260311", "P1", "Clean low-poly output, all-in pricing"),
    ("v3.0-20250812", "v3.0", "Previous generation"),
    ("v2.5-20250123", "v2.5", "Older, lighter meshes"),
]

TEXTURE_QUALITY_ITEMS = [
    ("standard", "Standard", "Default resolution, included in base price"),
    ("detailed", "Detailed", "High resolution (+10)"),
    ("extreme", "Extreme", "Highest resolution (+20)"),
]

GEOMETRY_QUALITY_ITEMS = [
    ("standard", "Standard", "Balanced detail and speed"),
    ("detailed", "Ultra", "Maximum detail (+20, measured)"),
]

STYLE_ITEMS = [
    ("NONE", "None", "No stylization"),
    ("lego", "Lego", "Lego bricks"),
    ("voxel", "Voxel", "Voxel form"),
    ("voronoi", "Voronoi", "Voronoi shell"),
    ("minecraft", "Minecraft", "Minecraft schematic"),
]

# Base task cost, textured at standard quality.
_BASE = {"P1-20260311": {"text": 40, "image": 50}}
_BASE_DEFAULT = {"text": 20, "image": 30}

# Post-processing, per Tripo's pricing page.
POST = {
    "highpoly_to_lowpoly": 30,
    "texture_model": 10,
    "mesh_segmentation": 40,
    "mesh_completion": 50,
    "convert_model": 5,      # +5 if any parameter beyond `format` is sent
    "animate_prerigcheck": 0,   # free
    "animate_rig": 25,
    "animate_retarget": 10,     # per animation
    "stylize_model": 0,      # priced with the source task
}

# Step up from standard texture quality; standard is already in the base price.
_TEXTURE_STEP = {"standard": 0, "detailed": 10, "extreme": 20}


def is_p1(model):
    return str(model or "").startswith("P1")


def base_cost(model, kind="text"):
    return _BASE.get(model, _BASE_DEFAULT)[kind]


def extra_cost(model, texture_quality="standard", geometry_quality="standard",
               smart_low_poly=False, generate_parts=False, quad=False):
    """Surcharges on top of the base price."""
    extra = _TEXTURE_STEP.get(texture_quality, 0)

    if is_p1(model):
        # P1 is all-in and rejects the other flags outright, so only the
        # texture step can apply. Unverified by measurement.
        return extra

    if geometry_quality == "detailed":
        extra += 20          # measured: v3.1 Ultra billed 50, not 30
    if smart_low_poly:
        extra += 10          # measured
    if generate_parts:
        extra += 20
    if quad:
        extra += 5
    return extra


def total_cost(model, kind="text", **opts):
    return base_cost(model, kind) + extra_cost(model, **opts)


CONVERT_BASE = 5
CONVERT_EXTRA = 5


def convert_cost(has_extra_params):
    """Conversion is 5, plus 5 if any parameter beyond `format` is sent.

    Measured: an export with pivot/texture/orientation options billed 10.
    """
    return CONVERT_BASE + (CONVERT_EXTRA if has_extra_params else 0)


RIG_TYPE_ITEMS = [
    ("biped", "Biped", "Humanoid, two legs"),
    ("quadruped", "Quadruped", "Four legs"),
    ("hexapod", "Hexapod", "Six legs"),
    ("octopod", "Octopod", "Eight legs"),
    ("avian", "Avian", "Bird"),
    ("serpentine", "Serpentine", "Snake-like"),
    ("aquatic", "Aquatic", "Fish-like"),
]

ANIMATION_ITEMS = [
    ("preset:idle", "Idle", ""), ("preset:walk", "Walk", ""),
    ("preset:run", "Run", ""), ("preset:jump", "Jump", ""),
    ("preset:climb", "Climb", ""), ("preset:dive", "Dive", ""),
    ("preset:slash", "Slash", ""), ("preset:shoot", "Shoot", ""),
    ("preset:hurt", "Hurt", ""), ("preset:fall", "Fall", ""),
    ("preset:turn", "Turn", ""),
    ("preset:quadruped:walk", "Quadruped walk", ""),
    ("preset:hexapod:walk", "Hexapod walk", ""),
    ("preset:octopod:walk", "Octopod walk", ""),
    ("preset:serpentine:march", "Serpentine march", ""),
    ("preset:aquatic:march", "Aquatic march", ""),
]


def retarget_cost(animation_count):
    return POST["animate_retarget"] * max(1, animation_count)
