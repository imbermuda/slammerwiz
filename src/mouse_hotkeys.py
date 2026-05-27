"""Mouse-button hotkeys — binds extra mouse buttons (XButton1 = Mouse4,
XButton2 = Mouse5, middle click) to app callbacks.

Uses a separate ``pynput`` listener thread. The listener runs in parallel
with the input guard's Win32 hook — Windows allows multiple low-level
mouse hooks, and pynput does not suppress events. Callbacks fire on
button *press* only (not release) and are dispatched into a daemon thread
so handler work doesn't block the mouse pipeline.
"""

from __future__ import annotations

import threading
from typing import Callable, Dict, Optional

try:
    from pynput.mouse import Button, Listener as _MouseListener
except Exception:  # pragma: no cover
    Button = None  # type: ignore
    _MouseListener = None  # type: ignore


_NAME_TO_BUTTON = {}
if Button is not None:
    _NAME_TO_BUTTON = {
        "mouse4": Button.x1,
        "mouse5": Button.x2,
        "x1":     Button.x1,
        "x2":     Button.x2,
        "middle": Button.middle,
    }


class MouseHotkeys:
    def __init__(self) -> None:
        self._bindings: Dict[object, Callable[[], None]] = {}
        self._listener: Optional[object] = None

    @property
    def available(self) -> bool:
        return _MouseListener is not None

    def bind(self, name: str, callback: Callable[[], None]) -> None:
        if not self.available or not name:
            return
        btn = _NAME_TO_BUTTON.get(name.lower())
        if btn is None:
            return
        self._bindings[btn] = callback

    def start(self) -> None:
        if not self.available or not self._bindings:
            return
        if self._listener:
            return
        bindings = dict(self._bindings)

        def _on_click(_x, _y, button, pressed):
            if not pressed:
                return
            cb = bindings.get(button)
            if cb is None:
                return
            # Callback should be trivially fast (e.g. QTimer.singleShot
            # dispatching to the Qt main thread). Call directly.
            _safe(cb)

        self._listener = _MouseListener(on_click=_on_click)
        self._listener.daemon = True
        self._listener.start()

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None


def _safe(fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception:
        pass
