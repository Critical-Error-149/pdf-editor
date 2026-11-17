"""Drucken mit eigener Seitenvorschau.

Der Druckdialog von Windows 11 zeigt für klassische Programme (PrintDlgEx) statt einer Vorschau nur „Diese App
unterstützt keine Seitenansicht“. Hier wählt man Drucker, Seiten und Einstellungen und sieht jede Seite so, wie sie
aufs Papier kommt; der Windows-Dialog bleibt für Treiberoptionen (Fach, Qualität …) über „Druckereinstellungen …“
erreichbar."""
from __future__ import annotations

import math
import re
from collections import OrderedDict

import pymupdf as fitz
from PySide6.QtCore import QRectF, QSettings, Qt
from PySide6.QtGui import QColor, QImage, QPageLayout, QPainter, QPen
from PySide6.QtPrintSupport import QPrintDialog, QPrinter, QPrinterInfo
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QProgressDialog, QPushButton, QSizePolicy, QSpinBox, QVBoxLayout,
                               QWidget)

MAX_DPI = 300  # mehr bringt beim Rendern fürs Papier nichts sichtbar, kostet aber Zeit und Speicher
SCALE_FIT, SCALE_SHRINK, SCALE_ACTUAL = "fit", "shrink", "actual"
ORIENT_AUTO, ORIENT_PORTRAIT, ORIENT_LANDSCAPE = "auto", "portrait", "landscape"


def parse_pages(text, count):
    """„1-3, 5, 8-“ → [0, 1, 2, 4, 7, …] (0-basiert, in der angegebenen Reihenfolge). ValueError bei Unsinn."""
    text = re.sub(r"\s*[-–]\s*", "-", text.strip())
    pages = []
    for part in re.split(r"[,;\s]+", text):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)?-(\d+)?|(\d+)", part)
        if not m or part == "-":
            raise ValueError(f"„{part}“ ist keine Seitenangabe (Beispiel: 1-3, 5, 8-).")
        if m.group(3):
            a = b = int(m.group(3))
        else:
            a = int(m.group(1) or 1)
            b = int(m.group(2) or count)
        for n in (a, b):
            if not 1 <= n <= count:
                raise ValueError(f"Seite {n} gibt es nicht – das Dokument hat {count} Seite{'n' if count != 1 else ''}.")
        pages.extend(range(a - 1, b) if a <= b else range(a - 1, b - 2, -1))
    if not pages:
        raise ValueError("Bitte Seiten angeben (Beispiel: 1-3, 5, 8-).")
    return pages


def orientation_for(page_rect, mode):
    if mode == ORIENT_PORTRAIT:
        return QPageLayout.Portrait
    if mode == ORIENT_LANDSCAPE:
        return QPageLayout.Landscape
    return QPageLayout.Landscape if page_rect.width > page_rect.height else QPageLayout.Portrait


def place(page_w, page_h, area_w, area_h, unit, scale):
    """Zielrechteck der Seite (page_w × page_h pt) im druckbaren Bereich (area_w × area_h; unit = Einheiten je pt),
    zentriert. Bei „Tatsächliche Größe“ darf es über den Bereich hinausragen (wird beschnitten)."""
    w, h = page_w * unit, page_h * unit
    if scale == SCALE_FIT or (scale == SCALE_SHRINK and (w > area_w + 0.5 or h > area_h + 0.5)):
        f = min(area_w / w, area_h / h)
        w, h = w * f, h * f
    return QRectF((area_w - w) / 2, (area_h - h) / 2, w, h)


def render(doc, index, w_px, h_px, gray, annots):
    page = doc.page(index)
    r = page.rect
    m = fitz.Matrix(max(1, w_px) / r.width, max(1, h_px) / r.height)
    with doc.reading():
        pix = page.get_pixmap(matrix=m, alpha=False, annots=annots, colorspace=fitz.csGRAY if gray else fitz.csRGB)
    fmt = QImage.Format_Grayscale8 if gray else QImage.Format_RGB888
    return QImage(pix.samples, pix.width, pix.height, pix.stride, fmt).copy()


def print_pages(doc, printer, pages, orientation=ORIENT_AUTO, scale=SCALE_FIT, gray=False, annots=True,
                progress=None):
    """Seiten auf den (fertig eingestellten) Drucker ausgeben. Querformatige Seiten kommen bei „Automatisch“
    quer aufs Papier – die Ausrichtung wechselt dafür seitenweise im selben Auftrag.
    progress(fertig, gesamt) → False bricht ab. Rückgabe: True = gedruckt, False = abgebrochen."""
    seq = list(pages)
    if printer.copyCount() > 1 and not printer.supportsMultipleCopies():
        seq *= printer.copyCount()  # Treiber kann keine Kopien: selbst wiederholen (sortiert)
        printer.setCopyCount(1)
    painter = QPainter()
    for n, idx in enumerate(seq):
        rect = doc.page(idx).rect
        o = orientation_for(rect, orientation)
        if n == 0:
            printer.setPageOrientation(o)
            if not painter.begin(printer):
                raise OSError("Der Drucker konnte nicht gestartet werden.")
        else:
            if printer.pageLayout().orientation() != o:
                printer.setPageOrientation(o)  # gilt ab der nächsten Seite
            printer.newPage()
        # printer.width()/height() folgen dem Ausrichtungswechsel, painter.viewport() nicht
        res = printer.resolution()
        target = place(rect.width, rect.height, printer.width(), printer.height(), res / 72, scale)
        k = min(1.0, MAX_DPI / res)
        img = render(doc, idx, round(target.width() * k), round(target.height() * k), gray, annots)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.drawImage(target, img)
        if progress is not None and progress(n + 1, len(seq)) is False:
            printer.abort()
            painter.end()
            return False
    painter.end()
    return True


class _Preview(QWidget):
    """Papier mit druckbarem Bereich und der Seite darauf – genau so platziert wie beim Drucken."""

    def __init__(self, dialog):
        super().__init__()
        self.dialog = dialog
        self._cache = OrderedDict()
        self.setMinimumSize(320, 360)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def clear_cache(self):
        self._cache.clear()
        self.update()

    def wheelEvent(self, e):
        self.dialog.step(-1 if e.angleDelta().y() > 0 else 1)

    def _image(self, idx, w, h, gray, annots):
        key = (idx, w, h, gray, annots)
        img = self._cache.get(key)
        if img is None:
            img = render(self.dialog.doc, idx, w, h, gray, annots)
            self._cache[key] = img
            while len(self._cache) > 8:
                self._cache.popitem(last=False)
        return img

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), self.palette().window().color().darker(130))
        geo = self.dialog.preview_geometry()
        if geo is None:
            return
        idx, page_rect, full, paint, scale, gray, annots = geo
        margin = 18
        s = min((self.width() - 2 * margin) / full.width(), (self.height() - 2 * margin) / full.height())
        pw, ph = full.width() * s, full.height() * s
        paper = QRectF((self.width() - pw) / 2, (self.height() - ph) / 2, pw, ph)
        p.fillRect(paper.translated(3, 4), QColor(0, 0, 0, 90))
        p.fillRect(paper, Qt.white)
        area = QRectF(paper.x() + paint.x() * s, paper.y() + paint.y() * s, paint.width() * s, paint.height() * s)
        if area.adjusted(-1, -1, 1, 1).contains(paper):
            area = QRectF(paper)
        else:  # nicht bedruckbaren Rand des Druckers andeuten
            p.setPen(QPen(QColor(170, 170, 170), 1, Qt.DashLine))
            p.drawRect(area)
        t = place(page_rect.width, page_rect.height, area.width(), area.height(), s, scale)
        t.translate(area.topLeft())
        dpr = self.devicePixelRatioF()
        img = self._image(idx, round(t.width() * dpr), round(t.height() * dpr), gray, annots)
        p.setClipRect(area)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.drawImage(t, img)
        p.setClipping(False)
        p.setPen(QPen(QColor(0, 0, 0, 60), 1))
        p.drawRect(paper)


class PrintDialog(QDialog):
    def __init__(self, parent, doc, current=0, selected=None):
        super().__init__(parent)
        self.setWindowTitle("Drucken")
        self.doc = doc
        self.count = doc.page_count
        self.current = current
        self.selected = sorted(selected or [])
        self.qs = QSettings("PDFEditor", "PDFEditor")
        self.printer = None
        self.pages = list(range(self.count))
        self.pos = 0
        self._error = ""

        root = QHBoxLayout(self)
        left = QVBoxLayout()
        self.preview = _Preview(self)
        left.addWidget(self.preview, 1)
        nav = QHBoxLayout()
        self.prev_btn = QPushButton("◀")
        self.next_btn = QPushButton("▶")
        for b in (self.prev_btn, self.next_btn):
            b.setMinimumSize(64, 40)
            b.setStyleSheet("font-size: 15pt")
            b.setAutoRepeat(True)
        self.prev_btn.clicked.connect(lambda: self.step(-1))
        self.next_btn.clicked.connect(lambda: self.step(1))
        self.nav_label = QLabel()
        self.nav_label.setAlignment(Qt.AlignCenter)
        nav.addWidget(self.prev_btn)
        nav.addWidget(self.nav_label, 1)
        nav.addWidget(self.next_btn)
        left.addLayout(nav)
        root.addLayout(left, 3)

        right = QVBoxLayout()
        form = QFormLayout()
        self.printer_box = QComboBox()
        names = QPrinterInfo.availablePrinterNames()
        self.printer_box.addItems(names)
        form.addRow("Drucker:", self.printer_box)

        copies_row = QHBoxLayout()
        self.copies = QSpinBox()
        self.copies.setRange(1, 999)
        self.collate = QCheckBox("Sortiert")
        self.collate.setChecked(True)
        self.collate.setToolTip("Mehrere Exemplare jeweils komplett nacheinander (1, 2, 3, 1, 2, 3 …)")
        copies_row.addWidget(self.copies, 1)
        copies_row.addWidget(self.collate)
        form.addRow("Exemplare:", copies_row)

        self.range_box = QComboBox()
        self.range_box.addItem("Alle Seiten", "all")
        self.range_box.addItem(f"Aktuelle Seite ({current + 1})", "current")
        if len(self.selected) > 1:
            self.range_box.addItem(f"In der Miniaturleiste ausgewählte ({len(self.selected)})", "selected")
        self.range_box.addItem("Seiten …", "custom")
        self.range_edit = QLineEdit()
        self.range_edit.setPlaceholderText("z. B. 1-3, 5, 8-")
        form.addRow("Seiten:", self.range_box)
        form.addRow("", self.range_edit)

        self.paper_box = QComboBox()
        form.addRow("Papier:", self.paper_box)
        self.orient_box = QComboBox()
        self.orient_box.addItem("Automatisch (je Seite)", ORIENT_AUTO)
        self.orient_box.addItem("Hochformat", ORIENT_PORTRAIT)
        self.orient_box.addItem("Querformat", ORIENT_LANDSCAPE)
        form.addRow("Ausrichtung:", self.orient_box)
        self.scale_box = QComboBox()
        self.scale_box.addItem("An Papier anpassen", SCALE_FIT)
        self.scale_box.addItem("Nur zu große Seiten verkleinern", SCALE_SHRINK)
        self.scale_box.addItem("Tatsächliche Größe", SCALE_ACTUAL)
        form.addRow("Größe:", self.scale_box)
        self.color_box = QComboBox()
        self.color_box.addItem("Farbe", False)
        self.color_box.addItem("Graustufen", True)
        form.addRow("Farbe:", self.color_box)
        self.duplex_box = QComboBox()
        form.addRow("Beidseitig:", self.duplex_box)
        self.annots = QCheckBox("Zeichnungen, Notizen und Markierungen drucken")
        self.annots.setChecked(True)
        form.addRow("", self.annots)
        right.addLayout(form)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        right.addWidget(self.summary)
        right.addStretch(1)

        self.system_btn = QPushButton("Druckereinstellungen …")
        self.system_btn.setToolTip("Windows-Druckdialog mit allen Optionen des Druckertreibers (Fach, Qualität …) – "
                                   "„Drucken“ dort druckt sofort")
        self.system_btn.clicked.connect(self._system_dialog)
        right.addWidget(self.system_btn)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.print_btn = QPushButton("Drucken")
        self.print_btn.setDefault(True)
        self.print_btn.clicked.connect(self._print)
        cancel = QPushButton("Abbrechen")
        cancel.clicked.connect(self.reject)
        for b in (self.print_btn, cancel):
            b.setMinimumHeight(36)
        buttons.addWidget(self.print_btn)
        buttons.addWidget(cancel)
        right.addLayout(buttons)
        root.addLayout(right, 2)
        self.resize(1000, 680)

        self._load_settings(names)
        self.printer_box.currentIndexChanged.connect(self._printer_changed)
        self.range_box.currentIndexChanged.connect(self._range_changed)
        self.range_edit.textChanged.connect(self._update_pages)
        self.paper_box.currentIndexChanged.connect(self._paper_changed)
        for w in (self.orient_box, self.scale_box, self.color_box):
            w.currentIndexChanged.connect(self.preview.update)
        self.color_box.currentIndexChanged.connect(self._update_summary)
        self.duplex_box.currentIndexChanged.connect(self._update_summary)
        self.copies.valueChanged.connect(self._update_summary)
        self.annots.toggled.connect(self.preview.update)
        self._printer_changed()
        self._range_changed()

    # ------------------------------------------------------------ Einstellungen
    def _load_settings(self, names):
        q = self.qs
        name = q.value("print/printer", "")
        if name not in names:
            name = QPrinterInfo.defaultPrinterName()
        if name in names:
            self.printer_box.setCurrentIndex(names.index(name))
        for box, key in ((self.orient_box, "print/orientation"), (self.scale_box, "print/scale")):
            i = box.findData(q.value(key, box.itemData(0)))
            box.setCurrentIndex(max(0, i))
        self.color_box.setCurrentIndex(1 if str(q.value("print/gray", "false")).lower() == "true" else 0)
        self.annots.setChecked(str(q.value("print/annots", "true")).lower() == "true")

    def _save_settings(self):
        q = self.qs
        q.setValue("print/printer", self.printer_box.currentText())
        q.setValue("print/orientation", self.orient_box.currentData())
        q.setValue("print/scale", self.scale_box.currentData())
        q.setValue("print/gray", "true" if self.color_box.currentData() else "false")
        q.setValue("print/annots", "true" if self.annots.isChecked() else "false")
        q.setValue("print/duplex", int(self.duplex_box.currentData() or 0))
        size = self.paper_box.currentData()
        if size is not None:
            q.setValue(f"print/paper/{self.printer_box.currentText()}", size.key())

    def _printer_changed(self):
        name = self.printer_box.currentText()
        info = QPrinterInfo.printerInfo(name)
        self.printer = QPrinter(info, QPrinter.HighResolution)
        self.printer.setDocName(self._doc_name())
        # Papierformate des Druckers, Standard: zuletzt hier gewählt, sonst Vorgabe des Druckers
        self.paper_box.blockSignals(True)
        self.paper_box.clear()
        sizes = info.supportedPageSizes() or [info.defaultPageSize()]
        want = self.qs.value(f"print/paper/{name}", info.defaultPageSize().key())
        for size in sizes:
            self.paper_box.addItem(size.name(), size)
        keys = [s.key() for s in sizes]
        self.paper_box.setCurrentIndex(keys.index(want) if want in keys else
                                       keys.index(info.defaultPageSize().key()) if info.defaultPageSize().key() in keys
                                       else 0)
        self.paper_box.blockSignals(False)
        self._paper_changed()
        # Farbe/Beidseitig nur anbieten, was der Drucker kann
        color_ok = QPrinter.Color in info.supportedColorModes()
        self.color_box.model().item(0).setEnabled(color_ok)
        if not color_ok:
            self.color_box.setCurrentIndex(1)
        modes = info.supportedDuplexModes()
        self.duplex_box.blockSignals(True)
        self.duplex_box.clear()
        self.duplex_box.addItem("Nein", int(QPrinter.DuplexNone.value))
        if QPrinter.DuplexLongSide in modes:
            self.duplex_box.addItem("Ja – an der langen Kante wenden", int(QPrinter.DuplexLongSide.value))
        if QPrinter.DuplexShortSide in modes:
            self.duplex_box.addItem("Ja – an der kurzen Kante wenden", int(QPrinter.DuplexShortSide.value))
        i = self.duplex_box.findData(int(self.qs.value("print/duplex", 0)))
        self.duplex_box.setCurrentIndex(max(0, i))
        self.duplex_box.setEnabled(self.duplex_box.count() > 1)
        self.duplex_box.blockSignals(False)
        self._update_summary()

    def _paper_changed(self):
        size = self.paper_box.currentData()
        if size is not None and self.printer is not None:
            self.printer.setPageSize(size)
        self.preview.update()

    def _doc_name(self):
        import os
        return os.path.basename(self.doc.path) if self.doc.path else "Neues Dokument"

    # ------------------------------------------------------------ Seiten
    def _range_changed(self):
        custom = self.range_box.currentData() == "custom"
        self.range_edit.setEnabled(custom)
        if custom:
            self.range_edit.setFocus()
        self._update_pages()

    def _update_pages(self):
        mode = self.range_box.currentData()
        self._error = ""
        if mode == "current":
            pages = [self.current]
        elif mode == "selected":
            pages = self.selected
        elif mode == "custom":
            try:
                pages = parse_pages(self.range_edit.text(), self.count)
            except ValueError as e:
                pages, self._error = [], str(e)
        else:
            pages = list(range(self.count))
        old = self.pages[self.pos] if self.pages else None
        self.pages = pages
        self.pos = pages.index(old) if old in pages else 0
        self._update_summary()
        self.preview.update()

    def step(self, d):
        if self.pages:
            self.pos = max(0, min(len(self.pages) - 1, self.pos + d))
            self._update_summary()
            self.preview.update()

    def _update_summary(self):
        n = len(self.pages)
        self.prev_btn.setEnabled(self.pos > 0)
        self.next_btn.setEnabled(self.pos < n - 1)
        self.nav_label.setText(f"{self.pos + 1} von {n}  (Seite {self.pages[self.pos] + 1})" if n else "–")
        self.print_btn.setEnabled(bool(n) and self.printer_box.count() > 0)
        if self._error:
            self.summary.setText(f"<span style='color:#ff6b6b'>{self._error}</span>")
            return
        copies = self.copies.value()
        duplex = bool(self.duplex_box.currentData())
        sheets = (math.ceil(n / 2) if duplex else n) * copies
        txt = f"{n} Seite{'n' if n != 1 else ''}"
        if copies > 1:
            txt += f" × {copies} Exemplare"
        self.summary.setText(f"{txt} → {sheets} Blatt Papier")

    def preview_geometry(self):
        if not self.pages or self.printer is None:
            return None
        idx = self.pages[self.pos]
        page_rect = self.doc.page(idx).rect
        layout = QPageLayout(self.printer.pageLayout())
        layout.setOrientation(orientation_for(page_rect, self.orient_box.currentData()))
        return (idx, page_rect, layout.fullRectPoints(), layout.paintRectPoints(), self.scale_box.currentData(),
                bool(self.color_box.currentData()), self.annots.isChecked())

    # ------------------------------------------------------------ Drucken
    def _apply_to_printer(self):
        p = self.printer
        p.setCopyCount(self.copies.value())
        p.setCollateCopies(self.collate.isChecked())
        p.setColorMode(QPrinter.GrayScale if self.color_box.currentData() else QPrinter.Color)
        p.setDuplex(QPrinter.DuplexMode(self.duplex_box.currentData() or 0))

    def _system_dialog(self):
        """Windows-Druckdialog für die Treiberoptionen. Was dort eingestellt wird, gilt; „Drucken“ druckt sofort."""
        if self.printer is None:
            return
        self._apply_to_printer()
        if self.pages:
            self.printer.setPageOrientation(orientation_for(self.doc.page(self.pages[self.pos]).rect,
                                                            self.orient_box.currentData()))
        before = self.printer.pageLayout().orientation()
        dlg = QPrintDialog(self.printer, self)
        dlg.setMinMax(1, self.count)
        if dlg.exec() != QPrintDialog.Accepted:
            return
        p = self.printer
        if p.printRange() == QPrinter.PageRange and p.fromPage():
            self.pages = list(range(p.fromPage() - 1, min(p.toPage() or self.count, self.count)))
        elif p.printRange() == QPrinter.CurrentPage:
            self.pages = [self.current]
        elif p.printRange() != QPrinter.Selection:
            self.pages = list(range(self.count))
        orientation = self.orient_box.currentData()
        if p.pageLayout().orientation() != before:  # im Treiber bewusst umgestellt
            orientation = ORIENT_LANDSCAPE if p.pageLayout().orientation() == QPageLayout.Landscape else ORIENT_PORTRAIT
        # gewählte Einstellungen (inkl. Treiberoptionen) unverändert übernehmen
        self._run(orientation, keep_printer_settings=True)

    def _print(self):
        if self._error or not self.pages:
            return
        self._apply_to_printer()
        self._run(self.orient_box.currentData())

    def _run(self, orientation, keep_printer_settings=False):
        self._save_settings()
        pages = list(self.pages)
        progress = QProgressDialog("Drucke …", "Abbrechen", 0, len(pages), self)
        progress.setWindowTitle("Drucken")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(400)

        def step(done, total):
            progress.setMaximum(total)
            progress.setLabelText(f"Drucke Seite {done} von {total} …")
            progress.setValue(done)
            return not progress.wasCanceled()

        try:
            ok = print_pages(self.doc, self.printer, pages, orientation, self.scale_box.currentData(),
                             bool(self.color_box.currentData()), self.annots.isChecked(), step)
        except Exception as e:  # noqa: BLE001 – Treiberfehler verständlich melden
            progress.close()
            QMessageBox.warning(self, "Drucken", f"Drucken fehlgeschlagen:\n{e}")
            return
        progress.close()
        self.printed = ok
        self.accept()

    @classmethod
    def run(cls, parent, doc, current=0, selected=None):
        """Dialog zeigen und ggf. drucken. Rückgabe: True = gedruckt."""
        if not QPrinterInfo.availablePrinterNames():
            QMessageBox.information(parent, "Drucken", "Es ist kein Drucker installiert.")
            return False
        dlg = cls(parent, doc, current, selected)
        dlg.printed = False
        dlg.exec()
        return dlg.printed
