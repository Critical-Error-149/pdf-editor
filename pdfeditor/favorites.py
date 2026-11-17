"""Selbst gewählte Speicherorte (Kurzwahl-Knöpfe) für die Dateileiste."""
from __future__ import annotations

import json
import os

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer, Signal
from . import icons
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QInputDialog, QLayout, QListWidget,
                               QPushButton, QToolButton, QVBoxLayout)

LONG_PRESS_MS = 600


def load_favorites(qs, default_path):
    raw = qs.value("browser_favorites")
    if raw:
        try:
            data = json.loads(raw)
            return [f for f in data if isinstance(f, dict) and f.get("path")]
        except ValueError:
            pass
    return [{"name": "Dokumente", "path": default_path}]


def save_favorites(qs, favs):
    qs.setValue("browser_favorites", json.dumps(favs))


def default_name(path):
    p = path.rstrip("/\\")
    return os.path.basename(p) or p


class FlowLayout(QLayout):
    """Ordnet Knöpfe nebeneinander an und bricht bei Platzmangel um."""

    def __init__(self, parent=None, spacing=4):
        super().__init__(parent)
        self._items = []
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._layout(rect, apply=True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        return size

    def _layout(self, rect, apply):
        x, y, line_h = rect.x(), rect.y(), 0
        sp = self.spacing()
        for it in self._items:
            hint = it.sizeHint()
            if x + hint.width() > rect.right() + 1 and line_h > 0:
                x = rect.x()
                y += line_h + sp
                line_h = 0
            if apply:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + sp
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y()


class FavoriteButton(QToolButton):
    """Kurzwahl-Knopf; langes Drücken (Finger/Stift/Maus) oder Rechtsklick -> Menü beim Loslassen."""

    long_pressed = Signal(QPoint)

    def __init__(self, fav, parent=None):
        super().__init__(parent)
        self.fav = fav
        # gezeichnetes Symbol statt Emoji: das erste Emoji lässt Windows eine Ersatzschrift laden (Programmstart)
        self.setIcon(icons.icon("folder"))
        self.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.setText(fav["name"])
        self.setToolTip(fav["path"])
        self.setAutoRaise(True)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(LONG_PRESS_MS)
        self._timer.timeout.connect(self._held)
        self._long = False
        self._press_pos = QPoint()

    def mousePressEvent(self, e):
        self._long = False
        self._press_pos = e.position().toPoint()
        if e.button() == Qt.LeftButton:
            self._timer.start()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if (e.position().toPoint() - self._press_pos).manhattanLength() > 24:
            self._timer.stop()
        super().mouseMoveEvent(e)

    def _held(self):
        self._long = True
        self.setDown(False)

    def mouseReleaseEvent(self, e):
        self._timer.stop()
        if self._long:  # kein Klick auslösen, stattdessen Menü
            self._long = False
            self.setDown(False)
            p = self.mapToGlobal(self._press_pos)
            QTimer.singleShot(0, lambda: self.long_pressed.emit(p))
            return
        super().mouseReleaseEvent(e)

    def contextMenuEvent(self, e):
        self.long_pressed.emit(e.globalPos())


class FavoritesDialog(QDialog):
    """Speicherorte umbenennen, entfernen und sortieren."""

    def __init__(self, favs, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Speicherorte verwalten")
        self.resize(460, 360)
        self.favs = [dict(f) for f in favs]
        lay = QHBoxLayout(self)
        self.list = QListWidget()
        lay.addWidget(self.list, 1)
        col = QVBoxLayout()
        for text, slot in (("Umbenennen …", self._rename), ("Entfernen", self._remove),
                           ("Nach oben", lambda: self._move(-1)), ("Nach unten", lambda: self._move(1))):
            b = QPushButton(text)
            b.setMinimumHeight(36)
            b.clicked.connect(slot)
            col.addWidget(b)
        col.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, Qt.Vertical)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        col.addWidget(buttons)
        lay.addLayout(col)
        self._refresh()

    def _refresh(self, select=0):
        self.list.clear()
        for f in self.favs:
            self.list.addItem(f"{f['name']}   —   {f['path']}")
        if self.favs:
            self.list.setCurrentRow(max(0, min(select, len(self.favs) - 1)))

    def _rename(self):
        i = self.list.currentRow()
        if i < 0:
            return
        name, ok = QInputDialog.getText(self, "Umbenennen", "Name:", text=self.favs[i]["name"])
        if ok and name.strip():
            self.favs[i]["name"] = name.strip()
            self._refresh(i)

    def _remove(self):
        i = self.list.currentRow()
        if i >= 0:
            del self.favs[i]
            self._refresh(i)

    def _move(self, d):
        i = self.list.currentRow()
        j = i + d
        if 0 <= i < len(self.favs) and 0 <= j < len(self.favs):
            self.favs[i], self.favs[j] = self.favs[j], self.favs[i]
            self._refresh(j)
