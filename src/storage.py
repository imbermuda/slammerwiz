"""SQLite storage for slam events, aligned with the poewiz ingest contract.

One row = one slam transition. Each carries a durable ``event_id`` (UUID v4)
that the server uses as an idempotency key — retries after 429 / 5xx carry
the same id so duplicates are no-ops.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


SCHEMA = """
CREATE TABLE IF NOT EXISTS slams (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id              TEXT    UNIQUE,
    ts                    TEXT    NOT NULL,
    slammed_at            TEXT,
    base_name             TEXT,
    currency_used         TEXT,
    item_level            INTEGER,
    patch                 TEXT,
    client_version        TEXT,
    before_mods_json      TEXT,
    after_mods_json       TEXT,
    source_hash           TEXT,
    user_tag              TEXT,
    league                TEXT,
    item_class            TEXT,
    rarity                TEXT,
    name                  TEXT,
    quality               INTEGER,
    corrupted             INTEGER,
    matched               INTEGER NOT NULL DEFAULT 0,
    hits_json             TEXT,
    raw                   TEXT,
    sent                  INTEGER NOT NULL DEFAULT 0,
    last_error            TEXT,
    attempts              INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_slams_sent ON slams (sent);
CREATE INDEX IF NOT EXISTS idx_slams_ts   ON slams (ts);
"""
# ``event_id`` index is created inside ``_migrate`` because the column may
# be missing from pre-pivot databases — ALTER TABLE runs first, then the
# index, so we never try to index a nonexistent column.


class Storage:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            self._archive_legacy_if_any(c)
            c.executescript(SCHEMA)
            self._migrate(c)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _archive_legacy_if_any(self, c: sqlite3.Connection) -> None:
        """Detect pre-pivot databases (NOT NULL on ``mods_json``, no ``event_id``)
        and archive the table under ``slams_v1_backup`` so SCHEMA can create a
        clean v2 table on the next statement. Data is preserved, not lost."""
        row = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='slams'"
        ).fetchone()
        if not row:
            return
        cols = {r[1] for r in c.execute("PRAGMA table_info(slams)").fetchall()}
        # Pre-pivot DB is recognizable by: has mods_json, lacks event_id.
        if "mods_json" in cols and "event_id" not in cols:
            suffix = 1
            backup = "slams_v1_backup"
            while c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (backup,),
            ).fetchone():
                suffix += 1
                backup = f"slams_v1_backup_{suffix}"
            c.execute(f"ALTER TABLE slams RENAME TO {backup}")

    def _migrate(self, c: sqlite3.Connection) -> None:
        """Best-effort add-columns for older slams.sqlite3 files."""
        cols = {row[1] for row in c.execute("PRAGMA table_info(slams)").fetchall()}
        additions = {
            "event_id":         "ALTER TABLE slams ADD COLUMN event_id TEXT",
            "slammed_at":       "ALTER TABLE slams ADD COLUMN slammed_at TEXT",
            "base_name":        "ALTER TABLE slams ADD COLUMN base_name TEXT",
            "currency_used":    "ALTER TABLE slams ADD COLUMN currency_used TEXT",
            "item_level":       "ALTER TABLE slams ADD COLUMN item_level INTEGER",
            "patch":            "ALTER TABLE slams ADD COLUMN patch TEXT",
            "client_version":   "ALTER TABLE slams ADD COLUMN client_version TEXT",
            "before_mods_json": "ALTER TABLE slams ADD COLUMN before_mods_json TEXT",
            "after_mods_json":  "ALTER TABLE slams ADD COLUMN after_mods_json TEXT",
            "source_hash":      "ALTER TABLE slams ADD COLUMN source_hash TEXT",
            "last_error":       "ALTER TABLE slams ADD COLUMN last_error TEXT",
            "attempts":         "ALTER TABLE slams ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
            "quality":          "ALTER TABLE slams ADD COLUMN quality INTEGER",
            "corrupted":        "ALTER TABLE slams ADD COLUMN corrupted INTEGER",
            "name":             "ALTER TABLE slams ADD COLUMN name TEXT",
            "rarity":           "ALTER TABLE slams ADD COLUMN rarity TEXT",
            "item_class":       "ALTER TABLE slams ADD COLUMN item_class TEXT",
            "hits_json":        "ALTER TABLE slams ADD COLUMN hits_json TEXT",
            "raw":              "ALTER TABLE slams ADD COLUMN raw TEXT",
            "league":           "ALTER TABLE slams ADD COLUMN league TEXT",
            "user_tag":         "ALTER TABLE slams ADD COLUMN user_tag TEXT",
        }
        for col, sql in additions.items():
            if col not in cols:
                try:
                    c.execute(sql)
                except sqlite3.OperationalError:
                    pass
        # Index on event_id for idempotency lookups
        try:
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_slams_eid_unique ON slams (event_id)")
        except sqlite3.OperationalError:
            pass

    # --- writes --------------------------------------------------------

    def record(
        self,
        before_mods: List[str],
        after_mods: List[str],
        item: dict,
        match: dict,
        user_tag: Optional[str],
        league: Optional[str],
        source_hash: Optional[str],
        currency_used: str = "chaos_orb",
        item_level: Optional[int] = None,
        patch: Optional[str] = None,
        client_version: Optional[str] = None,
    ) -> Tuple[int, str]:
        """Return ``(row_id, event_id)``."""
        event_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        row = (
            event_id,
            now,
            now,                                              # slammed_at — client wall clock
            item.get("base") or item.get("name"),
            currency_used,
            item_level if item_level is not None else item.get("item_level"),
            patch,
            client_version,
            json.dumps(before_mods or []),
            json.dumps(after_mods or []),
            source_hash,
            user_tag,
            league,
            item.get("item_class"),
            item.get("rarity"),
            item.get("name"),
            item.get("quality"),
            1 if item.get("corrupted") else 0,
            1 if match.get("matched") else 0,
            json.dumps(match.get("hits") or []),
            item.get("raw"),
        )
        with self._lock, self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO slams
                  (event_id, ts, slammed_at, base_name, currency_used,
                   item_level, patch, client_version, before_mods_json,
                   after_mods_json, source_hash, user_tag, league,
                   item_class, rarity, name, quality, corrupted,
                   matched, hits_json, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
            return cur.lastrowid or 0, event_id

    def record_attempt(self, row_id: int, error: Optional[str]) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE slams SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                (error, row_id),
            )

    # --- reads ---------------------------------------------------------

    def unsent(self, limit: int = 100) -> List[Tuple]:
        with self._lock, self._conn() as c:
            cur = c.execute(
                """
                SELECT id, event_id, slammed_at, base_name, currency_used,
                       item_level, patch, client_version, before_mods_json,
                       after_mods_json, source_hash, user_tag, league,
                       item_class, rarity, name, quality, corrupted,
                       matched, hits_json, raw
                FROM slams
                WHERE sent = 0
                ORDER BY id ASC
                LIMIT ?
                """,
                (limit,),
            )
            return cur.fetchall()

    def mark_sent(self, ids: Iterable[int]) -> None:
        ids = list(ids)
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._lock, self._conn() as c:
            c.execute(f"UPDATE slams SET sent = 1 WHERE id IN ({placeholders})", ids)

    def stats(self) -> dict:
        with self._lock, self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM slams").fetchone()[0]
            hits = c.execute("SELECT COUNT(*) FROM slams WHERE matched = 1").fetchone()[0]
            pending = c.execute("SELECT COUNT(*) FROM slams WHERE sent = 0").fetchone()[0]
        return {"total": total, "hits": hits, "pending_sync": pending}


def row_to_event(row: Tuple) -> dict:
    """Reshape a DB row into the ``POST /ingest/slam-event`` body."""
    (_id, event_id, slammed_at, base_name, currency_used, item_level,
     patch, client_version, before_json, after_json, source_hash,
     user_tag, league, item_class, rarity, name, quality, corrupted,
     matched, hits_json, raw) = row
    payload = {
        "event_id": event_id,
        "source_hash": source_hash,
        "base_name": base_name,
        "currency_used": currency_used or "chaos_orb",
        "slammed_at": slammed_at,
        "before_mods_raw": json.loads(before_json) if before_json else [],
        "after_mods_raw":  json.loads(after_json)  if after_json  else [],
    }
    if item_level is not None:
        payload["item_level"] = int(item_level)
    if client_version:
        payload["client_version"] = client_version
    if patch:
        payload["patch"] = patch
    # Diagnostic blob — the server stashes it in ``extra`` JSONB.
    payload["extra"] = {
        "user_tag": user_tag,
        "league": league,
        "item_class": item_class,
        "rarity": rarity,
        "name": name,
        "quality": quality,
        "corrupted": bool(corrupted),
        "matched_client": bool(matched),
        "matched_client_hits": json.loads(hits_json) if hits_json else [],
    }
    return payload
