"""In-memory mod catalog — the thing the rule dialog and matcher read.

Populated by :mod:`catalog_client`, which either pulls a fresh copy from
poewiz-api.fly.dev or falls back to a cached/shipped snapshot.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class ModTier:
    """Legacy tier metadata — still used by the old seed JSON."""
    t: int
    min_value: float
    max_value: float
    ilvl: Optional[int] = None

    @classmethod
    def from_dict(cls, d: dict) -> "ModTier":
        return cls(
            t=int(d["t"]),
            min_value=float(d.get("min", 0)),
            max_value=float(d.get("max", 0)),
            ilvl=d.get("ilvl"),
        )


@dataclass
class Mod:
    """One catalog entry."""
    id: str
    item_id: str
    display_name: str
    stat: str
    affix: str
    regex: str
    # --- v1 fields (spec-aligned) ------------------------------------
    example_text: str = ""
    roll_p10: Optional[float] = None
    roll_p90: Optional[float] = None
    price_p50_div: Optional[float] = None
    price_p90_div: Optional[float] = None
    bases_seen: List[str] = field(default_factory=list)
    n_observed: int = 0
    god_mod: bool = False
    category: Optional[str] = None
    # --- legacy (pre-spec) -------------------------------------------
    tiers: List[ModTier] = field(default_factory=list)

    def min_value_for_tier(self, t: int) -> Optional[float]:
        for tier in self.tiers:
            if tier.t == t:
                return tier.min_value
        return None

    def tiers_sorted(self) -> List[ModTier]:
        return sorted(self.tiers, key=lambda x: x.t)

    def suggested_min_value(self) -> Optional[float]:
        if self.roll_p10 is not None:
            return self.roll_p10
        if self.tiers:
            return min(t.min_value for t in self.tiers)
        return None


@dataclass
class ItemType:
    id: str
    display_name: str
    mods: Dict[str, Mod] = field(default_factory=dict)


class ModDB:
    """Catalog loaded from cache/remote/shipped JSON."""

    def __init__(self) -> None:
        self.items: Dict[str, ItemType] = {}
        self.catalog_version: str = "unknown"
        self._sources: List[str] = []

    # --- loading (legacy seed JSON path) -------------------------------

    @classmethod
    def load(cls, data_dir: Path) -> "ModDB":
        """Read every ``data/*.json`` shipped with the app (offline fallback)."""
        db = cls()
        if not data_dir.is_dir():
            return db
        for fp in sorted(data_dir.glob("*.json")):
            try:
                with fp.open("r", encoding="utf-8") as f:
                    payload = json.load(f)
            except Exception:
                continue
            db._ingest_legacy(payload, str(fp.name))
        return db

    def _ingest_legacy(self, payload: dict, source: str) -> None:
        version = payload.get("catalog_version")
        if version:
            self.catalog_version = version
        self._sources.append(source)
        for item_id, item_data in (payload.get("items") or {}).items():
            if item_id not in self.items:
                self.items[item_id] = ItemType(
                    id=item_id,
                    display_name=item_data.get("display_name", item_id),
                )
            item = self.items[item_id]
            for mod_id, mod_data in (item_data.get("mods") or {}).items():
                item.mods[mod_id] = Mod(
                    id=mod_id,
                    item_id=item_id,
                    display_name=mod_data.get("display_name", mod_id),
                    stat=mod_data.get("stat", mod_id),
                    affix=mod_data.get("affix", "prefix"),
                    regex=mod_data["regex"],
                    tiers=[ModTier.from_dict(t) for t in (mod_data.get("tiers") or [])],
                )

    # --- queries -------------------------------------------------------

    def item_types(self) -> List[Tuple[str, str]]:
        return [(i.id, i.display_name) for i in self.items.values()]

    def mods_for(self, item_id: str) -> List[Mod]:
        item = self.items.get(item_id)
        if not item:
            return []
        return sorted(
            item.mods.values(),
            key=lambda m: (not m.god_mod, m.affix, m.display_name),
        )

    def get_mod(self, mod_id: str) -> Optional[Mod]:
        for item in self.items.values():
            if mod_id in item.mods:
                return item.mods[mod_id]
        return None

    @property
    def sources(self) -> List[str]:
        return list(self._sources)
