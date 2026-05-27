"""Tiny reference server matching the real poewiz-api.fly.dev contract.

Stdlib-only. Useful when the real endpoint is slow or you want to see
exactly what the client sends without hitting production.

Endpoints:
  GET  /stats/mod-catalog?slot=Tablet&category=slam&min_observed=10
  POST /ingest/slam-event                (single event, UUID idempotent)

Usage:

    cd "C:\\dev\\chaoswiz\\chaoswiz\\poewiz-slammer"
    .\\.venv\\Scripts\\python.exe tools\\mock_server.py

Then point config.json at http://127.0.0.1:8000/...
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


SEED_CATALOG = {
    "catalog_version": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    "league": "Fate of the Vaal",
    "window_hours": 168,
    "filters": {"slot": None, "category": "slam", "min_observed": 10},
    "count": 3,
    "entries": [
        {
            "slot": "Tablet",
            "mod_family": "Pause the # Encounter timer",
            "example_text": "Pause the Breach Encounter timer",
            "bases_seen": ["Breach Precursor Tablet", "Ritual Precursor Tablet"],
            "n_observed": 245,
            "roll_p10": None,
            "roll_p90": None,
            "price_p50_div": 38.0,
            "price_p90_div": 52.0,
            "god_mod": True,
        },
        {
            "slot": "Tablet",
            "mod_family": "#% increased Quantity of Items found in Map",
            "example_text": "30% increased Quantity of Items found in Map",
            "bases_seen": ["Breach Precursor Tablet", "Delirium Precursor Tablet",
                           "Expedition Precursor Tablet", "Ritual Precursor Tablet"],
            "n_observed": 1832,
            "roll_p10": 14,
            "roll_p90": 32,
            "price_p50_div": 0.05,
            "price_p90_div": 0.8,
            "god_mod": False,
        },
        {
            "slot": "Tablet",
            "mod_family": "+# to Monster Pack Size in Map",
            "example_text": "+1 to Monster Pack Size in Map",
            "bases_seen": ["Breach Precursor Tablet", "Ritual Precursor Tablet"],
            "n_observed": 512,
            "roll_p10": 1,
            "roll_p90": 3,
            "price_p50_div": 0.2,
            "price_p90_div": 2.0,
            "god_mod": False,
        },
    ],
}

INGEST_LOG = Path("mock_events.jsonl")
_SEEN_IDS: set[str] = set()


class Handler(BaseHTTPRequestHandler):
    def _reply(self, status: int, body: dict) -> None:
        data = json.dumps(body, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/stats/mod-catalog":
            self._reply(200, SEED_CATALOG)
            return
        if parsed.path == "/":
            self._reply(200, {"ok": True, "endpoints": [
                "GET  /stats/mod-catalog?slot=Tablet",
                "POST /ingest/slam-event",
            ]})
            return
        self._reply(404, {"error": "not found", "path": parsed.path})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/ingest/slam-event":
            self._reply(404, {"error": "not found", "path": parsed.path})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            ev = json.loads(raw.decode("utf-8") or "{}")
        except Exception as e:
            self._reply(400, {"error": f"bad json: {e}"})
            return

        if ev.get("currency_used") not in ("chaos_orb", None):
            self._reply(400, {"error": f"currency_used={ev.get('currency_used')!r} not supported"})
            return

        eid = ev.get("event_id")
        if not eid:
            self._reply(400, {"error": "missing event_id"})
            return

        dup = eid in _SEEN_IDS
        _SEEN_IDS.add(eid)

        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(f"[{stamp}] ingest  id={eid[:8]}  base={ev.get('base_name')!r:32s} "
              f"before={len(ev.get('before_mods_raw') or [])}  "
              f"after={len(ev.get('after_mods_raw') or [])}  dup={dup}")

        with INGEST_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"received_at": stamp, **ev}) + "\n")

        # Fake "parsed_families" by stripping leading +/- and numbers.
        def _fake_parse(mods):
            out = []
            for m in mods or []:
                # Replace first run of digits with # — crude but representative.
                import re
                collapsed = re.sub(r"[+\-]?\d+", "#", m.strip(), count=1)
                out.append(collapsed)
            return out

        self._reply(200, {
            "accepted": True,
            "event_id": eid,
            "duplicate": dup,
            "parsed_after_families": _fake_parse(ev.get("after_mods_raw")),
            "parsed_before_families": _fake_parse(ev.get("before_mods_raw")),
        })

    def log_message(self, fmt, *args):
        pass


def main(host: str = "127.0.0.1", port: int = 8000) -> int:
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"ChaosWiz mock server listening on http://{host}:{port}")
    print(f"Catalog:  http://{host}:{port}/stats/mod-catalog?slot=Tablet")
    print(f"Ingest:   POST http://{host}:{port}/ingest/slam-event")
    print(f"Writing batches to {INGEST_LOG.resolve()}")
    print("Ctrl-C to stop.\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    sys.exit(main(args.host, args.port))
