"""Hauptansicht: zeigt alle Seiten untereinander und verarbeitet Stift-/Maus-Eingaben."""
from __future__ import annotations

import math
import time

import pymupdf as fitz
from PySide6.QtCore import QByteArray, QEvent, QMimeData, QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QCursor, QEventPoint, QGuiApplication, QImage, QInputDevice, QPainter,
                           QTransform,
                           QPainterPath,
                           QPen, QPixmap, QPointingDevice)
from PySide6.QtWidgets import (QApplication, QFileDialog, QGraphicsEllipseItem, QGraphicsItem,
                               QGraphicsPathItem, QGraphicsPixmapItem,
                               QGraphicsRectItem, QGraphicsScene, QGraphicsView, QInputDialog,
                               QMenu, QWidget)

import os

from . import gestures, gpu_view, images, pen_feedback, theme
from .document import PdfDocument, _load_annot
from .renderer import P_BASE, P_PREFETCH, P_VISIBLE, TILE, TileRenderer, level_for
from .media import MediaController, kind_of as media_kind_of
from . import prefs
from .ruler import Ruler, format_length
from .touch_scroll import long_press_ms

CLIP_MIME = "application/x-pdfeditor-annots"  # kopierte Objekte (kleines PDF) in der Zwischenablage
from .text_editor import TextEditorItem

PAGE_GAP = 18.0
SCREEN_ZOOM = 96.0 / 72.0  # 100 % = Originalgröße am Bildschirm
ZOOM_SETTLE_MS = 160    # so lange nach dem letzten Zoomschritt werden scharfe Kacheln nachgeladen
PREFETCH_SCREENS = 1.0  # so viele Bildschirmhöhen über/unter der Ansicht werden vorab gerendert

HANDLE = 18      # Größe der Anfasser in Bildschirmpixeln
HANDLE_HIT = 26  # Trefferbereich der Anfasser (fingertauglich)
LONG_PRESS_MS = 600  # langes Antippen = Objekt greifen bzw. Kontextmenü
RESAMPLE_S = 0.010   # Touch-Resampling: Fingerposition so weit in der Vergangenheit interpolieren
HOLD_MS = 450        # Stift am Strichende so lange ruhig halten -> Form erkennen
HOLD_SLOP = 4        # Bildschirmpixel, die der Stift beim Halten wackeln darf
TOUCH_SLOP = 24      # so weit darf sich der Finger bewegen, ohne dass gescrollt wird

# Mit dem Finger zeichnen (Fingermodus): diese Werkzeuge
FINGER_DRAW_TOOLS = ("pen", "highlighter", "eraser", "markup", "line", "arrow", "rect", "ellipse")
TOOLS = ["select", "pan", "pen", "highlighter", "eraser", "text", "note",
         "line", "arrow", "rect", "ellipse", "markup", "image"]


class PageItem(QGraphicsItem):
    """Eine Seite. Zeichnet nur fertige Kacheln/Vorschauen und fordert fehlende beim Renderer an –
    es wird beim Zeichnen nie blockierend gerendert."""

    def __init__(self, view: "PageView", index: int, rect: fitz.Rect):
        super().__init__()
        self.view = view
        self.index = index
        self.w = rect.width
        self.h = rect.height
        self.setCacheMode(QGraphicsItem.NoCache)
        self.setFlag(QGraphicsItem.ItemUsesExtendedStyleOption)  # liefert exposedRect

    def boundingRect(self):
        return QRectF(-3, -3, self.w + 6, self.h + 6)

    def page_rect(self):
        return QRectF(0, 0, self.w, self.h)

    def invalidate(self):
        if self.view.gpu:
            self.view.viewport().update()  # Seiten zeichnet die Grafikkarte (kein Szenen-Update nötig)
        else:
            self.update()

    def paint(self, painter: QPainter, option, widget=None):
        if self.view._overlay_pass:
            return  # Grafikkarte zeichnet die Seiten selbst; hier nur die Überlagerung
        page_rect = self.page_rect()
        exposed = option.exposedRect.intersected(page_rect)
        # dünner Rand rund um die Seite (ein Schatten wäre auf dem dunklen Hintergrund unsichtbar)
        edge = QPen(theme.PAGE_EDGE, 1)
        edge.setCosmetic(True)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.setPen(edge)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(page_rect.adjusted(-0.5 / painter.worldTransform().m11(), -0.5 / painter.worldTransform().m11(),
                                            0.5 / painter.worldTransform().m11(), 0.5 / painter.worldTransform().m11()))
        painter.restore()
        if exposed.isEmpty():
            return

        r = self.view.renderer
        idx = self.index
        t = painter.worldTransform()
        dpr = widget.devicePixelRatioF() if widget else 1.0
        dev_scale = max(abs(t.m11()), abs(t.m12())) * dpr
        level = level_for(dev_scale)
        painter.setRenderHint(QPainter.Antialiasing, False)
        # während des Zoomens schnell (etwas gröber) skalieren, danach glatt
        painter.setRenderHint(QPainter.SmoothPixmapTransform, not r.zooming)

        # 1) scharfe Kacheln für den sichtbaren Bereich bestimmen und fehlende anfordern
        step = TILE / level
        tx0, tx1 = int(exposed.left() // step), int((exposed.right() - 1e-6) // step)
        ty0, ty1 = int(exposed.top() // step), int((exposed.bottom() - 1e-6) // step)
        center = self.view.visible_center() - self.pos()
        ready, missing = [], []
        for ty in range(ty0, ty1 + 1):
            for tx in range(tx0, tx1 + 1):
                entry = r.get_tile((idx, level, tx, ty))
                trect = QRectF(tx * step, ty * step, step, step).intersected(page_rect)
                if entry is not None:
                    ready.append(entry)
                    if not r.is_stale(idx, entry[1]):
                        continue
                else:
                    missing.append(trect)
                if not r.zooming:
                    d = (trect.center() - center).manhattanLength()
                    r.request(("tile", idx, level, tx, ty), (P_VISIBLE, d))

        base = r.get_base(idx)
        if base is None or r.is_stale(idx, base[1]):
            r.request(("base", idx), (P_BASE, 0.0))

        # 2) Lücken füllen: kleine Seitenvorschau, darüber Kacheln der nächstgelegenen anderen
        #    Zoomstufe (unscharf, aber nicht leer) – nur dort, wo scharfe Kacheln fehlen
        if missing:
            for m in missing:  # weißes Papier nur dort, wo (noch) keine Kachel liegt
                painter.fillRect(m, Qt.white)
            if base is not None:
                bpm = base[0]
                sx, sy = bpm.width() / self.w, bpm.height() / self.h
                for m in missing:
                    _draw(painter, m, bpm, QRectF(m.x() * sx, m.y() * sy, m.width() * sx, m.height() * sy))
            levels = r.fallback_levels(idx, level)
            if levels:
                lv = levels[0]
                bounds = QRectF(missing[0])
                for m in missing[1:]:
                    bounds = bounds.united(m)
                st = TILE / lv
                for ty in range(int(bounds.top() // st), int((bounds.bottom() - 1e-6) // st) + 1):
                    for tx in range(int(bounds.left() // st), int((bounds.right() - 1e-6) // st) + 1):
                        entry = r.tiles.get((idx, lv, tx, ty))
                        if entry is not None:
                            _draw(painter, entry[2], entry[0], QRectF(entry[0].rect()))
        if not ready:
            return

        # 3) scharfe Kacheln 1:1 auf Gerätepixel kopieren (ohne Skalierung – der schnellste Weg)
        if abs(level - dev_scale) < 1e-6 and abs(t.m12()) < 1e-9 and abs(t.m21()) < 1e-9:
            ox, oy = round(t.dx() * dpr), round(t.dy() * dpr)
            painter.save()
            painter.setWorldTransform(QTransform())
            for pm, _, _, (px, py) in ready:
                pm.setDevicePixelRatio(dpr)
                _draw(painter, QPointF((ox + px) / dpr, (oy + py) / dpr), pm)
            painter.restore()
        else:
            for pm, _, target, _ in ready:
                pm.setDevicePixelRatio(1.0)
                _draw(painter, target, pm, QRectF(pm.rect()))


def _draw(painter, target, src, src_rect=None):
    """Kachel zeichnen – QPixmap (Software-Darstellung) oder QImage (nach Rückfall von der Grafikkarte)."""
    if isinstance(src, QImage):
        if src_rect is None:
            painter.drawImage(target, src)
        else:
            painter.drawImage(target, src, src_rect)
    elif src_rect is None:
        painter.drawPixmap(target, src)
    else:
        painter.drawPixmap(target, src, src_rect)


class PageView(QGraphicsView):
    current_page_changed = Signal(int)
    zoom_changed = Signal(float)
    status_message = Signal(str)
    selection_changed = Signal(bool)
    context_changed = Signal(str)  # "", "image", "text", "note", "annot", "editing"
    text_selection_changed = Signal(bool)  # mit der Maus markierter Seitentext vorhanden?
    tool_requested = Signal(str)
    text_edit_started = Signal(float)  # Schriftgröße des geöffneten Textfelds
    interacted = Signal()  # Maus/Stift/Finger auf der Seite -> dieser Tab wird aktiv

    def __init__(self, document: PdfDocument, settings, parent=None):
        super().__init__(parent)
        self.document = document
        self.settings = settings  # ToolSettings
        self.setScene(QGraphicsScene(self))
        self.setBackgroundBrush(theme.CANVAS)
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setViewportUpdateMode(QGraphicsView.SmartViewportUpdate)
        # Bildmitte, nicht „unter der Maus“: Qt kennt die Mausposition nur aus mouseMoveEvent, das hier nicht
        # weitergereicht wird – nach Scrollen mit Finger/Stift sprang der Zoom sonst an den PDF-Anfang.
        # Strg+Mausrad zoomt gezielt um den Mauszeiger (_zoom_at).
        self.setTransformationAnchor(QGraphicsView.AnchorViewCenter)
        self.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.setMouseTracking(True)
        self.overlays: list = []  # schwebende Kinder der Ansichtsfläche (siehe scrollContentsBy)
        # Zeichenfläche: Grafikkarte (Direct3D, siehe gpu_view.py) oder normale Software-Darstellung.
        # OpenGL/Vulkan wurden gemessen und verworfen (im Fenster auf 60 Hz begrenzt, langsamer).
        self.gpu = False
        self._overlay_pass = False     # Überlagerung für die Grafikkarte wird gerade gezeichnet
        self._overlay_cache = None     # (Bild, Szenenpunkt oben links, Maßstab, Szenenbereich)
        self._overlay_dirty = True
        self.ruler = None              # Lineal (ruler.Ruler), beim ersten Einblenden angelegt
        self._ruler_drag = False       # Stift/Maus verschiebt gerade das Lineal
        self._ruler_stroke = None      # Strich an der Linealkante: [Kante, Abstand px, s_min, s_max]
        self._sb_hot = None            # Bildlaufbalken unter der Maus (Qt.Vertical/Horizontal)
        self._sb_drag = None           # (Ausrichtung, Greifabstand im Balken)
        self._page_signal_pending = False
        self._prefetch_dirty = True    # Vorab-Rendern neu planen (z. B. nach Änderungen an Seiten)
        # Grafikkarte: Striche als eigene Ebene, pro Stiftbewegung nur das neue Stück zeichnen.
        # Ein Strich {"index", "item", "pts", "color", "width", "opacity", "done", "preview"} bleibt nach dem
        # Abheben auf der Ebene, bis die Seite mit ihm gerendert ist – auch wenn schon weitergeschrieben wird.
        self._live = None              # Strich in Arbeit (oder None)
        self._layer = []               # alle Striche auf der Ebene (in Arbeit + noch nicht gerendert)
        self._layer_removed = []       # entfernte Striche: ihre Stelle auf der Ebene leeren
        self._layer_epoch = 0          # erhöht -> Ebene ganz leeren (neues Dokument, Rückfall)
        self.viewport().setAttribute(Qt.WA_AcceptTouchEvents, True)
        self.viewport().setMouseTracking(True)

        self.items_: list[PageItem] = []
        self.tool = "pen"
        self._zoom = 1.0
        self._current = 0
        self._fit_mode = "width"  # "width", "page" oder None

        # Interaktionszustand
        self._active_page: PageItem | None = None
        self._stroke_pts: list[tuple[float, float]] = []
        self._preview: QGraphicsItem | None = None
        self._start: QPointF | None = None
        self._erasing_checkpointed = False
        self._erase_last = None
        self._panning = False
        self._pan_last = None
        self._active_tool = self.tool
        self._pen_down = False
        self._touch_points: dict[int, QPointF] = {}
        self._touch_time = 0.0
        self._velocity = QPointF()
        self._scroll_rest = QPointF()
        # Bewegungstakt: Scrollen/Zoomen/Nachlaufen wird gesammelt und genau einmal pro
        # Bildschirmbild angewendet (60/120 Hz) – gleichmäßige Bewegung statt Ruckeln
        self._frame = QTimer(self)
        self._frame.setTimerType(Qt.PreciseTimer)
        self._frame.timeout.connect(self._on_frame)
        self._frame_last = 0.0
        self._pending_scroll = QPointF()
        self._pending_zoom = None      # (Faktor, Ankerpunkt im Viewport)
        self._kinetic_on = False
        # Eingaben mit Zeitstempel (Summe aller Scrollbewegungen) für gleichmäßige Bilder
        self._input_samples = []       # [(Zeit, x, y)] – aufsummierte Bewegung
        self._input_sum = QPointF()
        self._input_applied = QPointF()
        self._idle_frames = 0
        self._moving_off = QTimer(self)  # kurz nach dem Stillstand wieder größere Renderhäppchen
        self._moving_off.setSingleShot(True)
        self._moving_off.setInterval(150)
        self._moving_off.timeout.connect(self._motion_stopped)

        self._selected = None  # (page_index, xref) – bei Mehrfachauswahl das erste Objekt
        self._group = None  # Lassoauswahl mit mehreren Objekten: Liste der xrefs (alle auf _selected[0])
        self._lasso_pts = None  # Punkte des gerade gezogenen Lassos (Seitenkoordinaten)
        self._right_press = None   # Rechtsklick (Maus/Stift-Seitentaste): (Ansichtspunkt, Bildschirmpunkt, Szene)
        self._right_lasso = False  # ... daraus wurde durch Ziehen ein Lasso
        self._tsel_press = None    # Maus-Werkzeug: Textauswahl begonnen (Ansichtspunkt, Seite, Seitenpunkt)
        self._tsel = None          # aktuelle Textauswahl {"index", "rects", "text", "item"}
        self._drag_prev = None     # Vorschau beim Verschieben: {"index", "item", "pos", "xrefs", "rect"}
        self._sel_item: QGraphicsRectItem | None = None
        self._moving = False
        self._resizing = None
        self._handles: list[QGraphicsEllipseItem] = []
        self._sel_rect = QRectF()
        self._sel_image = False

        self._search_items: list[QGraphicsRectItem] = []

        # Textfeld, das gerade direkt auf der Seite bearbeitet wird
        self._editor: TextEditorItem | None = None
        self._editor_state = None  # (seite, xref oder None, alter Text, alte Größe, alte Farbe, Änderungsstatus)
        self._editor_mouse = False
        self._committing = False
        self._last_tap = (0.0, None)  # (Zeit, xref) für Doppeltipp mit dem Stift

        # Langes Antippen (Finger oder Stift) -> Kontextmenü
        self._long_timer = QTimer(self)
        self._long_timer.setSingleShot(True)
        self._long_timer.setInterval(LONG_PRESS_MS)
        self._long_timer.timeout.connect(self._long_press)
        self._long_origin = QPointF()
        self._long_global = None
        self._long_source = "touch"
        self._suppress_until_release = False
        self._menu_pending = None  # (globale Position, Szenenposition) -> Menü beim Loslassen
        # Links (Maus-Werkzeug): je Seite [(Rechteck in Anzeige-Koordinaten, Link)], Rücksprung-Verlauf
        self._links = {}
        self._link_press = None    # (Viewport-Position, Link) beim Drücken auf einen Link
        self._history = []         # (Seite, Abstand der Ansicht-Oberkante zum Seitenanfang) vor Sprüngen
        self.media = MediaController(self)  # Video/Audio abspielen, GIFs animieren
        self._note_bubble = None             # Notiz zum Lesen (ohne Bearbeiten)
        self._note_hover_t = 0.0
        # nach einer Fingerberührung zeigt Windows für den Stift einen anderen Zeiger (kleines Fadenkreuz),
        # bis der Stift den Bildschirm verlässt – bei Stift-/Mausbewegungen prüfen und neu setzen
        self._cursor_checked = 0.0
        self._touch_mode = None
        self._touch_origin = QPointF()
        self._touch_last = QPointF()
        self._grip_drag = None  # Textfeld-Griff wird gezogen
        self._manip_started = False
        self._manip_from_long = False
        self._annot_op = False

        # Gesten beim Zeichnen: Halten am Ende -> Linie/Kreis/Rechteck
        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.setInterval(HOLD_MS)
        self._hold_timer.timeout.connect(self._on_pen_hold)
        self._hold_anchor = (0.0, 0.0)
        # None | ("line", start, ende) | ("rect"|"ellipse", box beim Erkennen, Griffpunkt, Fixpunkt, aktuelle box)
        self._gesture = None

        # Rendern in Kacheln im Leerlauf (flüssiges Scrollen)
        self.renderer = TileRenderer(document, self._render_relevant, self)
        self.renderer.batch_heavy = settings.qs.value("scan_batch", "true") in (True, "true")
        self.renderer.tile_ready.connect(self._on_tile_ready)
        self._zoom_settle = QTimer(self)
        self._zoom_settle.setSingleShot(True)
        self._zoom_settle.setInterval(ZOOM_SETTLE_MS)
        self._zoom_settle.timeout.connect(self._zoom_settled)
        self._ghosts: dict[int, list] = {}  # Vorschau-Striche, bis die Seite neu gerendert ist

        document.changed.connect(self._on_doc_changed)
        document.about_to_modify.connect(self.commit_text_edit)
        document.page_changed.connect(self._on_page_changed)
        self.verticalScrollBar().valueChanged.connect(self._update_current_page)
        self.scene().changed.connect(self._on_scene_changed)
        # verdeckter Tab: nach einer Weile Kacheln/Texturen freigeben (viele offene Tabs -> wenig Speicher)
        self._release_timer = QTimer(self)
        self._release_timer.setSingleShot(True)
        self._release_timer.setInterval(30_000)
        self._release_timer.timeout.connect(self._release_caches)
        self._setup_gpu()

    # Die Szene nimmt Ziehen/Ablegen sonst an und schluckt es: Tabs und Dateien sollen bis zur
    # Tab-Gruppe bzw. zum Hauptfenster durchgereicht werden
    def dragEnterEvent(self, event):
        event.ignore()

    def dragMoveEvent(self, event):
        event.ignore()

    def dropEvent(self, event):
        event.ignore()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._release_timer.start()

    def showEvent(self, event):
        super().showEvent(event)
        self._release_timer.stop()

    def _release_caches(self):
        if self.isVisible():
            return
        self.renderer.drop_tiles()
        if self.gpu:
            self.viewport().drop_textures()
        self._prefetch_dirty = True

    # ================================================================ Zeichenfläche
    def _setup_gpu(self):
        """Grafikkarten-Darstellung einschalten, wenn verfügbar (Einstellung „renderer“: auto/d3d11/d3d12/raster)."""
        mode = os.environ.get("PDFEDITOR_RENDERER") or str(self.settings.qs.value("renderer", "auto"))
        if mode == "raster" or os.name != "nt" or QGuiApplication.platformName() != "windows":
            return
        api = mode if mode in gpu_view.APIS else "d3d11"
        vp = gpu_view.GpuViewport(self, api)
        vp.renderFailed.connect(lambda: QTimer.singleShot(0, self._gpu_failed))
        vp.device_lost.connect(self._gpu_lost)
        self.gpu = True
        self.renderer.as_image = True
        self._swap_viewport(vp)
        # Ein Widget, das sich bei jedem Bild neu zeichnet (z. B. ein normaler Bildlaufbalken),
        # würde das ganze Fenster ausbremsen -> Balken zeichnet die Grafikkarte, Updates immer komplett
        self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

    def _gpu_lost(self):
        """Grafiktreiber wurde neu gestartet: sofort normale Darstellung (nichts mehr mit dem verlorenen Gerät tun),
        nach ein paar Sekunden die Grafikkarte noch einmal versuchen (höchstens zweimal pro Sitzung)."""
        self._gpu_failed()
        self.status_message.emit("Der Grafiktreiber wurde neu gestartet – vorübergehend normale Darstellung.")
        self._gpu_retries = getattr(self, "_gpu_retries", 0) + 1
        if self._gpu_retries <= 2:
            QTimer.singleShot(6000, self._gpu_retry)

    def _gpu_retry(self):
        if not self.gpu:
            self.renderer.clear()  # Kacheln der normalen Darstellung passen nicht zur Grafikkarte
            self._setup_gpu()
            if self.gpu:
                self.viewport().update()
                QTimer.singleShot(3000, self._gpu_verify)

    def _gpu_verify(self):
        """Nach dem erneuten Versuch: zeichnet die Grafikkarte wirklich wieder? Sonst normale Darstellung."""
        vp = self.viewport()
        if not self.gpu or not hasattr(vp, "frame_count"):
            return
        if vp.frame_count > 0 and vp._rhi is not None and not vp._device_lost():
            gpu_view.log("GPU: wieder aktiv")
            self.status_message.emit("Grafikkarten-Darstellung wieder aktiv.")
        else:
            gpu_view.log("GPU: erneuter Versuch zeichnet nicht – normale Darstellung")
            self._gpu_lost()

    def _gpu_failed(self):
        """Grafikkarte nicht nutzbar -> zurück zur normalen Darstellung."""
        if not self.gpu:
            return
        self.gpu = False
        self.renderer.as_image = False
        self.renderer.clear()
        self._overlay_cache = None
        for s in self._layer:  # Striche der Ebene zeigt jetzt wieder die Vorschau
            if s["preview"].scene() is not None:
                s["preview"].setVisible(True)
        self._layer, self._layer_removed, self._live = [], [], None
        self._swap_viewport(QWidget())
        self.setViewportUpdateMode(QGraphicsView.SmartViewportUpdate)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.viewport().update()
        self.status_message.emit("Grafikbeschleunigung nicht verfügbar – normale Darstellung.")

    def _swap_viewport(self, widget):
        if hasattr(self, "media"):
            self.media.stop()  # Player sitzt auf der alten Zeichenfläche, die gleich gelöscht wird
        for o in self.overlays:  # schwebende Kästchen gehören zur Zeichenfläche -> mitnehmen
            o.setParent(None)
        self.setViewport(widget)
        widget.setAttribute(Qt.WA_AcceptTouchEvents, True)
        widget.setMouseTracking(True)
        for o in self.overlays:
            o.setParent(widget)
            if hasattr(o, "refresh"):
                o.refresh()
            else:
                o.show()
        self.update_cursor()

    def _on_scene_changed(self, _regions):
        if self.gpu:
            self._overlay_dirty = True
            self.viewport().update()

    def drawBackground(self, painter, rect):
        if self._overlay_pass:
            return  # Überlagerung: durchsichtig, Hintergrund und Seiten kommen von der Grafikkarte
        super().drawBackground(painter, rect)

    def _overlay_image(self, dpr):
        """Bild aller Szenenelemente außer den Seiten (für die Grafikkarte), an Szenenkoordinaten gebunden.
        Gibt (Bild, Szenenpunkt oben links, neu gezeichnet?) zurück oder None."""
        items = [it for it in self.scene().items() if not isinstance(it, PageItem) and it.isVisible()]
        if not items:
            self._overlay_cache = None
            return None
        vt = self.viewportTransform()
        scale = vt.m11()
        region = QRectF()
        for it in items:
            region = region.united(it.sceneBoundingRect())
        pad = (HANDLE + 4) / scale  # Anfasser werden unabhängig vom Zoom in Bildschirmpixeln gezeichnet
        region = region.adjusted(-pad, -pad, pad, pad)
        visible = self.visible_scene_rect()
        m = visible.height() * 0.5
        region = region.intersected(visible.adjusted(-m, -m, m, m))
        if region.isEmpty():
            return None
        c = self._overlay_cache
        if c is not None and not self._overlay_dirty and abs(c[2] - scale) < 1e-9 and c[3].contains(region):
            return c[0], c[1], False
        img, src = gpu_view.render_overlay_image(self, vt.mapRect(region), dpr)
        inv = vt.inverted()[0]
        tl = inv.map(QPointF(src.topLeft()))
        covered = inv.mapRect(QRectF(src))
        self._overlay_cache = (img, tl, scale, covered)
        self._overlay_dirty = False
        return img, tl, True

    # ---- Bildlaufbalken der Grafikkarten-Darstellung
    def _scroll_thumb(self, orient):
        hot = self._sb_hot == orient or (self._sb_drag is not None and self._sb_drag[0] == orient)
        r = gpu_view.scroll_thumb_rect(self, orient, hot)
        return None if r is None else (r, hot)

    def _sb_zone(self, vpos: QPointF):
        """Liegt der Punkt im Bereich eines Bildlaufbalkens? -> Ausrichtung oder None."""
        if not self.gpu:
            return None
        vp = self.viewport()
        if not QRectF(vp.rect()).contains(vpos):
            return None
        edge = gpu_view.SCROLLBAR_HOVER + 2 * gpu_view.SCROLLBAR_MARGIN + 2
        if vpos.x() >= vp.width() - edge and gpu_view.scroll_thumb_rect(self, Qt.Vertical, True) is not None:
            return Qt.Vertical
        if vpos.y() >= vp.height() - edge and gpu_view.scroll_thumb_rect(self, Qt.Horizontal, True) is not None:
            return Qt.Horizontal
        return None

    def _sb_press(self, vpos: QPointF):
        orient = self._sb_zone(vpos)
        if orient is None:
            return False
        r = gpu_view.scroll_thumb_rect(self, orient, True)
        along = vpos.y() if orient == Qt.Vertical else vpos.x()
        start, length = (r.top(), r.height()) if orient == Qt.Vertical else (r.left(), r.width())
        grab = along - start if start <= along <= start + length else length / 2  # daneben: Balken springt hin
        self._stop_kinetic()
        self._sb_drag = (orient, grab)
        self._sb_move(vpos)
        return True

    def _sb_move(self, vpos: QPointF):
        orient, grab = self._sb_drag
        sb = self.verticalScrollBar() if orient == Qt.Vertical else self.horizontalScrollBar()
        r = gpu_view.scroll_thumb_rect(self, orient, True)
        vp = self.viewport()
        total = (vp.height() if orient == Qt.Vertical else vp.width()) - 2 * gpu_view.SCROLLBAR_MARGIN
        thumb = r.height() if orient == Qt.Vertical else r.width()
        along = (vpos.y() if orient == Qt.Vertical else vpos.x()) - grab - gpu_view.SCROLLBAR_MARGIN
        free = max(1.0, total - thumb)
        sb.setValue(round(sb.minimum() + (sb.maximum() - sb.minimum()) * min(max(along / free, 0.0), 1.0)))
        self.viewport().update()

    def _sb_hover(self, vpos: QPointF):
        hot = self._sb_zone(vpos)
        if hot != self._sb_hot:
            self._sb_hot = hot
            self.viewport().update()
            if hot is not None:
                self.viewport().setCursor(Qt.ArrowCursor)
            else:
                self.update_cursor()
        return hot is not None

    def _motion_stopped(self):
        self.renderer.moving = False
        if self._page_signal_pending:
            self._page_signal_pending = False
            self.current_page_changed.emit(self._current)

    # ================================================================ Aufbau
    def _on_doc_changed(self, structural: bool):
        # Szene wird neu aufgebaut -> ein offenes Textfeld geht verloren (wurde vorher übernommen)
        self._editor = None
        self._editor_state = None
        self._grip_drag = None
        self._touch_mode = None
        keep = self._current
        self.clear_selection()
        self.clear_search()
        self._links = {}
        if structural:
            self._history = []
        anchor = self._view_anchor()
        self.rebuild()
        if self.items_:
            if anchor is not None and anchor[0] < len(self.items_):
                self._restore_anchor(anchor)  # z. B. nach Rückgängig: an derselben Stelle bleiben
            else:
                self.goto_page(min(keep, len(self.items_) - 1))

    def _view_anchor(self):
        """Sichtbare Stelle merken: (Seite am oberen Rand, Abstand darin in pt, waagerechte Lage)."""
        if not self.items_:
            return None
        top = self.mapToScene(QPoint(self.viewport().width() // 2, 0))
        item = next((it for it in self.items_ if it.pos().y() + it.h > top.y()), self.items_[-1])
        return item.index, top.y() - item.pos().y(), self.horizontalScrollBar().value()

    def _restore_anchor(self, anchor):
        idx, dy, hval = anchor
        item = self.items_[idx]
        self._stop_kinetic()
        self._pending_scroll = QPointF()
        target = QPointF(0, item.pos().y() + min(max(dy, -PAGE_GAP), item.h))
        sb = self.verticalScrollBar()
        sb.setValue(sb.value() + self.mapFromScene(target).y())
        self.horizontalScrollBar().setValue(hval)
        self._update_current_page()

    def _on_page_changed(self, index: int):
        self._prefetch_dirty = True
        if hasattr(self, "media"):
            self.media.reposition()
        r = self.document.changed_rect  # nur dieser Bereich muss neu gerendert werden (None = ganze Seite)
        self.renderer.invalidate_page(index, QRectF(r.x0, r.y0, r.width, r.height) if r is not None else None)
        if 0 <= index < len(self.items_):
            self.items_[index].invalidate()
        if self._selected and self._selected[0] == index and not self._annot_op:
            self._update_selection_box()

    def rebuild(self):
        self._prefetch_dirty = True
        scene = self.scene()
        scene.clear()
        self.renderer.clear()
        self._ghosts = {}
        self._live = None
        self._layer, self._layer_removed = [], []
        self._layer_epoch += 1
        self._sel_item = None
        self._preview = None
        self._search_items = []
        self.items_ = []
        y = PAGE_GAP
        maxw = 0.0
        for i in range(self.document.page_count):
            rect = self.document.page(i).rect
            item = PageItem(self, i, rect)
            item.setPos(-rect.width / 2, y)
            scene.addItem(item)
            self.items_.append(item)
            y += rect.height + PAGE_GAP
            maxw = max(maxw, rect.width)
        scene.setSceneRect(QRectF(-maxw / 2 - PAGE_GAP, 0, maxw + 2 * PAGE_GAP, y))
        if self._fit_mode:
            QTimer.singleShot(0, lambda: self.fit(self._fit_mode))
        if hasattr(self, "media"):
            self.media.reset()
        if self._tsel is not None:
            self._tsel = None  # Seiten neu aufgebaut: Markierung gehörte zu den alten
            self.text_selection_changed.emit(False)

    # ================================================================ Zoom/Navigation
    @property
    def zoom(self):
        return self._zoom

    def set_zoom(self, z, keep_fit=False):
        z = max(0.1, min(z, 16.0))
        self._zoom = z
        if not keep_fit:
            self._fit_mode = None
        if abs(self.transform().m11() - z * SCREEN_ZOOM) > 1e-9:
            # Während des Zoomens nur vorhandene Bilder skalieren, danach scharf nachrendern
            self.renderer.zooming = True
            self._zoom_settle.start()
        self.setTransform(self.transform().fromScale(z * SCREEN_ZOOM, z * SCREEN_ZOOM))
        if hasattr(self, "media"):
            self.media.reposition()
        if getattr(self, "_note_bubble", None) is not None:
            self._place_note()
        self.zoom_changed.emit(z)
        self.update_cursor()

    # ================================================================ Rendern
    def _zoom_settled(self):
        self.renderer.zooming = False
        self.viewport().update()

    def _device_scale(self):
        return self._zoom * SCREEN_ZOOM * self.devicePixelRatioF()

    def visible_scene_rect(self, margin_screens=0.0):
        r = self.mapToScene(self.viewport().rect()).boundingRect()
        if margin_screens:
            m = r.height() * margin_screens
            r = r.adjusted(0, -m, 0, m)
        return r

    def visible_center(self):
        return self.mapToScene(self.viewport().rect().center())

    def _render_relevant(self, key):
        """Lohnt sich ein Renderauftrag noch? (Seite noch in der Nähe, Zoomstufe noch aktuell)"""
        idx = key[1]
        if idx >= len(self.items_):
            return False
        item = self.items_[idx]
        area = self.visible_scene_rect(PREFETCH_SCREENS + 0.5)
        if key[0] == "base":
            return area.intersects(item.sceneBoundingRect())
        _, _, level, tx, ty = key
        if self.renderer.zooming or level != level_for(self._device_scale()):
            return False
        step = TILE / level
        return area.intersects(QRectF(tx * step, ty * step, step, step).translated(item.pos()))

    def _on_tile_ready(self, index, rect):
        if 0 <= index < len(self.items_):
            if self.gpu:
                self.viewport().update()
            else:
                self.items_[index].update(rect.adjusted(-1, -1, 1, 1))
            if index in self._ghosts and self._page_fresh(index):
                self._drop_ghosts(index)

    def _page_fresh(self, index):
        """Sind alle sichtbaren Kacheln dieser Seite auf dem neuesten Stand?"""
        r = self.renderer
        item = self.items_[index]
        part = self.visible_scene_rect().intersected(item.sceneBoundingRect()).translated(-item.pos())
        part = part.intersected(item.page_rect())
        if part.isEmpty():
            return True
        level = level_for(self._device_scale())
        step = TILE / level
        for ty in range(int(part.top() // step), int((part.bottom() - 1e-6) // step) + 1):
            for tx in range(int(part.left() // step), int((part.right() - 1e-6) // step) + 1):
                entry = r.tiles.get((index, level, tx, ty))
                if entry is None or r.is_stale(index, entry[1]):
                    return False
        return True

    def paintEvent(self, event):
        super().paintEvent(event)
        self._prefetch()

    def scrollContentsBy(self, dx, dy):
        # Schwebende Elemente (falls vorhanden) sind Kinder der Ansichtsfläche. Würden sie
        # über ihr liegen, müsste Qt bei jedem Scrollschritt alles neu zeichnen; als Kinder wird
        # der Inhalt einfach verschoben – danach die Elemente an ihren festen Platz zurücksetzen.
        super().scrollContentsBy(dx, dy)
        for o in self.overlays:
            o.move(o.home)
        if hasattr(self, "media"):
            self.media.reposition()
        if getattr(self, "_note_bubble", None) is not None:
            self._place_note()  # Sprechblase wandert mit der Notiz mit (bleibt offen)
        if self.ruler_visible() and not self.gpu:
            self.viewport().update()  # Lineal bleibt stehen – verschobene Pixel enthielten es an alter Stelle

    def resizeEvent(self, event):
        super().resizeEvent(event)
        for o in self.overlays:
            o.place()
        if self.ruler is not None:
            self.ruler.keep_inside(self.viewport().width(), self.viewport().height())
        if self._fit_mode:
            if self._fit_mode == "width" and event.size().width() == event.oldSize().width():
                return  # nur die Höhe hat sich geändert (z. B. Leiste ein-/ausgeblendet): Zoom bleibt
            self.fit(self._fit_mode, keep_position=True)

    def _overlay_button_at(self, vpos: QPointF):
        """Knopf eines schwebenden Elements unter dem Finger (oder None)."""
        w = self.viewport().childAt(QPointF(vpos).toPoint())
        while w is not None and w is not self.viewport():
            if w in self.overlays:
                return None
            if hasattr(w, "click") and any(o.isAncestorOf(w) for o in self.overlays):
                return w
            w = w.parentWidget()
        return None

    def _prefetch(self):
        """Seiten knapp außerhalb der Ansicht vorab rendern (Vorschau + Kacheln)."""
        if not self.items_:
            return
        r = self.renderer
        vis = self.visible_scene_rect()
        # nur neu planen, wenn sich die Ansicht um mehr als eine halbe Kachel bewegt hat (spart pro Bild Zeit)
        key = (round(vis.left() * self._device_scale() / (TILE / 2)), round(vis.top() * self._device_scale() / (TILE / 2)),
               round(self._device_scale(), 6), r.zooming, len(r._queued) == 0)
        if key == getattr(self, "_prefetch_key", None) and not self._prefetch_dirty:
            return
        self._prefetch_key = key
        self._prefetch_dirty = False
        area = self.visible_scene_rect(PREFETCH_SCREENS)
        level = level_for(self._device_scale())
        step = TILE / level
        center = vis.center()
        for item in self.items_:
            br = item.sceneBoundingRect()
            if not area.intersects(br):
                if br.top() > area.bottom():
                    break
                continue
            idx = item.index
            dist = abs(br.center().y() - center.y())
            base = r.base.get(idx)
            if base is None or r.is_stale(idx, base[1]):
                r.request(("base", idx), (P_PREFETCH, dist))
            if r.zooming:
                continue
            part = area.intersected(br).translated(-item.pos()).intersected(item.page_rect())
            if part.isEmpty():
                continue
            for ty in range(int(part.top() // step), int((part.bottom() - 1e-6) // step) + 1):
                for tx in range(int(part.left() // step), int((part.right() - 1e-6) // step) + 1):
                    entry = r.tiles.get((idx, level, tx, ty))
                    if entry is None or r.is_stale(idx, entry[1]):
                        trect = QRectF(tx * step, ty * step, step, step).translated(item.pos())
                        r.request(("tile", idx, level, tx, ty), (P_PREFETCH, abs(trect.center().y() - center.y())))

    def _retire_preview(self, index):
        """Vorschau-Strich stehen lassen, bis die Seite mit dem neuen Strich neu gerendert ist
        (sonst blitzt der Strich kurz weg)."""
        if self._preview is None:
            return
        if self._live is not None and self._live["preview"] is self._preview:
            self._live["done"] = True  # Strich-Ebene zeigt ihn weiter, bis die Seite fertig ist
            self._live = None
        ghost = self._preview
        self._ghosts.setdefault(index, []).append(ghost)
        self._preview = None
        QTimer.singleShot(4000, lambda: self._drop_ghost(index, ghost))  # Sicherheitsnetz (nur dieser Strich)

    def _drop_ghost(self, index, ghost):
        """Einen einzelnen Strich entfernen (falls die Seite nach Sekunden noch nicht neu gerendert ist)."""
        self._layer_remove([s for s in self._layer if s["done"] and s["preview"] is ghost])
        ghosts = self._ghosts.get(index, [])
        if any(g is ghost for g in ghosts):
            ghosts[:] = [g for g in ghosts if g is not ghost]
            if ghost.scene():
                ghost.scene().removeItem(ghost)

    def _layer_remove(self, strokes):
        for s in strokes:
            if any(x is s for x in self._layer):
                self._layer = [x for x in self._layer if x is not s]
                self._layer_removed.append(s)
        if strokes:
            self.viewport().update()

    def _drop_ghosts(self, index):
        self._layer_remove([s for s in self._layer if s["done"] and s["index"] == index])
        for item in self._ghosts.pop(index, []):
            if item.scene():
                item.scene().removeItem(item)

    def zoom_by(self, factor):
        self.set_zoom(self._zoom * factor)

    def fit(self, mode, keep_position=False):
        """Zoom an Seitenbreite bzw. ganze Seite anpassen. Die sichtbare Stelle bleibt erhalten:
        Seitenbreite zoomt um die Bildmitte, „Ganze Seite“ zeigt die Seite in der Bildmitte vollständig."""
        if not self.items_:
            return
        center = self.visible_center()
        item = min(self.items_, key=lambda it: 0 if it.page_rect().translated(it.pos()).contains(center) else
                   abs(it.pos().y() + it.h / 2 - center.y()))  # Seite in der Bildmitte (sonst die nächste)
        vw = self.viewport().width() - 2 * PAGE_GAP * 1.5
        vh = self.viewport().height() - 2 * PAGE_GAP
        if mode == "width":
            # Seite samt Rand (Szenenbreite = breiteste Seite + 2 Abstände) passt ganz hinein -> kein Querlauf
            maxw = max(it.w for it in self.items_)
            z = (self.viewport().width() - 2) / (maxw + 3 * PAGE_GAP)
        else:
            z = min(vw / item.w, vh / item.h)
        self._fit_mode = mode
        anchor = self.mapToScene(QPoint(self.viewport().width() // 2, 0)) if keep_position else center
        self.set_zoom(z / SCREEN_ZOOM, keep_fit=True)
        self._stop_kinetic()
        self._pending_scroll = QPointF()
        sb = self.verticalScrollBar()
        if keep_position:  # Fenstergröße geändert: oberer Rand bleibt
            sb.setValue(sb.value() + self.mapFromScene(anchor).y())
        elif mode == "width":  # Stelle in der Bildmitte bleibt in der Mitte
            sb.setValue(sb.value() + self.mapFromScene(anchor).y() - self.viewport().height() // 2)
        else:  # ganze Seite mittig zeigen
            mid = self.mapFromScene(QPointF(item.pos().x() + item.w / 2, item.pos().y() + item.h / 2))
            sb.setValue(sb.value() + mid.y() - self.viewport().height() // 2)


    def goto_page(self, index):
        if not (0 <= index < len(self.items_)):
            return
        # Nachlaufen und noch ausstehende Bewegung verwerfen – sonst rutscht die Zielseite weg
        self._stop_kinetic()
        self._pending_scroll = QPointF()
        self._input_samples.clear()
        self._input_applied = QPointF(self._input_sum)
        item = self.items_[index]
        top = self.mapFromScene(QPointF(0, item.y() - PAGE_GAP / 2)).y()
        self.verticalScrollBar().setValue(self.verticalScrollBar().value() + top)
        self._current = index
        self.current_page_changed.emit(index)

    def current_page(self):
        return self._current

    def _update_current_page(self):
        if not self.items_:
            return
        center = self.mapToScene(self.viewport().rect().center()).y()
        top = self.mapToScene(0, 0).y()
        # Seite, die den oberen Bereich des Fensters füllt
        probe = top + (center - top) * 0.5
        idx = 0
        for item in self.items_:
            if item.y() <= probe:
                idx = item.index
            else:
                break
        if idx != self._current:
            self._current = idx
            if self.gpu and self.renderer.moving:
                # erst nach der Bewegung melden: jede Änderung außerhalb der Seitenfläche (Seitenzahl,
                # Miniaturen) kostet während des Scrollens ein ganzes Bild
                self._page_signal_pending = True
            else:
                self.current_page_changed.emit(idx)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            factor = 1.0015 ** event.angleDelta().y()
            self._queue_zoom(factor, event.position())  # um den Mauszeiger, im nächsten Bildtakt
            event.accept()
        elif self.ruler_visible() and self.ruler.contains(event.position()):
            self.ruler.begin_rotate()
            self.ruler.rotate_by(-event.angleDelta().y() / 120, snap=False)  # 1° je Raste
            self.viewport().update()
            event.accept()
        else:
            super().wheelEvent(event)

    def event(self, event):
        if event.type() == QEvent.NativeGesture and event.gestureType() == Qt.ZoomNativeGesture:
            vpos = QPointF(self.viewport().mapFromGlobal(event.globalPosition().toPoint()))
            self._queue_zoom(1 + event.value(), vpos)
            return True
        return super().event(event)

    # ================================================================ Hilfen
    def page_at(self, scene_pos: QPointF) -> PageItem | None:
        for item in self.items_:
            if item.page_rect().contains(scene_pos - item.pos()):
                return item
        return None

    def _local(self, item: PageItem, scene_pos: QPointF):
        p = scene_pos - item.pos()
        x = min(max(p.x(), 0.0), item.w)
        y = min(max(p.y(), 0.0), item.h)
        return (x, y)

    def set_tool(self, tool):
        self.clear_text_selection()
        if tool != self.tool:
            self.commit_text_edit()
        self._cancel_interaction()
        self.tool = tool
        self.close_note()
        if tool != "select":
            self.clear_selection()
        self.update_cursor()
        if hasattr(self, "media"):
            self.media.refresh()  # GIFs laufen im Maus- und Auswahl-Werkzeug

    def _check_system_cursor(self, event):
        """Stift/Maus über der Seite: zeigt Windows wirklich unseren Zeiger? Sonst neu setzen
        (höchstens alle 80 ms prüfen; Fingereingaben ausgenommen)."""
        now = time.monotonic()
        if now - getattr(self, "_cursor_checked", 0.0) < 0.08:
            return
        dev = event.device() if hasattr(event, "device") else None
        if dev is not None and dev.type() == QInputDevice.DeviceType.TouchScreen:
            return
        self._cursor_checked = now
        cur = self.viewport().cursor()
        if pen_feedback.cursor_is_wrong(cur.shape()):
            # Qts eigener Aufruf kommt in diesem Zustand nicht bei Windows an -> direkt setzen
            if not pen_feedback.force_cursor(cur) and not getattr(self, "_cursor_fix_pending", False):
                self._cursor_fix_pending = True
                QTimer.singleShot(40, self._reapply_cursor)

    def _reapply_cursor(self):
        """Zeiger kurz wechseln und zurücksetzen – zwingt Windows, ihn neu zu übernehmen."""
        self._cursor_fix_pending = False
        vp = self.viewport()
        cur = vp.cursor()
        vp.setCursor(Qt.BlankCursor if cur.shape() != Qt.BlankCursor else Qt.ArrowCursor)
        vp.setCursor(cur)

    def update_cursor(self):
        """Stift/Marker/Radierer: Kreis in Strichdicke statt Fadenkreuz. Radierende des Stifts in der Nähe:
        immer der Radierer-Kreis (sonst sähe man den Punkt des Stifts, radiert aber in Radierergröße)."""
        tool = "eraser" if getattr(self, "_eraser_end", False) else self.tool
        if tool in ("pen", "highlighter", "eraser"):
            scale = self._zoom * SCREEN_ZOOM
            width = self.settings.width(tool)
            if tool == "eraser":
                width = max(0.25, width)  # eingestellte Größe = Durchmesser des Kreises = Radierbereich
            self.viewport().setCursor(_circle_cursor(
                width * scale, QColor(self.settings.color_hex(tool)) if tool != "eraser" else None,
                filled=tool == "highlighter", dpr=self.devicePixelRatioF()))
            return
        cursors = {"select": Qt.ArrowCursor, "pan": Qt.ArrowCursor, "text": Qt.IBeamCursor}
        self.viewport().setCursor(cursors.get(tool, Qt.CrossCursor))

    # ================================================================ Lineal
    def ruler_visible(self):
        return self.ruler is not None and self.ruler.visible

    def set_ruler_visible(self, on):
        if on == self.ruler_visible():
            return
        vp = self.viewport()
        if on:
            if self.ruler is None:
                self.ruler = Ruler()
                self.ruler.place(vp.width(), vp.height())
            else:
                self.ruler.keep_inside(vp.width(), vp.height())
            self.ruler.visible = True
        else:
            self.ruler.visible = False
            self._ruler_drag = False
        vp.update()

    def _ruler_edge(self, vpos, tool):
        """Längskante des Lineals, an der ein Strich mit diesem Werkzeug beginnt (0 = keine)."""
        if not self.ruler_visible() or tool not in ("pen", "highlighter"):
            return 0
        return self.ruler.edge_at(vpos)

    def _ruler_begin_stroke(self, vpos, tool, edge):
        """Strich an der Linealkante: alle Punkte werden auf die Kante gezogen (gerade Linie, beliebiger Winkel),
        die Länge steht dabei auf dem Lineal. Der Strich liegt mit halber Dicke außen an der Kante."""
        offset = self.settings.width(tool) * self.transform().m11() / 2 + 0.5
        p = self.ruler.project(vpos, edge, offset)
        self._press(self._to_scene(p), tool, p)
        if self._active_page is None:
            return
        self._hold_timer.stop()  # keine Formerkennung durch Halten
        s = self.ruler.to_local(p)[0]
        self._ruler_stroke = [edge, offset, s, s]
        self.ruler.readout = format_length(0)
        self.viewport().update()

    def _ruler_map(self, vpos):
        """Position während eines Strichs an der Kante auf die Kante ziehen und den Messwert nachführen."""
        st = self._ruler_stroke
        if st is None:
            return vpos
        p = self.ruler.project(vpos, st[0], st[1])
        s = self.ruler.to_local(p)[0]
        st[2], st[3] = min(st[2], s), max(st[3], s)
        self.ruler.readout = format_length((st[3] - st[2]) / self.transform().m11())
        self.viewport().update()
        return p

    def _finish_ruler_stroke(self, item, tool):
        """Strich an der Kante fertig: als gerade Linie zwischen den äußersten Punkten speichern."""
        raw = self._stroke_pts
        self._stroke_pts = []
        self._ruler_stroke = None
        self.ruler.readout = None
        u, _ = self.ruler.axes()
        ts = [x * u.x() + y * u.y() for x, y in raw]
        a, b = raw[ts.index(min(ts))], raw[ts.index(max(ts))]
        pts = [a, b] if a != b else [a]
        if self._live is not None and any(x is self._live for x in self._layer) and len(pts) > 1:
            line = dict(self._live, pts=[tuple(p) for p in pts])  # Ebene zeigt gleich, was im PDF steht
            self._layer_remove([self._live])
            self._layer.append(line)
            self._live = line
        color, width, opacity = self._stroke_style(tool, as_float=True)
        self.document.add_ink(item.index, [pts], color, width, opacity)
        length = math.dist(a, b)
        if length > 0:
            self.status_message.emit(f"Linie: {format_length(length)}")
        self._retire_preview(item.index)
        self.viewport().update()

    def _ruler_grab(self, vpos):
        """Stift/Maus auf dem Lineal (nicht an der Kante): verschieben. True = übernommen."""
        if not self.ruler_visible() or not self.ruler.contains(vpos):
            return False
        self.ruler.begin_drag(vpos)
        self._ruler_drag = True
        return True

    def _ruler_drag_to(self, vpos):
        self.ruler.drag_to(vpos)
        self.viewport().update()

    def _ruler_drop(self):
        self.ruler.end_drag()
        self._ruler_drag = False

    def drawForeground(self, painter, rect):
        super().drawForeground(painter, rect)
        if self._overlay_pass or not self.ruler_visible():
            return  # Grafikkarte: zeichnet das Lineal selbst (gpu_view._draw_ruler)
        painter.save()
        painter.resetTransform()
        painter.setRenderHint(QPainter.Antialiasing)
        self.ruler.paint(painter, self.transform().m11(), self.devicePixelRatioF())
        painter.restore()

    # ================================================================ Stift (Tablet)
    # Stift  -> schreibt/zeichnet mit dem aktiven Werkzeug
    # Finger -> scrollt/zoomt; Tippen = Klick; langes Antippen = Objekt greifen bzw. Kontextmenü
    # Maus   -> wie Stift (für Arbeit ohne Touchscreen)
    def viewportEvent(self, event):
        t = event.type()
        if t in (QEvent.TabletMove, QEvent.TabletPress):
            end = event.pointerType() == QPointingDevice.PointerType.Eraser
            if end != getattr(self, "_eraser_end", False):
                self._eraser_end = end  # Stift umgedreht bzw. zurückgedreht
                self.update_cursor()
        elif t == QEvent.MouseMove and getattr(self, "_eraser_end", False) and \
                event.source() == Qt.MouseEventNotSynthesized and not QApplication.mouseButtons():
            self._eraser_end = False  # echte Maus: wieder der Zeiger des Werkzeugs
            self.update_cursor()
        if t in (QEvent.MouseButtonPress, QEvent.TabletPress, QEvent.TouchBegin):
            self.interacted.emit()
        if getattr(self, "_note_bubble", None) is not None:
            self._note_close_check(event, t)
        if t == QEvent.MouseMove and not QApplication.mouseButtons() and hasattr(self, "_note_hover_t"):
            self._note_hover(event)
        if getattr(self, "tool", None) == "pan" and t in (QEvent.MouseMove, QEvent.TabletMove, QEvent.HoverMove) \
                and not self._panning and not QApplication.mouseButtons():
            self._link_hover(event.position())
        if t in (QEvent.TabletMove, QEvent.TabletPress, QEvent.MouseMove, QEvent.HoverMove):
            self._check_system_cursor(event)
        if t == QEvent.Paint and self.gpu:
            return False  # die Grafikkarten-Zeichenfläche zeichnet selbst (gpu_view.GpuViewport.render)
        if t in (QEvent.TabletPress, QEvent.TabletMove, QEvent.TabletRelease):
            if self._tablet_event(event):
                return True
            event.ignore()  # Qt erzeugt daraus Mausereignisse (z. B. für das Textfeld)
            return False
        if t in (QEvent.TouchBegin, QEvent.TouchUpdate, QEvent.TouchEnd, QEvent.TouchCancel):
            self._touch_event(event)
            return True
        return super().viewportEvent(event)

    def _tablet_event(self, event):
        """Gibt False zurück, wenn das Ereignis als Mausereignis weitergereicht werden soll."""
        t = event.type()
        vpos = event.position()
        if t == QEvent.TabletPress and self._editor is not None and self._grip_drag is None:
            grip = self._editor.grip_at(self, vpos)
            if grip:
                event.accept()
                self._begin_grip_drag(grip, vpos)
                return True
            if self._editor.sceneBoundingRect().contains(self._to_scene(vpos)):
                self._editor_mouse = True  # Stift im Textfeld: Cursor setzen/markieren wie mit der Maus
        if self._editor_mouse:
            return False
        event.accept()
        if self._grip_drag is not None:
            if t == QEvent.TabletMove:
                self._grip_drag_to(vpos)
            elif t == QEvent.TabletRelease:
                self._grip_drag = None
            return True
        if self._suppress_until_release:
            if self._right_press is not None and t == QEvent.TabletMove:
                self._right_drag(vpos, 8)  # Stift zittert etwas mehr als die Maus
            elif t == QEvent.TabletRelease and self._right_press is not None and \
                    event.buttons() != Qt.NoButton:
                pass  # nur die Seitentaste losgelassen, Stift liegt noch auf: Lasso läuft weiter bis zum Abheben
            elif t == QEvent.TabletRelease:
                self._suppress_until_release = False
                if self._right_press is not None and self._right_lasso:
                    self._menu_pending = None
                    self._right_release(vpos)
                else:
                    self._right_press = None
                    self._open_pending_menu()
            return True
        if self._sb_drag is not None:  # Stift zieht den Bildlaufbalken
            if t == QEvent.TabletMove:
                self._sb_move(vpos)
            elif t == QEvent.TabletRelease:
                self._sb_drag = None
                self.viewport().update()
            return True
        if self._ruler_drag:  # Stift verschiebt das Lineal
            if t == QEvent.TabletMove:
                self._ruler_drag_to(vpos)
            elif t == QEvent.TabletRelease:
                self._ruler_drop()
            return True
        if t == QEvent.TabletPress and not event.buttons() & Qt.RightButton and self._sb_press(vpos):
            return True
        if t == QEvent.TabletPress:
            self._stop_kinetic()
            self._touch_mode = None
            if event.buttons() & Qt.RightButton:
                # Stift-Seitentaste = Rechtsklick; Menü öffnet beim Abheben (wie in Windows), Ziehen = Lasso
                self._menu_pending = (event.globalPosition().toPoint(), self._to_scene(vpos))
                self._right_press = (QPointF(vpos), event.globalPosition().toPoint(), self._to_scene(vpos))
                self._right_lasso = False
                self._suppress_until_release = True
                return True
            eraser = event.pointerType() == QPointingDevice.PointerType.Eraser
            tool = "eraser" if eraser else self.tool
            if tool != "select" and not eraser and self._grabs_selection(self._to_scene(vpos), vpos):
                tool = "select"  # ausgewähltes Objekt mit dem Stift verschieben/skalieren
            edge = 0 if eraser else self._ruler_edge(vpos, tool)
            if edge:  # Stift an der Linealkante: gerade Linie
                self._pen_down = True
                self._ruler_begin_stroke(vpos, tool, edge)
                return True
            if not eraser and self._ruler_grab(vpos):
                return True
            if tool in ("select", "pan", "text"):
                # langes Halten mit dem Stift = Rechtsklick (wie in Windows)
                self._start_long_press(vpos, event.globalPosition().toPoint(), "pen")
            if tool == "pan":
                self._link_press = (QPointF(vpos), self.link_at(self._to_scene(vpos)))
                self._start_pan(vpos)
            else:
                self._pen_down = True
                self._press(self._to_scene(vpos), tool, vpos)
        elif t == QEvent.TabletMove:
            self._check_long_press_move(vpos)
            if self._panning:
                self._pan_to(vpos)
            elif self._pen_down:
                self._move(self._to_scene(self._ruler_map(vpos)))
        elif t == QEvent.TabletRelease:
            self._long_timer.stop()
            if self._panning:
                self._end_pan()
                self._finish_link_press(vpos, TOUCH_SLOP / 2)
            elif self._pen_down:
                self._pen_down = False
                self._release(self._to_scene(self._ruler_map(vpos)))
        return True

    # ================================================================ Touch
    # Modi: "pending" (noch unklar: Tippen/Scrollen/langes Antippen), "scroll", "pinch",
    #       "manip" (Auswahl verschieben/Größe ändern), "grip" (Textfeld-Griff), "menu"
    def _touch_event(self, event):
        event.accept()
        if self._pen_down:  # Handballen ignorieren, solange der Stift schreibt
            return
        t = event.type()
        pts = [p for p in event.points() if p.state() != QEventPoint.State.Released]
        now = time.monotonic()

        if t in (QEvent.TouchEnd, QEvent.TouchCancel) or not pts:
            self._long_timer.stop()
            mode = self._touch_mode
            self._touch_mode = None
            self._touch_points = {}
            if t != QEvent.TouchEnd:
                if mode == "manip":
                    self._cancel_interaction()
                    self._update_selection_box()
                elif mode == "draw":
                    self._finger_abort()
                return
            last = self._touch_last
            if mode == "draw":
                self._release(self._to_scene(self._ruler_map(last)))  # Finger abgehoben: Zeichnung fertig
            elif mode == "manip" and not self._manip_started and not self._manip_from_long:
                self._cancel_interaction()
                self._update_selection_box()
                self._touch_tap(self._touch_origin)
            elif mode == "manip":
                self._release(self._to_scene(last))
            elif mode == "grip":
                self._grip_drag = None
            elif mode == "menu":
                self._open_pending_menu()
            elif mode == "pending":
                self._touch_tap(self._touch_origin)
            elif mode == "overlay":
                if self._overlay_button_at(last) is self._overlay_target:
                    self._overlay_target.click()
            elif mode == "scroll":
                if time.monotonic() - self._touch_time > 0.08:
                    self._velocity = QPointF()  # Finger lag vor dem Abheben still: kein Nachlaufen
                self._start_kinetic()
            return

        cur = {p.id(): p.position() for p in pts}
        if t == QEvent.TouchBegin and (self.media.player_at(pts[0].position()) or (
                self._note_bubble is not None and self._note_bubble.geometry().contains(pts[0].position().toPoint()))):
            event.ignore()  # Player-Knöpfe/Schieberegler bekommen Ersatz-Mausereignisse
            self._touch_mode = None
            return
        if t == QEvent.TouchBegin or self._touch_mode is None:
            self._stop_kinetic()
            self._velocity = QPointF()
            p = pts[0].position()
            self._touch_origin = QPointF(p)
            self._touch_last = QPointF(p)
            self._touch_time = now
            self._touch_points = cur
            scene = self._to_scene(p)
            btn = self._overlay_button_at(p)
            if btn is not None:
                self._touch_mode = "overlay"  # Finger auf einem schwebenden Knopf: beim Loslassen klicken
                self._overlay_target = btn
            elif len(pts) == 1 and prefs.finger_draw() and self.ruler_visible() and \
                    self._ruler_edge(p, self.tool) and not self.ruler.contains(p):
                # Fingermodus, Finger an der Linealkante: gerade Linie
                self._touch_mode = "draw"
                self._long_timer.stop()
                self._finger_was_modified = self.document.is_modified()
                self._ruler_begin_stroke(p, self.tool, self._ruler_edge(p, self.tool))
            elif self.ruler_visible() and any(self.ruler.contains(q.position()) for q in pts):
                self._touch_mode = "ruler"  # ein Finger verschiebt, zwei Finger drehen das Lineal
                self._long_timer.stop()
                self.ruler.begin_rotate()
            elif self._editor is not None and self._editor.grip_at(self, p):
                self._touch_mode = "grip"
                self._begin_grip_drag(self._editor.grip_at(self, p), p)
            elif self._selected and (self._handle_at(p) or self._on_selection(scene)):
                # ausgewähltes Objekt direkt mit dem Finger verschieben / an Ecken skalieren
                self._touch_mode = "manip"
                self._manip_from_long = False
                self._begin_manip(scene, p)
            elif len(pts) > 1:
                self._touch_mode = "pinch"  # mehrere Finger gleichzeitig: zoomen, kein Menü
            elif prefs.finger_draw() and self.tool in FINGER_DRAW_TOOLS and self.page_at(scene) is not None:
                self._finger_begin(scene, p)  # Fingermodus: sofort zeichnen
            else:
                self._touch_mode = "pending"
                self._start_long_press(p, pts[0].globalPosition().toPoint(), "touch")
            return

        mode = self._touch_mode
        prev = self._touch_points
        if len(cur) >= 2 and mode == "ruler":
            common = [i for i in cur if i in prev]
            if len(common) >= 2:
                a, b = common[:2]
                self.ruler.two_fingers(prev[a], prev[b], cur[a], cur[b])
                self.viewport().update()
            self._touch_points = cur
            return
        if len(cur) >= 2:
            if mode == "draw":
                # zweiter Finger kam etwas später: die angefangene Zeichnung war nicht gemeint -> verwerfen, zoomen
                self._finger_abort()
                mode = self._touch_mode = "pinch"
            if mode in ("pending", "scroll", "pinch"):
                self._long_timer.stop()
                self._touch_mode = "pinch"
                common = [i for i in cur if i in prev]
                if len(common) >= 2:
                    a, b = common[:2]
                    c_old = (prev[a] + prev[b]) / 2
                    c_new = (cur[a] + cur[b]) / 2
                    d_old = _len(prev[a] - prev[b])
                    d_new = _len(cur[a] - cur[b])
                    if d_old > 5:
                        self._queue_zoom(d_new / d_old, c_old)
                    self._scroll_by(c_new - c_old)
                self._velocity = QPointF()
            self._touch_points = cur
            return

        p = pts[0].position()
        if mode == "draw":
            self._move(self._to_scene(self._ruler_map(p)))
        elif mode == "ruler":
            pid = pts[0].id()
            if pid in prev:
                self.ruler.center += p - prev[pid]
                self.viewport().update()
        elif mode == "manip":
            if not self._manip_started and (self._manip_from_long or self._resizing
                                            or _len(p - self._touch_origin) > TOUCH_SLOP / 2):
                self._manip_started = True
            if self._manip_started:
                self._move(self._to_scene(p))
        elif mode == "grip":
            self._grip_drag_to(p)
        elif mode == "pending":
            if _len(p - self._touch_origin) > TOUCH_SLOP:
                self._long_timer.stop()
                self._touch_mode = "scroll"  # ab hier scrollen (ohne Sprung)
        elif mode in ("scroll", "pinch"):
            self._touch_mode = "scroll"
            pid = pts[0].id()
            if pid in prev:
                d = p - prev[pid]
                self._scroll_by(d)
                dt = max(now - self._touch_time, 1e-3)
                self._velocity = self._velocity * 0.6 + (d / dt) * 0.4
        self._touch_last = QPointF(p)
        self._touch_time = now
        self._touch_points = cur

    def _finger_begin(self, scene: QPointF, vpos: QPointF):
        self._touch_mode = "draw"
        self._long_timer.stop()
        self._finger_was_modified = self.document.is_modified()
        self._press(scene, self.tool, vpos)

    def _finger_abort(self):
        """Angefangene Fingerzeichnung verwerfen (zweiter Finger = Zoomen). Schon Wegradiertes kommt zurück."""
        page = self._active_page
        erased = self._active_tool == "eraser" and self._erasing_checkpointed
        self._cancel_interaction()
        if erased:
            self.document.drop_checkpoint(self._finger_was_modified)
            if page is not None:
                self.document.page_changed.emit(page.index)

    def _touch_tap(self, vpos: QPointF):
        """Kurzes Antippen mit dem Finger = Klick."""
        scene = self._to_scene(vpos)
        if self._editor is not None:
            if self._editor.sceneBoundingRect().contains(scene):
                self._editor.place_cursor(scene)
                self._show_keyboard()
            else:
                self.commit_text_edit()
                if self.tool == "text" and prefs.after_insert_mouse():
                    self.tool_requested.emit("pan")
            return
        if self._selected:
            if self._on_selection(scene):
                pidx, xref = self._selected
                kind = self.document.annot_kind(pidx, xref) if not self._group else None
                if kind == "text":
                    self.start_text_edit(pidx, xref)  # zweites Antippen: Text bearbeiten
                    self._show_keyboard()
                elif kind == "note":
                    self.show_note(pidx, xref)
            else:
                self.clear_selection()  # Antippen daneben hebt die Auswahl auf
            return
        item = self.page_at(scene)
        if item is None:
            return
        if self.tool == "pan":
            link = self.link_at(scene)
            if link is not None:
                self.follow_link(link)
                return
            hit = self.media.media_at(scene)
            if hit is not None:
                self.media.play(hit[0], hit[1]["xref"])
            elif (note := self.note_at(scene)) is not None:
                self.show_note(*note)
            return
        local = self._local(item, scene)
        tol = self._touch_tolerance()
        if self.tool == "text":
            annot = self.document.annotation_at(item.index, local, tol)
            if annot is not None and annot.type[0] == fitz.PDF_ANNOT_FREE_TEXT:
                self.start_text_edit(item.index, annot.xref)
            else:
                self.start_text_edit(item.index, None, QPointF(*local))
            self._show_keyboard()
        elif self.tool == "select":
            annot = self.document.annotation_at(item.index, local, tol)
            if annot is not None:
                self._select(item.index, annot.xref)
                if annot.type[0] == fitz.PDF_ANNOT_TEXT:
                    self.show_note(item.index, annot.xref)
        elif self.tool in ("note", "image"):
            self._press(scene, self.tool, vpos)
            self._release(scene)

    def _touch_tolerance(self):
        """Trefferradius für Finger in Seitenkoordinaten (ca. 12 Bildschirmpixel)."""
        return 12.0 / (self._zoom * SCREEN_ZOOM)

    def _show_keyboard(self):
        QGuiApplication.inputMethod().show()  # Bildschirmtastatur (Tablet-Modus)

    # ---- Auswahl mit Finger bewegen
    def _grabs_selection(self, scene: QPointF, vpos: QPointF):
        """Liegt der Punkt auf einem Anfasser oder im Rahmen der Auswahl? (dann wird sie bewegt/skaliert)"""
        return bool(self._selected) and self._editor is None and (
            self._handle_at(vpos) is not None or self._on_selection(scene))

    def _on_selection(self, scene: QPointF):
        if not self._selected or self._sel_item is None:
            return False
        item = self.items_[self._selected[0]]
        r = self._sel_rect
        pad = self._touch_tolerance()
        return r.adjusted(-pad, -pad, pad, pad).contains(scene - item.pos())

    def _begin_manip(self, scene: QPointF, vpos: QPointF, allow_resize=True):
        self._manip_started = False
        if allow_resize and self._handle_at(vpos):
            self._press(scene, "select", vpos)  # Griff: Größe ändern
            return
        item = self.items_[self._selected[0]]
        self._cancel_interaction()
        self._active_page = item
        self._active_tool = "select"
        self._start = QPointF(*self._local(item, scene))
        self._moving = True

    # ---- Griffe des Textfelds
    def _begin_grip_drag(self, kind, vpos: QPointF):
        ed = self._editor
        self._grip_drag = (kind, self._to_scene(vpos), QPointF(ed.anchor),
                           ed.fixed_width if ed.fixed_width is not None else ed.pdf_rect()[2] - ed.anchor.x())

    def _grip_drag_to(self, vpos: QPointF):
        if self._grip_drag is None or self._editor is None:
            return
        kind, start, anchor0, width0 = self._grip_drag
        d = self._to_scene(vpos) - start
        if kind == "move":
            self._editor.set_anchor(anchor0 + d)
        else:
            self._editor.set_width(width0 + d.x())

    # ================================================================ Bewegungstakt
    def _refresh_interval(self):
        screen = self.screen()
        hz = screen.refreshRate() if screen is not None else 60.0
        hz = hz if 30 <= hz <= 360 else 60.0
        return max(4, int(1000.0 / hz))  # abrunden: lieber einen Takt zu früh als zu spät

    def _ensure_frame(self):
        """Bewegung steht an: sofort ein Bild erzeugen, weitere im Bildschirmtakt."""
        self._idle_frames = 0
        self.renderer.moving = True
        self._moving_off.stop()
        if not self._frame.isActive():
            self._frame.setInterval(self._refresh_interval())
            self._frame_last = time.perf_counter() - self._frame.interval() / 1000.0
            self._on_frame()
            self._frame.start()

    def _scroll_by(self, d: QPointF):
        """Scrollbewegung (Finger/Maus) vormerken; angewendet wird sie im Bildschirmtakt."""
        self._input_sum += QPointF(d)
        self._input_samples.append((time.perf_counter(), self._input_sum.x(), self._input_sum.y()))
        self._ensure_frame()

    def _resampled_input(self, now):
        """Touch-Resampling: Wie weit war der Finger zum Zeitpunkt (jetzt - RESAMPLE_S)?
        Zwischen zwei Messpunkten wird interpoliert. So bewegt sich der Inhalt in jedem Bild
        gleichmäßig, auch wenn die Eingaben unregelmäßig oder im selben Takt wie der Bildschirm
        eintreffen (sonst: mal zwei Ereignisse pro Bild, mal keins -> Ruckeln)."""
        smp = self._input_samples
        if not smp:
            return self._input_applied
        t = now - RESAMPLE_S
        if t >= smp[-1][0]:
            target = QPointF(smp[-1][1], smp[-1][2])
            del smp[:-1]
            return target
        i = len(smp) - 1
        while i > 0 and smp[i - 1][0] > t:
            i -= 1
        if i == 0:
            # älter als der erste Messpunkt: vom bereits angewendeten Stand aus anfangen
            return self._input_applied
        (t0, x0, y0), (t1, x1, y1) = smp[i - 1], smp[i]
        f = (t - t0) / (t1 - t0) if t1 > t0 else 1.0
        del smp[:i - 1]
        return QPointF(x0 + (x1 - x0) * f, y0 + (y1 - y0) * f)

    def _queue_zoom(self, factor, vpos: QPointF):
        if self._pending_zoom is None:
            self._pending_zoom = (factor, QPointF(vpos))
        else:
            self._pending_zoom = (self._pending_zoom[0] * factor, QPointF(vpos))
        self._ensure_frame()

    def _on_frame(self):
        now = time.perf_counter()
        dt = min(max(now - self._frame_last, 0.001), 0.05)
        self._frame_last = now
        changed = False
        if self._pending_zoom is not None:
            factor, vpos = self._pending_zoom
            self._pending_zoom = None
            self._zoom_at(factor, vpos)
            changed = True
        if self._kinetic_on:
            # Nachlaufen zeitbasiert: gleich schnell, egal wie regelmäßig der Takt kommt
            self._velocity *= 0.92 ** (dt / 0.016)
            if _len(self._velocity) < 20:
                self._kinetic_on = False
            else:
                self._pending_scroll += self._velocity * dt
                changed = True
        target = self._resampled_input(now)
        self._pending_scroll += target - self._input_applied
        self._input_applied = target
        if self._apply_scroll():
            changed = True
        catching_up = _len(self._input_sum - self._input_applied) >= 0.5
        # Zeitgeber kurz weiterlaufen lassen, statt bei jeder Pause neu anzulaufen
        if changed or self._kinetic_on or catching_up:
            self._idle_frames = 0
        else:
            self._idle_frames += 1
            if self._idle_frames > 12 and self._touch_mode not in ("scroll", "pinch") and not self._panning:
                self._frame.stop()
                self._moving_off.start()

    def _apply_scroll(self):
        h, v = self.horizontalScrollBar(), self.verticalScrollBar()
        self._scroll_rest += self._pending_scroll
        self._pending_scroll = QPointF()
        dx, dy = int(self._scroll_rest.x()), int(self._scroll_rest.y())
        self._scroll_rest -= QPointF(dx, dy)
        if not dx and not dy:
            return False
        if dx:
            h.setValue(h.value() - dx)
        if dy:
            v.setValue(v.value() - dy)
        return True

    def _zoom_at(self, factor, vpos: QPointF):
        scene_pt = self._to_scene(vpos)
        anchor = self.transformationAnchor()
        self.setTransformationAnchor(QGraphicsView.NoAnchor)
        self.set_zoom(self._zoom * factor)
        self.setTransformationAnchor(anchor)
        # Punkt unter den Fingern festhalten (sofort, im selben Takt)
        new_vpos = self.viewportTransform().map(scene_pt)
        self._pending_scroll += QPointF(vpos) - new_vpos

    def _start_kinetic(self):
        if _len(self._velocity) > 150:
            self._kinetic_on = True
            self._ensure_frame()

    def _stop_kinetic(self):
        self._kinetic_on = False

    def _to_scene(self, vpos: QPointF) -> QPointF:
        return self.viewportTransform().inverted()[0].map(QPointF(vpos))

    # ================================================================ Maus
    def mousePressEvent(self, event):
        vpos = event.position()
        pos = self._to_scene(vpos)
        if self._editor is not None and event.button() == Qt.LeftButton:
            grip = None if self._editor_mouse else self._editor.grip_at(self, vpos)
            if grip and event.source() == Qt.MouseEventNotSynthesized:
                self._begin_grip_drag(grip, vpos)
                return
            if self._editor_mouse or self._editor.sceneBoundingRect().contains(pos):
                self._editor_mouse = True
                super().mousePressEvent(event)  # Klick ins Textfeld: Cursor setzen
                return
        if event.source() != Qt.MouseEventNotSynthesized or self._pen_down:
            return  # aus Touch/Stift erzeugte Ersatz-Ereignisse ignorieren
        if event.button() == Qt.LeftButton and self._sb_press(vpos):
            return
        if event.button() == Qt.LeftButton and self.ruler_visible():
            edge = self._ruler_edge(vpos, self.tool)
            if edge:  # an der Linealkante: gerade Linie
                self._ruler_begin_stroke(vpos, self.tool, edge)
                return
            if self._ruler_grab(vpos):
                return
        if event.button() in (Qt.BackButton, Qt.XButton1):
            self.go_back()  # Maus-Zurücktaste: zurück zur Stelle vor dem letzten Link
            return
        if event.button() == Qt.LeftButton and self.tool != "select" and self._grabs_selection(pos, vpos):
            self._press(pos, "select", vpos)  # z. B. per Rechtsklick ausgewählt: direkt verschieben/skalieren
            return
        if event.button() == Qt.MiddleButton:
            self._start_pan(vpos)  # mittlere Maustaste: Seite verschieben
            return
        if event.button() == Qt.LeftButton and self.tool == "pan":
            # Maus-Werkzeug: Ziehen markiert Text (zum Kopieren/Hervorheben), Klick folgt Links / spielt Medien
            self._link_press = (QPointF(vpos), self.link_at(pos))
            self._tsel_begin(pos, vpos)
            return
        if event.button() == Qt.RightButton:
            # Rechtsklick: Ziehen = Lassoauswahl, ohne Bewegung = Kontextmenü (beim Loslassen)
            self._right_press = (QPointF(vpos), event.globalPosition().toPoint(), pos)
            self._right_lasso = False
            return
        if event.button() == Qt.LeftButton:
            self._press(pos, self.tool, vpos)

    def mouseMoveEvent(self, event):
        if self._editor_mouse:
            super().mouseMoveEvent(event)
            return
        if event.source() != Qt.MouseEventNotSynthesized or self._pen_down:
            return
        if self._sb_drag is not None:
            self._sb_move(event.position())
            return
        if self._ruler_drag:
            self._ruler_drag_to(event.position())
            return
        if self._right_press is not None and event.buttons() & Qt.RightButton:
            self._right_drag(event.position(), 4)
            return
        if self._tsel_press is not None and event.buttons() & Qt.LeftButton:
            self._tsel_drag(event.position())
            return
        if not event.buttons() and self._sb_hover(event.position()):
            return
        if self._grip_drag is not None and self._touch_mode != "grip":
            self._grip_drag_to(event.position())
        elif self._panning:
            self._pan_to(event.position())
        else:
            self._move(self._to_scene(self._ruler_map(event.position())))

    def mouseReleaseEvent(self, event):
        if self._editor_mouse:
            super().mouseReleaseEvent(event)
            self._editor_mouse = False
            return
        if event.source() != Qt.MouseEventNotSynthesized or self._pen_down:
            return
        if self._sb_drag is not None:
            self._sb_drag = None
            self.viewport().update()
            return
        if self._ruler_drag:
            self._ruler_drop()
            return
        if event.button() == Qt.RightButton and self._right_press is not None:
            self._right_release(event.position())
            return
        if event.button() == Qt.LeftButton and self._tsel_press is not None:
            if not self._tsel_end():
                self._finish_link_press(event.position(), 4)  # nur geklickt: Link, Video/Audio, Notiz
            return
        if self._grip_drag is not None and self._touch_mode != "grip":
            self._grip_drag = None
        elif self._panning:
            self._end_pan()
            if event.button() == Qt.LeftButton:
                self._finish_link_press(event.position(), 4)
        elif event.button() == Qt.LeftButton:
            self._release(self._to_scene(self._ruler_map(event.position())))

    # ================================================================ Werkzeuglogik
    def _press(self, pos: QPointF, tool: str, vpos: QPointF):
        """tool "lasso": Lassoauswahl beginnen, egal welches Werkzeug aktiv ist oder was unter dem Punkt liegt."""
        self._cancel_interaction()
        if self._editor is not None:
            # Klick außerhalb des Textfelds: Text übernehmen, sonst nichts tun
            self.commit_text_edit()
            if tool == "text" and prefs.after_insert_mouse():
                self.tool_requested.emit("pan")
            return
        if tool != "select" and self._selected:
            self.clear_selection()
        if tool == "select" and self._selected:
            handle = self._handle_at(vpos)
            if handle is not None and not self._handle_at(vpos, exact=True):
                # Nur im erweiterten Fangbereich: liegt dort ein anderes Objekt, hat es Vorrang
                item = self.page_at(pos)
                other = item and self.document.annotation_at(item.index, self._local(item, pos))
                if other is not None and (item.index, other.xref) != tuple(self._selected):
                    handle = None
            if handle is not None:  # Größe ändern
                self._active_page = self.items_[self._selected[0]]
                self._active_tool = tool
                self._start = QPointF(*self._local(self._active_page, pos))
                self._resizing = handle
                for h in self._handles:
                    h.hide()
                return
        lasso_only = tool == "lasso"
        if lasso_only:
            tool = "select"
        item = self.page_at(pos)
        if item is None:
            if tool == "select" and not lasso_only:
                self.clear_selection()
                self._start_pan(vpos)
            return
        self._active_page = item
        self._active_tool = tool
        local = self._local(item, pos)
        self._start = QPointF(*local)

        if tool in ("pen", "highlighter"):
            self._stroke_pts = [local]
            path = QPainterPath()
            path.moveTo(QPointF(*local))
            self._preview = QGraphicsPathItem(item)
            self._preview.setPath(path)
            color, width, opacity = self._stroke_style(tool)
            self._preview.setPen(QPen(color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            self._preview.setOpacity(opacity)
            if self.gpu:
                self._preview.setVisible(False)  # zeichnet die Strich-Ebene der Grafikkarte
                self._live = {"index": item.index, "item": item, "pts": self._stroke_pts, "color": color,
                              "width": width, "opacity": opacity, "done": False, "preview": self._preview}
                self._layer.append(self._live)
            self._gesture = None
            self._hold_anchor = local
            self._hold_timer.start()
        elif tool == "eraser":
            self._erasing_checkpointed = False
            self._erase_last = None
            self._erase(item, local)
        elif tool == "text":
            self._active_page = None
            annot = self.document.annotation_at(item.index, local)
            if annot is not None and annot.type[0] == fitz.PDF_ANNOT_FREE_TEXT:
                self.start_text_edit(item.index, annot.xref)
            else:
                self.start_text_edit(item.index, None, QPointF(*local))
        elif tool in ("line", "arrow", "rect", "ellipse", "markup", "image"):
            self._preview = QGraphicsPathItem(item)
            if tool in ("image", "markup"):
                pen = QPen(QColor("#2f7de1"), 1, Qt.DashLine)
                pen.setCosmetic(True)
            else:
                pen = QPen(QColor(*[int(c * 255) for c in self.settings.color(tool)]), self.settings.width(tool),
                           Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            self._preview.setOpacity(self.settings.opacity(tool))
            self._preview.setPen(pen)
        elif tool == "note":
            self._active_page = None
            text, ok = QInputDialog.getMultiLineText(self, "Notiz", "Notiztext:")
            if ok and text.strip():
                self.document.add_note(item.index, local, text)
                if prefs.after_insert_mouse():
                    self.tool_requested.emit("pan")
        elif tool == "select" and lasso_only:
            self._begin_lasso(item, local)
        elif tool == "select":
            in_sel = bool(self._selected) and item.index == self._selected[0] and self._sel_rect.contains(QPointF(*local))
            if self._group and in_sel:
                self._moving = True  # Klick in die Lassoauswahl: alle Objekte gemeinsam verschieben
                self.viewport().setCursor(Qt.SizeAllCursor)
                return
            annot = self.document.annotation_at(item.index, local)
            if annot is None and in_sel:  # in den Rahmen des ausgewählten Objekts (nicht genau auf den Strich)
                self._moving = True
                self.viewport().setCursor(Qt.SizeAllCursor)
                return
            if annot is not None:
                # Doppeltipp mit dem Stift auf ein Textfeld -> bearbeiten
                now = time.monotonic()
                if self._pen_down and annot.type[0] == fitz.PDF_ANNOT_FREE_TEXT and \
                        self._last_tap[1] == annot.xref and now - self._last_tap[0] < 0.45:
                    self._active_page = None
                    self._last_tap = (0.0, None)
                    self.start_text_edit(item.index, annot.xref)
                    return
                self._last_tap = (now, annot.xref)
                self._select(item.index, annot.xref)
                self._moving = True
                self.viewport().setCursor(Qt.SizeAllCursor)
            else:
                self._begin_lasso(item, local)

    def _begin_lasso(self, item, local):
        self.clear_selection()
        self._lasso_pts = [local]
        self._preview = QGraphicsPathItem(item)
        pen = QPen(QColor("#2f7de1"), 1.5, Qt.DashLine)
        pen.setCosmetic(True)
        self._preview.setPen(pen)
        self._preview.setBrush(QColor(47, 125, 225, 30))

    # ---- Rechtsklick: Lasso oder Kontextmenü
    def _right_drag(self, vpos: QPointF, slop):
        start_v, _gpt, start_scene = self._right_press
        if not self._right_lasso:
            if _len(QPointF(vpos) - start_v) < slop:
                return
            self._right_lasso = True
            self._press(start_scene, "lasso", start_v)
        self._move(self._to_scene(vpos))

    def _right_release(self, vpos: QPointF):
        start_v, gpt, start_scene = self._right_press
        self._right_press = None
        if self._right_lasso:
            self._right_lasso = False
            self._release(self._to_scene(vpos))
        else:
            self._context_menu(gpt, start_scene)

    # ---- Textauswahl (Maus-Werkzeug mit der Maus)
    def _tsel_begin(self, scene: QPointF, vpos: QPointF):
        self.clear_text_selection()
        item = self.page_at(scene)
        self._tsel_press = (QPointF(vpos), item.index if item else None,
                            self._local(item, scene) if item else None)

    def _tsel_drag(self, vpos: QPointF):
        start_v, idx, a = self._tsel_press
        if idx is None or idx >= len(self.items_):
            return
        if self._tsel is None and _len(QPointF(vpos) - start_v) < 4:
            return
        h = self.viewport().height()
        if vpos.y() < 24 or vpos.y() > h - 24:  # am Rand: mitscrollen
            sb = self.verticalScrollBar()
            sb.setValue(sb.value() + (-24 if vpos.y() < 24 else 24))
        item = self.items_[idx]
        b = self._local(item, self._to_scene(vpos))  # auf der Startseite bleiben
        rects, text = self.document.text_selection(idx, a, b)
        self._show_text_selection(idx, rects, text)

    def _tsel_end(self):
        """Maus losgelassen. True, wenn Text markiert wurde (sonst war es ein Klick)."""
        self._tsel_press = None
        if self._tsel is None:
            return False
        if self._tsel["text"]:
            self.status_message.emit(f"{len(self._tsel['text'])} Zeichen markiert – Strg+C kopiert, "
                                     "Rechtsklick: Textmarker, Unterstreichen …")
        self.text_selection_changed.emit(bool(self._tsel["text"]))
        return True

    def _show_text_selection(self, idx, rects, text):
        if self._tsel is None:
            gi = QGraphicsPathItem(self.items_[idx])
            gi.setPen(Qt.NoPen)
            gi.setBrush(QColor(47, 125, 225, 80))
            gi.setZValue(5)
            self._tsel = {"index": idx, "item": gi}
        path = QPainterPath()
        for x0, y0, x1, y1 in rects:
            path.addRect(QRectF(x0, y0, x1 - x0, y1 - y0))
        self._tsel["item"].setPath(path)
        self._tsel.update(rects=rects, text=text)

    def clear_text_selection(self):
        sel, self._tsel = self._tsel, None
        if sel is not None and sel["item"].scene() is not None:
            sel["item"].scene().removeItem(sel["item"])
        if sel is not None:
            self.text_selection_changed.emit(False)

    def selected_text(self):
        return self._tsel["text"] if self._tsel else ""

    def copy_selected_text(self):
        text = self.selected_text()
        if text:
            QApplication.clipboard().setText(text)
            self.status_message.emit(f"{len(text)} Zeichen in die Zwischenablage kopiert.")
        return bool(text)

    def mark_selected_text(self, kind="highlight"):
        """Textmarker/Unterstreichen/Durchstreichen auf die Textauswahl (Farbe des Markieren-Werkzeugs)."""
        sel = self._tsel
        if not sel or not sel.get("rects"):
            return
        color = self.settings.color("markup") if kind == "highlight" else (0.85, 0.1, 0.1)
        self.document.highlight_rects(sel["index"], sel["rects"], color, kind)
        self.clear_text_selection()

    def _text_selection_hit(self, idx, local):
        sel = self._tsel
        if not sel or sel["index"] != idx:
            return False
        x, y = local
        return any(x0 - 3 <= x <= x1 + 3 and y0 - 3 <= y <= y1 + 3 for x0, y0, x1, y1 in sel.get("rects", ()))

    def _move(self, pos: QPointF):
        item = self._active_page
        if item is None:
            return
        local = self._local(item, pos)
        tool = self._active_tool
        if tool in ("pen", "highlighter") and self._preview is not None:
            if self._gesture is not None:
                g = self._gesture
                if g[0] == "line":  # erkannte Gerade: Endpunkt folgt dem Stift, rastet waagerecht/senkrecht ein
                    end = gestures.snap_line(g[1], local)
                    self._preview.setPath(self._shape_path("line", QPointF(*g[1]), QPointF(*end)))
                    self._gesture = ("line", g[1], end)
                else:  # erkannte Form: mit dem Stift skalieren, gegenüberliegende Seite bleibt fest
                    box = gestures.scale_box(g[1], g[2], g[3], local)
                    self._preview.setPath(self._shape_path(g[0], QPointF(box[0], box[1]), QPointF(box[2], box[3])))
                    self._gesture = g[:4] + (box,)
                return
            last = self._stroke_pts[-1]
            if (local[0] - last[0]) ** 2 + (local[1] - last[1]) ** 2 < 0.04:
                return
            self._stroke_pts.append(local)
            path = self._preview.path()
            path.lineTo(QPointF(*local))
            self._preview.setPath(path)
            if self._live is not None:
                self.viewport().update()
            # Stift bewegt sich -> Haltezeit neu starten
            scale = self._zoom * SCREEN_ZOOM
            if _dist(QPointF(*local), QPointF(*self._hold_anchor)) * scale > HOLD_SLOP:
                self._hold_anchor = local
                self._hold_timer.start()
        elif tool == "eraser":
            self._erase(item, local)
        elif tool == "select" and self._resizing and self._sel_item is not None:
            self._sel_item.setRect(self._resized_rect(QPointF(*local)))
        elif tool == "select" and self._moving and self._sel_item is not None:
            off = QPointF(*local) - self._start
            self._sel_item.setPos(off)
            if self._drag_prev is None and (abs(off.x()) > 0.5 or abs(off.y()) > 0.5):
                self._drag_preview_start()
            if self._drag_prev is not None:
                self._drag_prev["item"].setPos(self._drag_prev["pos"] + off)
        elif tool == "select" and self._lasso_pts is not None and self._preview is not None:
            last = self._lasso_pts[-1]
            if (local[0] - last[0]) ** 2 + (local[1] - last[1]) ** 2 < 0.25:
                return
            self._lasso_pts.append(local)
            path = QPainterPath(QPointF(*self._lasso_pts[0]))
            for p in self._lasso_pts[1:]:
                path.lineTo(QPointF(*p))
            path.closeSubpath()
            self._preview.setPath(path)
        elif self._preview is not None and self._start is not None:
            self._preview.setPath(self._shape_path(tool, self._start, QPointF(*local)))

    def _release(self, pos: QPointF):
        item = self._active_page
        if item is None:
            return
        local = self._local(item, pos)
        tool = self._active_tool
        start = self._start
        self._active_page = None
        self._start = None

        if tool in ("pen", "highlighter"):
            self._hold_timer.stop()
            if self._ruler_stroke is not None:
                self._finish_ruler_stroke(item, tool)
                return
            raw = self._stroke_pts
            # Glättung in Bildschirmpixeln (Stiftzittern entsteht dort) → in pt umrechnen: wirkt bei jedem Zoom gleich
            pts = _smooth(raw, prefs.stroke_smoothing_px() / self.transform().m11())
            self._stroke_pts = []
            if self._live is not None and any(x is self._live for x in self._layer) and len(pts) > 1:
                # Ebene zeigt den Strich jetzt geglättet – genau wie er gleich im PDF steht (kein Sprung beim Übergang)
                smooth = dict(self._live, pts=[tuple(p) for p in pts])
                self._layer_remove([self._live])
                self._layer.append(smooth)
                self._live = smooth
            color, width, opacity = self._stroke_style(tool, as_float=True)
            gesture, self._gesture = self._gesture, None
            if gesture is not None:
                # erst der Freihandstrich, dann (eigener Schritt) die erkannte Form an seiner Stelle:
                # „Rückgängig“ macht die Form wieder zum Strich
                ink = self.document.add_ink(item.index, [pts], color, width, opacity)
                kind = gesture[0]
                if kind == "line":
                    a, b = gesture[1], gesture[2] if len(gesture) > 2 else local
                    self.document.add_shape(item.index, "line", a, b, color, width, opacity=opacity, replace=ink)
                else:
                    x0, y0, x1, y1 = gesture[4]
                    self.document.add_shape(item.index, kind, (x0, y0), (x1, y1), color, width, opacity=opacity,
                                            replace=ink)
            elif gestures.is_scribble(raw) and self.document.scribble_erase(
                    item.index, raw, max(2.0, width) + 2.0, scribble=([pts], color, width, opacity)):
                self.status_message.emit("Durchgestrichenes gelöscht")
            else:
                self.document.add_ink(item.index, [pts], color, width, opacity)
            self._retire_preview(item.index)
            return
        self._remove_preview()
        if tool in ("line", "arrow", "rect", "ellipse"):
            if _dist(start, QPointF(*local)) > 2:
                fill = self.settings.fill() if tool in ("rect", "ellipse") else None
                self.document.add_shape(item.index, tool, (start.x(), start.y()), local,
                                        self.settings.color(tool), self.settings.width(tool), fill,
                                        opacity=self.settings.opacity(tool))
                if prefs.after_insert_mouse():
                    self.tool_requested.emit("pan")
        elif tool == "markup":
            r = _rect(start, QPointF(*local))
            if not self.document.highlight_text(item.index, (r.left(), r.top(), r.right(), r.bottom()),
                                                self.settings.color("markup")):
                self.status_message.emit("Kein Text im markierten Bereich gefunden.")
        elif tool == "image":
            r = _rect(start, QPointF(*local))
            if r.width() < 10 or r.height() < 10:
                r = QRectF(start.x(), start.y(), item.w * 0.5, item.h * 0.5)
            path, _ = QFileDialog.getOpenFileName(self, "Bild einfügen", "",
                                                  "Bilder (*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff *.webp)")
            if path:
                try:
                    xref = self.document.insert_image_on_page(item.index, path,
                                                              (r.left(), r.top(), r.right(), r.bottom()))
                except Exception as e:
                    self.status_message.emit(f"Bild konnte nicht eingefügt werden: {e}")
                    return
                self._after_insert(item.index, xref)
        elif tool == "select":
            if self._resizing:
                r = self._resized_rect(QPointF(*local))
                self._resizing = None
                if self._selected:
                    pidx, xref = self._selected
                    self._annot_op = True
                    try:
                        new_xref = self.document.resize_annotation(
                            pidx, xref, (r.left(), r.top(), r.right(), r.bottom()))
                    finally:
                        self._annot_op = False
                    self._select(pidx, new_xref or xref)
            elif self._moving:
                self._moving = False
                self.update_cursor()
                d = QPointF(*local) - start
                moved = bool(self._selected) and (abs(d.x()) > 1 or abs(d.y()) > 1)
                self._drag_preview_end(moved)
                if self._group and (abs(d.x()) > 1 or abs(d.y()) > 1):
                    pidx = self._selected[0]
                    self._annot_op = True
                    try:
                        new = self.document.move_annotations(pidx, self._group, d.x(), d.y())
                    finally:
                        self._annot_op = False
                    self._select_group(pidx, new)
                elif self._selected and (abs(d.x()) > 1 or abs(d.y()) > 1):
                    pidx, xref = self._selected
                    self._annot_op = True
                    try:
                        new_xref = self.document.move_annotation(pidx, xref, d.x(), d.y())
                    finally:
                        self._annot_op = False
                    self._select(pidx, new_xref or xref)
                elif self._sel_item is not None:
                    self._sel_item.setPos(0, 0)
                    if self._selected and not self._group and \
                            self.document.annot_kind(*self._selected) == "note":
                        self.show_note(*self._selected)  # Notiz angeklickt (nicht verschoben): lesen
            else:
                pts, self._lasso_pts = self._lasso_pts or [], None
                self._finish_lasso(item.index, pts)

    def mouseDoubleClickEvent(self, event):
        pos = self._to_scene(event.position())
        if self._editor is not None and self._editor.sceneBoundingRect().contains(pos):
            self._editor_mouse = True
            return super().mouseDoubleClickEvent(event)  # Wort markieren
        if self.tool != "select":
            return
        item = self.page_at(pos)
        if item is None:
            return
        annot = self.document.annotation_at(item.index, self._local(item, pos))
        if annot is None:
            return
        self._moving = False
        if self.document.annot_kind(item.index, annot.xref) == "media":
            self.media.play(item.index, annot.xref)
        elif self.document.is_image(annot):
            self._select(item.index, annot.xref)
            self.crop_selected()
        elif annot.type[0] == fitz.PDF_ANNOT_FREE_TEXT:
            self.start_text_edit(item.index, annot.xref)
        elif annot.type[0] == fitz.PDF_ANNOT_TEXT:
            self._edit_note(item.index, annot.xref)

    def _edit_note(self, index, xref):
        page = self.document.page(index)
        annot = _load_annot(page, xref)
        if annot is None:
            return
        old = annot.info.get("content", "")
        text, ok = QInputDialog.getMultiLineText(self, "Notiz bearbeiten", "Text:", old)
        if ok:
            self.document.update_text(index, xref, text)

    # ================================================================ Textfeld auf der Seite
    def start_text_edit(self, index, xref=None, anchor: QPointF | None = None):
        """Neues Textfeld an anchor öffnen oder ein vorhandenes (xref) bearbeiten."""
        self.commit_text_edit()
        self.tool_requested.emit("text")  # vor dem Öffnen, da ein Werkzeugwechsel das Feld übernimmt
        self.clear_selection()
        item = self.items_[index]
        width = None
        if xref is not None:
            info = self.document.text_info(index, xref)
            if info is None:
                return
            text, size, color, rect = info
            anchor = QPointF(rect.x0, rect.y0)
            width = rect.width
            old_rect = (rect.x0, rect.y0, rect.x1, rect.y1)
            was_modified = self.document.begin_text_edit(index, xref)
        else:
            text, size, color = "", float(self.settings.font_size), self.settings.color("text")
            old_rect = None
            was_modified = None
        qcolor = QColor.fromRgbF(*color)
        self._editor = TextEditorItem(item, anchor, text, size, qcolor, width)
        self._editor_state = (index, xref, text, size, tuple(color), was_modified, old_rect)
        self.text_edit_started.emit(size)
        self.setFocus()
        self._editor.focus_end()
        self._emit_context()

    def set_text_style(self, fontsize=None, color=None):
        """Schriftgröße/Farbe des gerade bearbeiteten Textfelds ändern."""
        if self._editor is None:
            return
        if fontsize is not None:
            self._editor.set_font_size(fontsize)
        if color is not None:
            self._editor.set_color(QColor.fromRgbF(*color))

    def commit_text_edit(self, delete=False):
        if self._editor is None or self._committing:
            return
        self._committing = True
        try:
            editor, state = self._editor, self._editor_state
            self._editor = None
            self._editor_state = None
            self._editor_mouse = False
            self._grip_drag = None
            index, xref, old_text, old_size, old_color, was_modified, old_rect = state
            text = "" if delete else editor.text()
            size = editor.fontsize
            c = editor.defaultTextColor()
            color = (c.redF(), c.greenF(), c.blueF())
            rect = editor.pdf_rect()
            editor.remove()
            if xref is not None:
                # Position und Breite unverändert? (Höhe ergibt sich aus dem Text)
                same_box = (abs(rect[0] - old_rect[0]) < 0.5 and abs(rect[1] - old_rect[1]) < 0.5
                            and abs((rect[2] - rect[0]) - (old_rect[2] - old_rect[0])) < 0.5)
                unchanged = (text == old_text.rstrip() and abs(size - old_size) < 0.01 and same_box
                             and all(abs(a - b) < 0.004 for a, b in zip(color, old_color)))
                self.document.end_text_edit(index, xref, text, rect, size, color, unchanged, was_modified)
            elif text.strip():
                self.document.add_text(index, rect, text, size, color)
        finally:
            self._committing = False
        self._emit_context()

    def delete_editing_text(self):
        self.commit_text_edit(delete=True)

    def is_editing_text(self):
        return self._editor is not None

    def is_busy(self):
        """Wird gerade geschrieben, gezogen oder getippt? (dann nicht automatisch speichern)"""
        return (self._pen_down or self._touch_mode is not None or self._editor is not None
                or self._active_page is not None or self._panning)

    # ================================================================ Langes Antippen
    def _start_long_press(self, vpos: QPointF, global_pt, source):
        self._long_origin = QPointF(vpos)
        self._long_global = global_pt
        self._long_source = source
        self._long_timer.start(long_press_ms())

    def _check_long_press_move(self, vpos: QPointF):
        if self._long_timer.isActive() and _len(QPointF(vpos) - self._long_origin) > TOUCH_SLOP:
            self._long_timer.stop()

    def _long_press(self):
        scene = self._to_scene(self._long_origin)
        item = self.page_at(scene)
        annot = None
        if item is not None and self._editor is None:
            annot = self.document.annotation_at(item.index, self._local(item, scene),
                                                self._touch_tolerance())
        if self._long_source == "touch":
            if self._touch_mode != "pending":
                return
            if annot is not None:
                # Objekt greifen: auswählen und mit demselben Finger verschieben
                if not self._in_group(item.index, annot.xref):
                    self._select(item.index, annot.xref)
                self._touch_mode = "manip"
                self._manip_from_long = True
                self._begin_manip(scene, self._long_origin, allow_resize=False)
            else:
                self._touch_mode = "menu"  # Menü öffnet beim Loslassen
                self._menu_pending = (self._long_global, scene)
            return
        # Stift gedrückt halten
        if self._moving or self._resizing:
            return  # Objekt wird schon verschoben
        self._cancel_interaction()
        if self._panning:
            self._panning = False
            self.update_cursor()
        if annot is not None:
            if not self._in_group(item.index, annot.xref):
                self._select(item.index, annot.xref)
            self._begin_manip(scene, self._long_origin, allow_resize=False)
            return
        self._pen_down = False
        self._suppress_until_release = True
        self._menu_pending = (self._long_global, scene)

    def _open_pending_menu(self):
        if self._menu_pending is None:
            return
        gp, sp = self._menu_pending
        self._menu_pending = None
        QTimer.singleShot(0, lambda: self._context_menu(gp, sp))

    # ================================================================ Links
    def _page_links(self, index):
        links = self._links.get(index)
        if links is None:
            page = self.document.page(index)
            m = page.rotation_matrix
            links = []
            try:
                for ln in page.get_links():
                    r = (ln["from"] * m).normalize()
                    links.append((QRectF(r.x0, r.y0, r.width, r.height), ln))
            except Exception:
                pass
            self._links[index] = links
        return links

    def link_at(self, scene: QPointF):
        """Link unter dem Punkt (Szenenkoordinaten) oder None."""
        item = self.page_at(scene)
        if item is None:
            return None
        p = QPointF(*self._local(item, scene))
        slop = 2.0 / max(self._zoom, 0.1)  # etwas Spielraum (dünne Zeilen-Links)
        for r, ln in self._page_links(item.index):
            if r.adjusted(-slop, -slop, slop, slop).contains(p):
                return ln
        return None

    def _link_hover(self, vpos: QPointF):
        scene = self._to_scene(vpos)
        if self.link_at(scene) is not None:
            over = "link"
        else:
            item = self.page_at(scene)
            over = "text" if item is not None and self.document.text_at(item.index, self._local(item, scene)) else ""
        if over != getattr(self, "_over_link", ""):
            self._over_link = over
            self.viewport().setCursor({"link": Qt.PointingHandCursor, "text": Qt.IBeamCursor}.get(over, Qt.ArrowCursor))

    def _finish_link_press(self, vpos: QPointF, slop):
        """Maus-Werkzeug losgelassen: kaum bewegt und auf einem Link -> Link folgen (sonst war es Verschieben)."""
        press, self._link_press = self._link_press, None
        if press is None or _len(QPointF(vpos) - press[0]) > slop:
            return
        if press[1] is None:  # kein Link: vielleicht ein Video/Audio-Objekt oder eine Notiz
            scene = self._to_scene(press[0])
            hit = self.media.media_at(scene)
            if hit is not None:
                QTimer.singleShot(0, lambda: self.media.play(hit[0], hit[1]["xref"]))
            else:
                note = self.note_at(scene)
                if note is not None:
                    QTimer.singleShot(0, lambda: self.show_note(*note))
            return
        if self.link_at(self._to_scene(vpos)) is press[1] or self.link_at(self._to_scene(press[0])) is press[1]:
            QTimer.singleShot(0, lambda: self.follow_link(press[1]))

    def follow_link(self, link):
        kind = link.get("kind")
        if kind == fitz.LINK_NAMED and link.get("page", -1) in (-1, None):
            name = link.get("nameddest") or link.get("name") or ""
            try:
                page, x, y = self.document.doc.resolve_link("#nameddest=" + name)
            except Exception:
                page, x, y = -1, 0, 0
            if page < 0:
                self.status_message.emit(f"Linkziel „{name}“ gibt es in diesem Dokument nicht.")
                return
            link = dict(link, page=page, to=fitz.Point(x if x == x else 0, y if y == y else 0))
            kind = fitz.LINK_GOTO
        if kind in (fitz.LINK_GOTO, fitz.LINK_NAMED):
            page = link.get("page", -1)
            if page is None or not (0 <= page < len(self.items_)):
                self.status_message.emit("Das Linkziel liegt außerhalb des Dokuments.")
                return
            self._push_history()
            self.goto_location(page, link.get("to"))
            self.status_message.emit(f"Seite {page + 1} – Alt+← oder Maus-Zurücktaste springt zurück.")
            return
        win = self.window()
        if hasattr(win, "open_external_link"):
            win.open_external_link(link, self.document.path)

    def goto_location(self, index, to=None):
        """Zur Stelle 'to' (PDF-Seitenkoordinaten) auf Seite index springen: oben im Fenster, etwas Rand."""
        if to is None or (to.x == 0 and to.y == 0) or to.y != to.y:
            self.goto_page(index)
            return
        self.goto_page(index)
        item = self.items_[index]
        page = self.document.page(index)
        p = fitz.Point(to) * page.rotation_matrix
        target = self.mapFromScene(item.pos() + QPointF(p.x, p.y)).y() - 24
        sb = self.verticalScrollBar()
        sb.setValue(sb.value() + target)
        self._update_current_page()

    def _push_history(self):
        if not self.items_:
            return
        item = self.items_[min(self._current, len(self.items_) - 1)]
        top = self.mapToScene(QPoint(0, 0)).y() - item.y()
        self._history.append((item.index, top))
        del self._history[:-50]

    def go_back(self):
        """Zurück zur Stelle vor dem letzten Link-Sprung."""
        if not self._history:
            self.status_message.emit("Kein vorheriger Ort.")
            return
        index, top = self._history.pop()
        if not (0 <= index < len(self.items_)):
            return
        self.goto_page(index)
        sb = self.verticalScrollBar()
        sb.setValue(sb.value() + self.mapFromScene(QPointF(0, self.items_[index].y() + top)).y())
        self._update_current_page()

    def can_go_back(self):
        return bool(self._history)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Left and event.modifiers() == Qt.AltModifier and self._editor is None:
            self.go_back()
            return
        if self._editor is not None:
            if event.key() == Qt.Key_Escape:
                self.commit_text_edit()
            else:
                super().keyPressEvent(event)  # Eingabe geht ins Textfeld
            return
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self._selected:
            self.delete_selected()
            return
        if event.key() == Qt.Key_Escape:
            self._cancel_interaction()
            self.clear_selection()
            self.clear_text_selection()
            return
        super().keyPressEvent(event)

    # ================================================================ Interaktions-Hilfen
    def _start_pan(self, vpos: QPointF):
        self._stop_kinetic()
        self._panning = True
        self._pan_last = QPointF(vpos)
        self.viewport().setCursor(Qt.ClosedHandCursor)

    def _pan_to(self, vpos: QPointF):
        self._scroll_by(QPointF(vpos) - self._pan_last)
        self._pan_last = QPointF(vpos)

    def _end_pan(self):
        self._panning = False
        self.update_cursor()

    def _end_live(self):
        """Strich-Ebene beenden; die (nun z. B. als Form erkannte) Vorschau zeigt die Überlagerung."""
        if self._live is not None:
            self._live["preview"].setVisible(True)
            self._layer_remove([self._live])
            self._live = None

    def _drag_preview_start(self):
        """Verschieben beginnt: die ausgewählten Zeichnungen als Bild mitführen und auf der Seite ausblenden –
        so sieht man sie live am Finger/Stift/Mauszeiger (statt erst nach dem Loslassen)."""
        if not self._selected:
            return
        pidx = self._selected[0]
        xrefs = set(self._group) if self._group else {self._selected[1]}
        page = self.document.page(pidx)
        for x in list(xrefs):  # Textmarkierungen eines Textfelds wandern mit
            if self.document.annot_kind(pidx, x) == "text":
                xrefs.update(self.document._attached_markups(pidx, _load_annot(page, x)))
        scale = self._device_scale()
        try:
            r = self.renderer.render_annots(pidx, xrefs, scale)
        except Exception:
            r = None
        if r is None:
            return
        img, rect = r
        gi = QGraphicsPixmapItem(QPixmap.fromImage(img), self.items_[pidx])
        gi.setTransformationMode(Qt.SmoothTransformation)
        gi.setScale(1.0 / scale)
        pos = QPointF(rect.x0, rect.y0)
        gi.setPos(pos)
        gi.setZValue(4)
        area = QRectF(rect.x0, rect.y0, rect.width, rect.height)
        self._drag_prev = {"index": pidx, "item": gi, "pos": pos, "xrefs": xrefs, "rect": area}
        self.renderer.set_hidden(pidx, xrefs, area)  # Originale nicht doppelt zeigen
        self.items_[pidx].invalidate()

    def _drag_preview_end(self, moved):
        """Loslassen: nach dem Verschieben bleibt die Vorschau am Ziel, bis die Seite neu gerendert ist."""
        prev, self._drag_prev = self._drag_prev, None
        if prev is None:
            return
        idx, gi = prev["index"], prev["item"]
        self.renderer.hidden.pop(idx, None)  # die Änderung (Verschieben) rendert die Seite ohnehin neu
        if moved:
            self._ghosts.setdefault(idx, []).append(gi)
            QTimer.singleShot(4000, lambda: self._drop_ghost(idx, gi))  # Sicherheitsnetz
        else:
            if gi.scene() is not None:
                gi.scene().removeItem(gi)
            self.renderer.invalidate_page(idx, prev["rect"])
            if idx < len(self.items_):
                self.items_[idx].invalidate()

    def _cancel_interaction(self):
        if self._drag_prev is not None:
            self._drag_preview_end(False)
        if self._live is not None and not self._live["done"]:
            self._layer_remove([self._live])
            self._live = None
        self._hold_timer.stop()
        self._gesture = None
        if self._ruler_stroke is not None:
            self._ruler_stroke = None
            self.ruler.readout = None
        self._remove_preview()
        self._active_page = None
        self._stroke_pts = []
        self._lasso_pts = None
        self._moving = False
        if self._resizing:
            self._resizing = None
            self._update_selection_box()

    def _remove_preview(self):
        if self._preview is not None:
            if self._preview.scene():
                self._preview.scene().removeItem(self._preview)
            self._preview = None

    def _stroke_style(self, tool, as_float=False):
        color = self.settings.color(tool)
        width = self.settings.width(tool)
        opacity = 0.4 if tool == "highlighter" else 1.0
        if as_float:
            return color, width, opacity
        return QColor(*[int(c * 255) for c in color]), width, opacity

    def _on_pen_hold(self):
        """Stift ruht am Strichende: gerade Linie, Kreis/Ellipse oder Rechteck erkennen."""
        if self._preview is None or self._active_tool not in ("pen", "highlighter") or self._gesture \
                or self._ruler_stroke is not None:
            return
        result = gestures.recognize(self._stroke_pts)
        if result is None:
            return
        kind, geo = result
        if kind == "line":
            a, b = geo
            b = gestures.snap_line(a, b)
            self._gesture = ("line", a, b)
            self._end_live()
            self._preview.setPath(self._shape_path("line", QPointF(*a), QPointF(*b)))
        else:
            x0, y0, x1, y1 = geo
            grab = self._stroke_pts[-1]
            # Fixpunkt: dem Stift gegenüber (am Mittelpunkt gespiegelt)
            anchor = (x0 + x1 - grab[0], y0 + y1 - grab[1])
            self._gesture = (kind, geo, grab, anchor, geo)
            self._end_live()
            self._preview.setPath(self._shape_path(kind, QPointF(x0, y0), QPointF(x1, y1)))
        self.status_message.emit({"line": "Gerade Linie – Stift bewegen verschiebt den Endpunkt",
                                  "rect": "Rechteck erkannt – Stift bewegen skaliert",
                                  "ellipse": "Kreis/Ellipse erkannt – Stift bewegen skaliert"}[kind])

    def _shape_path(self, tool, a: QPointF, b: QPointF):
        path = QPainterPath()
        if tool in ("line", "arrow"):
            path.moveTo(a)
            path.lineTo(b)
        elif tool == "ellipse":
            path.addEllipse(_rect(a, b))
        else:
            path.addRect(_rect(a, b))
        return path

    def _erase(self, item, local):
        tol = max(0.125, self.settings.width("eraser") / 2)  # Radius = halbe eingestellte Größe (wie der Kreis)
        # Zwischenpunkte prüfen, damit schnelle Bewegungen keine Striche überspringen
        last = self._erase_last or local
        dist = ((local[0] - last[0]) ** 2 + (local[1] - last[1]) ** 2) ** 0.5
        steps = max(1, min(400, int(dist / max(tol / 2, 0.5))))  # kleiner Radierer: höchstens alle 0,5 pt prüfen
        for k in range(1, steps + 1):
            p = (last[0] + (local[0] - last[0]) * k / steps, last[1] + (local[1] - last[1]) * k / steps)
            if self.document.erase_at(item.index, p, tol, checkpoint=not self._erasing_checkpointed):
                self._erasing_checkpointed = True
        self._erase_last = local

    def _finish_lasso(self, index, pts):
        """Lasso losgelassen: umschlossene Objekte auswählen, sonst umschlossenen Text kopieren."""
        if len(pts) < 3:
            return
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        if max(xs) - min(xs) < 3 or max(ys) - min(ys) < 3:
            return
        xrefs = self.document.annotations_in_lasso(index, pts)
        if xrefs:
            self._select_group(index, xrefs)
            return
        text = self.document.text_in_lasso(index, pts).strip()
        if text:
            QApplication.clipboard().setText(text)
            self.status_message.emit(f"{len(text)} Zeichen in die Zwischenablage kopiert.")
        else:
            self.status_message.emit("Nichts im Lasso gefunden.")

    def _copy_text(self, index, r: QRectF):
        page = self.document.page(index)
        rect = fitz.Rect(r.left(), r.top(), r.right(), r.bottom()) * page.derotation_matrix
        with self.document.reading():
            text = page.get_textbox(rect).strip()
        if text:
            QApplication.clipboard().setText(text)
            self.status_message.emit(f"{len(text)} Zeichen in die Zwischenablage kopiert.")
        else:
            self.status_message.emit("Kein Text im markierten Bereich.")

    # ================================================================ Auswahl
    def _select(self, page_index, xref):
        self._selected = (page_index, xref)
        self._group = None
        self._update_selection_box()
        self.selection_changed.emit(True)
        self._emit_context()

    def _select_group(self, page_index, xrefs):
        """Mehrere Objekte auswählen (Lasso). Ein einzelnes Objekt wird normal ausgewählt."""
        xrefs = list(xrefs)
        if not xrefs:
            self.clear_selection()
            return
        if len(xrefs) == 1:
            self._select(page_index, xrefs[0])
            return
        self._selected = (page_index, xrefs[0])
        self._group = xrefs
        self._update_selection_box()
        self.selection_changed.emit(True)
        self._emit_context()

    def _in_group(self, page_index, xref):
        return bool(self._group) and self._selected[0] == page_index and xref in self._group

    def selection_count(self):
        if not self._selected:
            return 0
        return len(self._group) if self._group else 1

    def selection_kind(self):
        if self._editor is not None:
            return "editing"
        if not self._selected:
            return ""
        if self._group:
            return "group"
        return self.document.annot_kind(*self._selected) or ""

    def _emit_context(self):
        self.context_changed.emit(self.selection_kind())

    def edit_selected_text(self):
        if not self._selected or self._group:
            return
        pidx, xref = self._selected
        kind = self.document.annot_kind(pidx, xref)
        if kind == "text":
            self.start_text_edit(pidx, xref)
        elif kind == "note":
            self._edit_note(pidx, xref)

    def _after_insert(self, page_index, xref):
        """Nach dem Einfügen über ein Werkzeug/eine Schaltfläche: je nach Einstellung zum Maus-Werkzeug,
        sonst das Eingefügte auswählen (zum Verschieben/Skalieren)."""
        if prefs.after_insert_mouse():
            self.tool_requested.emit("pan")
        else:
            self._select_after_insert(page_index, xref)

    # ---- Notizen lesen
    def note_at(self, scene):
        """(Seite, xref) einer Notiz unter dem Punkt oder None."""
        item = self.page_at(scene)
        if item is None:
            return None
        annot = self.document.annotation_at(item.index, self._local(item, scene), self._touch_tolerance())
        if annot is not None and annot.type[0] == fitz.PDF_ANNOT_TEXT:
            return item.index, annot.xref
        return None

    def _note_text(self, index, xref):
        page = self.document.page(index)
        annot = _load_annot(page, xref)
        if annot is None:
            return None, None
        r = annot.rect * page.rotation_matrix
        return annot.info.get("content", ""), QRectF(r.x0, r.y0, r.width, r.height)

    def show_note(self, index, xref):
        """Notiz als Sprechblase neben dem Symbol anzeigen (nur lesen; „Bearbeiten“ öffnet den Dialog)."""
        self.close_note()
        text, rect = self._note_text(index, xref)
        if text is None:
            return
        from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout
        b = QFrame(self.viewport())
        b.setObjectName("noteBubble")
        b.setStyleSheet("#noteBubble{background:#fff8c4;border:1px solid #c9a800;border-radius:6px}"
                        "QLabel{color:#222;background:transparent}"
                        "QPushButton{color:#222;background:#f1e7a6;border:1px solid #c9a800;border-radius:4px;"
                        "padding:3px 10px}QPushButton:hover{background:#ffe97a}")
        lay = QVBoxLayout(b)
        lay.setContentsMargins(10, 8, 10, 8)
        label = QLabel(text or "(leer)", b)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        label.setMaximumWidth(340)
        lay.addWidget(label)
        row = QHBoxLayout()
        row.addStretch(1)
        edit = QPushButton("Bearbeiten", b)
        edit.clicked.connect(lambda: (self.close_note(), self._edit_note(index, xref)))
        row.addWidget(edit)
        close = QPushButton("Schließen", b)
        close.clicked.connect(self.close_note)
        row.addWidget(close)
        lay.addLayout(row)
        b.adjustSize()
        from PySide6.QtWidgets import QToolTip
        QToolTip.hideText()  # sonst stünde der Text doppelt da (Tooltip + Sprechblase)
        self._note_bubble = b
        self._note_anchor = (index, rect)
        self._note_press = None
        self._place_note()

    def _place_note(self):
        """Sprechblase neben das Notizsymbol setzen; ist die Notiz aus dem Bild gescrollt, ausblenden."""
        b = self._note_bubble
        index, rect = self._note_anchor
        if index >= len(self.items_):
            self.close_note()
            return
        r = self.mapFromScene(rect.translated(self.items_[index].pos())).boundingRect()
        vp = self.viewport().rect()
        if not vp.intersects(r):
            b.hide()
            return
        x = min(max(4, r.right() + 6), vp.width() - b.width() - 4)
        y = min(max(4, r.top()), vp.height() - b.height() - 4)
        b.move(x, y)
        b.show()

    def _note_close_check(self, event, t):
        """Tippen/Klicken neben die Sprechblase schließt sie (Einstellung) – Scrollen, Zoomen und Ziehen nicht."""
        def pos():
            if t in (QEvent.TouchBegin, QEvent.TouchUpdate, QEvent.TouchEnd):
                pts = event.points()
                return QPointF(pts[0].position()) if pts else None
            return QPointF(event.position())
        if t in (QEvent.MouseButtonPress, QEvent.TabletPress, QEvent.TouchBegin):
            p = pos()
            inside = p is None or self._note_bubble.geometry().contains(p.toPoint())
            self._note_press = None if inside or not prefs.note_close_outside() else p
        elif self._note_press is None:
            return
        elif t == QEvent.TouchUpdate and len(event.points()) > 1:
            self._note_press = None  # zwei Finger: Zoomen
        elif t in (QEvent.MouseMove, QEvent.TabletMove, QEvent.TouchUpdate, QEvent.MouseButtonRelease,
                   QEvent.TabletRelease, QEvent.TouchEnd):
            p = pos()
            if p is not None and (p - self._note_press).manhattanLength() > 14:
                self._note_press = None  # gezogen (gescrollt): offen lassen
            elif t in (QEvent.MouseButtonRelease, QEvent.TabletRelease, QEvent.TouchEnd):
                self.close_note()
        elif t == QEvent.TouchCancel:
            self._note_press = None

    def close_note(self):
        b, self._note_bubble = getattr(self, "_note_bubble", None), None
        self._note_press = None
        if b is not None:
            b.hide()
            b.deleteLater()

    def _note_hover(self, event):
        """Maus über einer Notiz: Text als Tooltip (höchstens alle 150 ms prüfen)."""
        now = time.monotonic()
        if now - self._note_hover_t < 0.15 or self._editor is not None or self._note_bubble is not None:
            return  # offene Sprechblase: kein zusätzlicher Tooltip
        self._note_hover_t = now
        note = self.note_at(self._to_scene(event.position()))
        from PySide6.QtWidgets import QToolTip
        if note is None:
            return
        text, _r = self._note_text(*note)
        if text:
            QToolTip.showText(event.globalPosition().toPoint(), text[:600], self.viewport())

    def _select_after_insert(self, page_index, xref):
        """Nach dem Einfügen direkt zum Auswahlwerkzeug wechseln, damit man weiterbearbeiten kann."""
        self.tool_requested.emit("select")
        self._select(page_index, xref)

    def selected_is_image(self):
        if not self._selected or self._group:
            return False
        pidx, xref = self._selected
        return self.document.is_image_xref(pidx, xref)

    def _remove_selection_items(self):
        for it in [self._sel_item, *self._handles]:
            if it is not None and it.scene():
                it.scene().removeItem(it)
        self._sel_item = None
        self._handles = []

    def _update_selection_box(self):
        self._remove_selection_items()
        if not self._selected:
            return
        if self._group:
            self._update_group_box()
            return
        pidx, xref = self._selected
        page = self.document.page(pidx)
        annot = _load_annot(page, xref)
        if annot is None:
            self._selected = None
            self.selection_changed.emit(False)
            self._emit_context()
            return
        r = annot.rect * page.rotation_matrix
        self._sel_rect = QRectF(r.x0, r.y0, r.width, r.height)
        # Bilder (Fotos, GIFs) und Videos/Audio behalten beim Vergrößern ihr Seitenverhältnis
        self._sel_image = self.document.is_image(annot) or self.document.annot_kind(pidx, xref) == "media"
        item = self.items_[pidx]
        self._sel_item = QGraphicsRectItem(self._sel_rect, item)
        pen = QPen(QColor("#2f7de1"), 1.5, Qt.DashLine)
        pen.setCosmetic(True)
        self._sel_item.setPen(pen)
        self._sel_item.setBrush(QBrush(QColor(47, 125, 225, 25)))
        # Anfasser an den Ecken (bleiben unabhängig vom Zoom gleich groß)
        if annot.type[0] not in (fitz.PDF_ANNOT_TEXT, fitz.PDF_ANNOT_HIGHLIGHT,
                                 fitz.PDF_ANNOT_UNDERLINE, fitz.PDF_ANNOT_STRIKE_OUT):
            for name, pt in _corners(self._sel_rect).items():
                h = QGraphicsEllipseItem(QRectF(-HANDLE / 2, -HANDLE / 2, HANDLE, HANDLE), item)
                h.setFlag(QGraphicsItem.ItemIgnoresTransformations)
                h.setPos(pt)
                h.setPen(QPen(QColor("#2f7de1"), 2.5))
                h.setBrush(QColor("#ffffff"))
                h.setZValue(10)
                h.setData(0, name)
                self._handles.append(h)

    def _update_group_box(self):
        """Gemeinsamer Rahmen um alle Objekte der Lassoauswahl (ohne Größen-Anfasser)."""
        pidx = self._selected[0]
        rects = []
        alive = []
        for x in self._group:
            r = self.document.annot_rect_display(pidx, x)
            if r is not None:
                rects.append(QRectF(r.x0, r.y0, r.width, r.height))
                alive.append(x)
        if len(alive) < 2:
            self._group = None
            if alive:
                self._selected = (pidx, alive[0])
                self._update_selection_box()
            else:
                self._selected = None
                self.selection_changed.emit(False)
            self._emit_context()
            return
        self._group = alive
        self._selected = (pidx, alive[0])
        box = rects[0]
        for r in rects[1:]:
            box = box.united(r)
        self._sel_rect = box
        self._sel_image = False
        self._sel_item = QGraphicsRectItem(box, self.items_[pidx])
        pen = QPen(QColor("#2f7de1"), 1.5, Qt.DashLine)
        pen.setCosmetic(True)
        self._sel_item.setPen(pen)
        self._sel_item.setBrush(QBrush(QColor(47, 125, 225, 25)))

    def _handle_at(self, vpos: QPointF, exact=False):
        if not self._handles:
            return None
        scale = self._zoom * SCREEN_ZOOM
        small_side = min(self._sel_rect.width(), self._sel_rect.height()) * scale
        hit = HANDLE / 2 + 2 if exact else max(HANDLE / 2 + 2, min(HANDLE_HIT, small_side / 3))
        best, best_d = None, None
        for h in self._handles:
            hp = self.mapFromScene(h.scenePos())
            dx, dy = abs(hp.x() - vpos.x()), abs(hp.y() - vpos.y())
            if dx <= hit and dy <= hit and (best_d is None or dx + dy < best_d):
                best, best_d = h.data(0), dx + dy
        return best

    def _resized_rect(self, p: QPointF) -> QRectF:
        r0 = self._sel_rect
        name = self._resizing
        fixed = _corners(r0)[{"tl": "br", "tr": "bl", "bl": "tr", "br": "tl"}[name]]
        r = QRectF(fixed, p).normalized()
        if self._sel_image and r0.height() > 0 and r.height() > 0:
            # Bilder und Videos behalten ihr Seitenverhältnis
            aspect = r0.width() / r0.height()
            w = max(r.width(), r.height() * aspect)
            h = w / aspect
            x = fixed.x() - w if p.x() < fixed.x() else fixed.x()
            y = fixed.y() - h if p.y() < fixed.y() else fixed.y()
            r = QRectF(x, y, w, h)
        return r

    def clear_selection(self):
        self._remove_selection_items()
        self._group = None
        if self._selected:
            self._selected = None
            self.selection_changed.emit(False)
            self._emit_context()

    def delete_selected(self):
        if not self._selected:
            return
        pidx, xref = self._selected
        group = self._group
        self.clear_selection()
        if group:
            self.document.delete_annotations(pidx, group)
        else:
            self.document.delete_annotation(pidx, xref)

    # ================================================================ Bilder
    def crop_selected(self):
        if not self.selected_is_image():
            return
        pidx, xref = self._selected
        data = self.document.image_bytes(pidx, xref)
        if data is None:
            return
        img = images.load(data)
        dlg = images.CropDialog(img, self)
        if not dlg.exec():
            return
        c = dlg.crop_rect()
        if c == img.rect():
            return
        r = self._sel_rect
        sx, sy = r.width() / img.width(), r.height() / img.height()
        new_rect = (r.left() + c.x() * sx, r.top() + c.y() * sy,
                    r.left() + (c.x() + c.width()) * sx, r.top() + (c.y() + c.height()) * sy)
        new_xref = self.document.replace_image(pidx, xref, images.crop(data, c), new_rect)
        if new_xref:
            self._select(pidx, new_xref)

    def rotate_selected(self, degrees):
        if not self.selected_is_image():
            return
        pidx, xref = self._selected
        data = self.document.image_bytes(pidx, xref)
        if data is None:
            return
        r = self._sel_rect
        c = r.center()
        new_rect = (c.x() - r.height() / 2, c.y() - r.width() / 2,
                    c.x() + r.height() / 2, c.y() + r.width() / 2)
        new_xref = self.document.replace_image(pidx, xref, images.rotate(data, degrees), new_rect)
        if new_xref:
            self._select(pidx, new_xref)

    def paste_image(self, img: QImage, index=None, at=None, inserted=False):
        """Bild aus der Zwischenablage einfügen: mittig auf der aktuellen Seite oder an der Stelle at."""
        if img.isNull() or not self.items_:
            return
        item = self.items_[self._current if index is None else index]
        w = min(item.w * 0.6, img.width() * 0.75)
        h = w * img.height() / img.width()
        if h > item.h * 0.8:  # sehr hohe Bilder: an die Seitenhöhe anpassen
            h = item.h * 0.8
            w = h * img.width() / img.height()
        if at is None:
            x, y = (item.w - w) / 2, (item.h - h) / 3
        else:  # links oben an der angeklickten Stelle, aber ganz auf der Seite
            x = max(0.0, min(at[0], item.w - w))
            y = max(0.0, min(at[1], item.h - h))
        xref = self.document.add_image(item.index, images.encode(img), (x, y, x + w, y + h))
        if inserted:  # z. B. Foto von der Kamera: wie über das Bild-Werkzeug eingefügt
            self._after_insert(item.index, xref)
        else:
            self._select_after_insert(item.index, xref)

    # ================================================================ Kopieren / Einfügen / Stil der Auswahl
    def selection_xrefs(self):
        """(Seite, [xrefs]) der Auswahl oder (None, [])."""
        if not self._selected:
            return None, []
        pidx, xref = self._selected
        return pidx, (list(self._group) if self._group else [xref])

    def copy_selection(self, cut=False):
        """Ausgewähltes in die Zwischenablage: als Objekte (für dieses Programm), dazu Text bzw. Bild für andere."""
        pidx, xrefs = self.selection_xrefs()
        if not xrefs:
            return False
        data = self.document.copy_annotations(pidx, xrefs)
        if not data:
            return False
        md = QMimeData()
        md.setData(CLIP_MIME, QByteArray(data))
        texts = [self.document.text_info(pidx, x)[0] for x in xrefs if self.document.annot_kind(pidx, x) == "text"]
        if texts:
            md.setText("\n".join(texts))
        if len(xrefs) == 1 and self.document.is_image_xref(pidx, xrefs[0]):
            raw = self.document.image_bytes(pidx, xrefs[0])
            img = QImage.fromData(raw) if raw else QImage()
            if not img.isNull():
                md.setImageData(img)
        QApplication.clipboard().setMimeData(md)
        n = len(xrefs)
        if cut:
            self.delete_selected()
        self.status_message.emit(("Ausgeschnitten" if cut else "Kopiert") + (f": {n} Objekte" if n > 1 else "")
                                 + " – Einfügen mit Strg+V oder lange auf die Seite drücken.")
        return True

    def duplicate_selection(self):
        pidx, xrefs = self.selection_xrefs()
        data = self.document.copy_annotations(pidx, xrefs) if xrefs else None
        if not data:
            return
        new = self.document.paste_annotations(pidx, data)
        if new:
            self._select_group(pidx, new)

    def clipboard_kind(self):
        """Was ließe sich einfügen? "objects", "image", "text" oder ""."""
        md = QApplication.clipboard().mimeData()
        if md is None:
            return ""
        if md.hasFormat(CLIP_MIME):
            return "objects"
        win = self.window()
        if md.hasImage() or (hasattr(win, "clipboard_image") and md.hasUrls() and not win.clipboard_image().isNull()):
            return "image"
        if md.hasText() and md.text().strip():
            return "text"
        return ""

    def paste_clipboard(self, index=None, at=None):
        """Einfügen: kopierte Objekte, sonst Bild, sonst Text als Textfeld. at: linke obere Ecke (Seitenkoordinaten),
        sonst an der alten Stelle versetzt bzw. mitten im sichtbaren Teil der Seite."""
        if self._editor is not None:
            self.editor_paste()
            return
        if not self.items_:
            return
        index = self._current if index is None else index
        item = self.items_[index]
        kind = self.clipboard_kind()
        md = QApplication.clipboard().mimeData()
        visible = self.visible_scene_rect().intersected(item.sceneBoundingRect()).translated(-item.pos())
        if kind == "objects":
            data = bytes(md.data(CLIP_MIME))
            if at is None:
                box = self.document.clip_bbox(data)
                if box is not None and not visible.intersects(QRectF(box.x0 + 12, box.y0 + 12, box.width, box.height)):
                    c = visible.center()  # alte Stelle nicht zu sehen: mitten in den sichtbaren Teil
                    at = (c.x() - box.width / 2, c.y() - box.height / 2)
            new = self.document.paste_annotations(index, data, at)
            if new:
                self.tool_requested.emit("select")
                self._select_group(index, new)
        elif kind == "image":
            win = self.window()
            img = win.clipboard_image() if hasattr(win, "clipboard_image") else QApplication.clipboard().image()
            self.paste_image(img, index, at)
        elif kind == "text":
            self._paste_text(md.text(), index, at, visible)
        else:
            self.status_message.emit("Die Zwischenablage ist leer.")

    def _paste_text(self, text, index, at, visible):
        item = self.items_[index]
        fs = float(self.settings.font_size)
        text = text.replace("\r\n", "\n").replace("\t", "    ").strip("\n")
        lines = text.split("\n")
        w = min(item.w * 0.8, max(80.0, max(len(ln) for ln in lines) * fs * 0.52 + 12))
        rows = sum(max(1, int(len(ln) * fs * 0.52 // max(1.0, w - 12)) + 1) for ln in lines)
        h = rows * fs * 1.25 + fs * 0.6
        if at is None:
            c = visible.center()
            at = (c.x() - w / 2, c.y() - h / 2)
        x = max(0.0, min(at[0], item.w - w))
        y = max(0.0, min(at[1], item.h - h))
        xref = self.document.add_text(index, (x, y, x + w, y + h), text, fs, self.settings.color("text"))
        self._select_after_insert(index, xref)

    def editor_copy(self, cut=False):
        """Text im geöffneten Textfeld kopieren/ausschneiden."""
        if self._editor is None:
            return
        cur = self._editor.textCursor()
        if cur.hasSelection():
            QApplication.clipboard().setText(cur.selectedText().replace("\u2029", "\n"))
            if cut:
                cur.removeSelectedText()

    def editor_paste(self):
        if self._editor is not None:
            self._editor.textCursor().insertText(QApplication.clipboard().text().replace("\t", "    "))
            self._editor.show_cursor()

    # ================================================================ Video/Audio
    def media_place(self, name, aspect):
        """Platz für ein neues Video/Audio: (Seite, Rechteck) mitten im sichtbaren Teil der aktuellen Seite."""
        if not self.items_:
            return None
        item = self.items_[min(self._current, len(self.items_) - 1)]
        visible = self.visible_scene_rect().intersected(item.sceneBoundingRect()).translated(-item.pos())
        if visible.isEmpty():
            visible = item.page_rect()
        w = min(item.w * 0.6, visible.width() * 0.8)
        h = w / max(aspect, 0.1)
        if h > visible.height() * 0.8:
            h = visible.height() * 0.8
            w = h * aspect
        if media_kind_of(name)[0] == "audio":  # Audio: kleines Symbol wie eine Notiz
            from .media import AUDIO_ICON_PT
            w = h = AUDIO_ICON_PT
        c = visible.center()
        x = max(0.0, min(c.x() - w / 2, item.w - w))
        y = max(0.0, min(c.y() - h / 2, item.h - h))
        return item.index, (x, y, x + w, y + h)

    def insert_media(self, data, name, poster, aspect, place=None):
        """Video/Audio einfügen (place: (Seite, Rechteck), sonst mitten in den sichtbaren Teil) und auswählen."""
        place = place or self.media_place(name, aspect)
        if place is None or place[0] >= len(self.items_):
            return None
        index, rect = place
        xref = self.document.add_media(index, data, name, rect, poster)
        self._after_insert(index, xref)
        return xref

    def show_recording_marker(self, place):
        """Platzhalter für eine laufende Tonaufnahme: Audio-Symbol mit rotem Punkt an der späteren Stelle."""
        self.remove_recording_marker()
        index, (x0, y0, x1, y1) = place
        if index >= len(self.items_):
            return
        from .media import audio_icon_png
        pm = QPixmap()
        pm.loadFromData(audio_icon_png(128))
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor("#ffffff"), 6))
        p.setBrush(QColor("#e5322d"))
        p.drawEllipse(QPointF(pm.width() - 30, 30), 24, 24)  # „Aufnahme läuft“
        p.end()
        gi = QGraphicsPixmapItem(pm, self.items_[index])
        gi.setTransformationMode(Qt.SmoothTransformation)
        gi.setScale((x1 - x0) / pm.width())
        gi.setPos(x0, y0)
        gi.setZValue(6)
        gi.setToolTip("Aufnahme läuft – Steuerung in der Aufnahmeleiste oben")
        self._rec_marker = gi

    def remove_recording_marker(self):
        gi, self._rec_marker = getattr(self, "_rec_marker", None), None
        if gi is not None and gi.scene() is not None:
            gi.scene().removeItem(gi)

    def play_selected(self):
        pidx, xrefs = self.selection_xrefs()
        if xrefs and self.document.annot_kind(pidx, xrefs[0]) == "media":
            self.media.play(pidx, xrefs[0])

    def export_media(self, index, xref):
        data, name = self.document.media_bytes(index, xref)
        if not data:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Video/Audio speichern", name or "Medien")
        if path:
            with open(path, "wb") as f:
                f.write(data)
            self.status_message.emit(f"Gespeichert: {os.path.basename(path)}")

    def selection_style(self):
        """Stil der Auswahl für die Optionsleiste: Werte des ersten passenden Objekts und was sich ändern lässt."""
        pidx, xrefs = self.selection_xrefs()
        styles = [s for s in (self.document.annot_style(pidx, x) for x in xrefs) if s] if xrefs else []
        if not styles:
            return None

        def first(key):
            return next((s[key] for s in styles if s.get(key) is not None), None)

        return {"color": first("color"), "width": first("width"), "fontsize": first("fontsize"),
                "opacity": first("opacity") if any(s["type"] not in (fitz.PDF_ANNOT_STAMP, fitz.PDF_ANNOT_SCREEN)
                                                   for s in styles) else None}

    def apply_style(self, **kw):
        pidx, xrefs = self.selection_xrefs()
        if not xrefs:
            return
        group = self._group
        self._annot_op = True
        try:
            self.document.set_annot_style(pidx, xrefs, **kw)
        finally:
            self._annot_op = False
        if group:
            self._select_group(pidx, xrefs)
        else:
            self._select(pidx, xrefs[0])

    # ================================================================ Kontextmenü
    def _context_menu(self, global_pt, pos):
        self.commit_text_edit()
        item = self.page_at(pos)
        if item is None:
            return
        idx = item.index
        win = self.window()
        menu = QMenu(self)
        if self._text_selection_hit(idx, self._local(item, pos)):
            menu.addAction("Kopieren", self.copy_selected_text)
            menu.addAction("Textmarker", lambda: self.mark_selected_text("highlight"))
            menu.addAction("Unterstreichen", lambda: self.mark_selected_text("underline"))
            menu.addAction("Durchstreichen", lambda: self.mark_selected_text("strikeout"))
            menu.exec(global_pt)
            return
        annot = self.document.annotation_at(idx, self._local(item, pos))
        if annot is not None and self._in_group(idx, annot.xref):
            menu.addAction("Kopieren", self.copy_selection)
            menu.addAction("Ausschneiden", lambda: self.copy_selection(cut=True))
            menu.addAction("Duplizieren", self.duplicate_selection)
            menu.addAction(f"{len(self._group)} Objekte löschen", self.delete_selected)
            menu.addSeparator()
        elif annot is not None:
            self._select(idx, annot.xref)
            if self.document.annot_kind(idx, annot.xref) == "media":
                xref = annot.xref
                menu.addAction("Abspielen", lambda: self.media.play(idx, xref))
                menu.addAction("Video/Audio speichern unter …", lambda: self.export_media(idx, xref))
            elif self.document.is_image(annot):
                menu.addAction("Bild zuschneiden …", self.crop_selected)
                menu.addAction("Bild nach links drehen", lambda: self.rotate_selected(-90))
                menu.addAction("Bild nach rechts drehen", lambda: self.rotate_selected(90))
            elif annot.type[0] == fitz.PDF_ANNOT_FREE_TEXT:
                xref = annot.xref
                menu.addAction("Text bearbeiten", lambda: self.start_text_edit(idx, xref))
            elif annot.type[0] == fitz.PDF_ANNOT_TEXT:
                xref = annot.xref
                menu.addAction("Notiz lesen", lambda: self.show_note(idx, xref))
                menu.addAction("Notiz bearbeiten …", lambda: self._edit_note(idx, xref))
            menu.addAction("Kopieren", self.copy_selection)
            menu.addAction("Ausschneiden", lambda: self.copy_selection(cut=True))
            menu.addAction("Duplizieren", self.duplicate_selection)
            menu.addAction("Löschen", self.delete_selected)
            menu.addSeparator()
        if self.document.image_page_spec(idx) is not None:
            menu.addAction("Bildseite bearbeiten (Format, Zuschneiden) …", lambda: win.edit_image_page(idx))
            menu.addSeparator()
        kind = self.clipboard_kind()
        if kind:
            pos_local = self._local(item, pos)
            label = {"objects": "Einfügen", "image": "Bild hier einfügen", "text": "Text hier einfügen"}[kind]
            menu.addAction(label, lambda: self.paste_clipboard(idx, pos_local))
            if kind == "image" and hasattr(win, "paste_as_page"):
                menu.addAction("Bild als Seite danach einfügen …", lambda: win.paste_as_page(idx + 1))
            menu.addSeparator()
        menu.addAction("Leere Seite davor einfügen", lambda: win.quick_insert(idx))
        menu.addAction("Leere Seite danach einfügen", lambda: win.quick_insert(idx + 1))
        menu.addAction("Seiten einfügen …", lambda: win.insert_pages_dialog(idx + 1))
        menu.addSeparator()
        menu.addAction("Seite duplizieren", lambda: self.document.duplicate_page(idx))
        menu.addAction("Seite nach links drehen", lambda: self.document.rotate_pages([idx], -90))
        menu.addAction("Seite nach rechts drehen", lambda: self.document.rotate_pages([idx], 90))
        menu.addAction("Seite löschen", lambda: win.delete_pages([idx]))
        menu.exec(global_pt)

    # ================================================================ Suche
    def show_search(self, results, current):
        self.clear_search()
        for i, (pidx, rect) in enumerate(results):
            item = self.items_[pidx]
            ri = QGraphicsRectItem(QRectF(rect.x0, rect.y0, rect.width, rect.height), item)
            ri.setPen(Qt.NoPen)
            color = QColor(255, 120, 0, 120) if i == current else QColor(255, 220, 0, 90)
            ri.setBrush(color)
            self._search_items.append(ri)
        if 0 <= current < len(results):
            pidx, rect = results[current]
            item = self.items_[pidx]
            self.centerOn(item.pos() + QPointF(rect.x0 + rect.width / 2, rect.y0))

    def clear_search(self):
        for ri in self._search_items:
            if ri.scene():
                ri.scene().removeItem(ri)
        self._search_items = []


# ==================================================================== Geometrie
def _rect(a: QPointF, b: QPointF) -> QRectF:
    return QRectF(a, b).normalized()


def _circle_cursor(diameter_px, color: QColor | None, filled=False, dpr=1.0):
    d = max(2.0, min(diameter_px, 250.0))  # unter 2 px wäre der Kreis nicht mehr zu sehen
    size = int(d + 4)
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    r = QRectF((size - d) / 2, (size - d) / 2, d, d)
    if color is None:  # Radierer: grauer Ring
        p.setPen(QPen(QColor(255, 255, 255, 220), 2.5))
        p.drawEllipse(r)
        p.setPen(QPen(QColor(90, 90, 90), 1))
        p.drawEllipse(r)
    elif filled:  # Marker: halbtransparente Fläche
        c = QColor(color)
        c.setAlpha(110)
        p.setPen(QPen(QColor(color).darker(130), 1))
        p.setBrush(c)
        p.drawEllipse(r)
    else:  # Stift: Kreis in Strichfarbe mit hellem Rand für Kontrast
        p.setPen(QPen(QColor(255, 255, 255, 200), 1))
        p.setBrush(color)
        p.drawEllipse(r.adjusted(-0.5, -0.5, 0.5, 0.5))
    p.end()
    return QCursor(pm, size // 2, size // 2)


def _corners(r: QRectF):
    return {"tl": r.topLeft(), "tr": r.topRight(), "bl": r.bottomLeft(), "br": r.bottomRight()}


def _len(p: QPointF):
    return (p.x() ** 2 + p.y() ** 2) ** 0.5


def _dist(a: QPointF, b: QPointF):
    return ((a.x() - b.x()) ** 2 + (a.y() - b.y()) ** 2) ** 0.5


def _smooth(pts, sigma):
    """Strich glätten: Gauß-Mittel entlang der Strichlänge (sigma in pt). Gewichtet nach Wegstück statt nach
    Punktanzahl – so wirkt die Glättung bei langsamem (viele dichte Punkte) und schnellem Schreiben gleich.
    Zu den Enden hin wird sigma kleiner: Anfang und Ende bleiben an ihrem Platz, ohne Haken."""
    n = len(pts)
    if sigma <= 0 or n < 3:
        return pts
    s = [0.0]
    for i in range(1, n):
        s.append(s[-1] + math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]))
    total = s[-1]
    ds = [(s[min(j + 1, n - 1)] - s[max(j - 1, 0)]) / 2 or 1e-6 for j in range(n)]
    out = [pts[0]]
    lo = 0
    for i in range(1, n - 1):
        sg = min(sigma, s[i] / 2, (total - s[i]) / 2)
        if sg < 1e-3:
            out.append(pts[i])
            continue
        reach = 3 * sg
        while s[lo] < s[i] - reach:
            lo += 1
        k = -0.5 / (sg * sg)
        wx = wy = ws = 0.0
        j = lo
        while j < n and s[j] <= s[i] + reach:
            w = math.exp(k * (s[j] - s[i]) ** 2) * ds[j]
            wx += w * pts[j][0]
            wy += w * pts[j][1]
            ws += w
            j += 1
        out.append((wx / ws, wy / ws))
    out.append(pts[-1])
    return out
