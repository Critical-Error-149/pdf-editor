"""Seitenleiste mit Miniaturansichten.

Finger: wischen scrollt (mit Schwung), tippen springt zur Seite, lang antippen öffnet
beim Loslassen ein Menü für genau diese Seite. Kein Drag & Drop – Seiten werden über
das Menü verschoben.
"""
from __future__ import annotations

import pymupdf as fitz
from PySide6.QtCore import QPoint, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QListWidget, QListWidgetItem, QMenu

from . import theme
from .touch_scroll import TouchScroller

THUMB_W = 130


class ThumbnailPanel(QListWidget):
    page_clicked = Signal(int)

    def __init__(self, document, parent=None):
        super().__init__(parent)
        self.document = document
        self.setViewMode(QListWidget.ListMode)
        self.setIconSize(QSize(THUMB_W, int(THUMB_W * 1.5)))
        self.setSpacing(6)
        self.setUniformItemSizes(False)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)  # Strg/Umschalt mit der Maus
        self.setDragEnabled(False)
        self.setDragDropMode(QAbstractItemView.NoDragDrop)
        self.setAcceptDrops(False)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setMinimumWidth(THUMB_W + 40)
        self.setMaximumWidth(THUMB_W + 60)
        self.setFrameShape(QListWidget.NoFrame)  # Darstellung übernimmt das Neumorphism-Design

        # Miniaturen werden einzeln im Leerlauf gerendert – sichtbare zuerst, ferne erst bei Bedarf
        self._pending: set[int] = set()
        self._timer = QTimer(self)
        self._timer.setInterval(5)
        self._timer.timeout.connect(self._render_some)
        self._dirty: set[int] = set()  # geänderte Seiten, verzögert neu rendern (nicht bei jedem Strich)
        self._dirty_timer = QTimer(self)
        self._dirty_timer.setSingleShot(True)
        self._dirty_timer.setInterval(600)
        self._dirty_timer.timeout.connect(self._flush_dirty)
        self.verticalScrollBar().valueChanged.connect(lambda _: self._kick())

        # Finger: wischen, tippen, lang antippen
        self.touch = TouchScroller(self, on_tap=self._tap, on_long_press=self._menu, on_hold=self._hold)

        self.itemClicked.connect(lambda it: self.page_clicked.emit(self.row(it)))
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)

        document.changed.connect(lambda _: self.rebuild())
        document.page_changed.connect(self.refresh_page)

    # -------------------------------------------------- Miniaturen
    def rebuild(self):
        cur = self.currentRow()
        self.clear()
        for i in range(self.document.page_count):
            r = self.document.page(i).rect
            item = QListWidgetItem()
            item.setIcon(QIcon(self._placeholder(r, i)))
            item.setSizeHint(QSize(THUMB_W + 12, int(THUMB_W * r.height / r.width) + 30))
            self.addItem(item)
        self._pending = set(range(self.document.page_count))
        self._dirty.clear()
        self._kick()
        if 0 <= cur < self.count():
            self.set_current(cur)

    def refresh_page(self, index):
        self._dirty.add(index)
        self._dirty_timer.start()

    def _flush_dirty(self):
        self._pending |= self._dirty
        self._dirty.clear()
        self._kick()

    def _kick(self):
        if self._pending and self.isVisible() and not self._timer.isActive():
            self._timer.start()

    def showEvent(self, e):
        super().showEvent(e)
        self._kick()

    def _next_pending(self):
        """Sichtbare Miniatur zuerst, sonst die nächstgelegene (höchstens 8 Zeilen entfernt)."""
        vp = self.viewport().rect()
        first = self.indexAt(vp.topLeft() + QPoint(5, 5)).row()
        last = self.indexAt(vp.bottomLeft() + QPoint(5, -5)).row()
        if first < 0:
            first = 0
        if last < 0:
            last = self.count() - 1
        best, best_d = None, None
        for i in self._pending:
            d = 0 if first <= i <= last else min(abs(i - first), abs(i - last))
            if d <= 8 and (best_d is None or d < best_d):
                best, best_d = i, d
                if d == 0:
                    break
        return best

    def _render_some(self):
        # genau eine Miniatur pro Durchgang, damit die Oberfläche flüssig bleibt
        i = self._next_pending() if self.isVisible() else None
        if i is None:
            self._timer.stop()
            return
        self._pending.discard(i)
        if i < self.count():
            self.item(i).setIcon(QIcon(self._render(i)))

    def _placeholder(self, r, i):
        h = int(THUMB_W * r.height / r.width)
        dpr = self.devicePixelRatioF()
        pm = QPixmap(round(THUMB_W * dpr), round((h + 18) * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.fillRect(0, 0, THUMB_W, h, Qt.white)
        self._label(p, i, h)
        p.end()
        return pm

    def _render(self, i):
        page = self.document.page(i)
        # in Bildschirmpixeln rendern (bei 150 % Skalierung 1,5-fach) – sonst zieht Windows die Miniatur hoch
        dpr = self.devicePixelRatioF()
        z = THUMB_W * dpr / page.rect.width
        with self.document.reading():
            pix = page.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False)
        img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888)
        img.setDevicePixelRatio(dpr)
        h = pix.height / dpr
        pm = QPixmap(round(THUMB_W * dpr), round((h + 18) * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.drawImage(0, 0, img)
        self._label(p, i, h)
        p.end()
        return pm

    def _label(self, p: QPainter, i, h):
        p.setPen(theme.TEXT_DIM)
        p.drawText(QRectF(0, h, THUMB_W, 18), Qt.AlignCenter, str(i + 1))

    def set_current(self, index):
        if 0 <= index < self.count() and self.currentRow() != index:
            self.blockSignals(True)
            self.setCurrentRow(index)
            self.blockSignals(False)
            if not self.touch.is_active():
                self.scrollToItem(self.item(index))

    def selected_pages(self):
        rows = sorted(self.row(it) for it in self.selectedItems())
        return rows or ([self.currentRow()] if self.currentRow() >= 0 else [])

    # -------------------------------------------------- Finger
    def _tap(self, pos: QPoint):
        item = self.itemAt(pos)
        if item is not None:
            self.setCurrentItem(item)
            self.page_clicked.emit(self.row(item))

    def _hold(self, pos: QPoint):
        item = self.itemAt(pos)
        if item is not None:
            self.setCurrentItem(item)  # Rückmeldung: Miniatur wird markiert

    # -------------------------------------------------- Kontextmenü
    def _menu(self, pos: QPoint):
        item = self.itemAt(pos)
        if item is None:
            return
        idx = self.row(item)
        n = self.count()
        sel = self.selected_pages()
        # Mehrfachauswahl (Maus) nur, wenn auf eine der markierten Seiten geklickt wurde
        pages = sel if idx in sel and len(sel) > 1 else [idx]
        if pages == [idx]:
            self.setCurrentRow(idx)
        win = self.window()
        doc = self.document
        m = QMenu(self)
        title = m.addAction(f"Seite {idx + 1} von {n}" if len(pages) == 1 else f"{len(pages)} Seiten")
        title.setEnabled(False)
        m.addSeparator()
        if len(pages) == 1:
            m.addAction("Anzeigen", lambda: self.page_clicked.emit(idx))
            ins = m.addMenu("Einfügen")
            ins.addAction("Leere Seite davor", lambda: win.quick_insert(idx))
            ins.addAction("Leere Seite danach", lambda: win.quick_insert(idx + 1))
            ins.addAction("Seiten danach (Format, Vorlage) …", lambda: win.insert_pages_dialog(idx + 1))
            ins.addAction("PDF/Dokument danach …", lambda: win.insert_pdf(idx + 1))
            ins.addAction("Bild als Seite danach …", lambda: win.insert_image_page(idx + 1))
            if hasattr(win, "clipboard_image") and not win.clipboard_image().isNull():
                ins.addAction("Bild aus Zwischenablage als Seite danach …", lambda: win.paste_as_page(idx + 1))
            if doc.image_page_spec(idx) is not None:
                m.addAction("Bildseite bearbeiten (Format, Zuschneiden) …", lambda: win.edit_image_page(idx))
            m.addAction("Duplizieren", lambda: self._after(doc.duplicate_page(idx), idx + 1))
            mv = m.addMenu("Verschieben")
            a = mv.addAction("Eine Position nach oben", lambda: self._move(idx, idx - 1))
            a.setEnabled(idx > 0)
            a = mv.addAction("Eine Position nach unten", lambda: self._move(idx, idx + 1))
            a.setEnabled(idx < n - 1)
            a = mv.addAction("An den Anfang", lambda: self._move(idx, 0))
            a.setEnabled(idx > 0)
            a = mv.addAction("Ans Ende", lambda: self._move(idx, n - 1))
            a.setEnabled(idx < n - 1)
            mv.addAction("An Position …", lambda: win.move_page_dialog(idx))
        m.addAction("Nach links drehen", lambda: doc.rotate_pages(pages, -90))
        m.addAction("Nach rechts drehen", lambda: doc.rotate_pages(pages, 90))
        m.addSeparator()
        m.addAction("Als PDF exportieren …", lambda: win.extract_pages(pages))
        if len(pages) == 1:
            m.addAction("Als Bild exportieren …", lambda: win.export_page_image(idx))
        m.addSeparator()
        m.addAction("Seite löschen" if len(pages) == 1 else f"{len(pages)} Seiten löschen",
                    lambda: win.delete_pages(pages))
        m.exec(self.viewport().mapToGlobal(pos))

    def _move(self, src, dst):
        self.document.move_page(src, dst)
        self._after(None, dst)

    def _after(self, _result, index):
        """Nach einer Seitenoperation die betroffene Seite markieren und anzeigen."""
        self.set_current(index)
        self.page_clicked.emit(index)
