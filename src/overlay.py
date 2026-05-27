"""Always-on-top translucent overlay UI (PyQt6)."""

from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


CSS = """
QWidget#Root {
    background-color: rgba(12, 14, 18, 220);
    border: 1px solid #2a2f3a;
    border-radius: 10px;
}
QLabel#Title    { color: #e5c07b; font-size: 14px; font-weight: 600; letter-spacing: 0.5px; }
QLabel#Status   { color: #d7dae0; font-size: 12px; }
QLabel#Counter  { color: #98c379; font-size: 22px; font-weight: 700; }
QLabel#Hit      { color: #ff5f56; font-size: 28px; font-weight: 800; letter-spacing: 2px; }
QLabel#Rule     { color: #61afef; font-size: 11px; }
QLabel#Meta     { color: #7a808c; font-size: 10px; }
QPushButton#Close {
    background-color: transparent; color: #7a808c; border: none;
    font-size: 16px; font-weight: 700; padding: 0; min-width: 20px; max-width: 20px;
    min-height: 20px; max-height: 20px; border-radius: 10px;
}
QPushButton#Close:hover { background-color: #ff5f56; color: white; }
QLabel#Latency  { color: #abb2bf; font-size: 10px; }
QLabel#Sync     { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 8px; }
QLabel#Sync[state="live"]    { background-color: #1e3a24; color: #98c379; }
QLabel#Sync[state="sending"] { background-color: #1a3050; color: #61afef; }
QLabel#Sync[state="queued"]  { background-color: #3a2f1a; color: #e5c07b; }
QLabel#Sync[state="offline"] { background-color: #3b2d2d; color: #ff5f56; }
QLabel#Sync[state="idle"]    { background-color: #23262d; color: #7a808c; }
QLabel#Sync[state="off"]     { background-color: #23262d; color: #7a808c; }
QPushButton {
    background-color: #23262d; color: #d7dae0; border: 1px solid #2f3340;
    border-radius: 6px; padding: 4px 10px; font-size: 11px;
}
QPushButton:hover { background-color: #2c313c; }
QPushButton#Arm[armed="true"]  { background-color: #3b2d2d; color: #ff5f56; border-color: #5a3838; }
QPushButton#Arm[armed="false"] { background-color: #2a3a2a; color: #98c379; border-color: #3a5a3a; }
QFrame#HitFlash     { background-color: rgba(255, 80, 80, 180); border-radius: 10px; }
QFrame#HitFlashGod  { background-color: rgba(255, 200, 60, 210); border-radius: 10px; }
QLabel#HitGod       { color: #0e0e10; font-size: 30px; font-weight: 900; letter-spacing: 4px; }
"""


class Overlay(QWidget):
    hit_signal      = pyqtSignal(str, bool)  # message, is_god_mod
    status_signal   = pyqtSignal(str)
    counter_signal  = pyqtSignal(int, int)
    latency_signal  = pyqtSignal(float, float, int)  # total_ms, ocr_ms, credits
    sync_signal     = pyqtSignal(str, int, str)      # state, pending, detail
    dispatch_signal = pyqtSignal(object)             # runs any callable on the main thread

    def __init__(
        self,
        on_toggle_arm: Callable[[], None],
        on_manual_capture: Callable[[], None],
        on_flush: Callable[[], None],
        on_calibrate: Callable[[], None],
        on_rules: Callable[[], None],
    ):
        super().__init__()
        self._on_toggle = on_toggle_arm
        self._on_capture = on_manual_capture
        self._on_flush = on_flush
        self._on_calibrate = on_calibrate
        self._on_rules = on_rules
        self._drag_pos = None
        self._armed = False

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(340, 270)
        self._build()
        self.setStyleSheet(CSS)

        self.hit_signal.connect(self._show_hit)
        self.status_signal.connect(self._set_status)
        self.counter_signal.connect(self._set_counter)
        self.latency_signal.connect(self._set_latency)
        self.sync_signal.connect(self._set_sync)
        self.dispatch_signal.connect(self._run_on_main)

        self._hit_timer = QTimer(self)
        self._hit_timer.setSingleShot(True)
        self._hit_timer.timeout.connect(self._clear_hit)

    def _run_on_main(self, fn) -> None:
        """Slot for dispatch_signal — runs any callable on the Qt main thread."""
        try:
            fn()
        except Exception as e:
            print(f"[overlay] main-thread dispatch error: {type(e).__name__}: {e}")

    def _build(self) -> None:
        root = QWidget(self)
        root.setObjectName("Root")
        root.setGeometry(0, 0, self.width(), self.height())
        layout = QVBoxLayout(root)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("⚡ SlammerWiz")
        title.setObjectName("Title")
        header.addWidget(title)
        header.addStretch()
        self.meta = QLabel("drag to move")
        self.meta.setObjectName("Meta")
        header.addWidget(self.meta)
        self.sync_badge = QLabel("○ sync")
        self.sync_badge.setObjectName("Sync")
        self.sync_badge.setProperty("state", "idle")
        self.sync_badge.setToolTip("Sync status. Hover/click Sync button to flush now.")
        header.addWidget(self.sync_badge)

        self.close_btn = QPushButton("×")
        self.close_btn.setObjectName("Close")
        self.close_btn.setToolTip("Quit (Ctrl+Q)")
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(QApplication.quit)
        header.addWidget(self.close_btn)
        layout.addLayout(header)

        self.status = QLabel("idle")
        self.status.setObjectName("Status")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.counter = QLabel("0 slams")
        self.counter.setObjectName("Counter")
        layout.addWidget(self.counter)

        self.rule = QLabel("—")
        self.rule.setObjectName("Rule")
        self.rule.setWordWrap(True)
        layout.addWidget(self.rule)

        # Hidden by default — clipboard mode doesn't have anything
        # meaningful to put here. Kept as a widget so OCR-mode code paths
        # (if anyone ever re-enables vision) still wire up cleanly.
        self.latency = QLabel("")
        self.latency.setObjectName("Latency")
        self.latency.hide()
        layout.addWidget(self.latency)

        layout.addStretch()

        row1 = QHBoxLayout()
        self.arm_btn = QPushButton("ARM (F9)")
        self.arm_btn.setObjectName("Arm")
        self.arm_btn.setProperty("armed", "false")
        self.arm_btn.clicked.connect(self._on_toggle)
        row1.addWidget(self.arm_btn)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        self.rules_btn = QPushButton("Rules…")
        self.rules_btn.setToolTip("Open the target rules editor.")
        self.rules_btn.clicked.connect(self._on_rules)
        row2.addWidget(self.rules_btn)
        layout.addLayout(row2)
        # Sync flush button removed — the sync badge in the header
        # already communicates state, and auto-flush every 15s covers
        # the common case. The manual flush callback (_on_flush) is
        # still bound to the on_flush param so existing hotkey wiring
        # in main.py doesn't break.

        self.flash = QFrame(self)
        self.flash.setObjectName("HitFlash")
        self.flash.setGeometry(0, 0, self.width(), self.height())
        self.flash.hide()

        self.hit_label = QLabel("HIT", self)
        self.hit_label.setObjectName("Hit")
        self.hit_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hit_label.setGeometry(0, 0, self.width(), self.height())
        self.hit_label.hide()

    # --- public --------------------------------------------------------

    def set_armed(self, armed: bool) -> None:
        self._armed = armed
        self.arm_btn.setProperty("armed", "true" if armed else "false")
        self.arm_btn.setText("DISARM (F9)" if armed else "ARM (F9)")
        self.arm_btn.style().unpolish(self.arm_btn)
        self.arm_btn.style().polish(self.arm_btn)
        self.status_signal.emit("armed — gate closed until miss" if armed else "disarmed")

    # --- slots ---------------------------------------------------------

    def _set_status(self, text: str) -> None:
        self.status.setText(text)

    def _set_counter(self, slams: int, hit_flag: int) -> None:
        # Per-item counter: slams since the last reset (arm or RMB-release).
        # hit_flag set when the latest transition matched a rule.
        if hit_flag:
            self.counter.setText(f"{slams} slams · HIT")
        else:
            self.counter.setText(f"{slams} slams")

    def _set_latency(self, total_ms: float, ocr_ms: float, credits: int) -> None:
        gate = "OPEN" if credits > 0 else "CLOSED"
        self.latency.setText(f"vision {total_ms:>4.0f}ms  ocr {ocr_ms:>4.0f}ms  gate {gate}")

    def _set_sync(self, state: str, pending: int, detail: str) -> None:
        glyph = {
            "live":    "●",
            "sending": "→",
            "queued":  "●",
            "offline": "●",
            "idle":    "○",
            "off":     "⊘",
        }.get(state, "○")
        label_map = {
            "live":    f"{glyph} live",
            "sending": f"{glyph} syncing",
            "queued":  f"{glyph} queued {pending}",
            "offline": f"{glyph} offline",
            "idle":    f"{glyph} idle",
            "off":     f"{glyph} sync off",
        }
        self.sync_badge.setText(label_map.get(state, f"{glyph} sync"))
        self.sync_badge.setProperty("state", state)
        tip = f"sync: {state}"
        if pending:
            tip += f" · {pending} pending"
        if detail:
            tip += f"\n{detail}"
        self.sync_badge.setToolTip(tip)
        self.sync_badge.style().unpolish(self.sync_badge)
        self.sync_badge.style().polish(self.sync_badge)

    def _show_hit(self, rule_text: str, is_god: bool = False) -> None:
        self.rule.setText(f"{rule_text}  ·  right-click to release")
        self.flash.setObjectName("HitFlashGod" if is_god else "HitFlash")
        self.flash.style().unpolish(self.flash)
        self.flash.style().polish(self.flash)
        self.flash.show()
        self.hit_label.setObjectName("HitGod" if is_god else "Hit")
        self.hit_label.setText("★ GOD ★" if is_god else "HIT")
        self.hit_label.style().unpolish(self.hit_label)
        self.hit_label.style().polish(self.hit_label)
        self.hit_label.show()
        self.hit_label.raise_()
        self._hit_timer.start(1500)

    def _clear_hit(self) -> None:
        self.flash.hide()
        self.hit_label.hide()
        # Reset the rule line — otherwise the "right-click to release"
        # prompt stays on screen after the auto-clear timer (or RMB
        # release) fires, making it look like the gate is still locked
        # when it actually isn't. Confused users hit F9 and rebuild from
        # scratch when they could've just kept slamming.
        self.rule.setText("—")

    # --- drag ----------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()


def place_top_right(widget: QWidget, margin: int = 24) -> None:
    screen = QApplication.primaryScreen().availableGeometry()
    x = screen.right() - widget.width() - margin
    y = screen.top() + margin
    widget.move(x, y)
