"""Dateileiste: Ordner durchsuchen und Dokumente öffnen (finger- und stifttauglich)."""
from __future__ import annotations

import os
import subprocess

from PySide6.QtCore import QDir, QFileInfo, QPoint, QSize, QStandardPaths, Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (QApplication, QFileDialog, QFileSystemModel, QHBoxLayout,
                               QInputDialog, QLabel, QListView, QListWidget, QListWidgetItem, QMenu,
                               QSizePolicy, QStackedWidget, QToolButton, QVBoxLayout, QWidget)

from .favorites import (FavoriteButton, FavoritesDialog, FlowLayout, default_name, load_favorites,
                        save_favorites)
from . import icons, theme
from .touch_scroll import TouchScroller

FILTERS = ["*.pdf", "*.xps", "*.oxps", "*.epub", "*.cbz", "*.fb2", "*.png", "*.jpg", "*.jpeg", "*.tif",
           "*.tiff", "*.bmp"]

def documents_dir():
    return QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation) or QDir.homePath()


def show_in_explorer(path):
    path = os.path.normpath(path)
    if os.name == "nt":
        if os.path.isdir(path):
            subprocess.Popen(["explorer", path])
        else:
            subprocess.Popen(["explorer", "/select,", path])


class FileBrowser(QWidget):
    open_requested = Signal(str)
    reopen_requested = Signal(str)   # bereits geöffnete Datei in einem weiteren Tab öffnen
    rename_requested = Signal(str)   # Datei umbenennen (erledigt das Hauptfenster: offene Tabs anpassen)

    def __init__(self, qsettings, parent=None):
        super().__init__(parent)
        self.qs = qsettings
        self.setObjectName("filebrowser")
        self.setMinimumWidth(120)  # schmal: Knöpfe zeigen dann nur ihr Symbol
        self._current_file = None
        self.is_open = lambda path: False  # vom Hauptfenster gesetzt: ist die Datei in einem Tab offen?

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(2)

        # Navigation; „Zuletzt“ schaltet zwischen Ordnerinhalt und zuletzt geöffneten Dateien um
        nav = QHBoxLayout()
        nav.setSpacing(2)
        self.btn_up = self._tool("", "Übergeordneter Ordner (oberste Ebene: alle Laufwerke)", self.go_up, icon="up")
        nav.addWidget(self.btn_up)
        self.btn_explorer = self._tool("Explorer", "Ordner im Explorer-Dialog auswählen", self.choose_folder,
                                       icon="folder")
        nav.addWidget(self.btn_explorer)
        self.btn_recent = self._tool("Zuletzt", "Zuletzt geöffnete Dateien (nochmal tippen: Ordner)",
                                     checkable=True, icon="recent")
        self.btn_recent.toggled.connect(self._show_recent)
        nav.addWidget(self.btn_recent)
        self.btn_add_fav = self._tool("Speicherort", "Speicherorte hinzufügen und verwalten", icon="plus")
        self.btn_add_fav.setPopupMode(QToolButton.InstantPopup)
        fav_menu = QMenu(self.btn_add_fav)
        fav_menu.aboutToShow.connect(lambda: self._fill_fav_menu(fav_menu))
        self.btn_add_fav.setMenu(fav_menu)
        nav.addWidget(self.btn_add_fav)
        nav.addStretch(1)
        self.nav_widget = QWidget()
        self.nav_widget.setLayout(nav)
        # Breite der Knopfzeile soll die Leiste nicht breit halten (bei Platzmangel nur Symbole, s. resizeEvent)
        self.nav_widget.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._nav_buttons = [self.btn_up, self.btn_explorer, self.btn_recent, self.btn_add_fav]
        nav.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.nav_widget)
        # Selbst gewählte Speicherorte als Kurzwahl-Knöpfe
        self.favs = load_favorites(self.qs, documents_dir())
        self.fav_widget = QWidget()
        self.fav_layout = FlowLayout(self.fav_widget)
        lay.addWidget(self.fav_widget)
        self._rebuild_favorites()
        self.path_label = QLabel()
        self.path_label.setObjectName("path")
        pp = self.path_label.palette()
        pp.setColor(QPalette.WindowText, theme.TEXT_DIM)
        self.path_label.setPalette(pp)
        self.path_label.setContentsMargins(6, 0, 6, 4)
        self.path_label.setWordWrap(True)
        lay.addWidget(self.path_label)

        # Ordnerinhalt
        self.model = QFileSystemModel(self)
        self.model.setRootPath("")
        self.model.setFilter(QDir.AllDirs | QDir.Files | QDir.NoDotAndDotDot | QDir.Drives)
        self.model.setNameFilters(FILTERS)
        self.model.setNameFilterDisables(False)  # nicht passende Dateien ausblenden
        self.list = QListView()
        self.list.setModel(self.model)
        self.list.setIconSize(QSize(24, 24))
        self.list.setUniformItemSizes(True)
        self.list.setVerticalScrollMode(QListView.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setFrameShape(QListView.NoFrame)
        self.list.setTextElideMode(Qt.ElideMiddle)
        self.list.clicked.connect(lambda idx: self._activate(self.model.filePath(idx)))
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(lambda p: self._menu(self.list, p))
        self.list_touch = TouchScroller(self.list, on_tap=lambda p: self._tap(self.list, p),
                                        on_long_press=lambda p: self._menu(self.list, p),
                                        on_hold=lambda p: self._hold(self.list, p))

        # Zuletzt geöffnet
        self.recent = QListWidget()
        self.recent.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.recent.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.recent.setFrameShape(QListView.NoFrame)
        self.recent.setIconSize(QSize(24, 24))
        self.recent.itemClicked.connect(lambda it: self._activate(it.data(Qt.UserRole)))
        self.recent.setContextMenuPolicy(Qt.CustomContextMenu)
        self.recent.customContextMenuRequested.connect(lambda p: self._menu(self.recent, p))
        self.recent_touch = TouchScroller(self.recent, on_tap=lambda p: self._tap(self.recent, p),
                                          on_long_press=lambda p: self._menu(self.recent, p),
                                          on_hold=lambda p: self._hold(self.recent, p))

        self.stack = QStackedWidget()
        self.stack.addWidget(self.list)
        self.stack.addWidget(self.recent)
        lay.addWidget(self.stack, 1)

        start = self.qs.value("browser_dir") or documents_dir()
        self.set_folder(start if start == "" or os.path.isdir(start) else documents_dir())

    # ------------------------------------------------------------ Speicherorte
    def _rebuild_favorites(self):
        while self.fav_layout.count():
            it = self.fav_layout.takeAt(0)
            btn = it.widget()
            if btn is not None:
                btn.hide()
                btn.setParent(None)  # sofort aus der Leiste entfernen
                btn.deleteLater()
        for i, fav in enumerate(self.favs):
            b = FavoriteButton(fav)
            b.clicked.connect(lambda _=False, p=fav["path"]: self.set_folder(p))
            b.long_pressed.connect(lambda gp, i=i: self._fav_menu(i, gp))
            self.fav_layout.addWidget(b)
        self.fav_widget.setVisible(bool(self.favs))
        save_favorites(self.qs, self.favs)

    def add_favorite(self, path, name=None):
        path = QDir.fromNativeSeparators(path)
        if not path or any(f["path"] == path for f in self.favs):
            return
        self.favs.append({"name": name or default_name(path), "path": path})
        self._rebuild_favorites()

    def _fill_fav_menu(self, m):
        m.clear()
        cur = self.folder()
        a = m.addAction("Aktuellen Ordner hinzufügen", lambda: self.add_favorite(cur))
        a.setEnabled(bool(cur) and not any(f["path"] == cur for f in self.favs))
        m.addAction("Ordner auswählen und hinzufügen …", self._choose_favorite)
        m.addSeparator()
        m.addAction("Speicherorte verwalten …", self._manage_favorites)

    def _choose_favorite(self):
        path = QFileDialog.getExistingDirectory(self, "Speicherort hinzufügen", self.folder() or documents_dir())
        if path:
            self.add_favorite(path)

    def _manage_favorites(self):
        dlg = FavoritesDialog(self.favs, self)
        if dlg.exec():
            self.favs = dlg.favs
            self._rebuild_favorites()

    def _fav_menu(self, i, global_pos):
        if not (0 <= i < len(self.favs)):
            return
        fav = self.favs[i]
        m = QMenu(self)
        t = m.addAction(fav["name"])
        t.setEnabled(False)
        m.addSeparator()
        m.addAction("Öffnen", lambda: self.set_folder(fav["path"]))
        m.addAction("Umbenennen …", lambda: self._rename_favorite(i))
        if i > 0:
            m.addAction("Nach links", lambda: self._swap_favorites(i, i - 1))
        if i < len(self.favs) - 1:
            m.addAction("Nach rechts", lambda: self._swap_favorites(i, i + 1))
        m.addAction("Im Explorer anzeigen", lambda: show_in_explorer(fav["path"]))
        m.addSeparator()
        m.addAction("Entfernen", lambda: self._remove_favorite(i))
        m.exec(global_pos)

    def _rename_favorite(self, i):
        name, ok = QInputDialog.getText(self, "Speicherort umbenennen", "Name:", text=self.favs[i]["name"])
        if ok and name.strip():
            self.favs[i]["name"] = name.strip()
            self._rebuild_favorites()

    def _swap_favorites(self, i, j):
        self.favs[i], self.favs[j] = self.favs[j], self.favs[i]
        self._rebuild_favorites()

    def _remove_favorite(self, i):
        del self.favs[i]
        self._rebuild_favorites()

    def _tool(self, text, tip, slot=None, checkable=False, icon=None):
        b = QToolButton()
        b.setText(text)
        b.setToolTip(f"{text}: {tip}" if text and icon else tip)
        b.setCheckable(checkable)
        b.setAutoRaise(True)
        if icon:
            b.setIcon(icons.icon(icon, 18))
            b.setIconSize(QSize(18, 18))
            b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon if text else Qt.ToolButtonIconOnly)
        if slot:
            b.clicked.connect(slot)
        return b

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._update_nav_compact()

    def _update_nav_compact(self):
        """Reicht die Breite nicht für alle Beschriftungen, zeigen Knöpfe nur ihr Symbol (Tooltip bleibt) –
        schrittweise: zuerst „Speicherort“, dann „Zuletzt“, zuletzt „Explorer“."""
        lay = self.nav_widget.layout()
        full, icon_only = {}, {}
        for b in self._nav_buttons:
            style = b.toolButtonStyle()
            b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon if b.text() else Qt.ToolButtonIconOnly)
            full[b] = b.sizeHint().width()
            b.setToolButtonStyle(Qt.ToolButtonIconOnly)
            icon_only[b] = b.sizeHint().width()
            b.setToolButtonStyle(style)
        avail = self.nav_widget.width() - lay.spacing() * (len(self._nav_buttons) - 1)
        compact = set()
        need = sum(full.values())
        for b in (self.btn_add_fav, self.btn_recent, self.btn_explorer):
            if need <= avail:
                break
            compact.add(b)
            need -= full[b] - icon_only[b]
        for b in self._nav_buttons:
            if b.text():
                b.setToolButtonStyle(Qt.ToolButtonIconOnly if b in compact else Qt.ToolButtonTextBesideIcon)

    # ------------------------------------------------------------ Navigation
    def folder(self):
        return self.model.filePath(self.list.rootIndex()) if self.list.rootIndex().isValid() else ""

    def _show_recent(self, on):
        if on:
            self.refresh_recent()
        self.stack.setCurrentIndex(1 if on else 0)

    def set_folder(self, path):
        self.btn_recent.setChecked(False)  # Ordner-Navigation zeigt immer den Ordnerinhalt
        path = QDir.fromNativeSeparators(path) if path else ""
        self.model.setRootPath(path)
        self.list.setRootIndex(self.model.index(path) if path else self.model.index(""))
        self.path_label.setText(QDir.toNativeSeparators(path) if path else "Dieser PC")
        self.qs.setValue("browser_dir", path)
        self.list.scrollToTop()
        self._mark_current()

    def go_up(self):
        path = self.folder()
        if not path:
            return
        parent = os.path.dirname(path.rstrip("/"))
        if parent == path.rstrip("/") or len(path.rstrip("/")) <= 2:  # Laufwerkswurzel -> Laufwerke
            parent = ""
        elif len(parent) == 2 and parent.endswith(":"):
            parent += "/"
        self.set_folder(parent)

    def choose_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Ordner wählen", self.folder() or documents_dir())
        if path:
            self.set_folder(path)

    def _activate(self, path):
        if not path:
            return
        if os.path.isdir(path):
            self.set_folder(path)
        elif os.path.isfile(path):
            self.open_requested.emit(path)

    # ------------------------------------------------------------ Finger
    def _tap(self, view, pos: QPoint):
        idx = view.indexAt(pos)
        if not idx.isValid():
            return
        view.setCurrentIndex(idx)
        if view is self.list:
            self._activate(self.model.filePath(idx))
        else:
            self._activate(self.recent.itemFromIndex(idx).data(Qt.UserRole))

    def _hold(self, view, pos: QPoint):
        idx = view.indexAt(pos)
        if idx.isValid():
            view.setCurrentIndex(idx)

    # ------------------------------------------------------------ Kontextmenü
    def _menu(self, view, pos: QPoint):
        idx = view.indexAt(pos)
        if not idx.isValid():
            return
        path = self.model.filePath(idx) if view is self.list else self.recent.itemFromIndex(idx).data(Qt.UserRole)
        is_dir = os.path.isdir(path)
        m = QMenu(self)
        title = m.addAction(os.path.basename(path.rstrip("/")) or path)
        title.setEnabled(False)
        m.addSeparator()
        m.addAction("Ordner öffnen" if is_dir else "Öffnen", lambda: self._activate(path))
        if not is_dir and self.is_open(path):
            m.addAction("Erneut öffnen (weiterer Tab)", lambda: self.reopen_requested.emit(path))
        if not is_dir and os.path.exists(path):
            m.addAction("Umbenennen …", lambda: self.rename_requested.emit(path))
        if is_dir:
            a = m.addAction("Als Speicherort hinzufügen", lambda: self.add_favorite(path))
            a.setEnabled(not any(f["path"] == QDir.fromNativeSeparators(path) for f in self.favs))
        if not is_dir and view is self.recent:
            m.addAction("Enthaltenden Ordner anzeigen", lambda: self.set_folder(os.path.dirname(path)))
        m.addAction("Im Explorer anzeigen", lambda: show_in_explorer(path))
        m.addAction("Pfad kopieren", lambda: QApplication.clipboard().setText(QDir.toNativeSeparators(path)))
        if view is self.recent:
            m.addSeparator()
            m.addAction("Aus der Liste entfernen", lambda: self._remove_recent(path))
        m.exec(view.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------ Zuletzt geöffnet
    def refresh_recent(self):
        self.recent.clear()
        recent = self.qs.value("recent") or []
        if isinstance(recent, str):
            recent = [recent]
        provider = self.model.iconProvider()
        for p in recent:
            item = QListWidgetItem(os.path.basename(p))
            item.setToolTip(QDir.toNativeSeparators(p))
            item.setData(Qt.UserRole, p)
            if provider is not None:
                item.setIcon(provider.icon(QFileInfo(p)))
            if not os.path.exists(p):
                item.setForeground(Qt.gray)
            self.recent.addItem(item)
        self._mark_current()

    def _remove_recent(self, path):
        recent = [p for p in (self.qs.value("recent") or []) if p != path]
        self.qs.setValue("recent", recent)
        self.refresh_recent()

    # ------------------------------------------------------------ Aktuelle Datei markieren
    def set_current_file(self, path):
        self._current_file = path
        self._mark_current()
        if self.btn_recent.isChecked():
            self.refresh_recent()

    def _mark_current(self):
        p = self._current_file
        if not p:
            return
        idx = self.model.index(QDir.fromNativeSeparators(p))
        if idx.isValid() and idx.parent() == self.list.rootIndex():
            self.list.setCurrentIndex(idx)
        for i in range(self.recent.count()):
            if self.recent.item(i).data(Qt.UserRole) == p:
                self.recent.setCurrentRow(i)
