"""Guard-invariant test harness.

Every code change that touches ``input_guard.py``, ``main.py`` gate logic,
or the OCR pipeline MUST run this and see all tests pass before shipping.

Simulates the Win32 ``InputGuard._should_drop`` + ``notify_verdict`` state
machine in pure Python so the tests run on any OS. If you change the real
Win32 class, mirror the change here.

Run:
    python tests/test_guard_invariants.py

Exit code 0 = all invariants hold. Non-zero = regression, do not ship.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass

# Constants mirror input_guard.py (keep in sync).
STATE_UNBLOCKED = "unblocked"
STATE_WAITING = "waiting"
STATE_LOCKED = "locked"

WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONDOWN = 0x0204


@dataclass
class SimGuard:
    """Python port of the Win32 InputGuard state machine — same logic,
    testable on any OS. Mirror any change in input_guard.py here."""

    intercepting: bool = False
    state: str = STATE_UNBLOCKED
    _lmb_down_passed: bool = False
    _post_release_guard: bool = False
    _wait_started_at: float = 0.0
    _wait_timeout_sec: float = 30.0
    lmb_down_passed: int = 0
    _miss_streak_required: int = 3
    _miss_streak: int = 0

    def enable(self): self.intercepting = True

    def disable(self):
        self.intercepting = False
        self.state = STATE_UNBLOCKED
        self._lmb_down_passed = False
        self._post_release_guard = False
        self._miss_streak = 0

    def notify_verdict(self, match: bool, confident: bool = True):
        if self.state == STATE_WAITING:
            self.state = STATE_LOCKED if match else STATE_UNBLOCKED
            if self.state == STATE_UNBLOCKED and confident:
                self._post_release_guard = False
                self._miss_streak = 0
            return self.state
        if self.state == STATE_UNBLOCKED and self._post_release_guard:
            if match or not confident:
                self._miss_streak = 0
            else:
                self._miss_streak += 1
                if self._miss_streak >= self._miss_streak_required:
                    self._post_release_guard = False
                    self._miss_streak = 0
        return None

    def _should_drop(self, w_param: int) -> bool:
        if not self.intercepting:
            return False

        # WAITING timeout fails SAFE → LOCKED (mirror input_guard.py). A slam
        # whose verdict never confirmed a MISS is treated as a possible HIT;
        # the gate locks and only RMB releases it. Flipping to UNBLOCKED here
        # and passing the next click was the 2026-06-18 burn.
        if (self.state == STATE_WAITING
                and time.time() - self._wait_started_at > self._wait_timeout_sec):
            self.state = STATE_LOCKED
            self._lmb_down_passed = False

        if w_param in (WM_LBUTTONDOWN, WM_LBUTTONDBLCLK):
            if self.state == STATE_UNBLOCKED and not self._post_release_guard:
                self.state = STATE_WAITING
                self._wait_started_at = time.time()
                self._lmb_down_passed = True
                self.lmb_down_passed += 1
                return False
            self._lmb_down_passed = False
            return True

        if w_param == WM_LBUTTONUP:
            if self._lmb_down_passed:
                self._lmb_down_passed = False
                return False
            return True

        if w_param == WM_RBUTTONDOWN:
            if self.state == STATE_LOCKED:
                self.state = STATE_UNBLOCKED
                self._lmb_down_passed = False
                self._post_release_guard = True
                self._miss_streak = 0
            return False

        return False


# ---------- helpers ----------------------------------------------------


class Assert:
    def __init__(self): self.failures: list[str] = []

    def that(self, cond: bool, msg: str):
        if not cond:
            self.failures.append(msg)
            print(f"  FAIL  {msg}")
        else:
            print(f"  ok    {msg}")

    def report(self, name: str) -> bool:
        print(f"\n=== {name} ===")
        if self.failures:
            for f in self.failures: print(f"  FAILED: {f}")
            return False
        return True


# ---------- invariants --------------------------------------------------


def test_I1_no_hover_hit() -> bool:
    """I1: Pure hovers (no LMB) NEVER lock the gate, even with match verdicts."""
    a = Assert()
    print("I1: NO HOVER HIT")
    g = SimGuard(); g.enable()
    a.that(g.state == STATE_UNBLOCKED, "starts UNBLOCKED")
    # Fire 10 match verdicts with no user click
    for i in range(10):
        r = g.notify_verdict(True, confident=True)
        a.that(r is None, f"verdict #{i} on UNBLOCKED returns None (no transition)")
    a.that(g.state == STATE_UNBLOCKED, "still UNBLOCKED after 10 match verdicts")
    a.that(g._post_release_guard is False, "guard flag still False")
    # Now fire many unconfident miss verdicts (noisy OCR)
    for _ in range(10):
        g.notify_verdict(False, confident=False)
    a.that(g.state == STATE_UNBLOCKED, "still UNBLOCKED after noisy misses")
    return a.report("I1")


def test_I2_no_click_after_hit() -> bool:
    """I2: Once locked by a hit, NO subsequent LMB can reach the game
    until user acknowledges with RMB AND cursor has left the match."""
    a = Assert()
    print("I2: NO CLICK AFTER HIT")
    g = SimGuard(); g.enable()
    # 1. First click passes
    a.that(g._should_drop(WM_LBUTTONDOWN) is False, "LMB#1 passes (UNBLOCKED→WAITING)")
    a.that(g.state == STATE_WAITING, "state is WAITING")
    # 2. OCR says match → LOCKED
    g.notify_verdict(True, confident=True)
    a.that(g.state == STATE_LOCKED, "state is LOCKED after match verdict")
    # 3. Every LMB while LOCKED is dropped
    for i in range(5):
        a.that(g._should_drop(WM_LBUTTONDOWN) is True, f"LMB#{i+2} dropped while LOCKED")
    # 4. DBLCLK also dropped
    a.that(g._should_drop(WM_LBUTTONDBLCLK) is True, "DBLCLK dropped while LOCKED")
    # 5. LMB-UP without matched down also dropped (game never saw the down)
    a.that(g._should_drop(WM_LBUTTONUP) is True, "orphan LMB-UP dropped")
    return a.report("I2")


def test_I3_rmb_continues() -> bool:
    """I3: RMB releases the lock, itself passes through. Post-release guard
    keeps clicks blocked until OCR confidently says user is on a miss."""
    a = Assert()
    print("I3: RMB CONTINUES (with post-release guard)")
    g = SimGuard(); g.enable()
    # Set up: get to LOCKED
    g._should_drop(WM_LBUTTONDOWN)
    g.notify_verdict(True, confident=True)
    a.that(g.state == STATE_LOCKED, "LOCKED set up")
    # RMB: passes + releases
    a.that(g._should_drop(WM_RBUTTONDOWN) is False, "RMB passes through")
    a.that(g.state == STATE_UNBLOCKED, "state → UNBLOCKED on RMB")
    a.that(g._post_release_guard is True, "post-release guard engaged")
    # Next LMB should STILL be dropped (guard)
    a.that(g._should_drop(WM_LBUTTONDOWN) is True, "LMB dropped by post-release guard")
    # Noisy OCR: unconfident miss — guard must stay
    g.notify_verdict(False, confident=False)
    a.that(g._post_release_guard is True, "unconfident miss does NOT clear guard")
    # OCR sees same hit again: guard stays
    g.notify_verdict(True, confident=True)
    a.that(g._post_release_guard is True, "confident match keeps guard")
    # Cursor moves to miss item: need 3 consecutive confident misses
    g.notify_verdict(False, confident=True)
    g.notify_verdict(False, confident=True)
    g.notify_verdict(False, confident=True)
    a.that(g._post_release_guard is False, "3 consecutive confident misses clear guard")
    # Now LMB passes
    a.that(g._should_drop(WM_LBUTTONDOWN) is False, "LMB passes after clean miss streak")
    a.that(g.state == STATE_WAITING, "and enters new WAITING cycle")
    return a.report("I3")


def test_rapid_spam() -> bool:
    """Edge case: user spam-clicks during OCR window. Only ONE click reaches the game per OCR cycle."""
    a = Assert()
    print("edge: rapid spam during OCR window")
    g = SimGuard(); g.enable()
    passes = 0
    for _ in range(50):
        if g._should_drop(WM_LBUTTONDOWN) is False:
            passes += 1
        g._should_drop(WM_LBUTTONUP)
    a.that(passes == 1, f"exactly 1 click passed during spam (got {passes})")
    a.that(g.state == STATE_WAITING, "state WAITING after spam")
    # OCR says miss → back to UNBLOCKED
    g.notify_verdict(False, confident=True)
    a.that(g.state == STATE_UNBLOCKED, "verdict miss unblocks")
    # Now another click passes
    a.that(g._should_drop(WM_LBUTTONDOWN) is False, "next LMB passes post-verdict")
    return a.report("rapid-spam")


def test_fleeting_miss_does_not_release_guard() -> bool:
    """Regression: Victor's actual 23:38:55 burn log.
    Match → LOCKED → RMB → guard engaged → cursor briefly slips off tooltip
    (OCR reads only the gold counter for ONE frame) → guard must NOT release.
    Cursor returns to same hit item → click must still be dropped."""
    a = Assert()
    print("REGRESSION: single fleeting miss cannot release guard")
    g = SimGuard(); g.enable()
    # Get to LOCKED
    g._should_drop(WM_LBUTTONDOWN)
    g.notify_verdict(True, confident=True)
    a.that(g.state == STATE_LOCKED, "LOCKED after match")
    # RMB release
    g._should_drop(WM_RBUTTONDOWN)
    a.that(g._post_release_guard is True, "guard engaged post-RMB")
    # Multiple match reads while cursor still on hit item — guard holds
    for _ in range(4):
        g.notify_verdict(True, confident=True)
    a.that(g._post_release_guard is True, "guard holds across match reads")
    # ONE fleeting confident miss frame (cursor transit reads gold counter)
    g.notify_verdict(False, confident=True)
    a.that(g._post_release_guard is True,
           "SINGLE confident miss must NOT clear guard (streak=1/3)")
    # Cursor returns to hit — match reads reset the streak
    for _ in range(3):
        g.notify_verdict(True, confident=True)
    a.that(g._post_release_guard is True, "guard still held after returning to match")
    # User clicks — MUST be dropped
    a.that(g._should_drop(WM_LBUTTONDOWN) is True,
           "LMB DROPPED — this was the burn case that slipped in real session")
    # Only after N consecutive confident misses on a real different item:
    for _ in range(3):
        g.notify_verdict(False, confident=True)
    a.that(g._post_release_guard is False, "3 consecutive confident misses clear guard")
    a.that(g._should_drop(WM_LBUTTONDOWN) is False, "LMB finally passes")
    return a.report("fleeting-miss")


def test_waiting_timeout_must_not_burn() -> bool:
    """REGRESSION (Victor 2026-06-18 'slammed a hit but could click again'):
    A slam enters WAITING. The match verdict is late, missing, or deduped by
    the clipboard watcher (text == last → no _on_item → no notify_verdict).
    The WAITING safety timeout fires. The NEXT LMB must NOT pass — a slam
    whose verdict never confirmed a MISS must be treated as a possible HIT.
    Old behavior flipped WAITING→UNBLOCKED on timeout and passed the next
    click, burning the hit. Correct behavior: timeout fails safe → LOCKED,
    user releases with RMB."""
    a = Assert()
    print("REGRESSION: WAITING timeout must not let the next click burn")
    g = SimGuard(); g.enable()
    g._wait_timeout_sec = 1.5
    # 1. Slam — passes, enters WAITING.
    a.that(g._should_drop(WM_LBUTTONDOWN) is False, "slam passes (UNBLOCKED→WAITING)")
    g._should_drop(WM_LBUTTONUP)
    a.that(g.state == STATE_WAITING, "state WAITING")
    # 2. No verdict arrives (deduped clipboard / slow parse). Force the
    #    timeout by backdating when WAITING started.
    g._wait_started_at = time.time() - (g._wait_timeout_sec + 0.5)
    # 3. The next LMB lands while the verdict is still unknown. It MUST be
    #    dropped — this is the burn that hit the live item.
    a.that(g._should_drop(WM_LBUTTONDOWN) is True,
           "LMB after WAITING timeout DROPPED (no burn)")
    a.that(g.state == STATE_LOCKED,
           "timeout fails safe → LOCKED (requires RMB to continue)")
    # 4. Subsequent clicks stay dropped until RMB.
    a.that(g._should_drop(WM_LBUTTONDOWN) is True, "still dropped while LOCKED")
    # 5. RMB releases, as normal.
    a.that(g._should_drop(WM_RBUTTONDOWN) is False, "RMB passes + releases")
    a.that(g._post_release_guard is True, "post-release guard engaged after RMB")
    return a.report("waiting-timeout-no-burn")


def test_disarm_resets_everything() -> bool:
    """Disarm (F9) must clear all gate state so app is truly inert."""
    a = Assert()
    print("edge: disarm resets state")
    g = SimGuard(); g.enable()
    g._should_drop(WM_LBUTTONDOWN)
    g.notify_verdict(True)
    a.that(g.state == STATE_LOCKED, "LOCKED set up")
    g.disable()
    a.that(not g.intercepting, "intercepting False")
    a.that(g.state == STATE_UNBLOCKED, "state UNBLOCKED")
    a.that(g._post_release_guard is False, "guard flag cleared")
    # With intercepting False, all events pass
    a.that(g._should_drop(WM_LBUTTONDOWN) is False, "LMB passes when disarmed")
    return a.report("disarm")


# ---------- runner ------------------------------------------------------


def main() -> int:
    results = [
        test_I1_no_hover_hit(),
        test_I2_no_click_after_hit(),
        test_I3_rmb_continues(),
        test_fleeting_miss_does_not_release_guard(),
        test_waiting_timeout_must_not_burn(),
        test_rapid_spam(),
        test_disarm_resets_everything(),
    ]
    print()
    if all(results):
        print(f"ALL {len(results)} TESTS PASS — invariants hold.")
        return 0
    failed = sum(1 for r in results if not r)
    print(f"FAILED {failed} / {len(results)} — DO NOT SHIP.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
