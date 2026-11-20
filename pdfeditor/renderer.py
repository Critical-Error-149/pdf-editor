"""Kachelbasiertes Rendern im Leerlauf.

Statt beim Zeichnen ganze Seiten blockierend zu rendern, wird jede Seite in Kacheln
(TILE x TILE Bildschirmpixel) zerlegt. Fehlende Kacheln landen in einer Warteschlange,
die zwischen den Bildern in kleinen Zeitscheiben abgearbeitet wird – sichtbare Kacheln
zuerst. Bis eine Kachel fertig ist, wird eine kleine Vorschau der Seite (bzw. eine Kachel
aus einer anderen Zoomstufe) hochskaliert angezeigt. So bleibt das Scrollen flüssig.

Seiteninhalt und Anmerkungen werden getrennt gerendert: Der Inhalt (Text, Scanbild) jeder Kachel bleibt
gespeichert, die Anmerkungen (Striche, Textfelder …) werden darauf gezeichnet. Nach einem Strich oder
beim Radieren muss so nur die Anmerkungsebene neu gezeichnet werden (1–5 ms statt 40–60 ms pro Block
auf einer Scanseite) – das Ergebnis ist pixelgleich mit dem Rendern der ganzen Seite.
"""
from __future__ import annotations

import heapq
import itertools
import time
from collections import OrderedDict, defaultdict

import pymupdf as fitz
from pymupdf import mupdf
from PySide6.QtCore import QObject, QRectF, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap

TILE = 256                  # Kachelgröße in Gerätepixeln (klein = kurze Rechenschritte)
BASE_SCALE = 0.4            # Vorschau-Auflösung (Pixel je pt) für jede Seite
MAX_LEVEL = 12.0            # höchste Render-Auflösung (Pixel je pt)


def ram_gb() -> float:
    """Eingebauter Arbeitsspeicher in GB (Windows; sonst 16 angenommen)."""
    try:
        import ctypes

        class _Status(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                        ("avail", ctypes.c_ulonglong), ("pf_total", ctypes.c_ulonglong),
                        ("pf_avail", ctypes.c_ulonglong), ("v_total", ctypes.c_ulonglong),
                        ("v_avail", ctypes.c_ulonglong), ("ext", ctypes.c_ulonglong)]

        st = _Status()
        st.length = ctypes.sizeof(_Status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return st.total / 2 ** 30
    except Exception:
        pass
    return 16.0


# Zwischenspeicher wachsen beim Blättern bis zu diesen Grenzen. Mit wenig Arbeitsspeicher (8 GB) halb so groß –
# sonst belegt das Programm nach etwas Blättern in einem Fachbuch knapp 2 GB
MEMORY_SCALE = 0.5 if ram_gb() < 12 else 1.0
TILE_BUDGET = int(48_000_000 * MEMORY_SCALE)     # max. Pixel im Kachelspeicher (~190 MB, mit 8 GB ~95 MB)
CONTENT_BUDGET = int(24_000_000 * MEMORY_SCALE)  # max. Pixel für Seiteninhalte ohne Anmerkungen (~72 / 36 MB)
BASE_BUDGET = 400           # max. Seiten-Vorschauen im Speicher
DL_BUDGET = 24              # zwischengespeicherte Displaylisten (vorab interpretierte Seiten)
SLICE_MS = 8                # Rechenzeit pro Durchgang im Ruhezustand
SLICE_MOVING_MS = 2         # ... während gescrollt/gezoomt wird (Bildschirmtakt hat Vorrang)
FRAME_RESERVE_S = 0.0008    # so viel Zeit vor dem nächsten Bild frei lassen
HEAVY_TILE_S = 0.010        # Kachel dauert länger -> Seite gilt als „Scanseite“ (große Bilder)
HEAVY_IMAGE_PX = 3_000_000  # Seite mit einem Bild ab so vielen Pixeln gilt ohne Messung als Scanseite
BATCH_RADIUS = 3            # Scanseite: anstehende Kacheln bis zu so vielen Kacheln Abstand gemeinsam rendern
STARVE_S = 0.06             # spätestens nach so langer Pause darf eine Kachel auch ein Bild verzögern

# Prioritäten (kleiner = wichtiger)
P_VISIBLE = 0
P_BASE = 1
P_PREFETCH = 2


def level_for(scale: float) -> float:
    """Auflösung für einen Gerätemaßstab; gerundet, damit kleine Schwankungen Kacheln wiederverwenden."""
    # exakt (nur Rundungsrauschen entfernt), damit Kacheln 1:1 auf Gerätepixel passen
    return max(0.05, min(MAX_LEVEL, round(scale, 6)))


def _to_pixmap(pix: fitz.Pixmap, as_image=False):
    img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888)
    if as_image:  # Grafikkarte: direkt als Textur hochladbar (RGBA), kein QPixmap nötig
        return img.convertToFormat(QImage.Format_RGBA8888)
    return QPixmap.fromImage(img.copy())


class TileRenderer(QObject):
    # Seite, betroffenes Rechteck (Seitenkoordinaten) -> neu zeichnen
    tile_ready = Signal(int, QRectF)
    # Seite hat keine ausstehenden Kacheln mehr (Vorschau-Striche können verschwinden)
    page_settled = Signal(int)

    def __init__(self, document, is_relevant=None, parent=None):
        super().__init__(parent)
        self.document = document
        self.is_relevant = is_relevant or (lambda key: True)
        # (seite, level, tx, ty) -> (QPixmap, gen, Zielrechteck in pt, Gerätepixel-Position)
        self.tiles: OrderedDict = OrderedDict()
        self.levels = defaultdict(set)            # seite -> vorhandene Levels
        self.base: OrderedDict = OrderedDict()    # seite -> (QPixmap, gen)
        self.gen = defaultdict(int)               # Änderungszähler je Seite
        self._dl: OrderedDict = OrderedDict()     # seite -> DisplayList des Seiteninhalts (ohne Anmerkungen)
        self._adl: OrderedDict = OrderedDict()    # seite -> (DisplayList der Anmerkungen oder None, gen)
        # Seiteninhalt je Kachel (fitz.Pixmap, ohne Anmerkungen): bleibt über Anmerkungsänderungen erhalten
        self.content: OrderedDict = OrderedDict()  # (seite, level, tx, ty) -> Pixmap
        self.content_base: OrderedDict = OrderedDict()  # seite -> Pixmap (Vorschau-Auflösung)
        self.content_pixels = 0
        self.pixels = 0
        self._heap = []
        self._queued = {}                         # key -> beste Priorität
        self._seq = itertools.count()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._work)
        self.zooming = False                      # beim Zoomen keine neuen Kacheln anfordern
        self.moving = False                       # Ansicht bewegt sich gerade (kleinere Rechenhäppchen)
        self.render_count = 0                     # für Messungen
        self.hidden: dict[int, frozenset] = {}  # Seite -> beim Rendern ausgeblendete Anmerkungen
        self.next_frame_at = 0.0                  # Zeitpunkt des nächsten Bildes (von der Grafikkarten-Ansicht)
        self._tile_cost = 0.002                   # geschätzte Renderzeit einer Kachel (gleitender Mittelwert)
        self._last_done = 0.0
        self.as_image = False                     # Kacheln als QImage statt QPixmap (Grafikkarten-Darstellung)
        # Scanseiten am Stück: MuPDF entpackt große Bilder für jede Kachel neu (gemessen ~20× mehr Arbeit
        # als für die ganze Seite). Auf solchen Seiten werden benachbarte Kacheln in einem Durchgang gerendert.
        self.batch_heavy = True
        self.heavy = set()                        # Seiten mit teuren Kacheln
        self._checked = set()                     # Seiten, deren Bilder schon angesehen wurden
        # Änderungszähler für die Grafikkarten-Ansicht: Sie baut ihre Seitenebene nur neu auf, wenn sich bei den
        # Kacheln/Vorschauen einer sichtbaren Seite etwas geändert hat (page_version) bzw. alles (epoch)
        self.page_version = defaultdict(int)
        self.epoch = 0

    # ------------------------------------------------------------ Verwaltung
    def clear(self):
        self.epoch += 1
        self._dl.clear()
        self._adl.clear()
        self.content.clear()
        self.content_base.clear()
        self.content_pixels = 0
        self.tiles.clear()
        self.levels.clear()
        self.base.clear()
        self.gen.clear()
        self.pixels = 0
        self._heap.clear()
        self._queued.clear()
        self.heavy.clear()
        self._checked.clear()

    def drop_tiles(self):
        """Scharfe Kacheln freigeben (verdeckter Tab); die kleinen Seitenvorschauen bleiben erhalten."""
        self.epoch += 1
        self.tiles.clear()
        self.levels.clear()
        self.pixels = 0
        self._dl.clear()
        self._adl.clear()
        self.content.clear()
        self.content_pixels = 0
        self._heap = [e for e in self._heap if e[2][0] == "base"]
        heapq.heapify(self._heap)
        self._queued = {k: v for k, v in self._queued.items() if k[0] == "base"}

    def set_hidden(self, index, xrefs, rect: QRectF | None = None):
        """Diese Anmerkungen beim Rendern der Seite weglassen (während sie verschoben werden); None = alle zeigen."""
        if xrefs:
            self.hidden[index] = frozenset(xrefs)
        elif self.hidden.pop(index, None) is None:
            return
        self.invalidate_page(index, rect)

    def render_annots(self, index, xrefs, scale):
        """Nur diese Anmerkungen in ein durchsichtiges Bild rendern (Vorschau beim Verschieben).
        Rückgabe: (QImage, Rechteck in Seitenkoordinaten) oder None."""
        page = self.document.page(index)
        dl = mupdf.FzDisplayList(mupdf.fz_bound_page(page.this))
        dev = mupdf.fz_new_list_device(dl)
        try:
            with self.document.reading():
                bounds = self._run_annots(page, dev, include=set(xrefs))
        finally:
            mupdf.fz_close_device(dev)
        if bounds is None or bounds.is_empty or bounds.is_infinite:
            return None
        bounds = bounds + (-1, -1, 1, 1)
        ir = (bounds * fitz.Matrix(scale, scale)).irect
        if ir.width * ir.height > 40_000_000:  # riesig (z. B. sehr stark gezoomt): keine Vorschau
            return None
        pix = fitz.Pixmap(fitz.csRGB, ir, 1)
        pix.clear_with()  # ohne Wert: auch Alpha = 0 (durchsichtig); clear_with(0) machte es deckend schwarz
        dev = mupdf.fz_new_draw_device(mupdf.FzMatrix(), pix.this)
        try:
            mupdf.fz_run_display_list(dl, dev, mupdf.FzMatrix(scale, 0, 0, scale, 0, 0),
                                      mupdf.fz_rect_from_irect(mupdf.fz_pixmap_bbox(pix.this)), mupdf.FzCookie())
        finally:
            mupdf.fz_close_device(dev)
        img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGBA8888_Premultiplied).copy()
        return img, fitz.Rect(ir.x0 / scale, ir.y0 / scale, ir.x1 / scale, ir.y1 / scale)

    @staticmethod
    def _run_annots(page, dev, include=None, exclude=None):
        """Anmerkungen einzeln abspielen (nur include bzw. ohne exclude). Gibt die Umrandung der abgespielten
        Anmerkungen zurück (Seitenkoordinaten wie angezeigt) oder None."""
        pp = mupdf.pdf_page_from_fz_page(page.this)
        cookie = mupdf.FzCookie()
        annot = mupdf.pdf_first_annot(pp)
        bounds = None
        while annot.m_internal:
            x = mupdf.pdf_to_num(mupdf.pdf_annot_obj(annot))
            if (include is None or x in include) and (exclude is None or x not in exclude):
                mupdf.pdf_run_annot(annot, dev, mupdf.FzMatrix(), cookie)
                r = fitz.Rect(mupdf.pdf_bound_annot(annot))
                bounds = r if bounds is None else bounds | r
            annot = mupdf.pdf_next_annot(annot)
        return bounds

    def invalidate_page(self, index, rect: QRectF | None = None):
        """Anmerkungen der Seite haben sich geändert: alte Kacheln bleiben als Platzhalter, werden aber neu
        gerendert (der gespeicherte Seiteninhalt wird dabei wiederverwendet).
        rect: nur dieser Bereich (Seitenkoordinaten) hat sich geändert – Kacheln außerhalb bleiben gültig."""
        old = self.gen[index]
        self.gen[index] += 1
        self.page_version[index] += 1
        self._checked.discard(index)
        if rect is None:
            return
        new = self.gen[index]
        for key, entry in self.tiles.items():  # Wert ersetzen ändert die Reihenfolge nicht
            if key[0] == index and entry[1] == old and not entry[2].intersects(rect):
                self.tiles[key] = (entry[0], new) + entry[2:]

    def is_stale(self, index, gen):
        return gen != self.gen[index]

    # ------------------------------------------------------------ Abfragen fürs Zeichnen
    def get_tile(self, key):
        entry = self.tiles.get(key)
        if entry is not None:
            self.tiles.move_to_end(key)
        return entry

    def get_base(self, index):
        entry = self.base.get(index)
        if entry is not None:
            self.base.move_to_end(index)
        return entry

    def fallback_levels(self, index, level):
        """Vorhandene andere Levels dieser Seite, die nächstgelegene zuerst."""
        return sorted((lv for lv in self.levels.get(index, ()) if lv != level), key=lambda lv: abs(lv - level))

    # ------------------------------------------------------------ Aufträge
    def _check_heavy(self, index):
        """Große Bilder auf der Seite? Dann gleich als Scanseite behandeln (ohne erst teuer zu messen)."""
        self._checked.add(index)
        try:
            imgs = self.document.page(index).get_images(full=True)
        except Exception:
            return
        if any(i[2] * i[3] >= HEAVY_IMAGE_PX for i in imgs):
            self.heavy.add(index)

    def request(self, key, priority):
        """key: ("tile", seite, level, tx, ty) oder ("base", seite). priority: (Klasse, Abstand)."""
        if key[1] not in self._checked:
            self._check_heavy(key[1])
        best = self._queued.get(key)
        if best is not None and best <= priority:
            return
        self._queued[key] = priority
        heapq.heappush(self._heap, (priority, next(self._seq), key))
        if not self._timer.isActive():
            self._timer.start(0)

    def has_pending(self, index):
        return any(k[1] == index for k in self._queued)

    def _work(self):
        t0 = time.perf_counter()
        done_pages = set()
        budget = SLICE_MOVING_MS if self.moving else SLICE_MS
        deferred = False
        while self._heap and (time.perf_counter() - t0) * 1000 < budget:
            if self.moving and self.next_frame_at:
                # nur anfangen, wenn die Kachel voraussichtlich vor dem nächsten Bild fertig ist
                now = time.perf_counter()
                free = self.next_frame_at - FRAME_RESERVE_S - now
                if free < self._tile_cost and now - self._last_done < STARVE_S:
                    deferred = True
                    break
            top = self._heap[0][2]
            # am Stück nur, wenn der Seiteninhalt fehlt (nach einem Strich reicht die Anmerkungsebene)
            batch = self.batch_heavy and top[0] == "tile" and top[1] in self.heavy and \
                (top[1], top[2], top[3], top[4]) not in self.content
            if batch and self.moving:
                deferred = True  # Scanseite erst nach der Bewegung am Stück rendern (sonst Ruckler)
                break
            priority, _, key = heapq.heappop(self._heap)
            if self._queued.get(key) != priority:
                continue  # durch wichtigeren Auftrag ersetzt
            del self._queued[key]
            if not self.is_relevant(key):
                continue  # inzwischen weggescrollt
            t_r = time.perf_counter()
            try:
                rect = self._render_batch(key) if batch else self._render(key)
            except Exception:
                continue
            if rect is not None:
                now = time.perf_counter()
                if not batch:
                    self._tile_cost = self._tile_cost * 0.8 + (now - t_r) * 0.2
                    if key[0] == "tile" and now - t_r > HEAVY_TILE_S:
                        self.heavy.add(key[1])
                self._last_done = now
                done_pages.add(key[1])
                self.tile_ready.emit(key[1], rect)
        for index in done_pages:
            if not self.has_pending(index):
                self.page_settled.emit(index)
        if self._heap:
            self._timer.start(1 if deferred else 0)  # aufgeschoben: kurz nach dem nächsten Bild weiter

    # ------------------------------------------------------------ Rendern
    def _displaylist(self, index, page):
        """Seiteninhalt nur einmal interpretieren; Kacheln werden dann aus der Displayliste gerastert."""
        dl = self._dl.get(index)
        if dl is not None:
            self._dl.move_to_end(index)
            return dl
        with self.document.reading():  # MuPDF repariert beim Lesen evtl. nebenbei etwas (Journal!)
            dl = page.get_displaylist(annots=False)
        self._dl[index] = dl
        while len(self._dl) > DL_BUDGET:
            self._dl.popitem(last=False)
        return dl

    def _annot_displaylist(self, index, page, gen):
        """Anmerkungen und Formularfelder der Seite als eigene Displayliste (None = keine vorhanden)."""
        entry = self._adl.get(index)
        if entry is not None and entry[1] == gen:
            self._adl.move_to_end(index)
            return entry[0]
        dl = None
        if page.first_annot is not None or page.first_widget is not None:
            dl = mupdf.FzDisplayList(mupdf.fz_bound_page(page.this))
            dev = mupdf.fz_new_list_device(dl)
            try:
                with self.document.reading():
                    pp = mupdf.pdf_page_from_fz_page(page.this)
                    cookie = mupdf.FzCookie()
                    hidden = self.hidden.get(index)
                    if hidden:  # gerade verschobene Anmerkungen weglassen (die Vorschau zeigt sie)
                        self._run_annots(page, dev, exclude=hidden)
                    else:
                        mupdf.pdf_run_page_annots(pp, dev, mupdf.FzMatrix(), cookie)
                    mupdf.pdf_run_page_widgets(pp, dev, mupdf.FzMatrix(), cookie)
            finally:
                mupdf.fz_close_device(dev)
        self._adl[index] = (dl, gen)
        while len(self._adl) > DL_BUDGET:
            self._adl.popitem(last=False)
        return dl

    @staticmethod
    def _with_annots(content: fitz.Pixmap, adl, level):
        """Anmerkungen auf eine Kopie des Seiteninhalts zeichnen (wie MuPDF beim Rendern der ganzen Seite)."""
        if adl is None:
            return content
        out = fitz.Pixmap(content, 0)  # Kopie (ohne Alphakanal)
        dev = mupdf.fz_new_draw_device(mupdf.FzMatrix(), out.this)
        try:
            scissor = mupdf.fz_rect_from_irect(mupdf.fz_pixmap_bbox(out.this))
            mupdf.fz_run_display_list(adl, dev, mupdf.FzMatrix(level, 0, 0, level, 0, 0), scissor, mupdf.FzCookie())
        finally:
            mupdf.fz_close_device(dev)
        return out

    def _store_content(self, ckey, pix):
        old = self.content.pop(ckey, None)
        if old is not None:
            self.content_pixels -= old.width * old.height
        self.content[ckey] = pix
        self.content_pixels += pix.width * pix.height

    def _render(self, key):
        index = key[1]
        if index >= self.document.page_count:
            return None
        page = self.document.page(index)
        gen = self.gen[index]
        if key[0] == "base":
            entry = self.base.get(index)
            if entry is not None and entry[1] == gen:
                return None
            content = self.content_base.get(index)
            if content is None:
                content = self._displaylist(index, page).get_pixmap(matrix=fitz.Matrix(BASE_SCALE, BASE_SCALE),
                                                                    alpha=False)
                self.content_base[index] = content
                while len(self.content_base) > BASE_BUDGET:
                    self.content_base.popitem(last=False)
            else:
                self.content_base.move_to_end(index)
            pix = self._with_annots(content, self._annot_displaylist(index, page, gen), BASE_SCALE)
            self.base[index] = (_to_pixmap(pix, self.as_image), gen)
            self.page_version[index] += 1
            self.base.move_to_end(index)
            while len(self.base) > BASE_BUDGET:
                self.base.popitem(last=False)
            self.render_count += 1
            r = page.rect
            return QRectF(0, 0, r.width, r.height)

        _, _, level, tx, ty = key
        tkey = (index, level, tx, ty)
        entry = self.tiles.get(tkey)
        if entry is not None and entry[1] == gen:
            return None
        prect = page.rect
        step = TILE / level
        clip = fitz.Rect(tx * step, ty * step, (tx + 1) * step, (ty + 1) * step) & prect
        if clip.is_empty:
            return None
        # 1 Gerätepixel Überstand, damit zwischen Kacheln keine feinen Nahtlinien entstehen
        bleed = 1.0 / level
        clip_b = fitz.Rect(clip.x0 - bleed, clip.y0 - bleed, clip.x1 + bleed, clip.y1 + bleed) & prect
        content = self.content.get(tkey)
        if content is None:
            content = self._displaylist(index, page).get_pixmap(matrix=fitz.Matrix(level, level), clip=clip_b,
                                                                alpha=False)
            self._store_content(tkey, content)
        else:
            self.content.move_to_end(tkey)
        pix = self._with_annots(content, self._annot_displaylist(index, page, gen), level)
        qpm = _to_pixmap(pix, self.as_image)
        self._store(tkey, qpm, gen, pix.x, pix.y, level)
        self._evict()
        self.render_count += 1
        return QRectF(clip.x0, clip.y0, clip.width, clip.height)

    def _store(self, tkey, img, gen, px, py, level):
        """Kachel ablegen; (px, py) = Gerätepixel oben links (PyMuPDF rundet auf ganze Pixel)."""
        entry = self.tiles.get(tkey)
        if entry is not None:
            self.pixels -= entry[0].width() * entry[0].height()
        target = QRectF(px / level, py / level, img.width() / level, img.height() / level)
        self.tiles[tkey] = (img, gen, target, (px, py))
        self.page_version[tkey[0]] += 1
        self.tiles.move_to_end(tkey)
        self.levels[tkey[0]].add(level)
        self.pixels += img.width() * img.height()

    def _render_batch(self, key):
        """Scanseite: diese und benachbarte anstehende Kacheln mit einem Aufruf rendern und zerschneiden."""
        _, index, level, tx0, ty0 = key
        if index >= self.document.page_count:
            return None
        page = self.document.page(index)
        gen = self.gen[index]
        near = [k for k in list(self._queued) if k[0] == "tile" and k[1] == index and k[2] == level
                and abs(k[3] - tx0) <= BATCH_RADIUS and abs(k[4] - ty0) <= BATCH_RADIUS]
        for k in near:
            del self._queued[k]
        cells = [(tx0, ty0)] + [(k[3], k[4]) for k in near if self.is_relevant(k)]
        cells = [c for c in cells if not ((index, level) + c in self.tiles
                                          and self.tiles[(index, level) + c][1] == gen)]
        if not cells:
            return None
        prect = page.rect
        step = TILE / level
        bleed = 1.0 / level
        union = fitz.Rect(min(c[0] for c in cells) * step, min(c[1] for c in cells) * step,
                          (max(c[0] for c in cells) + 1) * step, (max(c[1] for c in cells) + 1) * step) & prect
        if union.is_empty:
            return None
        union_b = fitz.Rect(union.x0 - bleed, union.y0 - bleed, union.x1 + bleed, union.y1 + bleed) & prect
        mat = fitz.Matrix(level, level)
        content = self._displaylist(index, page).get_pixmap(matrix=mat, clip=union_b, alpha=False)
        pix = self._with_annots(content, self._annot_displaylist(index, page, gen), level)
        big = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888)
        for tx, ty in cells:
            clip = fitz.Rect(tx * step, ty * step, (tx + 1) * step, (ty + 1) * step) & prect
            if clip.is_empty:
                continue
            clip_b = fitz.Rect(clip.x0 - bleed, clip.y0 - bleed, clip.x1 + bleed, clip.y1 + bleed) & prect
            ir = (clip_b * mat).irect  # gleiche Pixelgrenzen wie beim Einzelrendern
            x0, y0 = max(ir.x0, pix.x), max(ir.y0, pix.y)
            x1, y1 = min(ir.x1, pix.x + pix.width), min(ir.y1, pix.y + pix.height)
            if x1 <= x0 or y1 <= y0:
                continue
            part = fitz.IRect(x0, y0, x1, y1)  # Seiteninhalt der Kachel für spätere Anmerkungsänderungen
            cpart = fitz.Pixmap(fitz.csRGB, part, False)
            cpart.copy(content, part)
            self._store_content((index, level, tx, ty), cpart)
            sub = big.copy(x0 - pix.x, y0 - pix.y, x1 - x0, y1 - y0)
            img = sub.convertToFormat(QImage.Format_RGBA8888) if self.as_image else QPixmap.fromImage(sub)
            self._store((index, level, tx, ty), img, gen, x0, y0, level)
        self._evict()
        self.render_count += len(cells)
        return QRectF(union.x0, union.y0, union.width, union.height)

    def _evict(self):
        while self.content_pixels > CONTENT_BUDGET and self.content:
            _, pix = self.content.popitem(last=False)
            self.content_pixels -= pix.width * pix.height
        while self.pixels > TILE_BUDGET and self.tiles:
            (index, level, _, _), (pm, *_) = self.tiles.popitem(last=False)
            self.page_version[index] += 1
            self.pixels -= pm.width() * pm.height()
            if not any(k[0] == index and k[1] == level for k in self.tiles):
                self.levels[index].discard(level)
