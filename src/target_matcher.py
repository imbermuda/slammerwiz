"""Rule engine that matches a ParsedItem against user-defined target rules.

Each rule has:
  * ``regex``      — primary pattern, applied as-is (case-insensitive, multiline).
  * ``min_value`` / ``max_value`` — numeric bounds on the ``value`` named group.

OCR backends occasionally collapse whitespace (e.g. RapidOCR renders
``+95 to maximum Life`` as ``+95tomaximumLife`` with some fonts). To stay robust
we *also* try each regex against a whitespace-stripped version of the mod text,
using a whitespace-stripped version of the pattern. This handles OCR quirks
without forcing users to write two regexes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from .item_parser import ParsedItem
from .mod_db import Mod, ModDB


_WS = re.compile(r"\s+")


def _strip_ws(s: str) -> str:
    return _WS.sub("", s)


def _strip_ws_pattern(pattern: str) -> str:
    """Remove literal whitespace from a regex pattern, including the
    ``\\ `` escaped-space forms that ``re.escape()`` emits.

    Keeps ``\\s`` / ``\\s+`` / ``\\s*`` (they still match zero whitespace
    after the target is whitespace-stripped). Whitespace inside character
    classes is left untouched.
    """
    out = []
    i = 0
    depth = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            nxt = pattern[i + 1]
            # Strip escaped literal whitespace (what re.escape produces).
            if depth == 0 and nxt.isspace():
                i += 2
                continue
            out.append(pattern[i:i + 2])
            i += 2
            continue
        if ch == "[":
            depth += 1
        elif ch == "]" and depth > 0:
            depth -= 1
        if depth == 0 and ch.isspace():
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


@dataclass
class Rule:
    name: str
    regex: re.Pattern
    regex_ws: re.Pattern  # whitespace-stripped fallback for quirky OCR
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    god_mod: bool = False
    required: bool = False  # "must have" rules — all required must hit

    @classmethod
    def from_config(cls, raw: dict, mod_db: Optional[ModDB] = None) -> "Rule":
        """Accept either:
          * Legacy regex rule: ``{"regex": "...", "min_value": N}``
          * Structured rule:  ``{"mod_id": "tablet_pack_size", "min_tier": 2}``
                              or ``{"mod_id": "...", "min_value": 13}``
        """
        required = bool(raw.get("required", False))
        mod_id = raw.get("mod_id")
        if mod_id:
            if mod_db is None:
                raise ValueError(f"rule uses mod_id '{mod_id}' but no mod_db loaded")
            mod = mod_db.get_mod(mod_id)
            if mod is None:
                raise ValueError(f"unknown mod_id '{mod_id}' — not in catalog")
            pattern = mod.regex
            min_value = raw.get("min_value")
            max_value = raw.get("max_value")
            # Tier-range rules. PoE convention: T1 = highest stat values,
            # T6 = lowest. So:
            #   * ``min_tier`` = best (lowest number) acceptable tier
            #   * ``max_tier`` = worst (highest number) acceptable tier
            # When both are set: value floor = T(max_tier).min, ceiling
            # = T(min_tier).max. When only one is set, fall back to the
            # legacy single-floor semantic so old configs keep working.
            min_tier = raw.get("min_tier")
            max_tier = raw.get("max_tier")
            if min_tier is not None and max_tier is not None:
                if min_value is None:
                    min_value = mod.min_value_for_tier(int(max_tier))
                if max_value is None:
                    max_value = mod.max_value_for_tier(int(min_tier))
                if min_value is None or max_value is None:
                    raise ValueError(
                        f"mod '{mod_id}' missing tier data for "
                        f"T{min_tier}-T{max_tier}")
            elif min_tier is not None and min_value is None:
                # Legacy "at least this tier" — set the floor, no ceiling.
                t_min = mod.min_value_for_tier(int(min_tier))
                if t_min is None:
                    raise ValueError(f"mod '{mod_id}' has no tier {min_tier}")
                min_value = t_min
            elif max_tier is not None and min_value is None:
                # "No worse than this tier" — also a floor (worst tier's min).
                t_min = mod.min_value_for_tier(int(max_tier))
                if t_min is None:
                    raise ValueError(f"mod '{mod_id}' has no tier {max_tier}")
                min_value = t_min
            if raw.get("name"):
                name = raw["name"]
            elif min_tier is not None and max_tier is not None:
                name = (f"{mod.display_name} T{min_tier}" if min_tier == max_tier
                        else f"{mod.display_name} T{min_tier}-T{max_tier}")
            elif min_tier is not None:
                name = f"{mod.display_name} T{min_tier}+"
            elif max_tier is not None:
                name = f"{mod.display_name} T{max_tier}+"
            elif min_value is not None:
                name = f"{mod.display_name} {int(min_value)}+"
            else:
                name = mod.display_name
            return cls(
                name=name,
                regex=re.compile(pattern, re.IGNORECASE | re.MULTILINE),
                regex_ws=re.compile(_strip_ws_pattern(pattern), re.IGNORECASE | re.MULTILINE),
                min_value=min_value,
                max_value=max_value,
                god_mod=bool(getattr(mod, "god_mod", False)),
                required=required,
            )

        # Legacy regex-based rule
        pattern = raw["regex"]
        return cls(
            name=raw.get("name", pattern),
            regex=re.compile(pattern, re.IGNORECASE | re.MULTILINE),
            regex_ws=re.compile(_strip_ws_pattern(pattern), re.IGNORECASE | re.MULTILINE),
            min_value=raw.get("min_value"),
            max_value=raw.get("max_value"),
            required=required,
        )

    def search(self, mod: str):
        """Try the regex on the raw mod; fall back to whitespace-stripped and
        leading-sign-stripped variants so we match regardless of how the mod
        renders (``+25%`` vs ``25%``) or how the OCR collapses spacing.
        """
        m = self.regex.search(mod)
        if m:
            return m
        # Strip leading +/- (PoE tooltips prefix numeric mods with +)
        stripped = mod.lstrip("+-").lstrip()
        if stripped != mod:
            m = self.regex.search(stripped)
            if m:
                return m
        return self.regex_ws.search(_strip_ws(mod))


@dataclass
class RuleHit:
    rule: str
    mod: str
    value: Optional[float] = None
    god_mod: bool = False


@dataclass
class MatchResult:
    matched: bool
    hits: List[RuleHit]

    @property
    def has_god(self) -> bool:
        return any(h.god_mod for h in self.hits)

    def to_dict(self) -> dict:
        return {
            "matched": self.matched,
            "hits": [h.__dict__ for h in self.hits],
            "has_god": self.has_god,
        }


class TargetMatcher:
    def __init__(self, rules: List[Rule], mode: str = "any_of"):
        self.rules = rules
        self.mode = mode  # "any_of" or "all_of"

    @classmethod
    def from_config(cls, cfg: dict, mod_db: Optional[ModDB] = None) -> "TargetMatcher":
        rules: List[Rule] = []
        for r in cfg.get("rules", []):
            try:
                rules.append(Rule.from_config(r, mod_db=mod_db))
            except Exception as e:
                # Skip bad rules rather than refusing to start the app.
                print(f"[matcher] skipping rule {r!r}: {e}")
        return cls(rules=rules, mode=cfg.get("mode", "any_of"))

    def evaluate(self, item: ParsedItem) -> MatchResult:
        hits: List[RuleHit] = []
        matched_rules: set[str] = set()
        for mod in item.mods:
            for rule in self.rules:
                m = rule.search(mod)
                if not m:
                    continue
                value: Optional[float] = None
                if "value" in m.groupdict() and m.group("value") is not None:
                    try:
                        value = float(m.group("value"))
                    except ValueError:
                        value = None
                if value is not None:
                    if rule.min_value is not None and value < rule.min_value:
                        continue
                    if rule.max_value is not None and value > rule.max_value:
                        continue
                hits.append(RuleHit(rule=rule.name, mod=mod, value=value, god_mod=rule.god_mod))
                matched_rules.add(rule.name)

        if not self.rules:
            return MatchResult(matched=False, hits=[])

        # Split rules into required (must-have) and optional.
        required = [r for r in self.rules if r.required]
        optional = [r for r in self.rules if not r.required]

        required_ok = all(r.name in matched_rules for r in required) if required else True

        if optional:
            if self.mode == "all_of":
                optional_ok = all(r.name in matched_rules for r in optional)
            else:
                optional_ok = any(r.name in matched_rules for r in optional)
        else:
            # No optional rules — the required set alone decides it.
            optional_ok = required_ok or not required

        # If there are only optional rules, drop the "required_ok" gate.
        matched = required_ok and (optional_ok if optional else True)
        return MatchResult(matched=matched, hits=hits)
