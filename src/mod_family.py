"""Convert canonical ``mod_family`` strings into runtime match regexes.

The poewiz spec ships mod families with ``#`` placeholders:

    "#% increased Quantity of Items found in Map"   — numeric
    "Pause the # Encounter timer"                   — keyword
    "+# to Monster Pack Size in Map"                — numeric

One placeholder semantics, two variable kinds:
  * numeric slots   — the rendered token is digits (``30``)
  * keyword slots   — the rendered token is a word (``Breach``)

A permissive ``\\S+?`` capture handles both so the client doesn't need to
classify. Only the first placeholder is named ``value`` so
``target_matcher.Rule`` can apply ``min_value``/``max_value`` bounds when
the slot is numeric; non-numeric captures fail the float cast and are
treated as "any match is a hit" (which is exactly what "Contains Breach"
and similar boolean-ish mods want).
"""

from __future__ import annotations

import re


HASH_TOKEN = "\x00HASH\x00"  # picked so it survives re.escape intact


def mod_family_to_regex(mod_family: str) -> str:
    """Return an anchored regex that matches one tooltip mod line.

    Tolerances baked in:
      * Optional leading ``+``/``-`` on the rendered mod (PoE tooltips use
        ``+25%`` while trade data is usually unsigned).
      * Trailing punctuation/whitespace.
    """
    if not mod_family:
        return r"^$"
    # Swap in a stable sentinel, escape, then substitute placeholders so
    # we don't have to hunt for escape variants of ``#``.
    placeholder = mod_family.replace("#", HASH_TOKEN)
    escaped = re.escape(placeholder)
    parts = escaped.split(re.escape(HASH_TOKEN))
    out = parts[0]
    for idx, tail in enumerate(parts[1:]):
        name = "value" if idx == 0 else f"value{idx + 1}"
        out += rf"(?P<{name}>\S+?)"
        out += tail
    # Tolerances: optional leading sign on the tooltip mod line.
    return rf"^[+\-]?\s*{out}\s*$"


def mod_family_display(mod_family: str) -> str:
    """Human-readable display — unchanged for now, kept centralized."""
    return mod_family
