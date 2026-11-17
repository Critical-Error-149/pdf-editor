"""Menüband im Aufbau wie bei OneNote: Registerkarten, darunter benannte Gruppen mit Knöpfen
(Symbol + Beschriftung daneben). Ist zu wenig Platz, werden die Beschriftungen ausgeblendet
und nur noch als Tooltip beim Überfahren gezeigt.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QStackedWidget,
                               QToolButton, QVBoxLayout, QWidget)

from . import theme

ICON = QSize(20, 20)
ROW_HEIGHT = 46


class RibbonButton(QToolButton):
    """Knopf im Menüband: Symbol mit Beschriftung daneben; im Kompaktmodus nur Symbol."""

    def __init__(self, action=None, parent=None, always_compact=False):
        super().__init__(parent)
        self.always_compact = always_compact
        if action is not None:
            self.setDefaultAction(action)
        self.setIconSize(ICON)
        self.setAutoRaise(True)
        self.set_compact(False)

    def set_compact(self, compact):
        icon_only = compact or self.always_compact or self.icon().isNull()
        if self.icon().isNull():
            self.setToolButtonStyle(Qt.ToolButtonTextOnly)
        else:
            self.setToolButtonStyle(Qt.ToolButtonIconOnly if icon_only else Qt.ToolButtonTextBesideIcon)


class _VLine(QWidget):
    """Senkrechte Haarlinie zwischen Gruppen."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(13)

    def paintEvent(self, _):
        p = QPainter(self)
        x = self.width() // 2
        theme.draw_hairline(p, QPointF(x, 6), QPointF(x, self.height() - 6))


class RibbonGroup(QWidget):
    """Gruppe mit Knöpfen und kleinem Titel darunter (z. B. „Zeichentools“)."""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 0)
        lay.setSpacing(0)
        # feste Zeilenhöhe, damit alle Gruppentitel auf einer Linie stehen (Stifte sind höher)
        row_host = QWidget(self)
        row_host.setFixedHeight(ROW_HEIGHT)
        self.row = QHBoxLayout(row_host)
        self.row.setSpacing(1)
        self.row.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(row_host)
        self.title = QLabel(title)
        self.title.setAlignment(Qt.AlignHCenter)
        f = self.title.font()
        f.setPointSizeF(f.pointSizeF() * 0.85)
        self.title.setFont(f)
        pal = self.title.palette()
        pal.setColor(pal.ColorRole.WindowText, theme.TEXT_DIM)
        self.title.setPalette(pal)
        lay.addWidget(self.title)

    def add_action(self, action, always_compact=False):
        b = RibbonButton(action, self, always_compact)
        self.row.addWidget(b, 0, Qt.AlignVCenter)
        return b

    def add_widget(self, w):
        self.row.addWidget(w, 0, Qt.AlignVCenter)
        return w

    def buttons(self):
        return self.findChildren(RibbonButton)


class RibbonPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.lay = QHBoxLayout(self)
        self.lay.setContentsMargins(6, 2, 6, 2)
        self.lay.setSpacing(0)
        self._groups = []
        self.compact = False

    def add_group(self, title):
        if self._groups:
            self.lay.addWidget(_VLine(self))
        g = RibbonGroup(title, self)
        self.lay.addWidget(g)
        self._groups.append(g)
        return g

    def finish(self):
        self.lay.addStretch(1)

    def set_compact(self, compact):
        self.compact = compact
        for b in self.findChildren(RibbonButton):
            b.set_compact(compact)

    def full_width(self, compact):
        """Benötigte Breite mit bzw. ohne Beschriftungen."""
        old = self.compact
        self.set_compact(compact)
        self._invalidate()
        w = self.sizeHint().width()
        self.set_compact(old)
        self._invalidate()
        return w

    def _invalidate(self):
        """Zwischengespeicherte Größen aller verschachtelten Layouts und Widgets verwerfen."""
        for w in self.findChildren(QWidget):
            if w.layout() is not None:
                w.layout().invalidate()
            w.updateGeometry()
        self.layout().invalidate()


class _TabButton(QToolButton):
    """Registerkarte: Text, aktive Karte mit Akzent-Unterstrich (wie OneNote)."""

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setAutoRaise(True)
        self.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.setMinimumHeight(32)

    def sizeHint(self):
        s = super().sizeHint()
        return QSize(s.width() + 12, 32)

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), theme.BG)
        r = QRectF(self.rect())
        hover = self.underMouse()
        if hover and not self.isChecked():
            theme.draw_box(p, r.adjusted(2, 3, -2, -3), theme.SURFACE, None, theme.RADIUS_S)
        f = self.font()
        f.setWeight(f.Weight.DemiBold if self.isChecked() else f.Weight.Normal)
        p.setFont(f)
        p.setPen(theme.TEXT if (self.isChecked() or hover) else theme.TEXT_DIM)
        p.drawText(r, Qt.AlignCenter, self.text().replace("&", ""))
        if self.isChecked():
            fm_w = p.fontMetrics().horizontalAdvance(self.text().replace("&", ""))
            p.fillRect(QRectF(r.center().x() - fm_w / 2, r.bottom() - 3, fm_w, 2), theme.ACCENT)


class Ribbon(QWidget):
    tab_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("ribbon", True)  # Knöpfe darin nur bei Hover/aktiv mit Fläche (Theme)
        self.setAutoFillBackground(True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 2, 6, 4)
        outer.setSpacing(0)
        # Zeile mit Registerkarten (+ rechts z. B. Suche)
        self.tab_row = QHBoxLayout()
        self.tab_row.setSpacing(2)
        self.tab_row.setContentsMargins(0, 0, 0, 0)
        self._left = QHBoxLayout()
        self._left.setSpacing(2)
        self.tab_row.addLayout(self._left)
        self._tabs = QHBoxLayout()
        self._tabs.setSpacing(2)
        self.tab_row.addLayout(self._tabs)
        self.tab_row.addStretch(1)
        self._right = QHBoxLayout()
        self._right.setSpacing(6)
        self.tab_row.addLayout(self._right)
        outer.addLayout(self.tab_row)
        # Seiten des Menübands (bei Platzmangel horizontal scrollbar)
        self.stack = QStackedWidget()
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.stack)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        outer.addWidget(self.scroll)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.group.idClicked.connect(self.set_current)
        self.pages = []
        self._relayout_timer = QTimer(self)
        self._relayout_timer.setSingleShot(True)
        self._relayout_timer.setInterval(0)
        self._relayout_timer.timeout.connect(self._apply_compact)

    # ------------------------------------------------------------ Aufbau
    def add_page(self, title):
        page = RibbonPage()
        idx = len(self.pages)
        self.pages.append(page)
        self.stack.addWidget(page)
        tab = _TabButton(title)
        self.group.addButton(tab, idx)
        self._tabs.addWidget(tab)
        if idx == 0:
            tab.setChecked(True)
        else:
            page.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        return page

    def add_left(self, w):
        self._left.addWidget(w)

    def add_right(self, w):
        self._right.addWidget(w)

    def set_current(self, idx):
        self.group.button(idx).setChecked(True)
        self.stack.setCurrentIndex(idx)
        # nur die sichtbare Karte bestimmt die Breite (sonst erzwingt die breiteste Karte einen Bildlauf)
        for i, p in enumerate(self.pages):
            pol = QSizePolicy.Preferred if i == idx else QSizePolicy.Ignored
            p.setSizePolicy(pol, pol)
        self.tab_changed.emit(idx)
        self.relayout()

    def current(self):
        return self.stack.currentIndex()

    # ------------------------------------------------------------ Beschriftungen ein/aus
    def relayout(self):
        self._relayout_timer.start()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._apply_compact()

    def _apply_compact(self):
        page = self.stack.currentWidget()
        if page is None:
            return
        avail = self.scroll.viewport().width() or self.width()
        compact = page.full_width(False) > avail
        if compact != page.compact:
            page.set_compact(compact)
        h = max(p.sizeHint().height() for p in self.pages) if self.pages else 70
        sb = self.scroll.horizontalScrollBar()
        extra = sb.sizeHint().height() if page.full_width(compact) > avail else 0
        self.scroll.setFixedHeight(h + extra)

    def is_compact(self):
        page = self.stack.currentWidget()
        return bool(page and page.compact)

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), theme.BG)
        theme.draw_hairline(p, QPointF(0, self.height() - 1), QPointF(self.width(), self.height() - 1))
