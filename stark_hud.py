"""F.R.I.D.A.Y. HUD — PyQt6 Tony Stark-style interface.

Layout:  title bar · telemetry / subsystem lamps / security feed (left) · animated arc reactor + controls (centre)
         comms transcript + typed input (right) · live terminal log (bottom)
Extras:  floating always-on-top "orb" overlay (double-click to expand), system-tray icon,
         authorisation dialog (Deny is the default button; auto-denies on timeout).

Threading: every public method here is safe to call from ANY thread — they only emit Qt signals, which Qt
delivers on the GUI thread. The GUI thread never blocks; all real work happens in the asyncio thread (main.py).
"""
from __future__ import annotations

import concurrent.futures
import html
import logging
import math
import random
import time
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QBrush, QColor, QFont, QIcon, QKeySequence, QLinearGradient, QPainter, QPen,
                         QPixmap, QPolygonF, QRadialGradient, QShortcut)
from PyQt6.QtWidgets import (QApplication, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                             QMenu, QPlainTextEdit, QPushButton, QSizeGrip, QSystemTrayIcon, QTextBrowser, QVBoxLayout,
                             QWidget)

from friday_ai.config import BG, CYAN, DIM, ORANGE, RED, TEXT

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore


# ───────────────────────────── helpers ─────────────────────────────
def qc(color: str, alpha: Optional[int] = None) -> QColor:
    c = QColor(color)
    if alpha is not None:
        c.setAlpha(max(0, min(255, int(alpha))))
    return c


def mono(size: float = 10, bold: bool = False) -> QFont:
    f = QFont()
    f.setFamilies(["Cascadia Mono", "Consolas", "SF Mono", "Menlo", "DejaVu Sans Mono", "Courier New"])
    f.setPointSizeF(size)
    f.setBold(bold)
    f.setStyleHint(QFont.StyleHint.Monospace)
    return f


def display(size: float = 14, bold: bool = True, spacing: float = 3) -> QFont:
    f = QFont()
    f.setFamilies(["Orbitron", "Bahnschrift", "Segoe UI", "Helvetica Neue", "DejaVu Sans"])
    f.setPointSizeF(size)
    f.setBold(bold)
    f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    return f


STATE_COLOR = {"idle": CYAN, "listening": "#8ffcff", "thinking": ORANGE, "speaking": CYAN, "alert": RED, "muted": "#3b5566"}
STATE_TEXT = {"idle": "STANDING BY", "listening": "LISTENING", "thinking": "PROCESSING", "speaking": "RESPONDING",
              "alert": "ALERT", "muted": "WAKE WORD MUTED"}
LOG_COLOR = {"DEBUG": DIM, "INFO": "#5fd8e0", "SYSTEM": CYAN, "TOOL": ORANGE, "WARNING": ORANGE, "ERROR": RED, "CRITICAL": RED}

STYLE = f"""
QWidget {{ color: {TEXT}; font-family: 'Cascadia Mono','Consolas','DejaVu Sans Mono',monospace; }}
QLabel {{ background: transparent; }}
QPlainTextEdit, QTextBrowser {{ background: rgba(6,8,14,200); border: 1px solid rgba(0,243,255,40); color: {TEXT};
    selection-background-color: rgba(0,243,255,70); font-size: 11px; }}
QLineEdit {{ background: rgba(6,8,14,230); border: 1px solid rgba(0,243,255,110); padding: 7px 9px; color: {TEXT};
    font-size: 12px; selection-background-color: {CYAN}; selection-color: #000; }}
QLineEdit:focus {{ border: 1px solid {CYAN}; }}
QPushButton {{ background: rgba(0,243,255,18); border: 1px solid rgba(0,243,255,140); color: {CYAN};
    padding: 7px 12px; font-size: 10px; font-weight: bold; }}
QPushButton:hover {{ background: rgba(0,243,255,60); }}
QPushButton:checked {{ background: rgba(255,170,0,60); border-color: {ORANGE}; color: {ORANGE}; }}
QPushButton#danger {{ border-color: rgba(255,59,59,170); color: {RED}; background: rgba(255,59,59,18); }}
QPushButton#danger:hover {{ background: rgba(255,59,59,80); }}
QPushButton#flat {{ padding: 2px 9px; border: 1px solid rgba(0,243,255,70); }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
QScrollBar::handle:vertical {{ background: rgba(0,243,255,80); min-height: 20px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QMenu {{ background: #0e1119; border: 1px solid {CYAN}; color: {TEXT}; }}
QMenu::item:selected {{ background: rgba(0,243,255,60); }}
"""


# ───────────────────────────── arc reactor ─────────────────────────────
class ArcReactor(QWidget):
    """Animated arc-reactor with a radial audio spectrum. Colour and motion follow the assistant's state."""

    def __init__(self, parent: Optional[QWidget] = None, compact: bool = False):
        super().__init__(parent)
        self.compact = compact
        self._state = "idle"
        self._level = 0.0
        self._target = 0.0
        self._angle = 0.0
        self._t0 = time.time()
        self._bars = [0.0] * (48 if compact else 72)
        self.setMinimumSize(96, 96)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)

    def set_state(self, state: str) -> None:
        self._state = state if state in STATE_COLOR else "idle"

    def set_level(self, level: float) -> None:
        self._target = max(0.0, min(1.0, float(level)))

    def _tick(self) -> None:
        if not self.isVisible():
            return
        t = time.time() - self._t0
        speed = {"idle": 0.5, "listening": 1.0, "thinking": 3.2, "speaking": 1.6, "alert": 4.0, "muted": 0.12}[self._state]
        self._angle = (self._angle + speed) % 360
        self._level += (self._target - self._level) * 0.35
        self._target *= 0.88
        lvl = self._level
        if self._state == "speaking":
            lvl = max(lvl, 0.35 + 0.25 * abs(math.sin(t * 7.0)))  # engines without amplitude data still look alive
        for i in range(len(self._bars)):
            base = 0.05 + 0.03 * math.sin(t * 2.0 + i * 0.45)
            if self._state in ("listening", "speaking"):
                goal = base + lvl * (0.35 + 0.65 * random.random())
            elif self._state == "thinking":
                goal = base + 0.28 * (0.5 + 0.5 * math.sin(t * 6.0 - i * 0.35))
            elif self._state == "alert":
                goal = base + 0.5 * (0.5 + 0.5 * math.sin(t * 10.0))
            elif self._state == "muted":
                goal = 0.02
            else:
                goal = base
            self._bars[i] += (goal - self._bars[i]) * 0.4
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        r = min(w, h) / 2 - 4
        if r < 24:
            return
        c = qc(STATE_COLOR[self._state])
        t = time.time() - self._t0
        pulse = 0.5 + 0.5 * math.sin(t * 2.2)
        p.translate(w / 2, h / 2)
        p.setPen(Qt.PenStyle.NoPen)

        # ambient glow
        g = QRadialGradient(QPointF(0, 0), r)
        g.setColorAt(0.0, qc(STATE_COLOR[self._state], 90 + 50 * pulse))
        g.setColorAt(0.55, qc(STATE_COLOR[self._state], 22))
        g.setColorAt(1.0, qc(STATE_COLOR[self._state], 0))
        p.setBrush(QBrush(g))
        p.drawEllipse(QPointF(0, 0), r, r)

        # radial spectrum
        inner, max_len, n = r * 0.60, r * 0.22, len(self._bars)
        pen = QPen(c)
        pen.setWidthF(max(1.4, r * (0.016 if not self.compact else 0.02)))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        for i, b in enumerate(self._bars):
            a = 2 * math.pi * i / n
            length = max_len * min(1.0, b)
            col = QColor(c)
            col.setAlpha(int(90 + 150 * min(1.0, b * 1.6)))
            pen.setColor(col)
            p.setPen(pen)
            p.drawLine(QPointF(math.cos(a) * inner, math.sin(a) * inner),
                       QPointF(math.cos(a) * (inner + length), math.sin(a) * (inner + length)))

        # outer tick ring
        if not self.compact:
            p.save()
            p.rotate(self._angle * 0.4)
            p.setPen(QPen(qc(STATE_COLOR[self._state], 150), 1.2))
            for i in range(60):
                a = math.radians(i * 6)
                r1, r2 = r * 0.975, r * (0.925 if i % 5 == 0 else 0.95)
                p.drawLine(QPointF(math.cos(a) * r1, math.sin(a) * r1), QPointF(math.cos(a) * r2, math.sin(a) * r2))
            p.restore()
            p.setPen(QPen(qc(STATE_COLOR[self._state], 70), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(0, 0), r * 0.90, r * 0.90)

        # golden-orange counter-rotating arcs
        p.save()
        p.rotate(-self._angle * 1.4)
        arc_pen = QPen(QColor(ORANGE))
        arc_pen.setWidthF(max(2.0, r * 0.03))
        arc_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(arc_pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        rr = r * 0.83
        for k in range(3):
            p.drawArc(QRectF(-rr, -rr, 2 * rr, 2 * rr), int(k * 120 * 16), int(58 * 16))
        p.restore()

        # reactor coil ring
        ring_r, th = r * 0.47, r * 0.12
        p.setBrush(QBrush(qc(STATE_COLOR[self._state], 90 + 60 * pulse)))
        p.setPen(QPen(qc(STATE_COLOR[self._state], 220), 1.2))
        for i in range(10):
            p.save()
            p.rotate(i * 36 + 18)
            p.drawPolygon(QPolygonF([QPointF(-r * 0.055, -ring_r - th), QPointF(r * 0.055, -ring_r - th),
                                     QPointF(r * 0.038, -ring_r), QPointF(-r * 0.038, -ring_r)]))
            p.restore()

        # core
        core = r * 0.27
        g2 = QRadialGradient(QPointF(0, 0), core * 1.6)
        g2.setColorAt(0.0, QColor(255, 255, 255, 255))
        g2.setColorAt(0.35, qc(STATE_COLOR[self._state], 255))
        g2.setColorAt(1.0, qc(STATE_COLOR[self._state], 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g2))
        p.drawEllipse(QPointF(0, 0), core * 1.6, core * 1.6)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(qc(STATE_COLOR[self._state], 255), max(1.5, r * 0.015)))
        p.drawEllipse(QPointF(0, 0), core * 1.05, core * 1.05)


# ───────────────────────────── small widgets ─────────────────────────────
class HudPanel(QFrame):
    """Dark glass panel with cyan corner brackets and a title."""

    def __init__(self, title: str, parent: Optional[QWidget] = None, accent: str = CYAN):
        super().__init__(parent)
        self._title, self._accent = title, accent
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(14, 34, 14, 12)
        self.body.setSpacing(6)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.fillRect(rect, QColor(12, 16, 26, 215))
        p.setPen(QPen(qc(self._accent, 55), 1))
        p.drawRect(rect)
        p.setPen(QPen(qc(self._accent, 235), 2))
        length = 14
        x0, y0, x1, y1 = rect.left(), rect.top(), rect.right(), rect.bottom()
        for cx, cy, dx, dy in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
            p.drawLine(QPointF(cx, cy), QPointF(cx + dx * length, cy))
            p.drawLine(QPointF(cx, cy), QPointF(cx, cy + dy * length))
        p.setFont(mono(8.5, True))
        p.setPen(qc(self._accent))
        p.drawText(QPointF(16, 21), self._title)
        p.setPen(QPen(qc(self._accent, 60), 1))
        p.drawLine(QPointF(14, 28), QPointF(rect.right() - 14, 28))


class Led(QWidget):
    COLORS = {"off": "#2a3644", "on": CYAN, "busy": ORANGE, "warn": ORANGE, "error": RED}

    def __init__(self, label: str):
        super().__init__()
        self._label, self._state = label, "off"
        self.setFixedHeight(22)
        self.setMinimumWidth(112)

    def set_state(self, state: str) -> None:
        self._state = state if state in self.COLORS else "off"
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cy = self.height() / 2
        col = QColor(self.COLORS[self._state])
        if self._state == "busy" and int(time.time() * 2.5) % 2 == 0:
            col.setAlpha(90)
        if self._state != "off":
            g = QRadialGradient(QPointF(10, cy), 10)
            g.setColorAt(0, qc(col.name(), 130))
            g.setColorAt(1, qc(col.name(), 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(g))
            p.drawEllipse(QPointF(10, cy), 10, 10)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawEllipse(QPointF(10, cy), 4, 4)
        p.setFont(mono(8.5))
        p.setPen(qc(TEXT if self._state != "off" else DIM))
        p.drawText(QPointF(24, cy + 4), self._label)


class Meter(QWidget):
    def __init__(self, label: str):
        super().__init__()
        self._label, self._value = label, 0.0
        self.setFixedHeight(30)

    def set_value(self, v: float) -> None:
        self._value = max(0.0, min(100.0, v))
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        p.setFont(mono(8.5))
        p.setPen(qc(DIM))
        p.drawText(QPointF(0, 11), self._label)
        p.setPen(qc(TEXT))
        p.drawText(QRectF(0, 0, w, 14), Qt.AlignmentFlag.AlignRight, f"{self._value:.0f}%")
        bar = QRectF(0, 19, w, 6)
        p.fillRect(bar, QColor(255, 255, 255, 18))
        fill = QRectF(0, 19, w * self._value / 100.0, 6)
        grad = QLinearGradient(0, 0, w, 0)
        grad.setColorAt(0, QColor(CYAN))
        grad.setColorAt(0.7, QColor(CYAN))
        grad.setColorAt(1, QColor(ORANGE))
        p.fillRect(fill, QBrush(grad))


# ───────────────────────────── confirmation dialog ─────────────────────────────
class ConfirmDialog(QDialog):
    """Authorisation prompt. DENY is the default button; timing out or closing counts as deny."""

    def __init__(self, message: str, future: concurrent.futures.Future, timeout: int):
        super().__init__(None)
        self._future = future
        self._remaining = max(5, timeout)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 24, 26, 20)
        lay.setSpacing(12)
        title = QLabel("▲  AUTHORISATION REQUIRED")
        title.setFont(display(13, True, 2))
        title.setStyleSheet(f"color: {ORANGE};")
        body = QLabel(message)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.setFont(mono(10))
        body.setStyleSheet("background: rgba(0,0,0,110); border: 1px solid rgba(255,170,0,70); padding: 12px;")
        self._count = QLabel()
        self._count.setStyleSheet(f"color: {DIM};")
        row = QHBoxLayout()
        self.btn_ok = QPushButton("AUTHORISE")
        self.btn_no = QPushButton("DENY")
        self.btn_no.setObjectName("danger")
        self.btn_no.setDefault(True)   # an accidental Enter must never approve anything
        self.btn_ok.setAutoDefault(False)
        self.btn_ok.clicked.connect(self.accept)
        self.btn_no.clicked.connect(self.reject)
        row.addWidget(self._count, 1)
        row.addWidget(self.btn_no)
        row.addWidget(self.btn_ok)
        lay.addWidget(title)
        lay.addWidget(body)
        lay.addLayout(row)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)
        self._tick(first=True)

    def _tick(self, first: bool = False) -> None:
        if not first:
            self._remaining -= 1
        self._count.setText(f"auto-deny in {self._remaining}s")
        if self._remaining <= 0:
            self.reject()

    def done(self, result: int) -> None:  # noqa: D401 — resolves the future exactly once
        self._timer.stop()
        try:
            if not self._future.done():
                self._future.set_result(result == 1)
        except Exception:  # noqa: BLE001 — future may have been cancelled by the async side
            pass
        super().done(result)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(10, 11, 16, 250))
        p.setPen(QPen(QColor(ORANGE), 2))
        p.drawRect(self.rect().adjusted(1, 1, -2, -2))


# ───────────────────────────── floating orb overlay ─────────────────────────────
class FloatingOrb(QWidget):
    open_requested = pyqtSignal()
    activate_requested = pyqtSignal()
    quit_requested = pyqtSignal()

    def __init__(self):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(128, 128)
        self.reactor = ArcReactor(self, compact=True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.reactor)
        self._drag: Optional[QPointF] = None
        self._placed = False
        self.setToolTip("F.R.I.D.A.Y. — double-click to open the HUD, right-click for options")

    def place_default(self) -> None:
        if self._placed:
            return
        geo = QApplication.primaryScreen().availableGeometry()
        self.move(geo.right() - self.width() - 24, geo.top() + 80)
        self._placed = True

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        if self._drag is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _e) -> None:  # noqa: N802
        self._drag = None

    def mouseDoubleClickEvent(self, _e) -> None:  # noqa: N802
        self.open_requested.emit()

    def contextMenuEvent(self, e) -> None:  # noqa: N802
        menu = QMenu(self)
        menu.addAction("Open HUD", self.open_requested.emit)
        menu.addAction("Activate (listen now)", self.activate_requested.emit)
        menu.addSeparator()
        menu.addAction("Power off F.R.I.D.A.Y.", self.quit_requested.emit)
        menu.exec(e.globalPos())


# ───────────────────────────── chrome ─────────────────────────────
class TitleBar(QWidget):
    orb_clicked = pyqtSignal()
    min_clicked = pyqtSignal()
    close_clicked = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setFixedHeight(48)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 0, 6, 0)
        lay.setSpacing(10)
        self.logo = ArcReactor(compact=True)
        self.logo.setFixedSize(42, 42)
        title = QLabel("F.R.I.D.A.Y.")
        title.setFont(display(17, True, 5))
        title.setStyleSheet(f"color: {CYAN};")
        sub = QLabel("FEMALE REPLACEMENT INTELLIGENT DIGITAL ASSISTANT YOUTH")
        sub.setFont(mono(7.5))
        sub.setStyleSheet(f"color: {DIM};")
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(title)
        col.addWidget(sub)
        lay.addWidget(self.logo)
        lay.addLayout(col)
        lay.addStretch(1)
        self.status = QLabel("")
        self.status.setStyleSheet(f"color: {ORANGE};")
        self.status.setFont(mono(9))
        lay.addWidget(self.status)
        for text, sig in (("ORB", self.orb_clicked), ("—", self.min_clicked), ("✕", self.close_clicked)):
            b = QPushButton(text)
            b.setObjectName("flat")
            b.setFixedHeight(24)
            b.clicked.connect(sig)
            lay.addWidget(b)

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton and self.window().windowHandle():
            self.window().windowHandle().startSystemMove()


class HudRoot(QWidget):
    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor(BG))
        p.setPen(QPen(QColor(0, 243, 255, 10), 1))
        for x in range(0, w, 40):
            p.drawLine(x, 0, x, h)
        for y in range(0, h, 40):
            p.drawLine(0, y, w, y)
        g = QRadialGradient(QPointF(w / 2, h / 2), max(w, h) * 0.7)
        g.setColorAt(0.55, QColor(0, 0, 0, 0))
        g.setColorAt(1.0, QColor(0, 0, 0, 160))
        p.fillRect(self.rect(), QBrush(g))
        p.setPen(QPen(QColor(0, 243, 255, 110), 1))
        p.drawRect(0, 0, w - 1, h - 1)
        p.setPen(QPen(QColor(ORANGE), 2))
        p.drawLine(0, 0, 26, 0)
        p.drawLine(0, 0, 0, 26)
        p.drawLine(w - 1, h - 1, w - 27, h - 1)
        p.drawLine(w - 1, h - 1, w - 1, h - 27)


def _make_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(CYAN), 4))
    p.setBrush(QColor(0, 243, 255, 60))
    p.drawEllipse(6, 6, 52, 52)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("white"))
    p.drawEllipse(24, 24, 16, 16)
    p.end()
    return QIcon(pm)


# ───────────────────────────── the HUD window ─────────────────────────────
class StarkHUD(QMainWindow):
    # inbound (emit from any thread)
    _sig_state = pyqtSignal(str)
    _sig_log = pyqtSignal(str, str)
    _sig_say = pyqtSignal(str, str)
    _sig_level = pyqtSignal(float)
    _sig_led = pyqtSignal(str, str)
    _sig_status = pyqtSignal(str)
    _sig_security = pyqtSignal(str)
    _sig_confirm = pyqtSignal(int, str, object, int)
    _sig_dismiss = pyqtSignal(int)
    _sig_sec_button = pyqtSignal(bool)
    # outbound
    text_submitted = pyqtSignal(str)
    activate_requested = pyqtSignal()
    mute_toggled = pyqtSignal(bool)
    security_toggled = pyqtSignal(bool)
    quit_requested = pyqtSignal()

    LED_NAMES = [("mic", "MICROPHONE"), ("wake", "WAKE WORD"), ("voice", "VOICE OUT"), ("link", "MODEL LINK"),
                 ("cam", "WEBCAM"), ("screen", "SCREEN VIEW"), ("security", "SECURITY"), ("whatsapp", "WHATSAPP"),
                 ("firetv", "FIRE TV"), ("agents", "AGENTS"), ("presence", "PRESENCE")]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("F.R.I.D.A.Y.")
        self.setWindowIcon(_make_icon())
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.resize(1260, 820)
        self.setMinimumSize(1000, 660)
        self.setStyleSheet(STYLE)
        self._really_quit = False
        self._dialog: Optional[ConfirmDialog] = None
        self._dialog_id: Optional[int] = None
        self._confirm_queue: list[tuple[int, str, concurrent.futures.Future, int]] = []
        self._confirm_seq = 0

        root = HudRoot()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 10, 14, 6)
        outer.setSpacing(8)

        self.titlebar = TitleBar()
        outer.addWidget(self.titlebar)
        body = QHBoxLayout()
        body.setSpacing(10)
        outer.addLayout(body, 1)

        # left column
        tele = HudPanel("TELEMETRY")
        self.clock = QLabel("--:--:--")
        self.clock.setFont(display(22, True, 3))
        self.clock.setStyleSheet(f"color: {CYAN};")
        self.date = QLabel("")
        self.date.setStyleSheet(f"color: {DIM};")
        self.m_cpu, self.m_mem, self.m_disk = Meter("CPU"), Meter("MEMORY"), Meter("DISK")
        self.battery = QLabel("")
        self.battery.setStyleSheet(f"color: {DIM};")
        for wdg in (self.clock, self.date, self.m_cpu, self.m_mem, self.m_disk, self.battery):
            tele.body.addWidget(wdg)

        subs = HudPanel("SUBSYSTEMS", accent=ORANGE)
        grid = QGridLayout()
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(2)
        self.leds: dict[str, Led] = {}
        for i, (key, label) in enumerate(self.LED_NAMES):
            self.leds[key] = Led(label)
            grid.addWidget(self.leds[key], i, 0)
        subs.body.addLayout(grid)

        sec = HudPanel("SECURITY FEED")
        self.sec_label = QLabel("SECURITY: DISARMED")
        self.sec_label.setWordWrap(True)
        self.sec_label.setFont(mono(8.5))
        self.sec_label.setStyleSheet(f"color: {TEXT};")
        sec.body.addWidget(self.sec_label)

        left = QVBoxLayout()
        left.setSpacing(10)
        left.addWidget(tele)
        left.addWidget(subs)
        left.addWidget(sec)
        left.addStretch(1)
        left_w = QWidget()
        left_w.setLayout(left)
        left_w.setFixedWidth(280)
        body.addWidget(left_w)

        # centre column
        centre = QVBoxLayout()
        self.reactor = ArcReactor()
        centre.addWidget(self.reactor, 1)
        self.state_label = QLabel(STATE_TEXT["idle"])
        self.state_label.setFont(display(20, True, 6))
        self.state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state_label.setStyleSheet(f"color: {CYAN};")
        self.status_label = QLabel("Say “Friday” — or type below.")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet(f"color: {DIM};")
        centre.addWidget(self.state_label)
        centre.addWidget(self.status_label)
        btn_row = QHBoxLayout()
        self.btn_activate = QPushButton("◉  ACTIVATE")
        self.btn_mute = QPushButton("MUTE WAKE WORD")
        self.btn_mute.setCheckable(True)
        self.btn_security = QPushButton("SECURITY MONITOR")
        self.btn_security.setCheckable(True)
        self.btn_orb = QPushButton("OVERLAY")
        self.btn_quit = QPushButton("POWER OFF")
        self.btn_quit.setObjectName("danger")
        for b in (self.btn_activate, self.btn_mute, self.btn_security, self.btn_orb, self.btn_quit):
            btn_row.addWidget(b)
        centre.addLayout(btn_row)
        body.addLayout(centre, 1)

        # right column
        comms = HudPanel("COMMS")
        self.transcript = QTextBrowser()
        self.transcript.setOpenLinks(False)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Type a command and press Enter…")
        comms.body.addWidget(self.transcript, 1)
        comms.body.addWidget(self.input)
        comms.setFixedWidth(390)
        body.addWidget(comms)

        # bottom terminal
        term = HudPanel("TERMINAL  ·  SYSTEM LOG", accent=ORANGE)
        self.terminal = QPlainTextEdit()
        self.terminal.setReadOnly(True)
        self.terminal.setMaximumBlockCount(700)
        self.terminal.setFont(mono(9))
        term.body.addWidget(self.terminal)
        term.setFixedHeight(190)
        outer.addWidget(term)

        foot = QHBoxLayout()
        foot_label = QLabel("STARK INDUSTRIES · F.R.I.D.A.Y. · ALL SYSTEMS LOCAL")
        foot_label.setFont(mono(7.5))
        foot_label.setStyleSheet(f"color: {DIM};")
        foot.addWidget(foot_label)
        foot.addStretch(1)
        foot.addWidget(QSizeGrip(self))
        outer.addLayout(foot)

        # overlay orb + tray
        self.orb = FloatingOrb()
        self.orb.open_requested.connect(self.show_hud)
        self.orb.activate_requested.connect(self.activate_requested)
        self.orb.quit_requested.connect(self.quit_requested)
        self._tray: Optional[QSystemTrayIcon] = None
        self._setup_tray()

        # wiring
        self._sig_state.connect(self._on_state)
        self._sig_log.connect(self._on_log)
        self._sig_say.connect(self._on_say)
        self._sig_level.connect(self._on_level)
        self._sig_led.connect(self._on_led)
        self._sig_status.connect(self._on_status)
        self._sig_security.connect(self._on_security)
        self._sig_confirm.connect(self._on_confirm)
        self._sig_dismiss.connect(self._on_dismiss)
        self._sig_sec_button.connect(self._on_sec_button)
        self.input.returnPressed.connect(self._submit)
        self.btn_activate.clicked.connect(self.activate_requested)
        self.btn_mute.toggled.connect(self._on_mute_toggled)
        self.btn_security.toggled.connect(self.security_toggled)
        self.btn_orb.clicked.connect(self.hide_to_orb)
        self.btn_quit.clicked.connect(self.quit_requested)
        self.titlebar.orb_clicked.connect(self.hide_to_orb)
        self.titlebar.min_clicked.connect(self.showMinimized)
        self.titlebar.close_clicked.connect(self.hide_to_orb)
        QShortcut(QKeySequence("Ctrl+Q"), self).activated.connect(self.quit_requested)

        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._tick_telemetry)
        self._clock_timer.start(1000)
        self._led_timer = QTimer(self)
        self._led_timer.timeout.connect(lambda: [led.update() for led in self.leds.values()])
        self._led_timer.start(400)
        if psutil:
            psutil.cpu_percent(interval=None)
        self._tick_telemetry()

    # ── thread-safe public API ────────────────────────────────
    def set_state(self, state: str) -> None:
        self._sig_state.emit(state)

    def log(self, level: str, text: str) -> None:
        self._sig_log.emit(level.upper(), text)

    def say(self, speaker: str, text: str) -> None:
        self._sig_say.emit(speaker, text)

    def set_level(self, level: float) -> None:
        self._sig_level.emit(float(level))

    def set_indicator(self, name: str, state: str) -> None:
        self._sig_led.emit(name, state)

    def set_status(self, text: str) -> None:
        self._sig_status.emit(text)

    def set_security_text(self, text: str) -> None:
        self._sig_security.emit(text)

    def set_security_button(self, armed: bool) -> None:
        self._sig_sec_button.emit(armed)

    def request_confirmation(self, message: str, timeout: int = 45) -> tuple[int, concurrent.futures.Future]:
        """Queue an authorisation dialog (several actors — the main conversation, background agents — can each
        have one pending; they're shown one at a time, in order). Returns (request_id, a Future resolving to
        True/False). Pass the request_id back to dismiss_confirmation() once you're done waiting on it."""
        self._confirm_seq += 1
        rid = self._confirm_seq
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._sig_confirm.emit(rid, message, fut, timeout)
        return rid, fut

    def dismiss_confirmation(self, request_id: int) -> None:
        """Close this request's dialog if it's the one on screen, or drop it if it's still queued. A no-op if
        it already resolved and closed on its own (the normal case: the user clicked a button)."""
        self._sig_dismiss.emit(request_id)

    def force_quit(self) -> None:
        self._really_quit = True
        if self._tray:
            self._tray.hide()
        self.orb.hide()
        self.close()

    # ── window management ─────────────────────────────────────
    def show_hud(self) -> None:
        self.orb.hide()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def hide_to_orb(self) -> None:
        self.orb.place_default()
        self.orb.show()
        self.hide()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._really_quit:
            event.accept()
        else:
            event.ignore()
            self.hide_to_orb()

    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray = QSystemTrayIcon(_make_icon(), self)
        menu = QMenu()
        menu.addAction("Open HUD", self.show_hud)
        menu.addAction("Overlay orb", self.hide_to_orb)
        menu.addAction("Activate", self.activate_requested.emit)
        menu.addSeparator()
        menu.addAction("Power off", self.quit_requested.emit)
        self._tray.setContextMenu(menu)
        self._tray.setToolTip("F.R.I.D.A.Y.")
        self._tray.activated.connect(lambda reason: self.show_hud() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self._tray.show()

    # ── slots (GUI thread) ────────────────────────────────────
    def _on_state(self, state: str) -> None:
        for reactor in (self.reactor, self.orb.reactor, self.titlebar.logo):
            reactor.set_state(state)
        self.state_label.setText(STATE_TEXT.get(state, state.upper()))
        self.state_label.setStyleSheet(f"color: {STATE_COLOR.get(state, CYAN)};")

    def _on_level(self, level: float) -> None:
        for reactor in (self.reactor, self.orb.reactor):
            reactor.set_level(level)

    def _on_status(self, text: str) -> None:
        self.status_label.setText(text)
        self.titlebar.status.setText(text[:60])

    def _on_led(self, name: str, state: str) -> None:
        if name in self.leds:
            self.leds[name].set_state(state)

    def _on_security(self, text: str) -> None:
        self.sec_label.setText(text)

    def _on_sec_button(self, armed: bool) -> None:
        self.btn_security.blockSignals(True)
        self.btn_security.setChecked(armed)
        self.btn_security.blockSignals(False)

    def _on_mute_toggled(self, muted: bool) -> None:
        self.btn_mute.setText("UNMUTE WAKE WORD" if muted else "MUTE WAKE WORD")
        self.mute_toggled.emit(muted)

    def _on_say(self, speaker: str, text: str) -> None:
        colour = ORANGE if speaker.upper() in {"YOU", "USER"} else CYAN
        self.transcript.append(
            f'<p style="margin:5px 0;"><span style="color:{colour};font-weight:bold;">{html.escape(speaker)}</span>'
            f'&nbsp;<span style="color:#e8f8ff;">{html.escape(text).replace(chr(10), "<br>")}</span></p>')
        self.transcript.verticalScrollBar().setValue(self.transcript.verticalScrollBar().maximum())

    def _on_log(self, level: str, text: str) -> None:
        colour = LOG_COLOR.get(level, TEXT)
        stamp = time.strftime("%H:%M:%S")
        self.terminal.appendHtml(
            f'<span style="color:{DIM};">{stamp}</span>&nbsp;'
            f'<span style="color:{colour};">{html.escape(level[:7]).ljust(7).replace(" ", "&nbsp;")}</span>&nbsp;'
            f'<span style="color:{TEXT};">{html.escape(text)}</span>')

    def _on_confirm(self, rid: int, message: str, future: object, timeout: int) -> None:
        self._confirm_queue.append((rid, message, future, timeout))  # type: ignore[arg-type]
        if self._dialog is None:
            self._show_next_confirmation()

    def _show_next_confirmation(self) -> None:
        if not self._confirm_queue:
            return
        rid, message, future, timeout = self._confirm_queue.pop(0)
        dlg = ConfirmDialog(message, future, timeout)  # type: ignore[arg-type]
        dlg.finished.connect(lambda _r, d=dlg: self._dialog_closed(d))
        self._dialog, self._dialog_id = dlg, rid
        geo = QApplication.primaryScreen().availableGeometry()
        dlg.adjustSize()
        dlg.move(geo.center().x() - dlg.width() // 2, geo.center().y() - dlg.height() // 2)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        dlg.btn_no.setFocus()
        if len(self._confirm_queue) >= 1:
            self.set_status(f"{len(self._confirm_queue)} more authorisation(s) waiting…")

    def _dialog_closed(self, dlg: ConfirmDialog) -> None:
        if self._dialog is dlg:
            self._dialog, self._dialog_id = None, None
        dlg.deleteLater()
        self._show_next_confirmation()

    def _on_dismiss(self, rid: int) -> None:
        if self._dialog_id == rid and self._dialog is not None:
            self._dialog.reject()  # -> _dialog_closed -> shows whatever's queued next
            return
        self._confirm_queue = [item for item in self._confirm_queue if item[0] != rid]

    def _submit(self) -> None:
        text = self.input.text().strip()
        if text:
            self.input.clear()
            self.text_submitted.emit(text)

    def _tick_telemetry(self) -> None:
        now = time.localtime()
        self.clock.setText(time.strftime("%H:%M:%S", now))
        self.date.setText(time.strftime("%A %d %B %Y", now).upper())
        if psutil:
            self.m_cpu.set_value(psutil.cpu_percent(interval=None))
            self.m_mem.set_value(psutil.virtual_memory().percent)
            try:
                self.m_disk.set_value(psutil.disk_usage(str(Path.home())).percent)
            except Exception:  # noqa: BLE001
                pass
            try:
                bat = psutil.sensors_battery()
                self.battery.setText(f"POWER  {bat.percent:.0f}% {'⚡ CHARGING' if bat.power_plugged else 'ON BATTERY'}" if bat else "POWER  MAINS")
            except Exception:  # noqa: BLE001
                pass


class QtLogHandler(logging.Handler):
    """Routes Python logging into the HUD's terminal panel."""

    def __init__(self, hud: StarkHUD):
        super().__init__(level=logging.INFO)
        self._hud = hud
        self.setFormatter(logging.Formatter("%(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._hud.log(record.levelname, self.format(record).replace("friday.", "", 1))
        except Exception:  # noqa: BLE001
            pass
