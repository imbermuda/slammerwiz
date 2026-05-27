# ChaosWiz ↔ poewiz integration contract

Single source of truth on the server side: `mod_parser.py` on **poewiz.com**.
Clients stay thin — they read the catalog, stream raw mod text, and let the
server re-parse canonically on ingest.

## GET /stats/mod-catalog

Discovery + sanity-check endpoint. Client pulls this at startup with a
~1-hour TTL disk cache, falls back to cache on network failure, falls back
to shipped `data/*.json` as last resort.

### Request

```
GET /stats/mod-catalog?slot=Tablet
User-Agent: PoEWizSlammer/1.0
Authorization: Bearer <optional>
```

### Response

```json
{
  "catalog_version": "2026.04.23-rev7",
  "entries": [
    {
      "mod_family":         "precursor_pack_size",
      "display_name":       "Monster Pack Size",
      "item_id":            "precursor_tablet",
      "item_display_name":  "Precursor Tablet",
      "slot":               "prefix",
      "regex":              "^(?:Waystones|Maps) in Range have (?P<value>\\d+)% increased Monster Pack Size$",
      "example_text":       "Waystones in Range have 14% increased Monster Pack Size",
      "typical_roll_range": [9, 20],
      "n_observed":         15234
    },
    ...
  ]
}
```

**`regex` is the only addition beyond the minimal spec.** It's emitted from
the server's `mod_parser.py` templates so the client-side gate can match a
tooltip within ~60 ms without an HTTP round-trip. Server's own ingest parse
stays authoritative for analytics.

## POST /ingest/slam-event

The client batches slam transitions and pushes them every 30 s (configurable).
Each event is a before/after pair of raw mod strings; the server re-parses
both through `mod_parser.py` to produce canonical `mod_family` keys.

### Request

```
POST /ingest/slam-event
Content-Type: application/json
Authorization: Bearer <optional>
User-Agent: PoEWizSlammer/1.0
```

### Batch payload

```json
{
  "catalog_version":      "2026.04.23-rev7",
  "user_tag":             "victor",
  "league":               "Standard",
  "source_account_hash":  "2b7d...a91f",
  "events": [
    {
      "tablet_base":           "Breach Precursor Tablet",
      "before_mods_raw":       ["Waystones in Range have 11% increased Monster Pack Size"],
      "after_mods_raw":        ["Waystones in Range have 17% increased Monster Pack Size"],
      "chaos_cost":            1,
      "source_account_hash":   "2b7d...a91f",
      "observed_at":           "2026-04-23T19:10:22Z",

      "user_tag":              "victor",
      "league":                "Standard",
      "item_class":            "Precursor Tablet",
      "rarity":                "Magic",
      "matched_client":        true,
      "matched_client_hits":   [{"rule": "Monster Pack Size T1+", "mod": "...", "value": 17}],
      "raw":                   "Rarity: Magic\nBreach Precursor Tablet\n..."
    }
  ]
}
```

The bottom block (`user_tag` through `raw`) is diagnostic — the server is
free to ignore or archive. Required fields are the top six per event.

### Response

Any 2xx marks the batch as synced. Suggested shape:

```json
{"accepted": 25, "rejected": 0, "parse_fallbacks": 2, "rate_limit_remaining": 1000}
```

On 4xx/5xx the client retries on the next flush.

## Probability aggregation (server-side, not client-side)

```sql
-- P(mod_family | slam on base) = observed empirical frequency
SELECT tablet_base, mod_family,
       COUNT(*) AS times_rolled,
       COUNT(*) * 1.0 / SUM(COUNT(*)) OVER (PARTITION BY tablet_base) AS p
FROM slam_events_parsed
WHERE delta_family IS NOT NULL  -- only counts mods that actually changed
GROUP BY tablet_base, mod_family;
```

No static odds table. As ChaosWiz volume grows this converges on the live
distribution, which is what matters — GGG's published weights often
diverge from what players actually observe.

## Patch drift

When GGG reworks mod wording (e.g. "Cold Resistance" → "Ice Resistance"):

1. Add an entry to `MOD_ALIAS` in server-side `mod_parser.py`.
2. Bump `catalog_version`.
3. Clients pick up the new catalog on their next TTL refresh.

No client rebuild required.

## Source account hash

Generated once per install:

```python
salt = secrets.token_hex(16)       # stored in config.json
hash = sha256(f"{user_tag}|{salt}").hexdigest()
```

Same install always hashes to the same opaque ID. The raw `user_tag` is
sent as a diagnostic label (for Victor's own dashboards) but the hash is
what should key persistent aggregation on the server.
