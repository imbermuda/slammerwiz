"""Global hotkey registration via the `keyboard` package."""

from __future__ import annotations

from typing import Callable, Dict

try:
    import keyboard as _keyboard
except Exception:  # pragma: no cover
    _keyboard = None  # type: ignore[assignment]


class Hotkeys:
    def __init__(self) -> None:
        self._registered: Dict[str, Callable] = {}
        self._available = _keyboard is not None

    @property
    def available(self) -> bool:
        return self._available

    def bind(self, combo: str, callback: Callable[[], None]) -> None:
        if not self._available or not combo:
            return
        # Remove prior binding so re-bind works cleanly.
        if combo in self._registered:
            try:
                _keyboard.remove_hotkey(combo)  # type: ignore[attr-defined]
            except Exception:
                pass
        try:
            _keyboard.add_hotkey(combo, callback, suppress=False)  # type: ignore[attr-defined]
            self._registered[combo] = callback
        except Exception:
            pass

    def unbind_all(self) -> None:
        if not self._available:
            return
        try:
            _keyboard.unhook_all()  # type: ignore[attr-defined]
        except Exception:
            pass
        self._registered.clear()
