# SlammerWiz v1.3 — Burn-vector hardening

This release closes two ways a confirmed hit could still be burned, and makes
the click-gate fail **safe** instead of fail **open** whenever it can't prove a
slam was a miss.

## Fixes

**Incomplete clipboard capture no longer burns a roll.**
With PoE2's "Advanced Item Descriptions" enabled, the heavier tooltip can lag
the auto Ctrl+C, so the snapshot contains only the item's *implicit* modifier —
the rolled affixes never reach the clipboard. The matcher then sees no match,
the gate opens, and the next click burns the roll. The gate now detects an
implicit-only / no-explicit-mods read and **holds** instead of unblocking
(`has_explicit_mods`, wired into the clipboard path). A slammed item that
reads with zero explicit mods can never open the gate.

**WAITING timeout now fails safe to LOCKED.**
Previously a WAITING state that timed out flipped straight to UNBLOCKED and
passed the next click — burning a hit whenever the verdict was late, missing,
or deduped. A slam with no confirmed *miss* is now treated as a possible *hit*:
the gate locks and waits for an explicit RMB. The timeout was also raised
1.5s → 5.0s so this is a true last resort, not a per-slam annoyance.

**Auto-copy delay 40ms → 120ms.**
Gives the (heavier, advanced) tooltip time to finish rendering before the
synthetic Ctrl+C fires, sharply reducing incomplete reads at the source.

## Notes
- The matcher and parser were verified correct against live patch-0.5 mod text
  (`Unstable Breaches… N(min-max)… Stabilised`) — no rule changes needed.
- Recommended in-game: turn **off** "Always Show Advanced Item Descriptions".
  With v1.3 it can no longer cause a burn, only slower reads.
- Ship `SlammerWiz.exe` **and** `config.json` together.

## Tests
All guard invariants green (I1/I2/I3 + fleeting-miss + rapid-spam + disarm),
plus new regressions: `test_waiting_timeout_must_not_burn`,
`tests/test_incomplete_read.py`.
