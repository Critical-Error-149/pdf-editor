"""Eigene Stifte und Marker (Farbe + Dicke), wie die Stiftauswahl in OneNote."""
from __future__ import annotations

import json

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QColorDialog, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFormLayout, QGridLayout, QLabel, QPushButton, QToolButton,
                               QVBoxLayout, QWidget)

KINDS = {"pen": "Stift", "highlighter": "Marker"}

DEFAULT_PRESETS = [
    {"kind": "pen", "color": "#000000", "width": 1.0},
    {"kind": "pen", "color": "#1f4fd1", "width": 1.5},
    {"kind": "pen", "color": "#d92b2b", "width": 1.5},
    {"kind": "pen", "color": "#1f9d3a", "width": 3.0},
    {"kind": "highlighter", "color": "#f2c200", "width": 12.0},
    {"kind": "highlighter", "color": "#4cd964", "width": 12.0},
]

PALETTE = ["#000000", "#5f6368", "#1f4fd1", "#2fa4e7", "#d92b2b", "#e84393", "#1f9d3a", "#7ac943",
           "#f2c200", "#ff7a00", "#8e44ad", "#8b5a2b", "#ffffff"]


def load_presets(qsettings):
    raw = qsettings.value("presets")
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [p for p in data if p.get("kind") in KINDS]
        except ValueError:
            pass
    return [dict(p) for p in DEFAULT_PRESETS]


def save_presets(qsettings, presets):
    qsettings.setValue("presets", json.dumps(presets))


PEN_ICON = QSize(22, 36)  # hochkant wie bei OneNote


def _pen_pixmap(preset, dpr):
    """Stift bzw. Marker als Silhouette: Farbe an Kappe und Spitze, Dicke als Breite der Spitze."""
    W, H = PEN_ICON.width(), PEN_ICON.height()
    pm = QPixmap(int(W * dpr), int(H * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    color = QColor(preset["color"])
    body = QColor("#d6d8de")
    outline = QPen(QColor("#5c6068"), 1)
    if preset["kind"] == "highlighter":
        p.setPen(outline)
        p.setBrush(body)
        p.drawRoundedRect(QRectF(4.5, 1.5, 13, 19), 2.5, 2.5)
        p.setBrush(color)
        p.drawRoundedRect(QRectF(4.5, 1.5, 13, 6), 2.5, 2.5)
        tip = QPainterPath(QPointF(6, 20.5))
        for pt in ((16, 20.5), (15, 28), (12.5, 33), (8, 29.5), (6, 20.5)):
            tip.lineTo(QPointF(*pt))
        p.setBrush(body)
        p.drawPath(tip)
        nib = QPainterPath(QPointF(8.2, 27))
        for pt in ((14.8, 27), (12.5, 33), (8, 29.5), (8.2, 27)):
            nib.lineTo(QPointF(*pt))
        p.setBrush(color)
        p.drawPath(nib)
    else:
        p.setPen(outline)
        p.setBrush(body)
        p.drawRoundedRect(QRectF(6, 1.5, 10, 20), 2, 2)
        p.setBrush(color)
        p.drawRoundedRect(QRectF(6, 1.5, 10, 6), 2, 2)
        tip = QPainterPath(QPointF(6, 21.5))
        for pt in ((16, 21.5), (12, 33.5), (10, 33.5), (6, 21.5)):
            tip.lineTo(QPointF(*pt))
        p.setBrush(body)
        p.drawPath(tip)
        # Spitze: je dicker der Stift, desto breiter die farbige Spitze
        half = max(1.2, min(3.4, 0.9 + preset["width"] * 0.55))
        nib = QPainterPath(QPointF(11 - half, 30.5 - half * 1.6))
        for pt in ((11 + half, 30.5 - half * 1.6), (11.6, 34), (10.4, 34)):
            nib.lineTo(QPointF(*pt))
        nib.closeSubpath()
        p.setBrush(color)
        p.drawPath(nib)
    p.end()
    return pm


def preset_icon(preset, size=None) -> QIcon:
    ic = QIcon()
    for dpr in (1.0, 2.0):
        ic.addPixmap(_pen_pixmap(preset, dpr))
    return ic


def preset_tooltip(preset):
    return f"{KINDS[preset['kind']]} · {preset['color']} · {preset['width']:g} pt"


def color_icon(hexc, size=20):
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(hexc))
    p.setPen(QColor("#888888"))
    p.drawEllipse(1, 1, size - 2, size - 2)
    p.end()
    return QIcon(pm)


def text_color_icon(hexc, size=22):
    """Schriftfarbe wie in Office: großes „A“ mit farbigem Balken darunter."""
    from PySide6.QtGui import QFont
    dpr = 2.0
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    f = QFont("Segoe UI")
    f.setPixelSize(int(size * 0.78))
    f.setBold(True)
    p.setFont(f)
    from . import theme
    p.setPen(theme.TEXT)
    p.drawText(0, -2, size, size - 4, Qt.AlignHCenter | Qt.AlignVCenter, "A")
    p.setPen(QColor("#888888"))
    p.setBrush(QColor(hexc))
    p.drawRect(2, size - 6, size - 4, 5)
    p.end()
    return QIcon(pm)


class ColorGrid(QWidget):
    """Farbfelder + 'Weitere Farben …'. Ruft on_pick(hex) auf."""

    def __init__(self, on_pick, parent=None):
        super().__init__(parent)
        grid = QGridLayout(self)
        grid.setContentsMargins(6, 6, 6, 6)
        grid.setSpacing(4)
        for i, c in enumerate(PALETTE):
            b = QToolButton()
            b.setIcon(color_icon(c, 24))
            b.setIconSize(QSize(24, 24))
            b.setAutoRaise(True)
            b.setToolTip(c)
            b.clicked.connect(lambda _=False, c=c: on_pick(c))
            grid.addWidget(b, i // 7, i % 7)
        more = QPushButton("Weitere Farben …")
        more.clicked.connect(lambda: self._more(on_pick))
        grid.addWidget(more, 2, 0, 1, 7)

    def _more(self, on_pick):
        c = QColorDialog.getColor(QColor("#000000"), self, "Farbe wählen")
        if c.isValid():
            on_pick(c.name())


class PresetDialog(QDialog):
    def __init__(self, parent, preset=None):
        super().__init__(parent)
        self.setWindowTitle("Stift bearbeiten" if preset else "Neuer Stift")
        self.preset = dict(preset or {"kind": "pen", "color": "#1f4fd1", "width": 1.5})
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.kind = QComboBox()
        for k, label in KINDS.items():
            self.kind.addItem(label, k)
        self.kind.setCurrentIndex(list(KINDS).index(self.preset["kind"]))
        form.addRow("Art:", self.kind)
        self.width = QDoubleSpinBox()
        self.width.setRange(0.25, 60)
        self.width.setSingleStep(0.5)
        self.width.setSuffix(" pt")
        self.width.setValue(self.preset["width"])
        form.addRow("Dicke:", self.width)
        lay.addLayout(form)
        lay.addWidget(QLabel("Farbe:"))
        lay.addWidget(ColorGrid(self._set_color))
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.preview)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.kind.currentIndexChanged.connect(self._refresh)
        self.width.valueChanged.connect(self._refresh)
        self._refresh()

    def _set_color(self, c):
        self.preset["color"] = c
        self._refresh()

    def _refresh(self):
        self.preset["kind"] = self.kind.currentData()
        self.preset["width"] = self.width.value()
        self.preview.setPixmap(preset_icon(self.preset, 48).pixmap(48, 48))

    def result_preset(self):
        return dict(self.preset)
