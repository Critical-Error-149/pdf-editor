"""Mehrere Dokumente in Tabs, Tab-Gruppen nebeneinander/untereinander teilbar.

  DocTab     – ein offenes Dokument: PdfDocument + PageView + Miniaturen + Autosave
  TabGroup   – Tableiste + Stapel der Ansichten (eine sichtbar)
  EditorArea – verwaltet die Gruppen in verschachtelten Splittern (wie Editor-Gruppen in VS Code)
"""
from __future__ import annotations

import os

from PySide6.QtCore import QMimeData, QObject, QPoint, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QDrag, QPainter
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QMenu, QSplitter, QStackedWidget, QTabBar, QToolButton,
                               QVBoxLayout, QWidget)

from . import icons, theme
from .side_panel import GripSplitter

TAB_HEIGHT = 34
TAB_MIME = "application/x-pdfeditor-tab"  # Tab wird gezogen (Daten: nur Kennung, der Tab steht in EditorArea)


class DocTab(QObject):
    """Ein geöffnetes Dokument mit allem, was dazugehört."""

    def __init__(self, document, view, thumbs, autosave, parent=None):
        super().__init__(parent)
        self.document = document
        self.view = view
        self.thumbs = thumbs
        self.autosave = autosave
        self.group: TabGroup | None = None

    def title(self):
        return os.path.basename(self.document.path) if self.document.path else "Unbenannt"

    def is_pristine(self):
        """Leeres, unverändertes neues Dokument (wird beim Öffnen einer Datei ersetzt)."""
        d = self.document
        return d.path is None and not d.is_modified() and not d.can_undo() and d.page_count <= 1


class TabBar(QTabBar):
    """Tableiste im Stil des Programms: aktiver Tab mit Akzentstrich, wenn seine Gruppe aktiv ist."""

    def __init__(self, group, parent=None):
        super().__init__(parent)
        self.group = group
        self.setDocumentMode(True)
        self.setExpanding(False)
        self.setMovable(False)  # eigenes Ziehen: auch in andere Gruppen, mit Einfügemarke
        self.setAcceptDrops(True)
        self._press = None      # (Position, Tab-Index) beim Drücken
        self.drop_index = None  # Einfügemarke während ein Tab darüber gezogen wird
        self.setElideMode(Qt.ElideMiddle)
        self.setUsesScrollButtons(True)
        self.setDrawBase(False)
        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self._hover = -1

    def tabSizeHint(self, index):
        fm = self.fontMetrics()
        w = fm.horizontalAdvance(self.tabText(index)) + 24 + 26  # Rand + Schließen-Knopf
        return QSize(max(110, min(w, 240)), TAB_HEIGHT)

    def minimumTabSizeHint(self, index):
        return QSize(90, TAB_HEIGHT)

    def mouseMoveEvent(self, e):
        pos = e.position().toPoint()
        if self._press is not None and e.buttons() & Qt.LeftButton:
            start, index = self._press
            if (pos - start).manhattanLength() >= QApplication.startDragDistance() and 0 <= index < self.count():
                self._press = None
                self._start_drag(index)
                return
        i = self.tabAt(pos)
        if i != self._hover:
            self._hover = i
            self.update()
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._press = None
        super().mouseReleaseEvent(e)

    def _start_drag(self, index):
        tab = self.group.tabs[index]
        pix = self.grab(self.tabRect(index))
        self.group.area.start_drag(tab, self, pix)

    # ---- Ablegen
    def insert_index_at(self, x):
        for i in range(self.count()):
            if x < self.tabRect(i).center().x():
                return i
        return self.count()

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(TAB_MIME):
            e.acceptProposedAction()
            self._set_drop(self.insert_index_at(e.position().x()))
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if e.mimeData().hasFormat(TAB_MIME):
            e.acceptProposedAction()
            self._set_drop(self.insert_index_at(e.position().x()))

    def dragLeaveEvent(self, e):
        self._set_drop(None)

    def dropEvent(self, e):
        index = self.insert_index_at(e.position().x())
        self._set_drop(None)
        if e.mimeData().hasFormat(TAB_MIME):
            e.acceptProposedAction()
            self.group.area.drop_tab(self.group, index)

    def _set_drop(self, index):
        if index != self.drop_index:
            self.drop_index = index
            self.update()

    def leaveEvent(self, e):
        self._hover = -1
        self.update()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        i = self.tabAt(e.position().toPoint())
        if e.button() == Qt.MiddleButton:  # Mittelklick schließt (wie im Browser)
            if i >= 0:
                self.tabCloseRequested.emit(i)
                return
        if e.button() == Qt.LeftButton:
            self._press = (e.position().toPoint(), i)
        super().mousePressEvent(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), theme.BG)
        active_group = self.group.area is not None and self.group.area.current_group() is self.group
        for i in range(self.count()):
            r = QRectF(self.tabRect(i)).adjusted(2, 4, -2, 0)
            selected = i == self.currentIndex()
            if selected:
                theme.draw_box(p, r.adjusted(0, 0, 0, theme.RADIUS_S), theme.SURFACE, theme.BORDER, theme.RADIUS_S)
            elif i == self._hover:
                theme.draw_box(p, r.adjusted(0, 2, 0, -2), theme.SURFACE_HOVER, None, theme.RADIUS_S)
            # Text (rechts Platz für den Schließen-Knopf)
            text_r = r.adjusted(12, 0, -28, 0)
            f = self.font()
            f.setWeight(f.Weight.DemiBold if selected else f.Weight.Normal)
            p.setFont(f)
            p.setPen(theme.TEXT if selected or i == self._hover else theme.TEXT_DIM)
            text = p.fontMetrics().elidedText(self.tabText(i), Qt.ElideMiddle, int(text_r.width()))
            p.drawText(text_r, Qt.AlignVCenter | Qt.AlignLeft, text)
            if selected and active_group:  # Akzentstrich: aktiver Tab der aktiven Gruppe
                p.fillRect(QRectF(r.left() + 8, r.top(), r.width() - 16, 2), theme.ACCENT)
        if self.drop_index is not None:  # Einfügemarke beim Ziehen eines Tabs
            if self.count() == 0:
                x = 4
            elif self.drop_index < self.count():
                x = self.tabRect(self.drop_index).left() + 1
            else:
                x = self.tabRect(self.count() - 1).right() - 1
            p.fillRect(QRectF(x - 1, 5, 3, self.height() - 8), theme.ACCENT_BRIGHT)
        theme.draw_hairline(p, QPointF(0, self.height() - 0.5), QPointF(self.width(), self.height() - 0.5))


class TabGroup(QWidget):
    """Tableiste + gestapelte Ansichten."""

    def __init__(self, area, parent=None):
        super().__init__(parent)
        self.area = area
        self.tabs: list[DocTab] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.header = QWidget()
        self.header.setAutoFillBackground(True)
        head = QHBoxLayout(self.header)
        head.setContentsMargins(4, 0, 4, 0)
        head.setSpacing(2)
        # Platz links der Tabs (Knöpfe zum Einblenden der Seitenleisten, nur in der Gruppe oben links)
        self.lead = QHBoxLayout()
        self.lead.setContentsMargins(0, 0, 4, 0)
        self.lead.setSpacing(2)
        head.addLayout(self.lead)
        self.bar = TabBar(self)
        head.addWidget(self.bar, 1)
        self.add_btn = QToolButton()
        self.add_btn.setIcon(icons.icon("plus", 18))
        self.add_btn.setIconSize(QSize(18, 18))
        self.add_btn.setAutoRaise(True)
        self.add_btn.setToolTip("Dokument öffnen (Strg+O)")
        self.add_btn.clicked.connect(lambda: (area.activate_group(self), area.open_requested.emit()))
        head.addWidget(self.add_btn)
        lay.addWidget(self.header)
        self.stack = QStackedWidget()
        lay.addWidget(self.stack, 1)
        self.setAcceptDrops(True)  # Tab auf die Ansicht dieser Gruppe ziehen -> hierher verschieben
        self.bar.currentChanged.connect(self._bar_changed)
        self.bar.tabMoved.connect(self._bar_moved)
        self.bar.tabCloseRequested.connect(lambda i: area.close_requested.emit(self.tabs[i]))
        self.bar.tabBarClicked.connect(lambda _i: area.activate_group(self))
        self.bar.customContextMenuRequested.connect(self._context_menu)

    # ---- Tabs
    def add(self, tab: DocTab, index=None):
        index = len(self.tabs) if index is None else index
        tab.group = self
        self.tabs.insert(index, tab)
        self.stack.addWidget(tab.view)
        self.bar.blockSignals(True)
        self.bar.insertTab(index, tab.title())
        self._add_close_button(index, tab)
        self.bar.blockSignals(False)

    def _add_close_button(self, index, tab):
        b = QToolButton(self.bar)
        b.setIcon(icons.icon("close", 14))
        b.setIconSize(QSize(14, 14))
        b.setAutoRaise(True)
        b.setFixedSize(22, 22)
        b.setToolTip("Schließen (Strg+W)")
        b.clicked.connect(lambda: self.area.close_requested.emit(tab))
        self.bar.setTabButton(index, QTabBar.RightSide, b)

    def reorder(self, tab: DocTab, index):
        """Tab innerhalb der Gruppe an die Einfügestelle `index` (vor dem Verschieben gezählt) setzen."""
        i = self.tabs.index(tab)
        if index > i:
            index -= 1
        index = max(0, min(index, len(self.tabs) - 1))
        if index != i:
            self.bar.moveTab(i, index)  # löst tabMoved aus -> self.tabs wird mitgeführt

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(TAB_MIME):
            e.acceptProposedAction()
            self.bar._set_drop(self.bar.count())
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if e.mimeData().hasFormat(TAB_MIME):
            e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self.bar._set_drop(None)

    def dropEvent(self, e):
        self.bar._set_drop(None)
        if e.mimeData().hasFormat(TAB_MIME):
            e.acceptProposedAction()
            self.area.drop_tab(self, None)

    def remove(self, tab: DocTab):
        i = self.tabs.index(tab)
        self.bar.blockSignals(True)
        self.bar.removeTab(i)
        self.bar.blockSignals(False)
        self.tabs.pop(i)
        self.stack.removeWidget(tab.view)
        tab.group = None
        if self.tabs:
            j = min(i, len(self.tabs) - 1)
            self.bar.setCurrentIndex(j)
            self.stack.setCurrentWidget(self.tabs[j].view)

    def current(self) -> DocTab | None:
        i = self.bar.currentIndex()
        return self.tabs[i] if 0 <= i < len(self.tabs) else None

    def set_current(self, tab: DocTab):
        i = self.tabs.index(tab)
        if self.bar.currentIndex() != i:
            self.bar.setCurrentIndex(i)
        self.stack.setCurrentWidget(tab.view)

    def set_title(self, tab, text, tip):
        i = self.tabs.index(tab)
        self.bar.setTabText(i, text)
        self.bar.setTabToolTip(i, tip)

    def _bar_changed(self, i):
        if 0 <= i < len(self.tabs):
            self.stack.setCurrentWidget(self.tabs[i].view)
            self.area.activate(self.tabs[i])

    def _bar_moved(self, frm, to):
        self.tabs.insert(to, self.tabs.pop(frm))
        self.area.layout_changed.emit()  # z. B. Nummern gleicher Dokumente neu vergeben

    def _context_menu(self, pos):
        i = self.bar.tabAt(pos)
        if i < 0:
            return
        tab = self.tabs[i]
        area = self.area
        m = QMenu(self)
        can_split = len(self.tabs) > 1
        a = m.addAction(icons.icon("split_right"), "Nebeneinander teilen", lambda: area.split(tab, Qt.Horizontal))
        a.setEnabled(can_split)
        a = m.addAction(icons.icon("split_down"), "Untereinander teilen", lambda: area.split(tab, Qt.Vertical))
        a.setEnabled(can_split)
        others = [g for g in area.groups() if g is not self]
        if others:
            mv = m.addMenu("In andere Gruppe verschieben")
            for n, g in enumerate(area.groups(), 1):
                if g is not self:
                    cur = g.current()
                    label = f"Gruppe {n}" + (f" ({cur.title()})" if cur else "")
                    mv.addAction(label, lambda g=g: area.move_tab(tab, g))
        m.addSeparator()
        m.addAction(icons.icon("duplicate"), "Duplizieren (weiterer Tab)", lambda: area.duplicate_requested.emit(tab))
        a = m.addAction("Umbenennen …", lambda: area.rename_requested.emit(tab))
        a.setEnabled(bool(tab.document.path))
        m.addSeparator()
        m.addAction("Schließen", lambda: area.close_requested.emit(tab))
        others_here = [t for t in self.tabs if t is not tab]
        a = m.addAction("Andere Tabs schließen", lambda: [area.close_requested.emit(t) for t in others_here])
        a.setEnabled(bool(others_here))
        m.exec(self.bar.mapToGlobal(pos))


class EditorArea(QWidget):
    """Alle Tab-Gruppen; Teilen erzeugt verschachtelte Splitter."""

    current_changed = Signal(object)    # DocTab
    close_requested = Signal(object)    # DocTab
    duplicate_requested = Signal(object)  # DocTab: dasselbe Dokument in einem weiteren Tab
    rename_requested = Signal(object)   # DocTab: Datei umbenennen
    open_requested = Signal()
    layout_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.root = self._splitter(Qt.Horizontal)
        lay.addWidget(self.root)
        self._current_group = TabGroup(self)
        self.root.addWidget(self._current_group)
        self._current_tab: DocTab | None = None
        self._chrome = True
        self._leading: list[QWidget] = []
        self._drag_tab: DocTab | None = None

    def _splitter(self, orientation):
        s = GripSplitter(orientation)
        s.setChildrenCollapsible(False)
        return s

    # ---- Abfragen
    def groups(self) -> list[TabGroup]:
        """Alle Gruppen in Lesereihenfolge (links/oben zuerst)."""
        out = []

        def walk(w):
            if isinstance(w, TabGroup):
                out.append(w)
            elif isinstance(w, QSplitter):
                for i in range(w.count()):
                    walk(w.widget(i))
        walk(self.root)
        return out

    def tabs(self) -> list[DocTab]:
        return [t for g in self.groups() for t in g.tabs]

    def current_group(self) -> TabGroup:
        return self._current_group

    def current_tab(self) -> DocTab | None:
        return self._current_tab

    def first_group(self) -> TabGroup:
        return self.groups()[0]

    def can_split(self):
        return len(self._current_group.tabs) > 1

    def is_split(self):
        return len(self.groups()) > 1

    # ---- Tabs
    def add_tab(self, tab: DocTab, group: TabGroup | None = None, index=None):
        group = group or self._current_group
        group.add(tab, index)
        self._apply_chrome(group)
        self.activate(tab)

    # ---- Knöpfe links der Tabs (immer in der ersten Gruppe)
    def set_leading_widgets(self, widgets):
        self._leading = list(widgets)
        self._place_leading()

    def _place_leading(self):
        if not self._leading:
            return
        target = self.groups()[0]
        for i, w in enumerate(self._leading):  # in der angegebenen Reihenfolge ganz links
            if w.parentWidget() is target.header and target.lead.indexOf(w) == i:
                continue
            # Umhängen blendet aus -> Sichtbarkeit merken
            hidden = getattr(w, "_parked_hidden", None)
            hidden = w.isHidden() if hidden is None else hidden
            w._parked_hidden = None
            target.lead.removeWidget(w)
            target.lead.insertWidget(i, w)
            w.setVisible(not hidden)

    def _structure_changed(self):
        self._place_leading()
        self.layout_changed.emit()

    def remove_tab(self, tab: DocTab):
        group = tab.group
        was_current = tab is self._current_tab
        group.remove(tab)
        if not group.tabs and len(self.groups()) > 1:
            self._remove_group(group)
        if was_current:
            self._current_tab = None
            g = group if group.tabs else self.groups()[0]
            nxt = g.current()
            if nxt is not None:
                self.activate(nxt)
        self._structure_changed()

    def activate(self, tab: DocTab):
        group = tab.group
        group.set_current(tab)
        changed_group = group is not self._current_group
        self._current_group = group
        if changed_group:
            for g in self.groups():
                g.bar.update()  # Akzentstrich wandert mit
        if tab is not self._current_tab:
            self._current_tab = tab
            self.current_changed.emit(tab)

    def activate_group(self, group: TabGroup):
        cur = group.current()
        if cur is not None:
            self.activate(cur)

    def set_title(self, tab: DocTab, text, tip):
        if tab.group is not None:
            tab.group.set_title(tab, text, tip)

    def cycle(self, step):
        tabs = self.tabs()
        if len(tabs) > 1 and self._current_tab in tabs:
            self.activate(tabs[(tabs.index(self._current_tab) + step) % len(tabs)])

    # ---- Teilen
    def split(self, tab: DocTab | None = None, orientation=Qt.Horizontal):
        """Tab in eine neue Gruppe rechts (Horizontal) bzw. unterhalb (Vertical) seiner Gruppe verschieben."""
        tab = tab or self._current_tab
        if tab is None or len(tab.group.tabs) < 2:
            return
        old = tab.group
        parent: QSplitter = old.parentWidget()
        idx = parent.indexOf(old)
        new = TabGroup(self)
        if parent.count() == 1 or parent.orientation() == orientation:
            parent.setOrientation(orientation)
            sizes = parent.sizes()
            parent.insertWidget(idx + 1, new)
            half = sizes[idx] // 2  # bisherige Gruppe teilt sich ihren Platz mit der neuen
            sizes[idx] -= half
            sizes.insert(idx + 1, half)
            parent.setSizes(sizes)
        else:
            sub = self._splitter(orientation)
            parent.replaceWidget(idx, sub)
            sub.addWidget(old)
            sub.addWidget(new)
            sub.setSizes([1, 1])
        self.move_tab(tab, new)

    # ---- Ziehen und Ablegen
    def start_drag(self, tab: DocTab, source: QWidget, pixmap):
        self._drag_tab = tab
        mime = QMimeData()
        mime.setData(TAB_MIME, b"tab")
        drag = QDrag(source)
        drag.setMimeData(mime)
        drag.setPixmap(pixmap)
        drag.setHotSpot(QPoint(pixmap.width() // 2, pixmap.height() // 2))
        try:
            drag.exec(Qt.MoveAction)
        finally:
            self._drag_tab = None
            for g in self.groups():
                g.bar._set_drop(None)

    def drop_tab(self, group: TabGroup, index=None):
        """Gezogenen Tab in `group` an Einfügestelle `index` ablegen (None = ans Ende)."""
        tab = self._drag_tab
        if tab is None or tab.group is None:
            return
        if tab.group is group:
            if index is not None:
                group.reorder(tab, index)
            self.activate(tab)
        else:
            self.move_tab(tab, group, index)

    def move_tab(self, tab: DocTab, target: TabGroup, index=None):
        source = tab.group
        if source is target:
            return
        source.remove(tab)
        target.add(tab, index)
        self._apply_chrome(target)
        if not source.tabs:
            self._remove_group(source)
        self.activate(tab)
        self._structure_changed()

    def unsplit(self):
        """Alle Gruppen zu einer zusammenführen."""
        groups = self.groups()
        if len(groups) < 2:
            return
        target = self._current_group
        current = self._current_tab
        for g in groups:
            if g is target:
                continue
            for t in list(g.tabs):
                g.remove(t)
                target.add(t)
            self._remove_group(g)
        if current is not None:
            self.activate(current)
        self._structure_changed()

    def _remove_group(self, group: TabGroup):
        for w in self._leading:  # Knöpfe vor dem Löschen der Gruppe in Sicherheit bringen
            if w.parentWidget() is group.header:
                w._parked_hidden = w.isHidden()
                w.setParent(self)
        parent: QSplitter = group.parentWidget()
        group.hide()
        group.setParent(None)
        group.deleteLater()
        if group is self._current_group:
            self._current_group = self.groups()[0]
        # leere Zwischen-Splitter auflösen
        while parent is not self.root and parent.count() == 1:
            child = parent.widget(0)
            grand: QSplitter = parent.parentWidget()
            grand.replaceWidget(grand.indexOf(parent), child)
            parent.deleteLater()
            parent = grand
        if parent is self.root and parent.count() == 1 and isinstance(parent.widget(0), QSplitter):
            sub = parent.widget(0)  # einzige Unterteilung nach oben ziehen
            parent.setOrientation(sub.orientation())
            sizes = sub.sizes()
            while sub.count():
                parent.addWidget(sub.widget(0))
            sub.setParent(None)
            sub.deleteLater()
            parent.setSizes(sizes)

    # ---- Vollbild: Tableisten ausblenden
    def set_chrome_visible(self, visible):
        self._chrome = visible
        for g in self.groups():
            self._apply_chrome(g)

    def _apply_chrome(self, group):
        group.header.setVisible(self._chrome)

