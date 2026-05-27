"""Full-screen overlay — user drags a rectangle to calibrate the tooltip ROI.

Implementation notes:

  * One widget per screen, each restricted to that screen's geometry. Avoids
    the virtual-desktop-spanning translucent widget which hangs Windows Qt
    on multi-monitor setups.
  * No CompositionMode_Clear — that primitive doesn't work on translucent
    windows on Windows and caused the freeze. We just draw a red border +
    dim fill outside the selection.
  * Escape or right-click cancels. Enter/space or mouse-release confirms.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from PyQt6.QtCore import QPoint, QRect, Qt
from PyQt6.QtGui import QBrush, QColor, QGuiApplication, QPainter, QPen
from PyQt6.QtWidgets import QApplication, QWidget


class _PickerPane(QWidget):
    def __init__(self, screen_geom: QRect, on_pick: Callable[[QRect], None],
                 on_cancel: Callable[[], None]):
        super().__init__()
        self._on_pick = on_pick
        self._on_cancel = on_cancel
        self._start: Optional[QPoint] = None
        self._end: Optional[QPoint] = None
        self._screen_geom = screen_geom

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.BypassWindowManagerHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setGeometry(screen_geom)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        # Dim the whole pane uniformly. No "clear inside" — that primitive
        # misbehaves on translucent widgets on Windows.
        p.fillRect(self.rect(), QColor(0, 0, 0, 90))
        if self._start and self._end:
            r = QRect(self._start, self._end).normalized()
            # Lighter tint inside the selection, full-opacity red border.
            p.fillRect(r, QColor(255, 95, 86, 40))
            p.setPen(QPen(QColor(255, 95, 86, 255), 2))
            p.drawRect(r)
            p.setPen(QColor(255, 255, 255, 230))
            p.drawText(r.topLeft() + QPoint(6, -6), f"{r.width()} x {r.height()}")
            hint = "release to confirm · right-click / Esc to cancel"
            p.drawText(r.bottomLeft() + QPoint(6, 16), hint)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self._on_cancel()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._start = event.pos()
            self._end = event.pos()
            self.update()

    def mouseMoveEvent(self, event):
        if self._start:
            old = QRect(self._start, self._end or self._start).normalized().adjusted(-4, -20, 4, 20)
            self._end = event.pos()
            new = QRect(self._start, self._end).normalized().adjusted(-4, -20, 4, 20)
            self.update(old.united(new))

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._start and self._end:
            r = QRect(self._start, self._end).normalized()
            if r.width() >= 40 and r.height() >= 40:
                # Translate local -> absolute screen coords
                abs_r = QRect(r.topLeft() + self._screen_geom.topLeft(), r.size())
                self._on_pick(abs_r)
            else:
                self._start = None
                self._end = None
                self.update()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Escape,):
            self._on_cancel()


class RegionPicker:
    """Opens one picker pane per monitor. Calls ``on_pick(x, y, w, h)`` once."""

    def __init__(self, on_pick: Callable[[Tuple[int, int, int, int]], None]):
        self._on_pick = on_pick
        self._panes: List[_PickerPane] = []

    def show(self) -> None:
        def picked(rect: QRect) -> None:
            self._on_pick((rect.x(), rect.y(), rect.width(), rect.height()))
            self.close()

        def cancelled() -> None:
            self.close()

        for screen in QGuiApplication.screens():
            pane = _PickerPane(screen.geometry(), picked, cancelled)
            pane.show()
            pane.raise_()
            pane.activateWindow()
            self._panes.append(pane)

    def close(self) -> None:
        for p in self._panes:
            try:
                p.close()
            except Exception:
                pass
        self._panes.clear()
