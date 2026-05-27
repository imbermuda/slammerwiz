"""
Catalog client — talks to ``poewiz-api.fly.dev /stats/mod-catalog``.

The server is authoritative for ``mod_family`` keys. This client mirrors its
output so the gate can regex-match a tooltip locally within ~60 ms. Local
regexes are *derived* from each entry's ``mod_family`` string (containing
``#`` placeholders) via :mod:`mod_family` — no hand-curation.

Fallback order:
  1. Fresh disk cache (≤ TTL)
  2. Remote fetch (updates cache)
  3. Stale disk cache
  4. Shipped ``data/*.json`` (legacy seed, offline bootstrap only)
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List, Optional

import requests

from .mod_db import ItemType, Mod, ModDB
from .mod_family import mod_family_to_regex


SLOT_TO_ITEM_ID = {
    "Tablet":   ("precursor_tablet",   "Precursor Tablet"),
    "Waystone": ("waystone",           "Waystone"),
    "Jewel":    ("jewel",              "Jewel"),
}


class CatalogClient:
    def __init__(
        self,
        endpoint: str,
        cache_path: Path,
        fallback_dir: Path,
        ttl_sec: int = 6 * 3600,      # spec recommends 6-12h
        api_key: str = "",
        slot: str = "Tablet",
        category: Optional[str] = "slam",
        min_observed: int = 10,
        window_hours: int = 168,
        league: Optional[str] = None,
    ):
        self.endpoint = endpoint
        self.cache_path = cache_path
        self.fallback_dir = fallback_dir
        self.ttl_sec = ttl_sec
        self.api_key = api_key
        self.slot = slot
        self.category = category
        self.min_observed = min_observed
        self.window_hours = window_hours
        self.league = league
        self.last_source: str = "none"
        self.last_error: Optional[str] = None

    # --- public --------------------------------------------------------

    def load(self) -> ModDB:
        if self._cache_fresh():
            db = self._load_cache()
            if db is not None:
                self.last_source = "cache"
                return db
        try:
            payload = self._fetch()
            self._save_cache(payload)
            self.last_source = "remote"
            return self._db_from_payload(payload)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"

        db = self._load_cache()
        if db is not None:
            self.last_source = "cache (stale)"
            return db

        self.last_source = "shipped fallback"
        return ModDB.load(self.fallback_dir)

    # --- cache ---------------------------------------------------------

    def _cache_fresh(self) -> bool:
        if not self.cache_path.is_file():
            return False
        age = time.time() - self.cache_path.stat().st_mtime
        return age < self.ttl_sec

    def _load_cache(self) -> Optional[ModDB]:
        try:
            with self.cache_path.open("r", encoding="utf-8") as f:
                payload = json.load(f)
            return self._db_from_payload(payload)
        except Exception:
            return None

    def _save_cache(self, payload: dict) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with self.cache_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    # --- remote --------------------------------------------------------

    def _fetch(self) -> dict:
        headers = {"User-Agent": "SlammerWiz/1.0", "Accept": "application/json"}
        params: dict = {"min_observed": self.min_observed,
                        "window_hours": self.window_hours}
        if self.slot:
            params["slot"] = self.slot
        elif self.category:
            params["category"] = self.category
        if self.league:
            params["league"] = self.league
        resp = requests.get(self.endpoint, headers=headers, params=params, timeout=8)
        resp.raise_for_status()
        return resp.json()

    # --- payload → ModDB ----------------------------------------------

    @staticmethod
    def _item_id_for(slot: Optional[str]) -> tuple[str, str]:
        if slot and slot in SLOT_TO_ITEM_ID:
            return SLOT_TO_ITEM_ID[slot]
        return (slot or "item").lower().replace(" ", "_"), slot or "Item"

    @classmethod
    def _db_from_payload(cls, payload: dict) -> ModDB:
        db = ModDB()
        db.catalog_version = payload.get("catalog_version", "remote-unknown")
        entries = payload.get("entries") or []
        for entry in entries:
            slot = entry.get("slot")
            item_id, item_display = cls._item_id_for(slot)
            if item_id not in db.items:
                db.items[item_id] = ItemType(id=item_id, display_name=item_display)
            fam = entry["mod_family"]
            mod = Mod(
                id=fam,
                item_id=item_id,
                display_name=fam,
                stat=fam,
                affix=(slot or "prefix"),
                regex=mod_family_to_regex(fam),
                example_text=entry.get("example_text", ""),
                roll_p10=_safe_float(entry.get("roll_p10")),
                roll_p90=_safe_float(entry.get("roll_p90")),
                price_p50_div=_safe_float(entry.get("price_p50_div")),
                price_p90_div=_safe_float(entry.get("price_p90_div")),
                bases_seen=list(entry.get("bases_seen") or []),
                n_observed=int(entry.get("n_observed") or 0),
                god_mod=bool(entry.get("god_mod")),
                category=entry.get("category"),
            )
            db.items[item_id].mods[fam] = mod
        return db


def _safe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
