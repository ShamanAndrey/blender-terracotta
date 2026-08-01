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


# Which optional generation parameters each model accepts (docs 2026-08-01).
# v2.5 predates the advanced block -- the docs say outright "do NOT use"
# these with it; P1 takes the texture/size extras but none of the geometry
# ones. Sending an unsupported field to a billed endpoint is never a no-op.
_ADVANCED = {"texture_quality", "geometry_quality", "auto_size", "quad",
             "smart_low_poly", "generate_parts", "compress"}


def caps(model):
    m = str(model or "")
    if m.startswith("P1"):
        return {"texture_quality", "auto_size", "compress", "export_uv"}
    if m.startswith("v2.5"):
        return {"export_uv"}
    return _ADVANCED | {"export_uv"}


def face_limit_range(model, quad=False, smart_low_poly=False,
                     geometry_quality="standard"):
    """Documented (min, max) for face_limit under the given options."""
    if smart_low_poly:
        return (500, 10000 if quad else 20000)
    if is_p1(model):
        return (50, 20000)
    if quad:
        return (1, 150000)
    m = str(model or "")
    ultra = geometry_quality == "detailed"
    if m.startswith("v2.5"):
        return (1, 500000)
    if m.startswith("v3.0"):
        return (1, 2000000 if ultra else 1000000)
    return (1, 2000000 if ultra else 1500000)


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
    # Enum values are stored by number; numbers are append-only
    # stable so a saved node never silently changes animation.
    # Core set: works on v2.5 rigs (and, measured, on v1 too).
    ("preset:idle", "Idle", "", 0),
    ("preset:walk", "Walk", "", 1),
    ("preset:run", "Run", "", 2),
    ("preset:jump", "Jump", "", 3),
    ("preset:climb", "Climb", "", 4),
    ("preset:dive", "Dive", "", 5),
    ("preset:slash", "Slash", "", 6),
    ("preset:shoot", "Shoot", "", 7),
    ("preset:hurt", "Hurt", "", 8),
    ("preset:fall", "Fall", "", 9),
    ("preset:turn", "Turn", "", 10),
    ("preset:quadruped:walk", "Quadruped walk", "", 11),
    ("preset:hexapod:walk", "Hexapod walk", "", 12),
    ("preset:octopod:walk", "Octopod walk", "", 13),
    ("preset:serpentine:march", "Serpentine march", "", 14),
    ("preset:aquatic:march", "Aquatic march", "", 15),
    # v1.0 biped rigs only -- the full catalogue from the docs.
    ("preset:biped:afraid", "Afraid (v1 biped)", "", 100),
    ("preset:biped:agree", "Agree (v1 biped)", "", 101),
    ("preset:biped:angry_01", "Angry 01 (v1 biped)", "", 102),
    ("preset:biped:angry_02", "Angry 02 (v1 biped)", "", 103),
    ("preset:biped:angry_03", "Angry 03 (v1 biped)", "", 104),
    ("preset:biped:basketball_shot", "Basketball Shot (v1 biped)", "", 105),
    ("preset:biped:bow", "Bow (v1 biped)", "", 106),
    ("preset:biped:box_01", "Box 01 (v1 biped)", "", 107),
    ("preset:biped:box_02", "Box 02 (v1 biped)", "", 108),
    ("preset:biped:box_03", "Box 03 (v1 biped)", "", 109),
    ("preset:biped:cast_a_spell", "Cast A Spell (v1 biped)", "", 110),
    ("preset:biped:cheer", "Cheer (v1 biped)", "", 111),
    ("preset:biped:chop", "Chop (v1 biped)", "", 112),
    ("preset:biped:clap", "Clap (v1 biped)", "", 113),
    ("preset:biped:climb", "Climb (v1 biped)", "", 114),
    ("preset:biped:complain_01", "Complain 01 (v1 biped)", "", 115),
    ("preset:biped:complain_02", "Complain 02 (v1 biped)", "", 116),
    ("preset:biped:cross_body_crunch", "Cross Body Crunch (v1 biped)", "", 117),
    ("preset:biped:crossover_dribble", "Crossover Dribble (v1 biped)", "", 118),
    ("preset:biped:cry", "Cry (v1 biped)", "", 119),
    ("preset:biped:dance_01", "Dance 01 (v1 biped)", "", 120),
    ("preset:biped:dance_02", "Dance 02 (v1 biped)", "", 121),
    ("preset:biped:dance_03", "Dance 03 (v1 biped)", "", 122),
    ("preset:biped:dance_04", "Dance 04 (v1 biped)", "", 123),
    ("preset:biped:dance_05", "Dance 05 (v1 biped)", "", 124),
    ("preset:biped:dance_06", "Dance 06 (v1 biped)", "", 125),
    ("preset:biped:defeat_02", "Defeat 02 (v1 biped)", "", 126),
    ("preset:biped:defeat_03", "Defeat 03 (v1 biped)", "", 127),
    ("preset:biped:depressed", "Depressed (v1 biped)", "", 128),
    ("preset:biped:dig", "Dig (v1 biped)", "", 129),
    ("preset:biped:dive", "Dive (v1 biped)", "", 130),
    ("preset:biped:dribble", "Dribble (v1 biped)", "", 131),
    ("preset:biped:fall", "Fall (v1 biped)", "", 132),
    ("preset:biped:fire", "Fire (v1 biped)", "", 133),
    ("preset:biped:flee_01", "Flee 01 (v1 biped)", "", 134),
    ("preset:biped:flee_02", "Flee 02 (v1 biped)", "", 135),
    ("preset:biped:flip", "Flip (v1 biped)", "", 136),
    ("preset:biped:fold_arms", "Fold Arms (v1 biped)", "", 137),
    ("preset:biped:football_catch", "Football Catch (v1 biped)", "", 138),
    ("preset:biped:football_save", "Football Save (v1 biped)", "", 139),
    ("preset:biped:football_pass", "Football Pass (v1 biped)", "", 140),
    ("preset:biped:freaky", "Freaky (v1 biped)", "", 141),
    ("preset:biped:frightened", "Frightened (v1 biped)", "", 142),
    ("preset:biped:front_kick_01", "Front Kick 01 (v1 biped)", "", 143),
    ("preset:biped:front_kick_02", "Front Kick 02 (v1 biped)", "", 144),
    ("preset:biped:frustrated_01", "Frustrated 01 (v1 biped)", "", 145),
    ("preset:biped:frustrated_02", "Frustrated 02 (v1 biped)", "", 146),
    ("preset:biped:golf", "Golf (v1 biped)", "", 147),
    ("preset:biped:greet_01", "Greet 01 (v1 biped)", "", 148),
    ("preset:biped:greet_02", "Greet 02 (v1 biped)", "", 149),
    ("preset:biped:greet_03", "Greet 03 (v1 biped)", "", 150),
    ("preset:biped:greet_04", "Greet 04 (v1 biped)", "", 151),
    ("preset:biped:heart_pose", "Heart Pose (v1 biped)", "", 152),
    ("preset:biped:hit_to_body_01", "Hit To Body 01 (v1 biped)", "", 153),
    ("preset:biped:hit_to_body_02", "Hit To Body 02 (v1 biped)", "", 154),
    ("preset:biped:hit_to_head", "Hit To Head (v1 biped)", "", 155),
    ("preset:biped:hit_to_side", "Hit To Side (v1 biped)", "", 156),
    ("preset:biped:hit_to_stomach", "Hit To Stomach (v1 biped)", "", 157),
    ("preset:biped:hug", "Hug (v1 biped)", "", 158),
    ("preset:biped:hurt", "Hurt (v1 biped)", "", 159),
    ("preset:biped:idle", "Idle (v1 biped)", "", 160),
    ("preset:biped:jump_down", "Jump Down (v1 biped)", "", 161),
    ("preset:biped:jump", "Jump (v1 biped)", "", 162),
    ("preset:biped:jump_rope_01", "Jump Rope 01 (v1 biped)", "", 163),
    ("preset:biped:jump_rope_02", "Jump Rope 02 (v1 biped)", "", 164),
    ("preset:biped:laugh_01", "Laugh 01 (v1 biped)", "", 165),
    ("preset:biped:laugh_02", "Laugh 02 (v1 biped)", "", 166),
    ("preset:biped:lift_heavy", "Lift Heavy (v1 biped)", "", 167),
    ("preset:biped:look_around", "Look Around (v1 biped)", "", 168),
    ("preset:biped:make_a_call_01", "Make A Call 01 (v1 biped)", "", 169),
    ("preset:biped:make_a_call_02", "Make A Call 02 (v1 biped)", "", 170),
    ("preset:biped:pitch_baseball", "Pitch Baseball (v1 biped)", "", 171),
    ("preset:biped:play_mobile_game", "Play Mobile Game (v1 biped)", "", 172),
    ("preset:biped:play_video_game", "Play Video Game (v1 biped)", "", 173),
    ("preset:biped:press-up", "Press Up (v1 biped)", "", 174),
    ("preset:biped:run_upstairs", "Run Upstairs (v1 biped)", "", 175),
    ("preset:biped:run", "Run (v1 biped)", "", 176),
    ("preset:biped:scared_01", "Scared 01 (v1 biped)", "", 177),
    ("preset:biped:scared_02", "Scared 02 (v1 biped)", "", 178),
    ("preset:biped:scratch", "Scratch (v1 biped)", "", 179),
    ("preset:biped:shoot", "Shoot (v1 biped)", "", 180),
    ("preset:biped:shovel", "Shovel (v1 biped)", "", 181),
    ("preset:biped:sing_01", "Sing 01 (v1 biped)", "", 182),
    ("preset:biped:sing_02", "Sing 02 (v1 biped)", "", 183),
    ("preset:biped:sing_03", "Sing 03 (v1 biped)", "", 184),
    ("preset:biped:sing_04", "Sing 04 (v1 biped)", "", 185),
    ("preset:biped:sit", "Sit (v1 biped)", "", 186),
    ("preset:biped:slash", "Slash (v1 biped)", "", 187),
    ("preset:biped:sob", "Sob (v1 biped)", "", 188),
    ("preset:biped:standing_relax", "Standing Relax (v1 biped)", "", 189),
    ("preset:biped:surf", "Surf (v1 biped)", "", 190),
    ("preset:biped:swagger", "Swagger (v1 biped)", "", 191),
    ("preset:biped:swim", "Swim (v1 biped)", "", 192),
    ("preset:biped:turn", "Turn (v1 biped)", "", 193),
    ("preset:biped:victory_celebration", "Victory Celebration (v1 biped)", "", 194),
    ("preset:biped:volleyball", "Volleyball (v1 biped)", "", 195),
    ("preset:biped:wait", "Wait (v1 biped)", "", 196),
    ("preset:biped:walk", "Walk (v1 biped)", "", 197),
    ("preset:biped:warm_up", "Warm Up (v1 biped)", "", 198),
    ("preset:biped:wave_goodbye_01", "Wave Goodbye 01 (v1 biped)", "", 199),
    ("preset:biped:wave_goodbye_02", "Wave Goodbye 02 (v1 biped)", "", 200),
]


def retarget_cost(animation_count):
    return POST["animate_retarget"] * max(1, animation_count)
