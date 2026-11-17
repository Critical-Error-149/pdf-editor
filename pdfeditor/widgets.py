"""Fingertaugliche Bedienelemente."""
from __future__ import annotations

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import (QInputDialog, QMenu, QStyle, QStyleOptionToolButton, QStylePainter,
                               QToolButton)

ARROW_W = 14  # Platz rechts für den gezeichneten Pfeil

class ValueButton(QToolButton):
    """Schaltfläche „Wert ⌄“ mit Auswahlliste statt winziger Pfeile eines Drehfelds. Der Pfeil wird gezeichnet:
    als Textzeichen (▾) fehlt er in der Programmschrift, und die Ersatzschrift kostete beim Start ~150 ms."""

    valueChanged = Signal(float)

    def __init__(self, values, suffix=" pt", tooltip="", decimals=1, minimum=0.25, maximum=200.0, parent=None):
        super().__init__(parent)
        self._value = float(values[0])
        self.suffix = suffix
        self.decimals = decimals
        self.minimum = minimum
        self.maximum = maximum
        self.setToolTip(tooltip)
        self.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self)
        for v in values:
            menu.addAction(self._fmt(v), lambda v=v: self.setValue(v, emit=True))
        menu.addSeparator()
        menu.addAction("Andere …", self._other)
        self.setMenu(menu)
        self.setMinimumWidth(78)
        self._update_text()

    def _fmt(self, v):
        txt = f"{v:.{self.decimals}f}".rstrip("0").rstrip(".") if self.decimals else f"{int(round(v))}"
        return txt.replace(".", ",") + self.suffix

    def _update_text(self):
        self.setText(self._fmt(self._value))

    def sizeHint(self):
        s = super().sizeHint()
        s.setWidth(s.width() + ARROW_W)
        return s

    def paintEvent(self, e):
        # Hintergrund/Rahmen vom Stil (ohne eigenen Text und Menüpfeil), Text links vom gezeichneten Pfeil
        p = QStylePainter(self)
        opt = QStyleOptionToolButton()
        self.initStyleOption(opt)
        opt.text = ""
        opt.features &= ~QStyleOptionToolButton.ToolButtonFeature.HasMenu
        p.drawComplexControl(QStyle.CC_ToolButton, opt)
        group = QPalette.Active if self.isEnabled() else QPalette.Disabled
        color = self.palette().color(group, QPalette.ButtonText)
        p.setPen(color)
        p.drawText(self.rect().adjusted(4, 0, -ARROW_W, 0), Qt.AlignCenter, self.text())
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(color, 1.4))
        x, y = self.width() - ARROW_W + 2, self.height() / 2
        path = QPainterPath(QPointF(x, y - 1.8))
        path.lineTo(QPointF(x + 3.5, y + 1.8))
        path.lineTo(QPointF(x + 7, y - 1.8))
        p.drawPath(path)

    def value(self):
        return self._value

    def setValue(self, v, emit=False):
        v = max(self.minimum, min(self.maximum, float(v)))
        changed = abs(v - self._value) > 1e-9
        self._value = v
        self._update_text()
        if emit and changed and not self.signalsBlocked():
            self.valueChanged.emit(v)

    def _other(self):
        v, ok = QInputDialog.getDouble(self, "Wert", self.toolTip() or "Wert:", self._value,
                                       self.minimum, self.maximum, self.decimals)
        if ok:
            self.setValue(v, emit=True)
