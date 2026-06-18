"""Regression: incomplete clipboard captures must not read as a confident miss.

Victor, 2026-06-18: a Breach tablet was slammed while "Advanced Item
Descriptions" was on. Every auto Ctrl+C snapshot came back with ONLY the
implicit modifier — the rolled explicit affixes (including the winning
"Unstable Breaches ... 2 additional Rare Monsters ... Stabilised") never
reached the clipboard. The matcher correctly returned match=False on that
input, the gate opened, and the next click burned the roll.

``has_explicit_mods`` lets the pipeline detect that case and HOLD the gate
instead of failing open. These strings are copied verbatim from the
events.log clip-read lines of that session.

Run:  python tests/test_incomplete_read.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.item_parser import ParsedItem, has_explicit_mods  # noqa: E402


def _item(mods):
    return ParsedItem(raw="", mods=list(mods))


# (label, mods, expected_has_explicit)
CASES = [
    # --- INCOMPLETE captures from the burn session: must be False ----------
    ("advanced breach implicit-only",
     ['{ Implicit Modifier }', 'Adds an Otherworldy Breach to a Map', '10 uses remaining'],
     False),
    ("advanced abyss implicit-only",
     ['{ Implicit Modifier }', 'Adds Abysses to a Map', '10 uses remaining'],
     False),

    # --- COMPLETE captures that worked (match=True session): must be True --
    ("compact abyss with explicit",
     ['Adds Abysses to a Map (implicit)', '10 uses remaining (implicit)',
      '30% increased Rarity of Items found in Map'],
     True),
    ("compact ritual with explicit",
     ['Adds Ritual Altars to a Map (implicit)', '10 uses remaining (implicit)',
      '8% increased Pack Size in Map'],
     True),
    # --- COMPLETE advanced capture (June-4 stored format): must be True ----
    ("advanced breach full roll",
     ['{ Implicit Modifier }', 'Adds an Otherworldy Breach to a Map', '10 uses remaining',
      '{ Prefix Modifier "Empyrean" (Tier: 1) }', '5% increased Pack Size in Map',
      '{ Suffix Modifier "of Champions" (Tier: 1) }',
      'Unstable Breaches in Map spawn 2(1-3) additional Rare Monsters when Stabilised'],
     True),
]


def main() -> int:
    failures = []
    print("=== has_explicit_mods ===")
    for label, mods, expected in CASES:
        got = has_explicit_mods(_item(mods))
        ok = got == expected
        print(f"  {'ok  ' if ok else 'FAIL'} {label}: got {got}, want {expected}")
        if not ok:
            failures.append(label)
    if failures:
        print(f"\nFAILED {len(failures)}/{len(CASES)} — DO NOT SHIP.")
        return 1
    print(f"\nALL {len(CASES)} cases pass — incomplete reads detected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
