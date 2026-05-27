"""
Polls the system clipboard on a background thread and emits parsed PoE 2
items to a callback. Cheap and reliable — no OS-specific hooks required.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

import pyperclip

from .item_parser import ParsedItem, parse_item


def is_poe_item_text(text: str) -> bool:
    """Spec-aligned clipboard filter. A PoE Ctrl+C dump always carries
    ``Item Class:`` + ``Rarity:`` + ``--------`` separators. Reject anything
    else so we don't accidentally POST screenshots of stash tabs, trade chat,
    or arbitrary clipboard copies.
    """
    if not text or len(text) < 50:
        return False
    if "Item Class:" not in text:
        return False
    if "--------" not in text:
        return False
    if "Rarity:" not in text:
        return False
    return True


# Backward-compat alias used by callers that were written before the rename.
_looks_like_poe_item = is_poe_item_text


class ClipboardWatcher:
    def __init__(self, on_item: Callable[[ParsedItem], None], poll_ms: int = 30):
        self._on_item = on_item
        self._poll = poll_ms / 1000.0
        self._last: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="clipboard-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def force_capture(self) -> Optional[ParsedItem]:
        """Manual read of the current clipboard — use for hotkey-driven capture."""
        try:
            text = pyperclip.paste()
        except Exception:
            return None
        if _looks_like_poe_item(text):
            item = parse_item(text)
            self._last = text
            self._on_item(item)
            return item
        return None

    def _run(self) -> None:
        from .input_guard import log_event  # avoid Win32 import at module-load
        while not self._stop.is_set():
            try:
                text = pyperclip.paste()
            except Exception:
                text = None
            if text and text != self._last and _looks_like_poe_item(text):
                self._last = text
                try:
                    item = parse_item(text)
                    mods_preview = (item.mods[:3] if item.mods else [])
                    log_event("clip read",
                              f"base={item.base!r} mods={mods_preview}")
                    self._on_item(item)
                except Exception as e:
                    # Log parse errors — they used to be swallowed silently,
                    # which meant a broken item format (e.g. PoE patch
                    # changed tooltip shape) would look exactly like "no
                    # clipboard event arrived" and cause burns.
                    log_event("clip err",
                              f"{type(e).__name__}: {e!s:.120}")
            self._stop.wait(self._poll)
