"""
Parse Path of Exile 2 item clipboard text into a structured dict.

PoE 2 items copied via Ctrl+C look like:

    Item Class: Body Armours
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
    +45 to maximum Life
    +58 to Strength

Sections are separated by a line of dashes (``--------``). The final sections
are the explicit/implicit mod rolls — we flatten them all into ``mods``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

SEP = re.compile(r"^-{3,}$")


@dataclass
class ParsedItem:
    raw: str
    item_class: Optional[str] = None
    rarity: Optional[str] = None
    name: Optional[str] = None
    base: Optional[str] = None
    item_level: Optional[int] = None
    quality: Optional[int] = None
    mods: List[str] = field(default_factory=list)
    corrupted: bool = False
    unidentified: bool = False

    def to_dict(self) -> dict:
        return {
            "item_class": self.item_class,
            "rarity": self.rarity,
            "name": self.name,
            "base": self.base,
            "item_level": self.item_level,
            "quality": self.quality,
            "mods": list(self.mods),
            "corrupted": self.corrupted,
            "unidentified": self.unidentified,
            "raw": self.raw,
        }


def _split_sections(text: str) -> List[List[str]]:
    sections: List[List[str]] = [[]]
    for line in text.splitlines():
        if SEP.match(line.strip()):
            sections.append([])
        else:
            sections[-1].append(line.rstrip())
    return [s for s in sections if any(l.strip() for l in s)]


_KV = re.compile(r"^(?P<k>[A-Za-z ]+?):\s*(?P<v>.+?)\s*$")


def _kv(line: str) -> Optional[tuple[str, str]]:
    m = _KV.match(line)
    return (m.group("k").strip(), m.group("v").strip()) if m else None


_NUM = re.compile(r"-?\d+")


def _first_int(s: str) -> Optional[int]:
    m = _NUM.search(s)
    return int(m.group(0)) if m else None


# Non-mod cosmetic lines we want to strip from mod sections.
_STAT_PREFIXES = (
    "Quality:", "Armour:", "Energy Shield:", "Evasion:", "Block:",
    "Physical Damage:", "Elemental Damage:", "Chaos Damage:",
    "Critical Hit Chance:", "Critical Strike Chance:", "Attacks per Second:",
    "Reload Time:", "Spirit:", "Radius:", "Stack Size:",
    "Requirements:", "Level:", "Str:", "Dex:", "Int:",
    "Item Level:", "Sockets:",
)


def _looks_like_mod(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if s.startswith(_STAT_PREFIXES):
        return False
    # Skip obvious meta lines.
    if s in ("Corrupted", "Unidentified", "Mirrored"):
        return False
    return True


def parse_item(text: str) -> ParsedItem:
    """Parse a PoE 2 item text into ``ParsedItem``.

    Handles two shapes:

      1. Clipboard (Ctrl+C) — section-separated, has ``Item Class:`` header.
         Full structured parse.
      2. Screen OCR — no section dividers, no header. Falls back to line
         scanning: anything that looks like a mod line (contains a number,
         ``%``, ``+``, or known mod verbs) is treated as a mod candidate.
    """
    item = ParsedItem(raw=text)
    if not text or not text.strip():
        return item

    sections = _split_sections(text)
    if not sections:
        return item

    # --- Header section: class, rarity, name, base ---
    header = sections[0]
    for line in header:
        kv = _kv(line)
        if kv and kv[0] == "Item Class":
            item.item_class = kv[1]
        elif kv and kv[0] == "Rarity":
            item.rarity = kv[1]

    # Name/base lines are any non-KV lines in the header.
    name_lines = [l for l in header if _kv(l) is None and l.strip()]
    if name_lines:
        item.name = name_lines[0].strip()
        if len(name_lines) > 1:
            item.base = name_lines[1].strip()
        else:
            item.base = item.name

    # --- Scan remaining sections for item level / quality / mods / flags ---
    mod_lines: List[str] = []
    for section in sections[1:]:
        is_mod_section = True
        for line in section:
            s = line.strip()
            if not s:
                continue
            if s == "Corrupted":
                item.corrupted = True
                is_mod_section = False
                continue
            if s == "Unidentified":
                item.unidentified = True
                is_mod_section = False
                continue
            kv = _kv(line)
            if kv:
                key, val = kv
                if key == "Item Level":
                    item.item_level = _first_int(val)
                    is_mod_section = False
                elif key == "Quality":
                    item.quality = _first_int(val)
                    is_mod_section = False
                elif key in ("Requirements", "Sockets"):
                    is_mod_section = False
                elif key.startswith(("Armour", "Evasion", "Energy Shield",
                                     "Block", "Physical Damage", "Elemental",
                                     "Chaos", "Critical", "Attacks", "Spirit",
                                     "Reload", "Stack Size", "Radius",
                                     "Level", "Str", "Dex", "Int")):
                    is_mod_section = False
                else:
                    # Unknown key — treat as mod text (rare in PoE 2).
                    pass
        if is_mod_section:
            for line in section:
                if _looks_like_mod(line):
                    mod_lines.append(line.strip())

    item.mods = mod_lines

    # --- OCR fallback ------------------------------------------------
    # In-game tooltips don't render --------- separators, so the main
    # parser above puts everything into "sections[0]" and finds no mods.
    # When that happens we line-scan the entire text for mod-shaped lines.
    if not item.mods and len(sections) <= 1:
        item.mods = _scan_mod_candidates(text)

    return item


_JUNK_EXACT = {
    "tablet", "waystone", "jewel", "relic", "inspect", "alt inspect",
    "corrupted", "unidentified", "mirrored",
}
_JUNK_STARTS = (
    "can be used", "adds a mirror", "adds a breach", "adds a ritual",
    "right click", "left click", "shift click", "requirements",
)
_JUNK_CONTAINS = ("uses remaining", "item level:")
_MOD_VERBS = (
    "increased", "reduced", "more ", "less ", "pauses", "pause the",
    "contains", "contain an", "contain a", "chance to", "adds a",
    "to all", "to maximum", "to monster", "faster", "slower",
    "per ", "of a ", "become ", "add an additional",
)


def _scan_mod_candidates(text: str) -> List[str]:
    """Line scan — return plausible mod lines from free-form tooltip text."""
    out: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or len(line) > 140:
            continue
        low = line.lower()
        if low in _JUNK_EXACT:
            continue
        if any(low.startswith(p) for p in _JUNK_STARTS):
            continue
        if any(s in low for s in _JUNK_CONTAINS):
            continue
        # Positive signals: a digit, a % or +, or a known mod verb.
        has_digit = any(ch.isdigit() for ch in line)
        has_mark = "%" in line or "+" in line
        has_verb = any(v in low for v in _MOD_VERBS)
        if not (has_digit or has_mark or has_verb):
            continue
        # Skip lines that look like item names (all-caps short phrases).
        alpha_chars = [c for c in line if c.isalpha()]
        if alpha_chars and all(c.isupper() for c in alpha_chars) and len(line) < 60:
            # "OTHERWORLDLY COMMANDMENT" etc — skip unless it also has digits.
            if not has_digit:
                continue
        out.append(line)
    return out


if __name__ == "__main__":  # manual smoke test
    sample = """Item Class: Body Armours
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
+45 to maximum Life
+58 to Strength
+35% to Fire Resistance
"""
    p = parse_item(sample)
    import json
    print(json.dumps(p.to_dict(), indent=2))
