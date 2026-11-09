#!/usr/bin/env python3
"""Turn a written-out film recipe into a Photomation preset.

Paste the settings from wherever you found them and this writes a JSON file
into recipes/, which then appears in the editor's preset list.

Usage:
    python import_recipe.py "Kodachrome 64"          # then paste, then Ctrl-D
    python import_recipe.py "Velvia" recipe.txt
"""

import json
import re
import sys
from pathlib import Path

import recipes


def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "recipe"


def main(argv):
    name = argv[1] if len(argv) > 1 else None
    if len(argv) > 2:
        text = Path(argv[2]).read_text(encoding="utf-8")
    else:
        if sys.stdin.isatty():
            print("Paste the recipe settings, then press Ctrl-D:\n")
        text = sys.stdin.read()

    try:
        parsed = recipes.parse_recipe_text(text, name)
    except recipes.RecipeError as exc:
        raise SystemExit(f"Could not read that: {exc}")

    recipes.RECIPES_DIR.mkdir(parents=True, exist_ok=True)
    target = recipes.RECIPES_DIR / f"{slugify(parsed['name'])}.json"
    if target.exists():
        print(f"{target.name} already exists; overwriting it.")
    parsed["source"] = "imported"
    target.write_text(json.dumps(parsed, indent=2) + "\n", encoding="utf-8")

    adj = recipes.to_adjustments(parsed["settings"])
    print(f"\nWrote {target}")
    print(f"  recognised {len(parsed['settings'])} settings")
    print(f"  film look     {adj.film or '(none)'}")
    print(f"  temperature   {adj.temperature:+.0f}")
    print(f"  saturation    {adj.saturation:+.0f}")
    print(f"  clarity       {adj.clarity:+.0f}   grain {adj.grain:.0f}")
    print("\nIt will appear in the editor's preset list. Reload the page.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
