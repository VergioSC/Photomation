#!/usr/bin/env python3
"""Film recipes: named presets that fill in the editor's sliders.

A "recipe" here follows the shape used by Fujifilm shooters: a film simulation
plus a handful of camera offsets (Highlight, Shadow, Color, white-balance shift
and so on). Those are CAMERA settings, applied by the camera to sensor data.
Photomation works on an already-processed photo, so a recipe here is an
APPROXIMATION of the look, not the same processing. Treat a recipe as a good
starting point that you then adjust by eye.

Recipes live as JSON in the recipes/ folder, so you can add your own without
touching code. Use import_recipe.py to turn a written-out recipe into one.
"""

import json
import re
from pathlib import Path

import config
import edit

RECIPES_DIR = Path(__file__).resolve().parent / "recipes"

# Fujifilm simulation names -> the look implemented in edit.py.
SIMULATION_ALIASES = {
    "provia": "provia", "standard": "provia", "provia/standard": "provia",
    "velvia": "velvia", "vivid": "velvia", "velvia/vivid": "velvia",
    "astia": "astia", "soft": "astia", "astia/soft": "astia",
    "classic chrome": "classic-chrome", "classicchrome": "classic-chrome",
    "classic negative": "classic-neg", "classic neg": "classic-neg",
    "nostalgic negative": "nostalgic-neg", "nostalgic neg": "nostalgic-neg",
    "eterna": "eterna", "cinema": "eterna", "eterna/cinema": "eterna",
    "pro neg std": "pro-neg-std", "pro neg standard": "pro-neg-std",
    "pro neg hi": "pro-neg-hi", "pro neg high": "pro-neg-hi",
    "acros": "acros", "acros+r": "acros-r", "acros + r": "acros-r",
    "acros+ye": "acros-g", "acros+g": "acros-g", "acros + g": "acros-g",
    "monochrome": "monochrome", "mono": "monochrome", "b&w": "monochrome",
    "sepia": "sepia",
}

# Named white-balance presets, as a rough warm/cool offset on our scale.
# These stand in for a colour temperature we cannot recover from a JPEG.
WB_MODES = {
    "auto": 0, "daylight": 0, "fine": 0, "sunny": 0,
    "shade": 16, "cloudy": 9,
    "incandescent": -26, "tungsten": -26,
    "fluorescent 1": -10, "fluorescent 2": -4, "fluorescent 3": 4,
    "underwater": -14, "kelvin": 0,
}

# How far each camera step moves our sliders. Deliberately gentle: a camera
# "Color +4" is a modest in-camera boost, and scaling it to near-double
# saturation turned noisy night shots into rainbow mush.
HIGHLIGHT_STEP = 11.0     # camera Highlight -2..+4
SHADOW_STEP = 11.0        # camera Shadow -2..+4, sign inverted (see below)
COLOR_STEP = 5.0          # camera Color -4..+4
SHARPEN_STEP = 8.0        # camera Sharpening -4..+4
CLARITY_STEP = 9.0        # camera Clarity -5..+5
WB_SHIFT_STEP = 4.0       # camera WB shift -9..+9 per axis

GRAIN_STRENGTH = {"off": 0.0, "weak": 16.0, "strong": 34.0}
GRAIN_SIZE = {"small": 1.0, "large": 2.3}
CHROME_SATURATION = {"off": 0.0, "weak": 2.0, "strong": 5.0}


class RecipeError(Exception):
    """A recipe file could not be read."""


def to_adjustments(settings):
    """Turn recipe settings into editor Adjustments.

    Two sign conventions are worth knowing:
      * Camera "Shadow +" makes shadows DARKER (more contrast). Our shadows
        slider LIFTS them, so the sign is flipped.
      * Camera "Highlight +" makes highlights brighter and harder, which is the
        same direction as ours, so it is not flipped.
    """
    get = lambda key, default=0.0: _number(settings.get(key), default)

    film = ""
    raw_sim = str(settings.get("film_simulation", "")).strip().lower()
    if raw_sim:
        film = SIMULATION_ALIASES.get(raw_sim, "")

    highlights = get("highlight") * HIGHLIGHT_STEP
    # Dynamic range settings exist to protect highlights, so DR400 pulls them down.
    dr = get("dynamic_range", 100)
    if dr >= 400:
        highlights -= 16
    elif dr >= 200:
        highlights -= 8

    wb = settings.get("white_balance") or {}
    if isinstance(wb, str):
        wb = {"mode": wb}
    mode = str(wb.get("mode", "")).strip().lower()
    temperature = WB_MODES.get(mode, 0)
    # Fujifilm shifts red and blue on separate axes; together they read as warmth.
    temperature += (_number(wb.get("red"), 0) - _number(wb.get("blue"), 0)) * WB_SHIFT_STEP

    saturation = get("color") * COLOR_STEP
    # Colour Chrome deepens already-strong colours. We have no exact equivalent,
    # so it becomes a small extra saturation push.
    chrome = str(settings.get("color_chrome", "off")).strip().lower()
    saturation += CHROME_SATURATION.get(chrome, 0.0)

    grain_text = str(settings.get("grain", "off")).strip().lower()
    grain, grain_size = _parse_grain(grain_text)

    return edit.Adjustments(
        film=film,
        temperature=max(-100, min(100, temperature)),
        exposure=get("exposure_compensation"),
        highlights=max(-100, min(100, highlights)),
        shadows=max(-100, min(100, -get("shadow") * SHADOW_STEP)),
        saturation=max(-100, min(100, saturation)),
        clarity=max(-100, min(100, get("clarity") * CLARITY_STEP)),
        sharpen=max(-100, min(100, get("sharpening") * SHARPEN_STEP)),
        grain=grain,
        grain_size=grain_size,
    )


def _parse_grain(text):
    """'Weak, Small' / 'Strong Large' / 'Off' -> (strength, size)."""
    strength = 0.0
    for word, value in GRAIN_STRENGTH.items():
        if word in text:
            strength = value
            break
    size = GRAIN_SIZE["large"] if "large" in text else GRAIN_SIZE["small"]
    return strength, size


def _number(value, default=0.0):
    """Accept 2, '+2', '-1/3', '2/3', '' and nonsense without blowing up."""
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("–", "-").replace("−", "-")
    match = re.match(r"^([+-]?\d+)\s*/\s*(\d+)$", text)
    if match:
        return float(match.group(1)) / float(match.group(2))
    match = re.match(r"^([+-]?\d*\.?\d+)", text)
    return float(match.group(1)) if match else default


def load_all(folder=None):
    """Read every recipe JSON. Returns (recipes, problems)."""
    folder = Path(folder or RECIPES_DIR)
    if not folder.is_dir():
        return [], []
    recipes, problems = [], []
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "name" not in data:
                raise RecipeError("needs at least a \"name\" and \"settings\"")
            adj = to_adjustments(data.get("settings") or {})
            recipes.append({
                "id": path.stem,
                "name": data["name"],
                "notes": data.get("notes", ""),
                "source": data.get("source", ""),
                "settings": data.get("settings") or {},
                "adjustments": adj,
            })
        except (OSError, ValueError, RecipeError) as exc:
            problems.append({"name": path.name, "reason": str(exc)})
    return recipes, problems


def find(recipe_id, folder=None):
    """One recipe by its filename stem, or None."""
    for recipe in load_all(folder)[0]:
        if recipe["id"] == recipe_id:
            return recipe
    return None


# --------------------------------------------------------------- the importer

FIELD_ALIASES = {
    "film simulation": "film_simulation",
    "dynamic range": "dynamic_range",
    "highlight": "highlight", "highlight tone": "highlight",
    "shadow": "shadow", "shadow tone": "shadow",
    "color": "color", "colour": "color",
    "noise reduction": "noise_reduction", "high iso nr": "noise_reduction",
    "sharpening": "sharpening", "sharpness": "sharpening",
    "clarity": "clarity",
    "grain effect": "grain", "grain": "grain",
    "color chrome effect": "color_chrome", "color chrome": "color_chrome",
    "color chrome effect blue": "color_chrome_blue",
    "color chrome fx blue": "color_chrome_blue",
    "white balance": "white_balance",
    "exposure compensation": "exposure_compensation",
    "iso": "iso",
}


def parse_recipe_text(text, name=None):
    """Turn a written-out recipe into recipe settings.

    Accepts the usual 'Field: value' lines in any order, ignoring anything it
    does not recognise, so pasting a few extra lines is harmless.
    """
    settings = {}
    title = name
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*• ").strip()
        if not line or ":" not in line:
            continue
        label, _, value = line.partition(":")
        key = FIELD_ALIASES.get(label.strip().lower())
        value = value.strip()
        if key is None or not value:
            continue

        if key == "white_balance":
            settings[key] = _parse_white_balance(value)
        elif key == "dynamic_range":
            settings[key] = _number(value.lower().replace("dr", ""), 100)
        elif key in ("film_simulation", "grain", "color_chrome",
                     "color_chrome_blue", "iso"):
            settings[key] = value
        else:
            settings[key] = _number(value)

    if not settings:
        raise RecipeError("no recognisable settings; expected lines like "
                          "'Highlight: +1'")
    return {"name": title or "Imported recipe", "settings": settings}


def _parse_white_balance(value):
    """'Daylight, +2 Red & -5 Blue' -> {mode, red, blue}."""
    red = re.search(r"([+-]?\d+)\s*red", value, re.I)
    blue = re.search(r"([+-]?\d+)\s*blue", value, re.I)
    mode = re.split(r"[,(]", value)[0].strip()
    # Strip a trailing shift that had no comma before it.
    mode = re.sub(r"[+-]?\d+\s*(red|blue).*$", "", mode, flags=re.I).strip(" &,")
    return {"mode": mode, "red": _number(red.group(1) if red else 0),
            "blue": _number(blue.group(1) if blue else 0)}
