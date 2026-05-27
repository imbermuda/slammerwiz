"""Minimal offline smoke tests — runs parser + matcher without PyQt."""

from __future__ import annotations

import json

from src.item_parser import parse_item
from src.target_matcher import TargetMatcher


SAMPLE = """Item Class: Body Armours
Rarity: Rare
Doom Shell
Expert Dusk Plate
--------
Quality: +20% (augmented)
Armour: 1234 (augmented)
--------
Requirements:
Level: 72
Str: 155
--------
Item Level: 82
--------
+95 to maximum Life
+58 to Strength
+35% to Fire Resistance
"""


def run():
    item = parse_item(SAMPLE)
    assert item.item_class == "Body Armours", item.item_class
    assert item.rarity == "Rare", item.rarity
    assert item.name == "Doom Shell"
    assert item.base == "Expert Dusk Plate"
    assert item.item_level == 82
    assert item.quality == 20
    assert "+95 to maximum Life" in item.mods
    assert "+58 to Strength" in item.mods
    assert "+35% to Fire Resistance" in item.mods
    assert "Armour: 1234 (augmented)" not in item.mods

    cfg = {
        "mode": "any_of",
        "rules": [
            {"name": "Life", "regex": r"^\+(?P<value>\d+) to maximum Life$", "min_value": 80},
            {"name": "Str",  "regex": r"^\+(?P<value>\d+) to Strength$",      "min_value": 70},
        ],
    }
    m = TargetMatcher.from_config(cfg)
    r = m.evaluate(item)
    assert r.matched is True, r
    assert any(h.rule == "Life" for h in r.hits)

    # Strength at 58 should fail its rule (min 70). Life at 95 passes — any_of matches.
    cfg2 = dict(cfg, mode="all_of")
    m2 = TargetMatcher.from_config(cfg2)
    r2 = m2.evaluate(item)
    assert r2.matched is False, r2

    print("OK — parser + matcher")
    print(json.dumps(item.to_dict(), indent=2)[:400], "...")


if __name__ == "__main__":
    run()
