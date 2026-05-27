"""Summarize every unique mod text the slammer has observed.

Usage (PowerShell):

    cd "C:\\dev\\chaoswiz\\chaoswiz\\poewiz-slammer"
    .\\.venv\\Scripts\\python.exe tools\\discover.py                          # markdown
    .\\.venv\\Scripts\\python.exe tools\\discover.py --json > discovery.json  # machine-readable
    .\\.venv\\Scripts\\python.exe tools\\discover.py --base "Precursor"       # filter

Reads ``%APPDATA%\\PoEWizSlammer\\slams.sqlite3`` and groups every mod line
observed per tablet base. Numbers are collapsed to ``X`` so "14% increased"
and "17% increased" count as the same template. Min/max/mean give the
empirical roll range.

Paste the output back so we can update the catalog from real data.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Tuple


NUM = re.compile(r"\d+(?:\.\d+)?")


def default_db_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "PoEWizSlammer" / "slams.sqlite3"


def template_of(mod: str) -> str:
    return NUM.sub("X", mod).strip()


def first_number(mod: str):
    m = NUM.search(mod)
    return float(m.group()) if m else None


def load_rows(db: Path, base_filter: str | None, limit: int) -> List[Tuple[str, str, str]]:
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    if base_filter:
        cur.execute(
            """
            SELECT tablet_base, before_mods_json, after_mods_json
            FROM slams
            WHERE tablet_base LIKE ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (f"%{base_filter}%", limit),
        )
    else:
        cur.execute(
            """
            SELECT tablet_base, before_mods_json, after_mods_json
            FROM slams
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
    return cur.fetchall()


def summarize(rows):
    templates_by_base: Dict[str, Counter] = defaultdict(Counter)
    values: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    example: Dict[Tuple[str, str], str] = {}

    for base, before_json, after_json in rows:
        if not base:
            continue
        pool: List[str] = []
        for blob in (before_json, after_json):
            if blob:
                try:
                    pool.extend(json.loads(blob))
                except Exception:
                    pass
        seen_in_row = set()
        for mod in pool:
            mod = (mod or "").strip()
            if not mod:
                continue
            tmpl = template_of(mod)
            if not tmpl:
                continue
            key = (base, tmpl)
            if key in seen_in_row:
                continue  # dedupe within a single tooltip snapshot
            seen_in_row.add(key)
            templates_by_base[base][tmpl] += 1
            v = first_number(mod)
            if v is not None:
                values[key].append(v)
            example.setdefault(key, mod)

    return templates_by_base, values, example


def emit_markdown(templates_by_base, values, example, min_count: int) -> None:
    total_rows = sum(sum(c.values()) for c in templates_by_base.values())
    print(f"# Discovery report  ·  {total_rows} mod observations across "
          f"{len(templates_by_base)} base(s)\n")
    for base in sorted(templates_by_base, key=lambda b: -sum(templates_by_base[b].values())):
        counts = templates_by_base[base]
        print(f"## {base}  ·  {sum(counts.values())} observations\n")
        print("| N | Template | Min | Max | Mean | Example |")
        print("|---|---|---|---|---|---|")
        for tmpl, n in counts.most_common():
            if n < min_count:
                continue
            vals = values.get((base, tmpl), [])
            ex = example.get((base, tmpl), "")
            if vals:
                print(f"| {n} | `{tmpl}` | {int(min(vals))} | {int(max(vals))} "
                      f"| {sum(vals)/len(vals):.1f} | `{ex}` |")
            else:
                print(f"| {n} | `{tmpl}` | - | - | - | `{ex}` |")
        print()


def emit_json(templates_by_base, values, example, min_count: int) -> None:
    out = {"bases": {}}
    for base, counts in templates_by_base.items():
        entries = []
        for tmpl, n in counts.most_common():
            if n < min_count:
                continue
            vals = values.get((base, tmpl), [])
            entry = {
                "template": tmpl,
                "count": n,
                "example": example.get((base, tmpl), ""),
            }
            if vals:
                entry["min"] = min(vals)
                entry["max"] = max(vals)
                entry["mean"] = sum(vals) / len(vals)
                entry["samples"] = len(vals)
            entries.append(entry)
        out["bases"][base] = entries
    print(json.dumps(out, indent=2))


def main() -> int:
    p = argparse.ArgumentParser(description="Summarize observed mods from slams.sqlite3")
    p.add_argument("--db", type=Path, default=None, help="Path to slams.sqlite3")
    p.add_argument("--base", type=str, default=None, help="Substring filter on tablet_base")
    p.add_argument("--limit", type=int, default=100000, help="Max rows to scan")
    p.add_argument("--min-count", type=int, default=1,
                   help="Skip templates observed fewer than N times")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of markdown")
    args = p.parse_args()

    db = args.db or default_db_path()
    if not db.is_file():
        print(f"no slams database found at {db}", file=sys.stderr)
        print("slam a bit first (without arming), then rerun.", file=sys.stderr)
        return 1

    rows = load_rows(db, args.base, args.limit)
    if not rows:
        print("no rows in slams table yet.", file=sys.stderr)
        return 1

    templates, values, example = summarize(rows)
    if args.json:
        emit_json(templates, values, example, args.min_count)
    else:
        emit_markdown(templates, values, example, args.min_count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
