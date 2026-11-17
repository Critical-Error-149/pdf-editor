"""Fingerbedienung für Listen: wischen scrollt (mit Schwung), tippen = Klick,
lang antippen = Kontextmenü (öffnet beim Loslassen).

Wird als Ereignisfilter auf den Viewport einer QAbstractScrollArea gesetzt. Ersatz-
Mausereignisse, die Windows aus Fingerberührungen erzeugt, werden verworfen, damit sie
keine Markierungen oder Drag & Drop auslösen. Stift und Maus bleiben unverändert.
"""
from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QInputDevice

TOUCH_SLOP = 24        # so weit darf sich der Finger bewegen, bevor gescrollt wird
LONG_PRESS_MS = 450   # Voreinstellung; Datei → Einstellungen → Bedienung
_long_press = None


def long_press_ms() -> int:
    """Haltezeit für langes Drücken (Finger/Stift) in ms – einstellbar."""
    global _long_press
    if _long_press is None:
        from PySide6.QtCore import QSettings
        try:
            _long_press = int(QSettings("PDFEditor", "PDFEditor").value("input/long_press_ms", LONG_PRESS_MS))
        except (TypeError, ValueError):
            _long_press = LONG_PRESS_MS
    return _long_press


def set_long_press_ms(ms):
    global _long_press
    _long_press = int(ms)
FLING_MIN = 450        # Pixel/Sekunde, ab denen die Liste nachläuft

_MOUSE_EVENTS = (QEvent.MouseButtonPress, QEvent.MouseButtonRelease, QEvent.MouseMove,
                 QEvent.MouseButtonDblClick)
_TOUCH_EVENTS = (QEvent.TouchBegin, QEvent.TouchUpdate, QEvent.TouchEnd, QEvent.TouchCancel)


def from_touch(event) -> bool:
    """Von Windows/Qt aus einer Fingerberührung erzeugtes Ersatz-Mausereignis?"""
    dev = event.device()
    return dev is not None and dev.type() == QInputDevice.DeviceType.TouchScreen


class TouchScroller(QObject):
    def __init__(self, area, on_tap=None, on_long_press=None, on_hold=None):
        """area: QAbstractScrollArea. Rückrufe bekommen Viewport-Koordinaten (QPoint):
        on_tap(pos), on_long_press(pos) beim Loslassen nach langem Halten,
        on_hold(pos) sofort nach Ablauf der Haltezeit (z. B. Eintrag markieren)."""
        super().__init__(area)
        self.area = area
        self.on_tap = on_tap
        self.on_long_press = on_long_press
        self.on_hold = on_hold
        self.mode = None       # "pending", "scroll", "menu", "stop"
        self.start = None
        self.last = None
        self.time = 0.0
        self.velocity = 0.0
        self.kinetic = QTimer(self)
        self.kinetic.setInterval(16)
        self.kinetic.timeout.connect(self._kinetic_step)
        self.long_timer = QTimer(self)
        self.long_timer.setSingleShot(True)
        self.long_timer.setInterval(LONG_PRESS_MS)
        self.long_timer.timeout.connect(self._long_press)
        vp = area.viewport()
        vp.setAttribute(Qt.WA_AcceptTouchEvents, True)
        vp.installEventFilter(self)

    def is_active(self):
        return self.mode is not None or self.kinetic.isActive()

    # -------------------------------------------------------------------
    def eventFilter(self, obj, event):
        t = event.type()
        if t in _MOUSE_EVENTS and from_touch(event):
            return True  # Finger wird komplett über die Touch-Ereignisse behandelt
        if t not in _TOUCH_EVENTS:
            return False
        event.accept()
        pts = event.points()
        if not pts:
            return True
        pos = pts[0].position()
        now = time.monotonic()
        sb = self.area.verticalScrollBar()

        if t == QEvent.TouchBegin:
            # Läuft die Liste noch nach, stoppt Antippen sie nur; mehrere Finger: weder Tippen noch Menü
            self.mode = "stop" if self.kinetic.isActive() or len(pts) > 1 else "pending"
            self.kinetic.stop()
            self.start = self.last = pos
            self.velocity = 0.0
            self.time = now
            if self.mode == "pending":
                self.long_timer.start(long_press_ms())
            return True

        if self.start is None:
            return True

        if t == QEvent.TouchUpdate:
            if len(pts) > 1:
                self.long_timer.stop()
                if self.mode in ("pending", "menu"):
                    self.mode = "stop"
            moved = max(abs(pos.y() - self.start.y()), abs(pos.x() - self.start.x()))
            if self.mode in ("pending", "stop") and moved > TOUCH_SLOP:
                self.mode = "scroll"  # ab hier scrollen, ohne Sprung
                self.long_timer.stop()
                self.last = pos
            if self.mode == "scroll":
                dy = pos.y() - self.last.y()
                sb.setValue(sb.value() - round(dy))
                dt = max(now - self.time, 1e-3)
                self.velocity = self.velocity * 0.5 + (dy / dt) * 0.5
                self.time = now
                self.last = pos
            return True

        # TouchEnd / TouchCancel
        self.long_timer.stop()
        mode, start = self.mode, self.start
        self.mode = None
        self.start = self.last = None
        if t == QEvent.TouchCancel:
            return True
        p = start.toPoint()
        if mode == "menu" and self.on_long_press:
            QTimer.singleShot(0, lambda: self.on_long_press(p))  # erst nach dem Loslassen öffnen
        elif mode == "pending" and self.on_tap:
            self.on_tap(p)
        elif mode == "scroll":
            if now - self.time > 0.08:  # Finger stand vor dem Loslassen still
                self.velocity = 0.0
            if abs(self.velocity) > FLING_MIN:
                self.kinetic.start()
        return True

    def _long_press(self):
        if self.mode != "pending" or self.start is None:
            return
        self.mode = "menu"
        if self.on_hold:
            self.on_hold(self.start.toPoint())

    def _kinetic_step(self):
        self.velocity *= 0.93
        sb = self.area.verticalScrollBar()
        before = sb.value()
        sb.setValue(before - round(self.velocity * 0.016))
        if abs(self.velocity) < 20 or sb.value() == before:  # ausgelaufen oder am Rand
            self.kinetic.stop()

