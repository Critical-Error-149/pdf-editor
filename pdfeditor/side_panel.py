"""Einklappbare Seitenleisten und ein Splitter mit fingertauglichen Griffen.

Eingeklappt verschwindet eine Leiste vollständig; übrig bleibt nur ein kleiner Knopf in der
Tableiste (links neben den Tabs), der sie wieder einblendet.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPainter
from PySide6.QtWidgets import (QAbstractButton, QHBoxLayout, QSplitter, QSplitterHandle, QToolButton, QVBoxLayout,
                               QWidget)

from . import icons, theme

HANDLE_WIDTH = 12        # Breite des (unsichtbaren) Griffstreifens – ganze Höhe ist ziehbar
GRIP_LENGTH = 40         # sichtbare Griffmarke in der Mitte
GRIP_THICKNESS = 6
EXPAND_SIZE = QSize(34, 30)     # Knopf zum Einblenden (in der Tableiste)
HEADER_HEIGHT = 40              # Titelzeile der Leiste


class GripHandle(QSplitterHandle):
    """Griff: ein kleines abgerundetes Rechteck in der Mitte; gezogen werden kann überall."""

    def sizeHint(self):
        s = super().sizeHint()
        if self.orientation() == Qt.Vertical:  # Teilung untereinander: waagerechter Griff
            return QSize(s.width(), HANDLE_WIDTH)
        return QSize(HANDLE_WIDTH, s.height())

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), theme.BG)
        hover = self.underMouse()
        line = theme.ACCENT_BORDER if hover else theme.BORDER
        fill = theme.ACCENT if hover else theme.SURFACE_HOVER
        edge = theme.ACCENT if hover else theme.BORDER_HOVER
        # Haarlinie über die ganze Länge, in der Mitte eine kleine Griffmarke (Akzent beim Überfahren)
        if self.orientation() == Qt.Vertical:
            y = int(self.height() / 2)
            theme.draw_hairline(p, QPointF(0, y), QPointF(self.width(), y), line)
            r = QRectF((self.width() - GRIP_LENGTH) / 2, y - GRIP_THICKNESS / 2 + 0.5, GRIP_LENGTH, GRIP_THICKNESS)
        else:
            x = int(self.width() / 2)
            theme.draw_hairline(p, QPointF(x, 0), QPointF(x, self.height()), line)
            r = QRectF(x - GRIP_THICKNESS / 2 + 0.5, (self.height() - GRIP_LENGTH) / 2, GRIP_THICKNESS, GRIP_LENGTH)
        theme.draw_box(p, r, fill, edge, GRIP_THICKNESS / 2)

    def enterEvent(self, e):
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.update()
        super().leaveEvent(e)


class GripSplitter(QSplitter):
    def __init__(self, *a):
        super().__init__(*a)
        self.setHandleWidth(HANDLE_WIDTH)
        self.setChildrenCollapsible(False)

    def createHandle(self):
        return GripHandle(self.orientation(), self)


class PanelHeader(QAbstractButton):
    """Kopfzeile einer Seitenleiste: ‹ Symbol Titel – ein Klick irgendwo darauf blendet die Leiste aus."""

    def __init__(self, icon: QIcon, title: str, parent=None):
        super().__init__(parent)
        self._icon = icon
        self.setText(title)
        self.setToolTip(f"{title} ausblenden")
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)

    def sizeHint(self):
        return QSize(120, HEADER_HEIGHT)

    def minimumSizeHint(self):
        return QSize(56, HEADER_HEIGHT)  # Pfeil + Symbol; die Beschriftung entfällt, wenn sie nicht passt

    def enterEvent(self, e):
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(4, 4, -4, -2)
        hover = self.underMouse()
        if self.isDown():
            theme.draw_box(p, r, theme.SURFACE, theme.BORDER_HOVER, theme.RADIUS_S)
        elif hover:
            theme.draw_box(p, r, theme.SURFACE_HOVER, theme.BORDER, theme.RADIUS_S)
        x = r.left() + 6
        cy = r.center().y()
        chevron = icons.colored("prev", theme.TEXT if hover else theme.TEXT_DIM, 16)
        p.drawPixmap(QPointF(x, cy - 8), chevron.pixmap(QSize(16, 16), self.devicePixelRatioF()))
        x += 20
        p.drawPixmap(QPointF(x, cy - 9), self._icon.pixmap(QSize(18, 18), self.devicePixelRatioF()))
        x += 26
        f = self.font()
        f.setBold(True)
        p.setFont(f)
        space = r.right() - x - 4
        if p.fontMetrics().horizontalAdvance(self.text()) <= space:  # zu schmal: nur Pfeil und Symbol
            p.setPen(theme.TEXT)
            p.drawText(QRectF(x, r.top(), space, r.height()), Qt.AlignVCenter | Qt.AlignLeft, self.text())


class CollapsiblePanel(QWidget):
    """Seitenleiste mit klickbarer Titelzeile (‹ Dateien) zum Ausblenden. Eingeklappt verschwindet sie
    ganz; zum Einblenden dient `expand_button` (sitzt in der Tableiste, siehe MainWindow)."""

    collapsed_changed = Signal(bool)
    visibility_changed = Signal()  # eingeklappt/ausgeklappt oder im Vollbild verborgen

    def __init__(self, icon: QIcon, title: str, content: QWidget, default_width=240, shortcut="", parent=None):
        super().__init__(parent)
        self.setObjectName("sidepanel")
        self.setAutoFillBackground(True)
        self.content = content
        self.collapsed = False
        self.suppressed = False  # Vollbild: weder Leiste noch Knopf anzeigen
        self._expanded_width = default_width  # Breite beim Aufklappen
        # Knopf zum Einblenden (nur sichtbar, solange die Leiste eingeklappt ist)
        b = QToolButton()
        b.setIcon(icon)
        b.setIconSize(QSize(20, 20))
        b.setAutoRaise(True)
        b.setFixedSize(EXPAND_SIZE)
        b.setToolTip(f"{title} einblenden" + (f" ({shortcut})" if shortcut else ""))
        b.clicked.connect(lambda: self.set_collapsed(False))
        b.hide()
        self.expand_button = b

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.header = PanelHeader(icon, title)
        self.header.clicked.connect(lambda: self.set_collapsed(True))
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(0)
        # Platz links der Titelzeile (z. B. Knopf der eingeklappten Leiste links von dieser)
        self.lead = QHBoxLayout()
        self.lead.setContentsMargins(4, 4, 0, 2)
        self.lead.setSpacing(2)
        top.addLayout(self.lead)
        top.addWidget(self.header, 1)
        lay.addLayout(top)
        lay.addWidget(content, 1)

    def set_collapsed(self, collapsed: bool):
        if collapsed == self.collapsed:
            return
        splitter = self.parentWidget() if isinstance(self.parentWidget(), QSplitter) else None
        if collapsed and self.isVisible() and self.width() > 80:
            self._expanded_width = self.width()
        self.collapsed = collapsed
        self._apply_visibility()
        if not collapsed and splitter is not None and self._expanded_width:
            i = splitter.indexOf(self)
            sizes = splitter.sizes()
            delta = self._expanded_width - sizes[i]
            sizes[i] = self._expanded_width
            sizes[-1] = max(100, sizes[-1] - delta)  # Ausgleich über das Dokument
            splitter.setSizes(sizes)
        self.collapsed_changed.emit(collapsed)

    def set_suppressed(self, suppressed: bool):
        """Vollbildmodus: Leiste und Aufklapp-Kästchen ganz verbergen."""
        self.suppressed = suppressed
        self._apply_visibility()

    def _apply_visibility(self):
        self.setVisible(not self.collapsed and not self.suppressed)
        self.expand_button.setVisible(self.collapsed and not self.suppressed)
        self.visibility_changed.emit()
