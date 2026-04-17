#!/usr/bin/env python3
"""Generate three Scooby-Doo game asset ComfyUI workflows.

Reads merge_board_item_workflow.app.json as the canonical base and produces
all three workflow files idempotently — safe to re-run at any time.

Prompt architecture in each workflow:
  [Style Guide  ⚠ Do Not Edit]  ──┐
                                    ├──► [Text Concatenate] ──► CLIPTextEncode
  [Your Prompt  ← edit this]     ──┘

Style Guide: per-workflow art direction + LoRA triggers (locked)
Your Prompt: short description of the specific asset (user edits this)
"""

import json
import copy

WORKFLOW_DIR = (
    r"c:/dev/AI-toolkit/data/comfyui-storage/ComfyUI/user/default/workflows/scooby-doo-game"
)
# Builder reads from items workflow (canonical base) and overwrites all three.
BASE_FILE = f"{WORKFLOW_DIR}/merge_board_item_workflow.app.json"

# ──────────────────────────────────────────────────────────────────────────────
# Style guides — per workflow type.
# STYLE_BASE has NO character names so it never triggers character generation.
# ──────────────────────────────────────────────────────────────────────────────

STYLE_BASE = (
    "PIVIG image style GRPZA, "
    "90s Hanna-Barbera cartoon art style, hand-drawn cel animation, "
    "bold clean black outlines, bright flat colors, "
    "clean crisp linework, vibrant saturated colors"
)

STYLE_ITEMS = (
    STYLE_BASE + ", "
    "isolated single food item or prop object, pure white background, "
    "no cartoon characters, no dogs, no people, no animals, "
    "centered product shot, object only, floating on white"
)

STYLE_CHARACTERS = (
    STYLE_BASE + ", "
    "cartoon character design, full body pose, expressive face, "
    "Scooby-Doo inspired character cast, pure white background"
)

STYLE_BACKGROUNDS = (
    STYLE_BASE + ", "
    "2D game environment art, wide establishing shot, "
    "spooky cartoon setting, no characters, full scene"
)

# ──────────────────────────────────────────────────────────────────────────────
# Node IDs
# ──────────────────────────────────────────────────────────────────────────────
NODE_ID_STYLE_GUIDE = 200
NODE_ID_CONCAT      = 201


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def remove_link(data, link_id):
    data["links"] = [l for l in data["links"] if l[0] != link_id]


def ensure_link(data, link_entry):
    """Add link only if its ID is not already present."""
    link_id = link_entry[0]
    if not any(l[0] == link_id for l in data["links"]):
        data["links"].append(link_entry)


def ensure_node(data, node_obj):
    """Add node only if its ID is not already present."""
    nid = node_obj["id"]
    if not any(n["id"] == nid for n in data["nodes"]):
        data["nodes"].append(node_obj)


# ──────────────────────────────────────────────────────────────────────────────
# Core transformations (all idempotent)
# ──────────────────────────────────────────────────────────────────────────────

def apply_common(data):
    """Apply all shared settings. Safe to call on an already-processed workflow."""
    nbi = {n["id"]: n for n in data["nodes"]}

    # 1. Fix CLIP loader: DualCLIPLoaderGGUF → DualCLIPLoader
    nbi[161]["type"] = "DualCLIPLoader"
    nbi[161]["properties"] = {"Node name for S&R": "DualCLIPLoader"}
    nbi[161]["widgets_values"] = ["t5/t5xxl_fp16.safetensors", "clip_l.safetensors", "flux"]
    nbi[161]["inputs"] = []

    # 2. Scheduler: beta, 35 steps
    nbi[59]["widgets_values"] = ["beta", 35, 1]

    # 3. Flux guidance: 3.5 on both the slider and the FluxGuidance node
    nbi[25]["widgets_values"] = [3.5, 3.5, 1]
    nbi[156]["widgets_values"] = [3.5]

    # 4. Upscaler
    nbi[38]["widgets_values"] = ["4x-UltraSharp.pth"]

    # 5. LoRA loader — update weights
    nbi[40]["widgets_values"] = [
        {},
        {"type": "PowerLoraLoaderHeaderWidget"},
        {"on": True, "lora": "PetkaV3.safetensors", "strength": 0.75, "strengthTwo": None},
        {"on": True, "lora": "game_assets_v3.safetensors", "strength": 0.5, "strengthTwo": None},
        {},
        "",
    ]

    # 6. Wire LoRA into MODEL/CLIP chain (idempotent via ensure_link)
    #    Remove legacy direct bypasses only if they still exist
    remove_link(data, 407)   # UNet → PatchModel (bypassing LoRA)
    remove_link(data, 319)   # CLIP → positive encode (bypassing LoRA)
    remove_link(data, 430)   # CLIP → negative encode (bypassing LoRA)

    ensure_link(data, [500, 160, 0, 40,  0, "MODEL"])  # UNet → LoRA
    ensure_link(data, [501, 161, 0, 40,  1, "CLIP"])   # CLIP → LoRA
    ensure_link(data, [502, 40,  0, 151, 0, "MODEL"])  # LoRA → PatchModel
    ensure_link(data, [503, 40,  1, 98,  0, "CLIP"])   # LoRA → positive encode
    ensure_link(data, [504, 40,  1, 163, 0, "CLIP"])   # LoRA → negative encode

    nbi[160]["outputs"][0]["links"] = [429, 500]
    nbi[161]["outputs"][0]["links"] = [501]
    nbi[40]["inputs"][0]["link"] = 500
    nbi[40]["inputs"][1]["link"] = 501
    nbi[40]["outputs"][0]["links"] = [502]
    nbi[40]["outputs"][1]["links"] = [503, 504]
    nbi[151]["inputs"][0]["link"] = 502
    nbi[98]["inputs"][0]["link"] = 503
    nbi[163]["inputs"][0]["link"] = 504


def apply_style_prompt_nodes(data, style_guide, user_prompt_default):
    """Add or update the Style Guide + Text Concatenate nodes.

    Flow:  node200 (style) ──550──┐
                                   ├──► node201 (concat) ──552──► node98 (CLIP encode)
           node31  (user)  ──551──┘                       ──553──► node22 (save metadata)
                                                           ──554──► node39 (save metadata)
    """
    nbi = {n["id"]: n for n in data["nodes"]}

    # Always update the user-prompt node label and reset its outgoing links
    nbi[31]["title"] = "Your Prompt"
    nbi[31]["widgets_values"] = [user_prompt_default]
    nbi[31]["outputs"][0]["links"] = [551]

    # Remove old direct links from node31 to encode/save nodes
    remove_link(data, 330)   # 31 → 98 text
    remove_link(data, 120)   # 31 → 22 metadata
    remove_link(data, 229)   # 31 → 39 metadata

    # Add / update Style Guide node (200)
    ensure_node(data, {
        "id": NODE_ID_STYLE_GUIDE,
        "type": "String Literal (Image Saver)",
        "title": "Style Guide  [do not edit]",
        "pos": [nbi[31]["pos"][0], nbi[31]["pos"][1] - 160],
        "size": [nbi[31]["size"][0], 100],
        "flags": {},
        "mode": 0,
        "order": 1,
        "inputs": [{"name": "string", "type": "STRING", "link": None}],
        "outputs": [{"name": "STRING", "type": "STRING", "links": [550], "slot_index": 0}],
        "properties": {"Node name for S&R": "String Literal (Image Saver)"},
        "widgets_values": [style_guide],
    })
    # If node already existed, update its style text
    nbi2 = {n["id"]: n for n in data["nodes"]}
    nbi2[NODE_ID_STYLE_GUIDE]["widgets_values"] = [style_guide]
    nbi2[NODE_ID_STYLE_GUIDE]["outputs"][0]["links"] = [550]

    # Add / update Concat node (201)
    ensure_node(data, {
        "id": NODE_ID_CONCAT,
        "type": "Text Concatenate",
        "title": "Combine Style + Your Prompt",
        "pos": [nbi[31]["pos"][0] + 520, nbi[31]["pos"][1] - 80],
        "size": [340, 130],
        "flags": {},
        "mode": 0,
        "order": 5,
        "inputs": [
            {"name": "text_a", "type": "STRING", "link": 550},
            {"name": "text_b", "type": "STRING", "link": 551},
            {"name": "text_c", "type": "STRING", "link": None},
            {"name": "text_d", "type": "STRING", "link": None},
        ],
        "outputs": [{"name": "STRING", "type": "STRING", "links": [552, 553, 554], "slot_index": 0}],
        "properties": {"Node name for S&R": "Text Concatenate"},
        "widgets_values": [", ", "true"],
    })
    nbi2 = {n["id"]: n for n in data["nodes"]}
    nbi2[NODE_ID_CONCAT]["inputs"][0]["link"] = 550
    nbi2[NODE_ID_CONCAT]["inputs"][1]["link"] = 551
    nbi2[NODE_ID_CONCAT]["outputs"][0]["links"] = [552, 553, 554]

    # Wire concat → encode and savers
    ensure_link(data, [550, NODE_ID_STYLE_GUIDE, 0, NODE_ID_CONCAT, 0, "STRING"])
    ensure_link(data, [551, 31,                  0, NODE_ID_CONCAT, 1, "STRING"])
    ensure_link(data, [552, NODE_ID_CONCAT,      0, 98,             1, "STRING"])
    ensure_link(data, [553, NODE_ID_CONCAT,      0, 22,             9, "STRING"])
    ensure_link(data, [554, NODE_ID_CONCAT,      0, 39,             9, "STRING"])

    nbi2[98]["inputs"][1]["link"] = 552
    nbi2[22]["inputs"][9]["link"] = 553
    nbi2[39]["inputs"][9]["link"] = 554


def apply_negative_and_paths(data, negative, save_subdir):
    nbi = {n["id"]: n for n in data["nodes"]}
    nbi[162]["widgets_values"] = [negative]
    nbi[163]["widgets_values"] = [negative]
    nbi[22]["widgets_values"] = ["%time_%seed_Original", f"FLUX/{save_subdir}/%date/", "png", 20, 7, "", "", "normal"]
    nbi[39]["widgets_values"] = ["%time_%seed_Upscale",  f"FLUX/{save_subdir}/%date/", "png", 20, 7, "", "", "normal"]


def apply_no_rembg(data):
    """Bypass rembg and wire upscaler output directly to savers."""
    nbi = {n["id"]: n for n in data["nodes"]}
    remove_link(data, 436)
    remove_link(data, 437)
    remove_link(data, 438)
    ensure_link(data, [600, 37, 0, 39, 0, "IMAGE"])
    ensure_link(data, [601, 37, 0, 36, 1, "IMAGE"])
    nbi[37]["outputs"][0]["links"] = [600, 601]
    nbi[39]["inputs"][0]["link"] = 600
    nbi[36]["inputs"][1]["link"] = 601
    nbi[165]["mode"] = 4
    nbi[164]["mode"] = 4


def apply_rembg(data):
    """Ensure rembg is active (items/characters)."""
    nbi = {n["id"]: n for n in data["nodes"]}
    # Remove background-bypass links if present
    remove_link(data, 600)
    remove_link(data, 601)
    # Restore upscaler → rembg → savers chain
    ensure_link(data, [436, 37,  0, 165, 0, "IMAGE"])
    ensure_link(data, [437, 165, 0, 39,  0, "IMAGE"])
    ensure_link(data, [438, 165, 0, 36,  1, "IMAGE"])
    nbi[37]["outputs"][0]["links"] = [436]
    nbi[165]["inputs"][0]["link"] = 436
    nbi[165]["outputs"][0]["links"] = [437, 438]
    nbi[39]["inputs"][0]["link"] = 437
    nbi[36]["inputs"][1]["link"] = 438
    nbi[165]["mode"] = 0
    nbi[164]["mode"] = 4  # disabled rembg on original stays off


# ──────────────────────────────────────────────────────────────────────────────
# Workflow factories
# ──────────────────────────────────────────────────────────────────────────────

def make_items_workflow(base):
    data = copy.deepcopy(base)
    apply_common(data)
    apply_style_prompt_nodes(data,
        style_guide=STYLE_ITEMS,
        # Default shows the correct pattern: describe physical appearance, NEVER use character-linked names.
        # e.g. instead of "Scooby Snacks" → "bone-shaped dog biscuit treats, golden-brown crunchy cookies"
        user_prompt_default=(
            "bone-shaped dog biscuit treats, small pile of golden-brown crunchy cookies, "
            "cartoon dog snack crackers, isolated food item, white background"
        ),
    )
    apply_negative_and_paths(data,
        negative=(
            "Scooby-Doo, Scooby, dog, Great Dane, Shaggy, Fred, Velma, Daphne, "
            "cartoon character, animated character, person, human, animal, fur, paws, collar, "
            "realistic, photographic, 3d render, "
            "background scene, environment, dark background, smoke, gradient, "
            "text, watermark, blurry, hands, people, characters, "
            "dark gritty style, complex background, cropped, face, eyes, nose"
        ),
        save_subdir="items",
    )
    apply_rembg(data)
    return data


def make_characters_workflow(base):
    data = copy.deepcopy(base)
    apply_common(data)
    apply_style_prompt_nodes(data,
        style_guide=STYLE_CHARACTERS,
        user_prompt_default="cartoon character full body portrait, pure white background, arms at sides, no shadow",
    )
    apply_negative_and_paths(data,
        negative=(
            "realistic, photographic, 3d render, background scene, dark background, "
            "text, watermark, blurry, multiple characters, cropped body, "
            "partial figure, dark gritty style, close-up face only, props only, no body"
        ),
        save_subdir="characters",
    )
    apply_rembg(data)
    return data


def make_backgrounds_workflow(base):
    data = copy.deepcopy(base)
    apply_common(data)
    apply_style_prompt_nodes(data,
        style_guide=STYLE_BACKGROUNDS,
        user_prompt_default="environment background scene, no characters, wide establishing shot, full scene",
    )
    apply_negative_and_paths(data,
        negative=(
            "characters, people, animals, Scooby-Doo, dog, person, human, "
            "realistic, photographic, 3d render, text, watermark, "
            "blurry, dark horror style, modern art style, anime, UI elements, HUD"
        ),
        save_subdir="backgrounds",
    )
    apply_no_rembg(data)
    return data


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main():
    with open(BASE_FILE, encoding="utf-8") as f:
        base = json.load(f)

    workflows = {
        "merge_board_item_workflow.app.json":  make_items_workflow(base),
        "merge_characters_workflow.app.json":  make_characters_workflow(base),
        "merge_backgrounds_workflow.app.json": make_backgrounds_workflow(base),
    }

    for filename, wf in workflows.items():
        out_path = f"{WORKFLOW_DIR}/{filename}"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(wf, f, indent=2)
        print(f"Written: {out_path}")

    print("Done.")


if __name__ == "__main__":
    main()
