"""SlammerWiz — entry point."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sys
from pathlib import Path
from typing import List, Optional, Set


_MOD_NORMALIZE = re.compile(r"\s+")


def _normalize_mods(mods: List[str]) -> tuple:
    """Canonical form of a mod list for equality comparison.

    Strips all whitespace + lowercases. Absorbs OCR noise like ``gain in Map``
    vs ``gainin Map`` so two reads of the same tooltip don't look like a
    "transition" when nothing actually changed.
    """
    return tuple(_MOD_NORMALIZE.sub("", m).lower() for m in (mods or []))


_DIGITS_RE = re.compile(r"\d+")


def _mod_signature(mod: str) -> tuple:
    """(template, values) where template has all digits replaced with ``#``
    and values is the tuple of numeric values in order. Two OCR reads of the
    same tooltip produce the same signature. A chaos reroll produces either
    a different template (mod swapped) or different values (same mod, new
    roll) — both real transitions. OCR character jitter that doesn't touch
    digits leaves both fields identical.
    """
    norm = _MOD_NORMALIZE.sub("", mod).lower()
    values = tuple(int(v) for v in _DIGITS_RE.findall(norm))
    template = _DIGITS_RE.sub("#", norm)
    return (template, values)


def _mods_meaningfully_different(before: List[str], after: List[str]) -> bool:
    """True only when the semantic content actually changed.

    Two-stage filter:
      1. If multisets of ``(template, values)`` match — same mods, same rolls.
         This covers reordered but identical content.
      2. Templates differ but the multiset of numeric values is identical —
         means OCR character jitter produced different spellings of the same
         mods. Not a slam.
      3. Otherwise (values multiset differs) — real chaos roll.
    """
    if len(before) != len(after):
        return True
    if not before:
        return False
    b_sigs = [_mod_signature(m) for m in before]
    a_sigs = [_mod_signature(m) for m in after]
    if sorted(b_sigs) == sorted(a_sigs):
        return False
    b_values = sorted(s[1] for s in b_sigs)
    a_values = sorted(s[1] for s in a_sigs)
    if b_values != a_values:
        return True
    # Same values, different templates → OCR jitter, ignore.
    return False

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QApplication

from .catalog_client import CatalogClient
from .clipboard_watcher import ClipboardWatcher
from .hotkeys import Hotkeys
from .input_guard import InputGuard
from .mouse_hotkeys import MouseHotkeys
from .item_parser import ParsedItem, parse_item
from .mod_db import ModDB
from .overlay import Overlay, place_top_right
from .region_picker import RegionPicker
from .roi_marker import RoiMarker
from .rule_dialog import RuleDialog
from .storage import Storage
from .sync import Syncer
from .target_matcher import MatchResult, TargetMatcher
from .vision import VisionFrame, VisionWorker, auto_detect_tooltip


APP_NAME = "SlammerWiz"


def _app_dir() -> Path:
    """Where config.json lives — next to the .exe / next to repo root."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _bundled_data_dir() -> Path:
    """Where shipped read-only `data/` lives. In PyInstaller onefile this is
    the runtime extract dir (``sys._MEIPASS``); in source / onedir it's
    next to the .exe / repo root."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass) / "data"
    return _app_dir() / "data"


def _data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _config_path() -> Path:
    return _app_dir() / "config.json"


def load_config() -> dict:
    p = _config_path()
    if not p.exists():
        # First-run bootstrap: copy the bundled default template next to the
        # .exe (or to repo root in dev) so the user has an editable file.
        template = _bundled_data_dir().parent / "config.default.json"
        if template.exists():
            try:
                import secrets, shutil
                with template.open("r", encoding="utf-8") as f:
                    cfg = json.load(f)
                # Each install gets its own anonymisation salt.
                cfg.setdefault("session", {})["account_salt"] = secrets.token_hex(16)
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2)
                print(f"[config] first run — wrote default config to {p}")
            except Exception as e:
                raise SystemExit(f"Could not bootstrap config.json: {e}")
        else:
            raise SystemExit(f"Missing config.json at {p} and no bundled template found.")
    # If the primary file is corrupt, fall back to the last-known-good backup.
    for candidate in (p, p.with_suffix(p.suffix + ".bak")):
        if not candidate.exists():
            continue
        try:
            with candidate.open("r", encoding="utf-8") as f:
                cfg = json.load(f)
            if candidate != p:
                print(f"[config] primary unreadable — restored from {candidate.name}")
                # Restore primary so future saves don't cascade from the backup.
                try:
                    with p.open("w", encoding="utf-8") as f:
                        json.dump(cfg, f, indent=2)
                except Exception:
                    pass
            return cfg
        except Exception as e:
            print(f"[config] {candidate.name}: {e}")
    raise SystemExit(f"Could not parse {p} or its backup.")


_save_config_lock = __import__("threading").Lock()


def save_config(cfg: dict) -> None:
    """Atomic write with a rolling ``.bak`` of the previous good version.

    Order of operations (each step must succeed before the next):
      1. Serialize to an in-memory string and round-trip parse it. If this
         fails, we raise BEFORE touching the filesystem — a partial write
         has never happened.
      2. Write the verified string to a PID-scoped ``.tmp`` file
         (``config.json.<pid>.tmp``). Two instances of the app cannot
         collide on the same temp filename.
      3. Copy the existing good file to ``.bak``.
      4. ``os.replace`` the tmp file over the primary.

    The in-process lock ensures two threads in THIS process can't interleave.
    The PID-scoped tmp covers the two-instances case. The pre-serialize
    round-trip catches the "cfg dict is somehow broken" case before it ever
    hits disk.
    """
    p = _config_path()
    tmp = p.with_suffix(p.suffix + f".{os.getpid()}.tmp")
    bak = p.with_suffix(p.suffix + ".bak")

    # 1. Serialize + verify in memory. Raise before any IO if the dict is bad.
    payload = json.dumps(cfg, indent=2)
    json.loads(payload)  # sanity check — never write unparseable content
    data = payload.encode("utf-8")

    with _save_config_lock:
        try:
            with tmp.open("wb") as f:
                f.write(data)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            # Sanity-check the tmp file itself parses, before we swap.
            try:
                with tmp.open("r", encoding="utf-8") as f:
                    json.load(f)
            except Exception as e:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                raise RuntimeError(f"refusing to replace config — tmp unparseable: {e}")
            if p.exists():
                try:
                    import shutil
                    shutil.copy2(p, bak)
                except Exception:
                    pass
            os.replace(tmp, p)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            raise


def _ensure_source_hash(cfg: dict) -> str:
    """``sha256:`` + sha256(user_tag + per-install salt). Raw tag never leaves."""
    sess = cfg.setdefault("session", {})
    salt = sess.get("account_salt")
    if not salt:
        salt = secrets.token_hex(16)
        sess["account_salt"] = salt
        save_config(cfg)
    tag = sess.get("user_tag", "anonymous")
    digest = hashlib.sha256(f"{tag}|{salt}".encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _beep() -> None:
    """Non-blocking beep. winsound.Beep is synchronous and would block the
    Qt main thread for the duration of the tone, which is why the audio
    used to lag visibly behind the HIT flash."""
    if sys.platform != "win32":
        return
    import threading
    def _ring():
        try:
            import winsound  # type: ignore
            winsound.Beep(1100, 180)
        except Exception:
            pass
    threading.Thread(target=_ring, daemon=True).start()


class App:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        sess = cfg["session"]
        self.user_tag = sess.get("user_tag")
        self.league = sess.get("league")
        self.patch = sess.get("patch")
        self.client_version = sess.get("client_version", "chaoswiz/0.1.2")
        self.source_hash = _ensure_source_hash(cfg)

        # --- catalog ---
        api = cfg.get("api", {})
        self.catalog = CatalogClient(
            endpoint=api.get("catalog_endpoint",
                             "https://poewiz-api.fly.dev/stats/mod-catalog"),
            cache_path=_data_dir() / "catalog_cache.json",
            fallback_dir=_bundled_data_dir(),
            ttl_sec=int(api.get("catalog_ttl_sec", 21600)),
            api_key=api.get("api_key", ""),
            slot=api.get("catalog_slot", "Tablet"),
            category=api.get("catalog_category", "slam"),
            min_observed=int(api.get("catalog_min_observed", 10)),
            window_hours=int(api.get("catalog_window_hours", 168)),
            league=self.league,
        )
        self.mod_db: ModDB = self.catalog.load()
        self.matcher = TargetMatcher.from_config(cfg.get("targets", {}), mod_db=self.mod_db)

        # --- gate + vision + storage ---
        safety_cfg = cfg.get("safety", {})
        # Clipboard mode is the source of truth when auto_ctrl_c is on.
        # Vision must NOT be allowed to push verdicts to the guard or
        # record transitions — stale/garbled OCR reads cause:
        #   * the gate to UNBLOCK during WAITING (next click slams free)
        #   * the slam counter to balloon from parsing the overlay's own
        #     buttons as item mods.
        self._clipboard_mode = bool(safety_cfg.get("auto_ctrl_c", True))
        self.guard = InputGuard(
            wait_timeout_sec=float(safety_cfg.get("wait_timeout_sec", 1.5)),
            auto_ctrl_c=bool(safety_cfg.get("auto_ctrl_c", True)),
            auto_ctrl_c_delay_ms=int(safety_cfg.get("auto_ctrl_c_delay_ms", 40)),
            verdict_window_sec=float(safety_cfg.get("verdict_window_sec", 3.0)),
        )
        self.guard.disable()  # boot safe — hook runs but doesn't intercept

        self.storage = Storage(_data_dir() / "slams.sqlite3")

        self.syncer = Syncer(
            storage=self.storage,
            endpoint=api.get("endpoint", "https://poewiz-api.fly.dev/ingest/slam-event"),
            api_key=api.get("api_key", ""),
            user_tag=self.user_tag,
            league=self.league,
            source_hash=self.source_hash,
            flush_interval_sec=int(api.get("flush_interval_sec", 15)),
            enabled=api.get("sync_enabled", True),
            drift_log_path=_data_dir() / "drift.jsonl",
            catalog_families=self._catalog_families(),
        )

        self.watcher = ClipboardWatcher(
            on_item=self._on_item,
            poll_ms=int(safety_cfg.get("clipboard_poll_ms", 30)),
        )
        self.hotkeys = Hotkeys()
        self.mouse_hotkeys = MouseHotkeys()
        self.overlay: Optional[Overlay] = None
        self._armed = False
        self._last_hash_evaluated = 0
        self._prev_mods: List[str] = []
        self._prev_base: Optional[str] = None
        # Per-item slam counter. Increments on every confirmed slam
        # transition. Resets on RMB release (LOCKED → UNBLOCKED) so the
        # value visible at the time of HIT == slams it took for the
        # current item. Cumulative across-items stats still live in
        # sqlite, but they're not in the overlay anymore.
        self._slams_since_reset: int = 0

        # --- sync health polling ---
        self._sync_timer: Optional[QTimer] = None
        self._last_sent_count_seen = 0
        self._sending_flash_until = 0.0

        # --- cursor-follow ROI ---
        self._follow_timer: Optional[QTimer] = None
        self._last_cursor_for_follow: tuple[int, int] = (-9999, -9999)

        # --- vision worker ---
        vcfg = cfg.get("vision", {})
        roi = vcfg.get("roi", {"x": 0, "y": 0, "w": 0, "h": 0})
        self.vision = VisionWorker(
            roi=(roi.get("x", 0), roi.get("y", 0), roi.get("w", 0), roi.get("h", 0)),
            on_frame=self._on_vision_frame,
            poll_ms=int(vcfg.get("poll_ms", 6)),
            backend_cfg={
                "backend": vcfg.get("backend", ""),
                "tesseract_path": vcfg.get("tesseract_path", ""),
                "tessdata_dir": vcfg.get("tessdata_dir", ""),
                "psm": int(vcfg.get("psm", 6)),
                "lang": vcfg.get("lang", "eng"),
            },
        )

    # --- helpers -------------------------------------------------------

    def _catalog_families(self) -> Set[str]:
        out: Set[str] = set()
        for item in self.mod_db.items.values():
            out.update(item.mods.keys())
        return out

    # --- lifecycle -----------------------------------------------------

    def start(self) -> None:
        self.guard.start()
        self.syncer.start()
        self.watcher.start()

        vcfg = self.cfg.get("vision", {})
        # Clipboard mode locks vision out entirely. Even if config says
        # enabled=true (legacy field), we refuse to start OCR — its
        # verdicts race with clipboard's and have caused real burns.
        if (not self._clipboard_mode
                and vcfg.get("enabled", True)
                and self.vision.roi[2] > 0):
            self.vision.start()

        # All hotkey callbacks must run on the Qt main thread (they touch
        # QWidgets). ``overlay.dispatch_signal`` is a pyqtSignal with an
        # auto-queued connection across threads — bullet-proof cross-thread
        # invocation, unlike ``QTimer.singleShot`` which can be dropped
        # silently when called from a non-Qt thread.
        def on_main(fn):
            def _emit():
                try:
                    if self.overlay is not None:
                        self.overlay.dispatch_signal.emit(fn)
                except Exception as e:
                    print(f"[hotkey] dispatch error: {type(e).__name__}: {e}")
            return _emit

        safety = self.cfg.get("safety", {})
        # Single toggle hotkey: arm_hotkey arms when off, disarms when on.
        # F8 collides with Windows' default screenshot binding on some
        # setups so default toggle is F9. Separate disarm_hotkey is only
        # bound if it differs from the toggle key (back-compat).
        toggle_key = safety.get("arm_hotkey", "f9")
        self.hotkeys.bind(toggle_key, on_main(self._toggle_arm))
        disarm_key = safety.get("disarm_hotkey", "f9")
        if disarm_key and disarm_key != toggle_key:
            self.hotkeys.bind(disarm_key, on_main(self._disarm))
        self.hotkeys.bind(safety.get("calibrate_hotkey", "f10"), on_main(self._auto_calibrate))
        self.hotkeys.bind(safety.get("manual_calibrate_hotkey", "shift+f10"), on_main(self._calibrate))
        self.hotkeys.bind(safety.get("panic_hotkey", "ctrl+shift+q"), on_main(self._panic))
        self.hotkeys.bind(safety.get("quit_hotkey", "ctrl+q"), on_main(self._quit))

        # Mouse-button hotkeys — same dispatch
        mouse_cfg = self.cfg.get("mouse", {})
        if mouse_cfg.get("calibrate_button", "mouse4"):
            self.mouse_hotkeys.bind(
                mouse_cfg.get("calibrate_button", "mouse4"),
                on_main(self._auto_calibrate),
            )
        self.mouse_hotkeys.start()

        stats = self.storage.stats()
        if self.overlay:
            # Boot counter is per-item, so start at zero.
            self.overlay.counter_signal.emit(0, 0)
            msg = f"catalog: {self.catalog.last_source} · "
            msg += f"{sum(len(i.mods) for i in self.mod_db.items.values())} families"
            if self.catalog.last_error:
                msg += f" · err: {self.catalog.last_error[:60]}"
            self.overlay.status_signal.emit(msg)
            if not self._clipboard_mode and self.vision.roi[2] <= 0:
                self.overlay.status_signal.emit("ROI not set — press F10 to auto-find")

        # Sync badge: poll every 2s, push state to the overlay
        self._sync_timer = QTimer()
        self._sync_timer.timeout.connect(self._push_sync_state)
        self._sync_timer.start(2000)
        self._push_sync_state()

        # Cursor-follow: anchor the ROI to the cursor so the tooltip is always
        # in view. Runs on main thread; Qt-safe to mutate ROI marker here.
        # Clipboard mode doesn't need ROI tracking — skip the timer entirely.
        vcfg = self.cfg.get("vision", {})
        if not self._clipboard_mode and vcfg.get("follow_cursor", True):
            self._follow_timer = QTimer()
            self._follow_timer.timeout.connect(self._tick_cursor_follow)
            self._follow_timer.start(int(vcfg.get("follow_poll_ms", 150)))
            # Kick once on startup so the ROI snaps to cursor immediately.
            self._tick_cursor_follow(force=True)

        # Watch for lock-release (right-click on hook thread → flag cleared).
        # When that transitions True → False, clear the HIT flash immediately.
        self._lock_watch_timer = QTimer()
        self._lock_watch_prev = False
        self._lock_watch_timer.timeout.connect(self._watch_lock_release)
        self._lock_watch_timer.start(80)

    def stop(self) -> None:
        if self._sync_timer is not None:
            self._sync_timer.stop()
        if self._follow_timer is not None:
            self._follow_timer.stop()
        if getattr(self, "_lock_watch_timer", None) is not None:
            self._lock_watch_timer.stop()
        self.watcher.stop()
        self.syncer.stop()
        self.guard.stop()
        self.vision.stop()
        self.hotkeys.unbind_all()
        self.mouse_hotkeys.stop()

    def _watch_lock_release(self) -> None:
        """Fire HIT flash on state→LOCKED and clear it on LOCKED→UNBLOCKED.

        Runs on the Qt main thread (80 ms cadence) so overlay updates are
        safe. The state machine in the guard owns transitions; we just
        mirror their visual side here.
        """
        if self.overlay is None:
            return
        cur = bool(getattr(self.guard, "locked_until_rmb", False))
        prev = self._lock_watch_prev
        self._lock_watch_prev = cur
        if cur and not prev:
            # Transitioned into LOCKED — fire the flash.
            try:
                matcher_result = self._last_match_result
            except AttributeError:
                matcher_result = None
            if matcher_result and matcher_result.hits:
                pretty = " · ".join(
                    f"{h.rule}: {h.mod[:48]}" for h in matcher_result.hits[:3]
                )
                self.overlay.hit_signal.emit(pretty or "match", matcher_result.has_god)
            else:
                self.overlay.hit_signal.emit("match", False)
        elif not cur and prev:
            # LOCKED → UNBLOCKED (RMB release). Clear the flash AND
            # reset the per-item slam counter so the next item starts
            # from zero.
            try:
                self.overlay._clear_hit()
                self.overlay._hit_timer.stop()
            except Exception:
                pass
            self._slams_since_reset = 0
            self.overlay.counter_signal.emit(0, 0)

    def _tick_cursor_follow(self, force: bool = False) -> None:
        """Snap the ROI to a cursor-centered box when the cursor moves.

        We re-anchor only on significant movement (≥ ``follow_move_threshold``
        pixels) so a user slamming in place doesn't get the ROI re-centered
        every poll tick — that would invalidate the OCR change-detect hash.

        Frozen while the gate is locked: re-anchoring mid-hit would crop the
        tooltip, OCR would return a shortened mod list, and the transition
        detector would fire a bogus HIT when the pixel content recovered.
        """
        if getattr(self.guard, "locked_until_rmb", False):
            return
        try:
            from pynput.mouse import Controller
            cx, cy = Controller().position
        except Exception:
            return
        cx, cy = int(cx), int(cy)
        lx, ly = self._last_cursor_for_follow
        dx, dy = cx - lx, cy - ly
        vcfg = self.cfg.get("vision", {})
        threshold = int(vcfg.get("follow_move_threshold", 60))
        if not force and (dx * dx + dy * dy) < threshold * threshold:
            return
        self._last_cursor_for_follow = (cx, cy)

        # Compute cursor-centered ROI (with optional offset so the box
        # extends more to one side — PoE tooltips push to the LEFT of the
        # cursor when hovering right-side inventory, so a negative offset_x
        # keeps the tooltip inside the box).
        w = int(vcfg.get("follow_box_w", 1300))
        h = int(vcfg.get("follow_box_h", 700))
        ox = int(vcfg.get("follow_offset_x", -300))
        oy = int(vcfg.get("follow_offset_y", -50))
        import mss
        with mss.mss() as sct:
            mon = sct.monitors[0]
        vx, vy, vw, vh = mon["left"], mon["top"], mon["width"], mon["height"]
        x = max(vx, cx - w // 2 + ox)
        y = max(vy, cy - h // 2 + oy)
        if x + w > vx + vw:
            x = vx + vw - w
        if y + h > vy + vh:
            y = vy + vh - h
        x = max(vx, x)
        y = max(vy, y)
        roi = (x, y, w, h)

        # Don't write to config (cursor-follow is ephemeral) — just update
        # the worker and the marker.
        self.vision.set_roi(roi)
        self.vision.start()
        marker = getattr(self, "_roi_marker", None)
        if marker is not None:
            marker.set_roi(roi)

    def _push_sync_state(self) -> None:
        """Compute the current sync badge state and push it to the overlay."""
        if not self.overlay:
            return
        import time
        pending = self.storage.stats().get("pending_sync", 0)
        if not self.syncer.enabled or not self.syncer.endpoint:
            self.overlay.sync_signal.emit("off", pending, "sync disabled in config")
            return
        # Detect an in-progress flush: last_sent_count just ticked up.
        if self.syncer.last_sent_count != self._last_sent_count_seen:
            self._last_sent_count_seen = self.syncer.last_sent_count
            self._sending_flash_until = time.time() + 1.5
        if time.time() < self._sending_flash_until:
            self.overlay.sync_signal.emit(
                "sending", pending, f"just sent {self.syncer.last_sent_count}")
            return
        if self.syncer.last_error:
            self.overlay.sync_signal.emit("offline", pending, self.syncer.last_error[:140])
            return
        if pending > 0:
            self.overlay.sync_signal.emit(
                "queued", pending, f"{pending} event(s) waiting for next flush")
            return
        if self.syncer.last_sent_count > 0:
            self.overlay.sync_signal.emit("live", 0, f"last batch: {self.syncer.last_sent_count}")
            return
        self.overlay.sync_signal.emit("idle", 0, "no events yet")

    # --- slam pipeline -------------------------------------------------

    def _record_and_evaluate(self, item: ParsedItem) -> tuple[MatchResult, bool]:
        """Returns ``(result, is_transition)``.

        ``is_transition`` is kept for **storage purposes only** now — it
        tells us whether to log a (before → after) slam event. The GATE no
        longer consults it; the guard's state machine decides lock/unlock
        based on LMB clicks + OCR verdicts alone.
        """
        result = self.matcher.evaluate(item)
        after = list(item.mods)
        before = list(self._prev_mods)
        after_base = (item.base or item.name or "").strip() or None
        current_sig = (after_base, tuple(after))

        # Only record a slam event when:
        #   * we have a previous observation to pair against
        #   * the base is the same (otherwise user just hovered a different item)
        #   * the mod list *semantically* changed (normalize whitespace/case
        #     so OCR noise doesn't look like a slam)
        same_base = (
            self._prev_base is not None
            and after_base is not None
            and self._prev_base == after_base
        )
        # Storage-side transition detection: same base, same count,
        # meaningfully different mods. Not used by the gate anymore.
        same_count = len(after) == len(before)
        is_transition = (
            bool(before)
            and same_base
            and same_count
            and _mods_meaningfully_different(before, after)
        )

        if is_transition:
            self.storage.record(
                before_mods=before,
                after_mods=after,
                item=item.to_dict(),
                match=result.to_dict(),
                user_tag=self.user_tag,
                league=self.league,
                source_hash=self.source_hash,
                currency_used="chaos_orb",
                item_level=item.item_level,
                patch=self.patch,
                client_version=self.client_version,
            )
            # Per-item counter: bump on every confirmed slam transition.
            # Reset happens in _watch_lock_release on RMB. The HIT flag
            # is set when this transition matched a rule — that's the
            # number the user wants to see ("took N slams to hit").
            self._slams_since_reset += 1
            if self.overlay:
                self.overlay.counter_signal.emit(
                    self._slams_since_reset,
                    1 if result.matched else 0,
                )

        self._prev_mods = after
        self._prev_base = after_base
        self._last_safety_sig = current_sig

        if self.overlay:
            if result.matched and is_transition:
                pretty = " · ".join(f"{h.rule}: {h.mod[:48]}" for h in result.hits[:3])
                self.overlay.hit_signal.emit(pretty or "match", result.has_god)
            elif not result.matched:
                last = item.mods[-1] if item.mods else "no mods"
                if is_transition:
                    prefix = "slam · "
                elif before and not same_base:
                    prefix = "switched · "
                else:
                    prefix = "seen · "
                self.overlay.status_signal.emit(f"{prefix}{last[:72]}")

        return result, is_transition

    def _on_item(self, item: ParsedItem) -> None:
        result, is_transition = self._record_and_evaluate(item)
        self._deliver_verdict(result)
        # ``is_transition`` may be used later for storage filtering, but we
        # already record inside _record_and_evaluate.

    def _on_vision_frame(self, frame: VisionFrame) -> None:
        if self.overlay:
            self.overlay.latency_signal.emit(frame.ms_total, frame.ms_ocr, self.guard.credits)

        # Clipboard mode: vision is a passenger, not a driver. OCR verdicts
        # racing with clipboard caused the "one more slam after hit" bug —
        # a stale/empty OCR read would notify_verdict(match=False) during
        # WAITING, unlock the gate, and let the next LMB through before the
        # real clipboard verdict arrived. Vision is also responsible for the
        # ballooning slam counter (OCR was reading the overlay's own buttons
        # as item mods, registering bogus transitions). Bail before doing
        # any of that.
        if self._clipboard_mode:
            return

        # While the gate is locked (waiting for right-click) we MUST NOT
        # re-process frames. Otherwise:
        #   * the same matched text gets re-evaluated on every hash tick and
        #     re-fires lock_until_rmb + re-starts the GOD-flash timer, which
        #     makes the overlay appear stuck;
        #   * the moment the user right-clicks to release, the very next
        #     vision tick instantly re-locks on the cached match text.
        # Early-return here means the lock state machine has exactly one
        # entry point (a fresh match) and one exit point (RMB on the hook).
        if getattr(self.guard, "locked_until_rmb", False):
            return

        # Stream every OCR'd tooltip to a log so drift/misreads are inspectable.
        if frame.changed and frame.ok and frame.text:
            try:
                log = _data_dir() / "last_ocr.log"
                with log.open("a", encoding="utf-8") as f:
                    f.write("=" * 60 + "\n")
                    f.write(f"ms_ocr={frame.ms_ocr:.0f} roi_hash={frame.roi_hash}\n")
                    f.write(frame.text + "\n")
            except Exception:
                pass

        # Short-circuit ONLY when we know the content hasn't changed, OR
        # when the OCR backend errored out. "Changed but empty text" is
        # NOT safe to fail-open: the pixels just changed, OCR gave us
        # nothing, and we can't tell whether that's a match. Keeping the
        # gate closed here prevents the "3 clicks slipped through while
        # OCR was catching up" bug — during slow OCR, empty intermediate
        # reads used to grant credits.
        # No change or OCR error → nothing to deliver. Guard keeps its state.
        short_circuit = (not frame.changed) or (not frame.ok)
        if short_circuit:
            if not frame.ok and frame.error and self.overlay:
                import time
                now = time.time()
                last_err = getattr(self, "_last_vision_err_at", 0.0)
                last_msg = getattr(self, "_last_vision_err_msg", "")
                if frame.error != last_msg or now - last_err > 5.0:
                    self.overlay.status_signal.emit(f"vision: {frame.error[:120]}")
                    self._last_vision_err_at = now
                    self._last_vision_err_msg = frame.error
            return

        if frame.roi_hash == self._last_hash_evaluated:
            return
        self._last_hash_evaluated = frame.roi_hash

        item = parse_item(frame.text)
        if not item.mods:
            class _NullResult:
                matched = False
                has_god = False
                hits: list = []
                def to_dict(self): return {"matched": False, "hits": [], "has_god": False}
            self._deliver_verdict(_NullResult(), confident=False, mods_observed=[])
            return
        result, _is_transition = self._record_and_evaluate(item)
        self._deliver_verdict(result, confident=True, mods_observed=list(item.mods))

    # --- safety --------------------------------------------------------

    def _deliver_verdict(self, result: MatchResult, confident: bool = True,
                         mods_observed: Optional[List[str]] = None) -> None:
        """Push the OCR verdict to the guard. The guard decides whether
        to lock, unlock, or ignore based on its own state. Beep fires when
        a match actually transitions the guard into LOCKED."""
        if not self._armed:
            return
        self._last_match_result = result
        # Detailed log so failed matches can be audited later.
        try:
            from .input_guard import log_event
            if mods_observed is not None:
                hit_str = ",".join(h.rule for h in result.hits) if result.hits else "-"
                log_event("match_eval",
                          f"matched={result.matched} conf={confident} "
                          f"hits=[{hit_str}] mods={mods_observed[:6]}")
        except Exception:
            pass
        new_state = self.guard.notify_verdict(bool(result.matched), confident=confident)
        if new_state == "locked":
            safety = self.cfg.get("safety", {})
            if safety.get("beep_on_match", True):
                _beep()

    # --- arm/disarm/panic ----------------------------------------------

    def _arm(self) -> None:
        self._armed = True
        self.guard.enable()
        if self.overlay:
            self.overlay.set_armed(True)

    def _disarm(self) -> None:
        self._armed = False
        self.guard.disable()
        if self.overlay:
            self.overlay.set_armed(False)

    def _panic(self) -> None:
        self._armed = False
        self.guard.disable()
        if self.overlay:
            self.overlay.set_armed(False)
            self.overlay.status_signal.emit("OFFLINE — press F8 to re-arm")

    def _toggle_arm(self) -> None:
        (self._disarm if self._armed else self._arm)()

    def _quit(self) -> None:
        """Clean shutdown. Safe for use from a hotkey thread."""
        QApplication.quit()

    # --- capture / sync / rules ---------------------------------------

    def _manual_capture(self) -> None:
        captured = self.watcher.force_capture()
        if not captured and self.overlay:
            self.overlay.status_signal.emit("clipboard doesn't look like a PoE item")

    def _manual_flush(self) -> None:
        n = self.syncer.flush_now()
        if self.overlay:
            if self.syncer.last_error:
                self.overlay.status_signal.emit(f"sync err: {self.syncer.last_error[:60]}")
            else:
                self.overlay.status_signal.emit(f"synced {n} event(s)")

    def _open_rules(self) -> None:
        def _apply(new_targets: dict) -> None:
            self.cfg["targets"] = new_targets
            save_config(self.cfg)
            self.matcher = TargetMatcher.from_config(new_targets, mod_db=self.mod_db)
            if self.overlay:
                n = len(new_targets.get("rules") or [])
                mode = "ALL" if new_targets.get("mode") == "all_of" else "ANY"
                self.overlay.status_signal.emit(f"rules saved · {n} rule(s) · {mode}")

        dlg = RuleDialog(
            mod_db=self.mod_db,
            current_targets=self.cfg.get("targets", {"mode": "any_of", "rules": []}),
            on_save=_apply,
            parent=self.overlay,
        )
        dlg.exec()

    # --- calibration --------------------------------------------------

    def _apply_roi(self, roi) -> None:
        x, y, w, h = roi
        self.cfg.setdefault("vision", {})["roi"] = {"x": x, "y": y, "w": w, "h": h}
        save_config(self.cfg)
        self.vision.set_roi((x, y, w, h))
        # Clipboard mode never starts the OCR worker — see _on_vision_frame
        # for the reasoning.
        if not self._clipboard_mode:
            self.vision.start()
        if hasattr(self, "_roi_marker") and self._roi_marker is not None:
            self._roi_marker.set_roi((x, y, w, h))
        if self.overlay:
            self.overlay.status_signal.emit(f"ROI set: {w}×{h} at ({x},{y})")

    def _auto_calibrate(self) -> None:
        # In clipboard mode the ROI is meaningless — F10/mouse4 used to
        # force-start the OCR worker even when vision.enabled=false in
        # config, which is how the slam counter ballooned to 553 and the
        # gate started unblocking from OCR-jitter verdicts. Hard no-op.
        if self._clipboard_mode:
            if self.overlay:
                self.overlay.status_signal.emit(
                    "clipboard mode — calibration disabled (no OCR)")
            return
        try:
            from pynput.mouse import Controller
            cursor = Controller().position
        except Exception as e:
            if self.overlay:
                self.overlay.status_signal.emit(f"auto-calibrate: can't read cursor ({e})")
            return
        self.vision.stop()
        debug_dir = _data_dir() / "debug"
        roi = auto_detect_tooltip(
            (int(cursor[0]), int(cursor[1])), debug_dir=debug_dir,
        )
        if roi is None:
            if self.overlay:
                self.overlay.status_signal.emit(
                    f"no tooltip found — debug dump saved to {debug_dir}. "
                    "Hover closer to the tooltip and retry, or Shift+F10 for manual drag."
                )
            if self.vision.roi[2] > 0:
                self.vision.start()
            return
        self._apply_roi(roi)

    def _calibrate(self) -> None:
        if self._clipboard_mode:
            if self.overlay:
                self.overlay.status_signal.emit(
                    "clipboard mode — calibration disabled (no OCR)")
            return
        self.vision.stop()
        # Temporarily hide the ROI marker so it doesn't steal the drag visuals.
        marker = getattr(self, "_roi_marker", None)
        if marker is not None and marker.isVisible():
            marker.hide()

        def _on_pick(roi):
            self._apply_roi(roi)
            # set_roi re-shows the marker; nothing else to do

        self._picker = RegionPicker(on_pick=_on_pick)
        self._picker.show()


def main() -> int:
    cfg = load_config()
    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName(APP_NAME)

    # Mark a boot line in the event log so sessions are easy to scan.
    try:
        from .input_guard import log_event
        log_event("==== BOOT", f"pid={os.getpid()} APP_NAME={APP_NAME}")
    except Exception:
        pass

    app = App(cfg)
    overlay = Overlay(
        on_toggle_arm=app._toggle_arm,
        on_manual_capture=app._manual_capture,
        on_flush=app._manual_flush,
        on_calibrate=app._auto_calibrate,
        on_rules=app._open_rules,
    )
    app.overlay = overlay
    # Visible ROI outline on screen so you can see what OCR is watching.
    # Only shown when vision is enabled; clipboard mode doesn't use OCR so
    # the green rectangle is just visual noise.
    marker = RoiMarker()
    app._roi_marker = marker
    vcfg_boot = cfg.get("vision", {})
    r = vcfg_boot.get("roi", {})
    if vcfg_boot.get("enabled", True) and r.get("w", 0) > 0:
        marker.set_roi((r["x"], r["y"], r["w"], r["h"]))
    else:
        marker.hide()
    app.start()

    ui_cfg = cfg.get("ui", {})
    overlay.setWindowOpacity(float(ui_cfg.get("opacity", 0.92)))
    overlay.show()
    if ui_cfg.get("position", "top_right") == "top_right":
        place_top_right(overlay)

    try:
        return qt_app.exec()
    finally:
        app.stop()


if __name__ == "__main__":
    raise SystemExit(main())
