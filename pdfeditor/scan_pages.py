"""Scanseiten direkt auf der Grafikkarte (nur Grafikkarten-Ansicht).

Eine Scanseite besteht aus einem einzigen JPEG-Bild (dazu evtl. unsichtbarer OCR-Text). Statt das Bild für jede Kachel und
Zoomstufe von MuPDF neu entpacken und rastern zu lassen (bei Scans der teuerste Fall: ~110 ms je Vorschau, ein Vielfaches
für scharfe Kacheln), wird es einmal im Hintergrund dekodiert und als Textur mit vorberechneten Verkleinerungsstufen
(Mipmaps) auf die Grafikkarte gelegt: Scrollen und Zoomen sind dann sofort scharf. Anmerkungen (Striche, Textfelder …)
rendert MuPDF weiterhin – auf diesen Seiten als durchsichtige Kacheln, nur dort, wo Anmerkungen liegen.

Wann ist das Ergebnis gleich wie bei MuPDF? Die Seite darf außer dem Bild nichts Sichtbares enthalten und das Bild muss
schlicht platziert sein:
  * genau ein Bild, JPEG in RGB oder Grau, 8 Bit, ohne Maske/Decode-Angabe, aufrecht (nicht gedreht/gespiegelt)
  * im Inhaltsstrom nur Bild, Text und Zustände – keine Pfade, Verläufe, Inline-Bilder, kein Beschneiden
  * keine Grafikzustände mit Transparenz (SMask, CA/ca < 1, Mischmodus), keine Formular-XObjects, nicht gedreht
  * MuPDF zeichnet die Seite ohne Bilder (Hinweis FZ_DONT_DECODE_IMAGES, ~0,3 ms) komplett weiß – fängt sichtbaren
    Text und alles Übersehene ab
  * keine Textmarker-Anmerkungen (sie multiplizieren mit dem Untergrund – geht nur auf deckenden Kacheln)
"""
from __future__ import annotations

import hashlib
import re
from collections import OrderedDict

import pymupdf as fitz
from pymupdf import mupdf
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QRectF, QRunnable, QSize, Qt, QThreadPool, QTimer, \
    Signal
from PySide6.QtGui import QImage, QImageReader

MAX_PIXELS = 12_000_000     # größere Scans verkleinert dekodieren (Textur mit Mipmaps ~4/3 davon × 4 Byte)
DECODED_KEEP = 6            # dekodierte Bilder, die auf das Hochladen warten dürfen
CHECK_SCALE = 0.2           # Auflösung für „ohne Bilder weiß?“ (Pixel je pt)

# Befehle im Inhaltsstrom, die es auf einer schlichten Scanseite nicht gibt: Pfade füllen/zeichnen, Verläufe,
# Inline-Bilder, Beschneiden
_BAD_OPS = {b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*", b"sh", b"BI", b"W", b"W*"}
_STRINGS = re.compile(rb"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]*>")
_NAMES = re.compile(rb"/[^\s/\[\]()<>{}%]*")
_OPS = re.compile(rb"[A-Za-z*'\"]+")
_REF = re.compile(r"(\d+) 0 R")
_TRANSPARENCY = re.compile(r"/SMask\s*(?!/None)\S|/(?:CA|ca)\s*(?!1(?:\.0*)?[\s/>])[\d.]|/BM\s*/(?!Normal|Compatible)")


# schnelle Vorprüfung: bricht beim ersten Malbefehl ab (kann auch in Textstrings anschlagen – dann wird die Seite eben
# normal gerastert, das ist die sichere Richtung)
_FAST_BAD = re.compile(rb"(?:^|[\s\]\)>])(?:f\*?|F|S|s|B\*?|b\*?|sh|BI|W\*?)(?=[\s\[\(</]|$)")
MAX_CONTENT = 2_000_000     # Scanseiten haben kleine Inhaltsströme (auch mit OCR-Text) – entpackt …
MAX_STORED = 150_000        # … und gespeichert (gepackt): größere werden gar nicht erst entpackt


def _content_ok(data: bytes) -> bool:
    """Inhaltsstrom: genau ein Do, keine Mal- oder Beschneide-Befehle (Text und Zustände sind erlaubt)."""
    if len(data) > MAX_CONTENT or _FAST_BAD.search(data):
        return False
    data = _NAMES.sub(b" ", _STRINGS.sub(b" ", data))
    ops = _OPS.findall(data)
    return ops.count(b"Do") == 1 and not any(op in _BAD_OPS for op in ops)


_DO = re.compile(rb"/([^\s/\[\]()<>{}%]+)\s*Do(?![A-Za-z])")


def _do_target(pdf, page, contents: bytes):
    """xref des einzigen gezeichneten XObjects (aus dem Ressourcenverzeichnis der Seite) oder None."""
    m = _DO.search(contents)
    if m is None:
        return None
    name = m.group(1).decode("latin-1")
    kind, value = pdf.xref_get_key(page.xref, f"Resources/XObject/{name}")
    if kind == "xref":
        return int(value.split()[0])
    # Ressourcen geerbt o. Ä.: über PyMuPDFs (langsamere) Liste
    for item in page.get_images(full=True):
        if item[7] == name:
            return item[0]
    return None


def _stored_length(pdf, page) -> int:
    """Gespeicherte (meist gepackte) Länge der Inhaltsströme – ohne sie zu entpacken."""
    total = 0
    for x in page.get_contents():
        kind, value = pdf.xref_get_key(x, "Length")
        if kind == "int":
            total += int(value)
        elif kind == "xref":
            total += int(pdf.xref_object(int(value.split()[0])).strip() or 0)
    return total


def _no_transparency(pdf, page) -> bool:
    """Keine Grafikzustände mit weicher Maske, Deckkraft unter 1 oder besonderem Mischmodus."""
    kind, value = pdf.xref_get_key(page.xref, "Resources/ExtGState")
    if kind == "null":
        return True
    texts = [pdf.xref_object(int(value.split()[0])) if kind == "xref" else value]
    for ref in _REF.findall(texts[0]):
        texts.append(pdf.xref_object(int(ref)))
    return not any(_TRANSPARENCY.search(t) for t in texts)


def _blank_without_images(page) -> bool:
    """Zeichnet MuPDF die Seite ohne Bilder (und ohne Anmerkungen) komplett weiß?"""
    s = CHECK_SCALE
    pix = fitz.Pixmap(fitz.csRGB, (page.rect * fitz.Matrix(s, s)).irect, 0)
    pix.clear_with(255)
    dev = mupdf.fz_new_draw_device(mupdf.FzMatrix(), pix.this)
    try:
        mupdf.fz_enable_device_hints(dev, mupdf.FZ_DONT_DECODE_IMAGES)
        mupdf.fz_run_page_contents(page.this, dev, mupdf.FzMatrix(s, 0, 0, s, 0, 0), mupdf.FzCookie())
    finally:
        mupdf.fz_close_device(dev)
    return min(bytes(pix.samples), default=255) >= 250


class _Decode(QRunnable):
    """Dekodiert das JPEG im Hintergrund. QImageReader.read() gibt dabei Python frei (gemessen: Hauptthread ≤ 3 ms
    unterbrochen) – anders als PyMuPDF, das den Hauptthread für die ganze Zeit anhalten würde."""

    def __init__(self, owner, index, key, raw, size):
        super().__init__()
        self.owner, self.index, self.key, self.raw, self.size = owner, index, key, raw, size
        self.setAutoDelete(True)

    def run(self):
        img = None
        try:
            ba = QByteArray(self.raw)
            buf = QBuffer(ba)
            buf.open(QIODevice.ReadOnly)
            reader = QImageReader(buf, b"jpg")
            reader.setAutoTransform(False)  # PDF kennt keine EXIF-Drehung
            reader.setScaledSize(self.size)
            img = reader.read()
            if img.isNull():
                img = None
            elif img.format() not in (QImage.Format_RGB32, QImage.Format_ARGB32):
                img = img.convertToFormat(QImage.Format_RGB32)  # Grau o. Ä.: Speicherlage passt zu BGRA8
        except Exception:
            img = None
        self.owner._decoded.emit(self.index, self.key, img)


class ScanPages(QObject):
    """Erkennt Scanseiten, dekodiert ihr Bild und meldet dem Renderer, welche Seiten die Grafikkarte direkt zeichnet.

    Zustände je Seite: unbekannt (nicht in _info), None (keine Scanseite) oder dict mit "state":
    "new" (geeignet, noch nicht dekodiert) → "decoding" → "ok" (aktiv; Bild bis zum Hochladen in _images)."""
    _decoded = Signal(int, object, object)   # aus dem Hintergrund-Thread

    def __init__(self, document, renderer, parent=None):
        super().__init__(parent)
        self.document = document
        self.renderer = renderer
        self.enabled = True
        self._info = {}
        self._queue = []              # zu bearbeitende Seiten, wichtigste zuletzt
        self._images: OrderedDict = OrderedDict()  # (Seite, Schlüssel) -> QImage, wartet aufs Hochladen
        self._decoding = set()
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._work)
        self._decoded.connect(self._on_decoded, Qt.QueuedConnection)

    # ------------------------------------------------------------ Abfragen
    def maybe_scan(self, index) -> bool:
        """Noch unbekannt oder auf dem Weg zur Scanseite? (Dann lohnt teures Rastern durch MuPDF vorab nicht.)"""
        if not self.enabled:
            return False
        info = self._info.get(index, ...)
        return info is ... or (info is not None and info["state"] != "ok")

    def texture_source(self, index):
        """(Schlüssel, Bildrechteck in Seitenkoordinaten, QImage oder None) einer aktiven Seite, sonst None.
        Das Bild ist nur bis zum Hochladen da (danach None – die Textur liegt auf der Grafikkarte)."""
        info = self._info.get(index)
        if not info or info["state"] != "ok":
            return None
        return info["key"], info["bbox"], self._images.get((index, info["key"]))

    def uploaded(self, index, key):
        """Textur liegt auf der Grafikkarte: Bild im Arbeitsspeicher freigeben."""
        self._images.pop((index, key), None)

    def texture_lost(self, index, key):
        """Textur verdrängt (oder Bild nie hochgeladen): beim nächsten Bedarf neu dekodieren."""
        info = self._info.get(index)
        if info and info["key"] == key and info["state"] == "ok" and (index, key) not in self._images:
            info["state"] = "new"
            self.request(index, urgent=True)

    # ------------------------------------------------------------ Aufträge
    def request(self, index, urgent=False):
        """Seite bearbeiten (prüfen, dekodieren). urgent: sichtbare Seite – vor allen vorab angeforderten."""
        if not self.enabled:
            return
        info = self._info.get(index, ...)
        if info is ...:
            # sofort prüfen: billig (die meisten Seiten scheiden schon am Ressourcenverzeichnis aus) – so steht beim
            # Zeichnen fest, ob die Seite normale Kacheln braucht
            info = self._info[index] = self._analyze(index)
        if info is None:
            return
        if info is not ... and info["state"] == "ok":
            if index not in self.renderer.scan_pages:
                self._enter(index, info)  # Renderer wurde zwischendurch geleert
            return
        if info is not ... and info["state"] == "decoding":
            return
        if index in self._queue:
            if not urgent:
                return
            self._queue.remove(index)
        if urgent:
            self._queue.append(index)
        else:
            self._queue.insert(0, index)
        self._timer.start()

    def page_changed(self, index):
        """Seite geändert: Inhalt neu prüfen (ändert sich nur eine Anmerkung, bleibt alles, wie es ist)."""
        info = self._info.get(index, ...)
        if info is ...:
            return
        if info is None:
            del self._info[index]  # z. B. Text gelöscht: vielleicht jetzt eine Scanseite
            return
        try:
            with self.document.reading():
                digest = hashlib.blake2b(self.document.page(index).read_contents(), digest_size=16).digest()
        except Exception:
            digest = None
        if digest != info["contents"]:
            self._leave(index)
            del self._info[index]

    def clear(self):
        self.renderer.scan_pages.clear()
        self._info.clear()
        self._queue.clear()
        self._images.clear()
        self._decoding.clear()

    # ------------------------------------------------------------ Ablauf
    def _work(self):
        if not self._queue:
            return
        index = self._queue.pop()
        if self._queue:
            self._timer.start()
        info = self._info.get(index)
        if info is not None and info["state"] == "new":
            self._start_decode(index, info)

    def _analyze(self, index):
        """Kommt die Seite in Frage? Rückgabe: dict oder None."""
        doc = self.document
        if index >= doc.page_count:
            return None
        try:
            with doc.reading():
                return self._analyze_page(doc, index)
        except Exception:
            return None

    def _analyze_page(self, doc, index):
        page = doc.page(index)
        pdf = doc.doc
        # zuerst, was nichts kostet: Drehung und die gespeicherte Länge des Inhaltsstroms (Vektorseiten haben große
        # Inhaltsströme; entpacken kostete bis 0,3 s, und auch das Ressourcenverzeichnis zu durchsuchen kann bei
        # Büchern mit gemeinsamen Ressourcen ~85 ms dauern)
        if page.rotation != 0 or _stored_length(pdf, page) > MAX_STORED:
            return None
        contents = page.read_contents()
        if not _content_ok(contents):  # genau ein Do, keine Malbefehle
            return None
        xref = _do_target(pdf, page, contents)  # gezeichnetes XObject direkt nachschlagen (get_images durchsucht
        if xref is None:                         # alle Ressourcen – bei manchen Büchern ~90 ms je Seite)
            return None
        if pdf.xref_get_key(xref, "Subtype")[1] != "/Image" or pdf.xref_get_key(xref, "Filter")[1] != "/DCTDecode":
            return None  # Formular-XObject (könnte alles enthalten) oder kein JPEG
        infos = page.get_image_info()  # ohne xrefs=True: das würde das Bild für eine Prüfsumme dekodieren
        if len(infos) != 1:
            return None
        a, b, c, d, _e, _f = infos[0]["transform"]
        if abs(b) > 1e-6 or abs(c) > 1e-6 or a <= 0 or d <= 0:
            return None  # gedreht oder gespiegelt platziert
        if pdf.xref_get_key(xref, "SMask")[0] != "null" or pdf.xref_get_key(xref, "Mask")[0] != "null" \
                or pdf.xref_get_key(xref, "Decode")[0] != "null":
            return None
        ext = pdf.extract_image(xref)
        if not ext or ext.get("ext") != "jpeg" or ext.get("colorspace") not in (1, 3) or ext.get("bpc") != 8:
            return None
        if not _no_transparency(pdf, page):
            return None
        if any(a_.type[0] == 8 for a_ in page.annots()):  # Textmarker: multipliziert mit dem Untergrund
            return None
        if not _blank_without_images(page):
            return None  # sichtbarer Text oder anderes außer dem Bild
        w, h = ext["width"], ext["height"]
        if w * h > MAX_PIXELS:
            s = (MAX_PIXELS / (w * h)) ** 0.5
            w, h = max(1, int(w * s)), max(1, int(h * s))
        x0, y0, x1, y1 = infos[0]["bbox"]
        return {"state": "new", "xref": xref, "key": (xref, w, h), "size": QSize(w, h),
                "bbox": QRectF(x0, y0, x1 - x0, y1 - y0),
                "contents": hashlib.blake2b(contents, digest_size=16).digest()}

    def _start_decode(self, index, info):
        try:
            with self.document.reading():
                raw = self.document.doc.xref_stream_raw(info["xref"])  # das JPEG, wie es im PDF steht
        except Exception:
            self._info[index] = None
            self.renderer.page_version[index] += 1
            return
        info["state"] = "decoding"
        self._decoding.add((index, info["key"]))
        self._pool.start(_Decode(self, index, info["key"], raw, info["size"]))

    def _on_decoded(self, index, key, img):
        self._decoding.discard((index, key))
        info = self._info.get(index)
        if not info or info["key"] != key or info["state"] != "decoding":
            return
        if img is None:
            self._info[index] = None  # nicht lesbar: normale Kacheln
            self.renderer.page_version[index] += 1
            return
        self._images[(index, key)] = img
        while len(self._images) > DECODED_KEEP:  # zu viele warten: älteste wieder vergessen (neu dekodieren)
            (old, okey), _img = self._images.popitem(last=False)
            oinfo = self._info.get(old)
            if oinfo and oinfo["key"] == okey and oinfo["state"] == "ok":
                oinfo["state"] = "new"
                self._leave(old)
        self._enter(index, info)

    def _enter(self, index, info):
        info["state"] = "ok"
        if index not in self.renderer.scan_pages:
            self.renderer.scan_pages.add(index)
            self.renderer.invalidate_page(index)  # Kacheln dieser Seite: künftig nur noch die Anmerkungen
        else:
            self.renderer.page_version[index] += 1

    def _leave(self, index):
        if index in self.renderer.scan_pages:
            self.renderer.scan_pages.discard(index)
            self.renderer.drop_page(index)  # durchsichtige Anmerkungs-Kacheln passen nicht mehr
