"""Sync client — streams slam events to poewiz-api, one POST per event.

Spec highlights (see ``ENDPOINT_SPEC.md``):

  * ``POST /ingest/slam-event`` is single-event, not batch.
  * ``event_id`` is the idempotency key; retries reuse it.
  * 429 → exponential backoff + jitter, same event_id.
  * 5xx → wait a minute, retry.
  * Response includes ``parsed_after_families`` / ``parsed_before_families``
    which we cross-check against our local regex matches and log any drift
    for the catalog maintainer.
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Set

import requests

from .mod_family import mod_family_to_regex
from .storage import Storage, row_to_event


_BACKOFF_MIN = 2.0
_BACKOFF_MAX = 90.0


class Syncer:
    def __init__(
        self,
        storage: Storage,
        endpoint: str,
        api_key: str = "",
        user_tag: Optional[str] = None,
        league: Optional[str] = None,
        source_hash: Optional[str] = None,
        flush_interval_sec: int = 15,
        enabled: bool = True,
        drift_log_path: Optional[Path] = None,
        catalog_families: Optional[Set[str]] = None,
    ):
        self.storage = storage
        self.endpoint = endpoint
        self.api_key = api_key
        self.user_tag = user_tag
        self.league = league
        self.source_hash = source_hash
        self.flush_interval_sec = flush_interval_sec
        self.enabled = enabled
        self.drift_log_path = drift_log_path
        self.catalog_families: Set[str] = catalog_families or set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_error: Optional[str] = None
        self.last_sent_count: int = 0
        self._backoff = _BACKOFF_MIN

    # --- public --------------------------------------------------------

    def start(self) -> None:
        if not self.enabled or not self.endpoint:
            return
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def flush_now(self) -> int:
        return self._drain()

    def update_catalog_families(self, families: Set[str]) -> None:
        self.catalog_families = set(families)

    # --- internals -----------------------------------------------------

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "User-Agent": "SlammerWiz/1.0"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _drain(self) -> int:
        """Send all unsent rows sequentially. Returns count accepted."""
        sent = 0
        while not self._stop.is_set():
            rows = self.storage.unsent(limit=1)
            if not rows:
                break
            row = rows[0]
            row_id = row[0]
            payload = row_to_event(row)
            ok, err = self._post_one(payload)
            if ok:
                self.storage.mark_sent([row_id])
                sent += 1
                self.last_error = None
                self._backoff = _BACKOFF_MIN
            else:
                self.storage.record_attempt(row_id, err)
                self.last_error = err
                # Stop draining on failure; the periodic loop will retry.
                break
        if sent:
            self.last_sent_count = sent
        return sent

    def _post_one(self, payload: dict) -> tuple[bool, Optional[str]]:
        try:
            resp = requests.post(
                self.endpoint, json=payload, headers=self._headers(), timeout=10
            )
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

        if resp.status_code == 429:
            # Rate limited — wait, then bail from the drain.
            self._sleep_backoff()
            return False, f"HTTP 429 (rate-limited) — backing off {self._backoff:.0f}s"

        if resp.status_code >= 500:
            return False, f"HTTP {resp.status_code}: {resp.text[:160]}"

        if resp.status_code == 400:
            # Bad request — DO NOT retry. Mark as sent so we don't loop.
            return True, None

        if 200 <= resp.status_code < 300:
            try:
                body = resp.json()
            except Exception:
                body = {}
            if body.get("accepted") or body.get("duplicate"):
                self._check_drift(payload, body)
                return True, None
            return True, None  # 2xx without accepted flag — still treat as delivered

        return False, f"HTTP {resp.status_code}: {resp.text[:160]}"

    def _sleep_backoff(self) -> None:
        jitter = random.uniform(0.5, 1.5)
        time.sleep(self._backoff * jitter)
        self._backoff = min(self._backoff * 2, _BACKOFF_MAX)

    def _check_drift(self, payload: dict, body: dict) -> None:
        """Compare server's parsed families to our local regex matches."""
        if not self.drift_log_path:
            return
        server_after = set(body.get("parsed_after_families") or [])
        server_before = set(body.get("parsed_before_families") or [])
        client_after = _local_parse(payload.get("after_mods_raw") or [], self.catalog_families)
        client_before = _local_parse(payload.get("before_mods_raw") or [], self.catalog_families)

        only_server = (server_after | server_before) - (client_after | client_before)
        only_client = (client_after | client_before) - (server_after | server_before)
        if not (only_server or only_client):
            return
        entry = {
            "event_id": payload.get("event_id"),
            "base_name": payload.get("base_name"),
            "only_server": sorted(only_server),
            "only_client": sorted(only_client),
            "after_mods_raw": payload.get("after_mods_raw"),
            "before_mods_raw": payload.get("before_mods_raw"),
        }
        try:
            self.drift_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.drift_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._drain()
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
            self._stop.wait(self.flush_interval_sec)


def _local_parse(mods: List[str], families: Set[str]) -> Set[str]:
    """Return the set of catalog families that match any of ``mods`` locally."""
    matched: Set[str] = set()
    if not families:
        return matched
    for fam in families:
        rx = re.compile(mod_family_to_regex(fam), re.IGNORECASE)
        for m in mods:
            if rx.search(m):
                matched.add(fam)
                break
    return matched
