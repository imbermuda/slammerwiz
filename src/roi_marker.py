"""Small always-on-top widget that outlines the active ROI on screen.

Click-through so it doesn't steal mouse events from the game. Shows the user
exactly what rectangle OCR is watching — invaluable for diagnosing "why is
the matcher reading inventory stack numbers instead of mod lines."
"""

from __future__ import annotations

from typing import Tuple

from PyQt6.QtCore import Qt, QRect
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QWidget


class RoiMarker(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput  # clicks pass through
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.hide()

    def set_roi(self, roi: Tuple[int, int, int, int]) -> None:
        x, y, w, h = roi
        if w <= 0 or h <= 0:
            self.hide()
            return
        pad = 4
        self.setGeometry(x - pad, y - pad, w + pad * 2, h + pad * 2)
        self.update()
        if not self.isVisible():
            self.show()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        # Lime-green outline so it's visible against most PoE tooltips.
        pen = QPen(QColor(80, 220, 120, 220), 2)
        p.setPen(pen)
        p.drawRect(2, 2, self.width() - 5, self.height() - 5)
        # Tiny corner ticks for easy visual anchor.
        p.setPen(QPen(QColor(80, 220, 120, 255), 3))
        tick = 12
        for cx, cy in (
            (2, 2), (self.width() - 3, 2),
            (2, self.height() - 3), (self.width() - 3, self.height() - 3),
        ):
            p.drawLine(cx, cy, cx + (tick if cx < self.width() / 2 else -tick), cy)
            p.drawLine(cx, cy, cx, cy + (tick if cy < self.height() / 2 else -tick))
