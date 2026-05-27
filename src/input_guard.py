"""
Input guard — Win32 low-level mouse hook + 3-state click gate.

State machine:

                ┌─────────────────────────────────────────────────────┐
                │                                                     │
                ▼                                                     │
         ┌─────────────┐   LMB pass     ┌──────────┐   verdict=miss  │
    ┌──▶│  UNBLOCKED   │───────────────▶│ WAITING  │──────────────── ┘
    │   └─────────────┘                 └──────────┘
    │                                         │
    │                                         │ verdict=match
    │                                         ▼
    │                                   ┌──────────┐
    └──────────── RMB ──────────────────│  LOCKED  │
                                        └──────────┘

  UNBLOCKED: LMB passes freely. First LMB-down → WAITING.
  WAITING:   Post-click. LMB dropped. Ends on OCR verdict or safety timeout.
  LOCKED:    OCR matched. LMB dropped indefinitely until user right-clicks.

No credits, no fail-open, no transition bookkeeping.  Only LMB clicks and
OCR verdicts drive state.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional


# --------------------------------------------------------------------------
#  Event log — every state-affecting event is appended here with a
#  monotonic timestamp so any "got a slam through" report can be verified
#  post-hoc. Located at %APPDATA%\SlammerWiz\events.log on Windows.
# --------------------------------------------------------------------------

_EVENT_LOG_LOCK = threading.Lock()
_EVENT_LOG_PATH: Optional[Path] = None


def _event_log_path() -> Path:
    global _EVENT_LOG_PATH
    if _EVENT_LOG_PATH is not None:
        return _EVENT_LOG_PATH
    base = os.environ.get("APPDATA") or str(Path.home())
    d = Path(base) / "SlammerWiz"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    _EVENT_LOG_PATH = d / "events.log"
    return _EVENT_LOG_PATH


def log_event(tag: str, msg: str = "") -> None:
    """Append a timestamped event line to events.log. Best-effort — never
    raises; silent failure keeps the hot path clean."""
    try:
        line = f"{time.strftime('%H:%M:%S')}.{int((time.time() % 1) * 1000):03d} " \
               f"{tag:<14} {msg}\n"
        with _EVENT_LOG_LOCK:
            with _event_log_path().open("a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass


# Public state constants
STATE_UNBLOCKED = "unblocked"
STATE_WAITING = "waiting"
STATE_LOCKED = "locked"


# --------------------------------------------------------------------------
#  Stub for non-Windows dev machines — keeps the module importable.
# --------------------------------------------------------------------------

class _StubGuard:
    def __init__(self, wait_timeout_sec: float = 30.0) -> None:
        self.intercepting = False
        self.state = STATE_UNBLOCKED
        self.lmb_down_passed = 0
        self.dropped = 0
        self.allowed = 0
        self._wait_timeout_sec = wait_timeout_sec

    def start(self) -> None: pass
    def stop(self) -> None: pass
    def enable(self) -> None: self.intercepting = True
    def disable(self) -> None:
        self.intercepting = False
        self.state = STATE_UNBLOCKED

    def notify_verdict(self, match: bool, confident: bool = True) -> Optional[str]:
        if self.state == STATE_WAITING:
            new = STATE_LOCKED if match else STATE_UNBLOCKED
            self.state = new
            return new
        return None

    def force_unblock(self) -> None:
        self.state = STATE_UNBLOCKED

    @property
    def locked_until_rmb(self) -> bool:
        return self.state == STATE_LOCKED

    @property
    def credits(self) -> int:
        # Back-compat shim — overlay shows this in the latency line.
        return 1 if self.state == STATE_UNBLOCKED else 0


if sys.platform != "win32":
    InputGuard = _StubGuard  # type: ignore[misc,assignment]

else:
    import ctypes
    from ctypes import wintypes

    WH_MOUSE_LL = 14
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONDOWN = 0x0204
    WM_RBUTTONUP = 0x0205
    WM_RBUTTONDBLCLK = 0x0206

    # SendInput constants (Route 3: auto Ctrl+C after slam passes)
    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    VK_CONTROL = 0x11
    VK_C = 0x43

    LRESULT = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("pt", wintypes.POINT),
            ("mouseData", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    user32.CallNextHookEx.restype = LRESULT
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    # ----------  SendInput bindings  (Route 3) -----------------------------
    #
    # We synthesize Ctrl+C via SendInput — more reliable than the legacy
    # keybd_event and atomic across scan codes. The sequence fires on a
    # daemon thread with a small delay after the user's slam clicks through,
    # giving PoE time to update the hovered-item tooltip before we copy.
    # The hook only observes mouse events, so synthesized KEYBOARD events
    # don't re-enter this callback.

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class _INPUT_UNION(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]

    class _INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]

    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT

    def _send_ctrl_c() -> None:
        """Synthesize Ctrl-down, C-down, C-up, Ctrl-up via SendInput."""
        def ki(vk: int, flags: int = 0) -> _INPUT:
            inp = _INPUT()
            inp.type = INPUT_KEYBOARD
            inp.ki = _KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0,
                                 dwExtraInfo=None)
            return inp
        seq = (_INPUT * 4)(
            ki(VK_CONTROL),
            ki(VK_C),
            ki(VK_C, KEYEVENTF_KEYUP),
            ki(VK_CONTROL, KEYEVENTF_KEYUP),
        )
        user32.SendInput(4, seq, ctypes.sizeof(_INPUT))


    class InputGuard:  # type: ignore[no-redef]
        """Three-state click gate backed by a Win32 low-level mouse hook."""

        def __init__(self, wait_timeout_sec: float = 1.5,
                     auto_ctrl_c: bool = False,
                     auto_ctrl_c_delay_ms: int = 40,
                     miss_streak_required: Optional[int] = None,
                     verdict_window_sec: float = 3.0) -> None:
            self.intercepting = False
            self.state = STATE_UNBLOCKED
            self._wait_started_at = 0.0
            self._wait_timeout_sec = wait_timeout_sec
            self._lmb_down_passed = False  # track for matching LMB-up pass
            # Set True when RMB releases a LOCKED gate. LMB stays dropped
            # until a verdict confirms the current tooltip isn't still a
            # match — otherwise the user's next click burns the just-
            # acknowledged hit because their cursor hasn't moved off the
            # item yet.
            self._post_release_guard = False
            # Require N consecutive confident-miss verdicts to clear the
            # guard. A single fleeting miss read (e.g. OCR captured only
            # the gold counter while cursor was in transit) must NOT
            # release the guard — that was a real burn vector.
            #
            # OCR mode: 3 (noisy, needs consensus).
            # Clipboard mode (auto_ctrl_c): 1 (deterministic, never lies).
            if miss_streak_required is None:
                miss_streak_required = 1 if auto_ctrl_c else 3
            self._miss_streak_required = max(1, int(miss_streak_required))
            self._miss_streak = 0
            self._lock = threading.Lock()
            self._hook: Optional[int] = None
            self._thread: Optional[threading.Thread] = None
            self._proc = HOOKPROC(self._low_level_proc)
            # Analytics
            self.lmb_down_passed = 0
            self.dropped = 0
            self.allowed = 0
            # Route 3: fire a synthetic Ctrl+C after each LMB pass so PoE
            # exports the freshly-slammed tooltip to the clipboard. The
            # ClipboardWatcher picks it up, parses, and delivers a confident
            # verdict. No OCR, no race.
            self._auto_ctrl_c = bool(auto_ctrl_c)
            self._auto_ctrl_c_delay = max(0, int(auto_ctrl_c_delay_ms)) / 1000.0
            # Late-verdict safety net. When clipboard → parse → verdict takes
            # longer than ``wait_timeout_sec``, the state machine has already
            # flipped back to UNBLOCKED and a match verdict would be dropped
            # — meaning the next click burns the hit. If a verdict arrives
            # within this window of our last auto Ctrl+C fire, we honor it
            # regardless of state. This closes the late-verdict burn hole.
            self._verdict_window_sec = float(verdict_window_sec)
            self._last_ctrl_c_at: float = 0.0
            # Set True when an RMB release auto-disarmed the guard. Main
            # thread polls this and syncs its self._armed flag + overlay.
            # Edge-triggered: main thread clears it after acting.
            self._auto_disarmed: bool = False
            # Post-release-guard timeout. The streak-based clear (one
            # confident miss in clipboard mode, three in OCR mode) only
            # fires when a fresh PoE item lands in the clipboard. If the
            # user clicks on the ground / their character / inventory UI
            # after RMB, no clipboard verdict arrives and the guard
            # strands them. Cap the guard at this many seconds — past
            # that, the user has had time to move off the locked item
            # and we trust LMB to resume normal duty.
            self._post_release_guard_at: float = 0.0
            self._post_release_guard_timeout: float = 0.6

        # ------ public --------------------------------------------------

        def start(self) -> None:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="input-guard", daemon=True)
            self._thread.start()

        def stop(self) -> None:
            self.intercepting = False
            self.state = STATE_UNBLOCKED
            if self._hook:
                user32.UnhookWindowsHookEx(self._hook)
                self._hook = None

        def enable(self) -> None:
            with self._lock:
                self.intercepting = True
            log_event("ARM", "intercepting=True")

        def disable(self) -> None:
            with self._lock:
                self.intercepting = False
                self.state = STATE_UNBLOCKED
                self._lmb_down_passed = False
                self._post_release_guard = False
                self._miss_streak = 0
            log_event("DISARM", "state reset")

        def notify_verdict(self, match: bool, confident: bool = True) -> Optional[str]:
            """Main thread calls this after OCR evaluates the tooltip.

            ``match``     — True if parsed mods hit a rule.
            ``confident`` — True only when we actually parsed usable mod
                            content. False means OCR returned empty/garbage
                            and we can't prove there isn't a match here.

            Returns the NEW state if a WAITING transition happened, else None.
            The post-release guard clears ONLY on a confident miss.
            """
            with self._lock:
                if self.state == STATE_WAITING:
                    prev = self.state
                    self.state = STATE_LOCKED if match else STATE_UNBLOCKED
                    if self.state == STATE_UNBLOCKED and confident:
                        self._post_release_guard = False
                        self._miss_streak = 0
                    log_event("verdict",
                              f"match={match} conf={confident} {prev}→{self.state} "
                              f"guard={self._post_release_guard}")
                    return self.state
                # Late-verdict safety net. State timed out back to UNBLOCKED
                # before the verdict arrived — but we recently fired a Ctrl+C
                # so this verdict belongs to a real slam. Honor it.
                now = time.time()
                if (self.state == STATE_UNBLOCKED
                        and not self._post_release_guard
                        and self._last_ctrl_c_at > 0
                        and (now - self._last_ctrl_c_at) <= self._verdict_window_sec
                        and match
                        and confident):
                    self.state = STATE_LOCKED
                    log_event("verdict",
                              f"match={match} conf={confident} LATE "
                              f"UNBLOCKED→LOCKED (dt={now-self._last_ctrl_c_at:.2f}s)")
                    return self.state
                if self.state == STATE_UNBLOCKED and self._post_release_guard:
                    if match or not confident:
                        # Any match OR uncertain frame resets the streak —
                        # we need consecutive CONFIDENT MISSES to release.
                        self._miss_streak = 0
                        log_event("verdict",
                                  f"match={match} conf={confident} guard holds "
                                  f"(streak reset)")
                    else:
                        self._miss_streak += 1
                        if self._miss_streak >= self._miss_streak_required:
                            self._post_release_guard = False
                            self._miss_streak = 0
                            log_event("verdict",
                                      f"match={match} conf={confident} "
                                      f"guard cleared (streak reached)")
                        else:
                            log_event("verdict",
                                      f"match={match} conf={confident} guard holds "
                                      f"(miss streak {self._miss_streak}/"
                                      f"{self._miss_streak_required})")
                return None

        def force_unblock(self) -> None:
            """Explicit reset — used by panic hotkey."""
            with self._lock:
                self.state = STATE_UNBLOCKED
                self._lmb_down_passed = False
                self._post_release_guard = False
                self._miss_streak = 0

        @property
        def locked_until_rmb(self) -> bool:
            return self.state == STATE_LOCKED

        @property
        def credits(self) -> int:
            """Back-compat shim for the overlay latency line."""
            return 1 if self.state == STATE_UNBLOCKED else 0

        # ------ hook ----------------------------------------------------

        def _should_drop(self, w_param: int) -> bool:
            with self._lock:
                if not self.intercepting:
                    return False

                # Safety timeout on WAITING — absolute fallback if OCR never
                # delivers a verdict. Set large (30 s default) so it's well
                # above any realistic OCR latency; otherwise the timeout can
                # race the verdict and let a click slip through.
                if (self.state == STATE_WAITING
                        and time.time() - self._wait_started_at > self._wait_timeout_sec):
                    self.state = STATE_UNBLOCKED

                if w_param in (WM_LBUTTONDOWN, WM_LBUTTONDBLCLK):
                    # Time-based guard expiry: if the post-release guard
                    # has been engaged longer than the timeout AND we
                    # haven't received a verdict to clear it, let it go.
                    # User has had time to move off the locked item.
                    if (self._post_release_guard
                            and self._post_release_guard_at > 0
                            and (time.time() - self._post_release_guard_at)
                                > self._post_release_guard_timeout):
                        log_event("guard timeout",
                                  f"cleared after {self._post_release_guard_timeout}s")
                        self._post_release_guard = False
                        self._miss_streak = 0
                    if self.state == STATE_UNBLOCKED and not self._post_release_guard:
                        # Allow this click and enter WAITING.
                        self.state = STATE_WAITING
                        self._wait_started_at = time.time()
                        self._lmb_down_passed = True
                        self.lmb_down_passed += 1
                        log_event("LMB pass",
                                  f"#{self.lmb_down_passed} → WAITING")
                        return False
                    log_event("LMB drop",
                              f"state={self.state} guard={self._post_release_guard}")
                    self._lmb_down_passed = False
                    return True

                if w_param == WM_LBUTTONUP:
                    if self._lmb_down_passed:
                        self._lmb_down_passed = False
                        return False
                    return True

                if w_param in (WM_RBUTTONDOWN, WM_RBUTTONDBLCLK):
                    # Always log so events.log proves the hook is seeing
                    # the RMB. If a user reports "RMB doesn't release"
                    # and these lines are missing, the hook is the
                    # problem — not the state machine.
                    if self.state == STATE_LOCKED:
                        self.state = STATE_UNBLOCKED
                        self._lmb_down_passed = False
                        self._post_release_guard = True
                        self._post_release_guard_at = time.time()
                        self._miss_streak = 0
                        log_event("RMB release",
                                  "LOCKED → UNBLOCKED, guard engaged")
                    else:
                        log_event("RMB seen",
                                  f"state={self.state} guard={self._post_release_guard}")
                    return False

                return False

        def _fire_ctrl_c_async(self) -> None:
            """Schedule a synthetic Ctrl+C after the auto_ctrl_c_delay.

            Runs on a daemon thread so the low-level hook returns within
            its strict time budget (Windows silently unhooks slow LL hooks).
            The delay gives PoE a window to update the hovered-item tooltip
            with freshly-slammed mods before we copy it.
            """
            if not self._auto_ctrl_c:
                return
            delay = self._auto_ctrl_c_delay

            def _ring():
                try:
                    if delay > 0:
                        time.sleep(delay)
                    _send_ctrl_c()
                    with self._lock:
                        self._last_ctrl_c_at = time.time()
                    log_event("auto C+C", f"sent after {int(delay*1000)}ms")
                except Exception as e:
                    log_event("auto C+C",
                              f"error {type(e).__name__}: {e}")

            threading.Thread(target=_ring, name="auto-ctrl-c", daemon=True).start()

        def _low_level_proc(self, n_code, w_param, l_param):
            try:
                if n_code >= 0:
                    wp = int(w_param)
                    drop = self._should_drop(wp)
                    # Auto-copy fires on every LMB-down attempt whether the
                    # click passes or is dropped. Rationale:
                    #   * passed click = fresh slam → copy new mods.
                    #   * dropped click during post-release guard = user is
                    #     probing the next item; we still need the clipboard
                    #     to update so the guard can see a confident miss
                    #     and unlock. Firing only on pass creates a deadlock
                    #     where no verdict ever arrives and the guard never
                    #     clears.
                    if wp in (WM_LBUTTONDOWN, WM_LBUTTONDBLCLK) and self.intercepting:
                        self._fire_ctrl_c_async()
                    if drop:
                        self.dropped += 1
                        return 1
                    if wp in (WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                        self.allowed += 1
            except Exception as e:
                try:
                    print(f"[guard] hook error (passing through): {type(e).__name__}: {e}")
                except Exception:
                    pass
            return user32.CallNextHookEx(self._hook or 0, n_code, w_param, l_param)

        def _run(self) -> None:
            h_mod = kernel32.GetModuleHandleW(None)
            self._hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc, h_mod, 0)
            if not self._hook:
                return
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
