"""Seitenfläche über die Grafikkarte (QRhi, Direct3D 11/12).

Gemessen auf Intel Iris Xe, 2880×1920 bei 120 Hz: Die normale (Software-)Darstellung schafft beim
Scrollen ~50 Bilder/s, weil Windows jedes Bild als große Fläche übertragen muss. Über Direct3D
werden die fertigen Seitenkacheln als Texturen gezeichnet – volle 120 Bilder/s. OpenGL und Vulkan
sind in Fenstern auf diesem Grafikchip auf 60 Hz begrenzt und werden deshalb nicht verwendet.

Die PageView (QGraphicsView) bleibt für Eingaben und Logik zuständig; dieses Widget ersetzt nur
ihre Zeichenfläche (Viewport):
  * Seiten: Kacheln aus dem TileRenderer als Texturen, Lücken mit Vorschau/anderer Zoomstufe.
  * Überlagerung: alles andere in der Szene (Vorschau-Striche, Auswahlrahmen, Textfeld, Suche)
    wird mit QPainter in ein Bild gezeichnet und als Textur darübergelegt. Das Bild ist an
    Szenenkoordinaten gebunden: Beim Scrollen wird es nur verschoben, nicht neu gezeichnet.
  * Bildlaufbalken: schmal, direkt auf der Grafikkarte (ein normales Widget, das sich bei
    jedem Bild neu zeichnet, würde die Bildrate wieder auf ~50 drücken).
"""
from __future__ import annotations

import math
import os
import time
from array import array
from collections import OrderedDict

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QImage, QMatrix4x4, QPainter, QPainterPath, QPen, QRhiBuffer,
                           QRhiDepthStencilClearValue, QRhiGraphicsPipeline, QRhiSampler,
                           QRhiShaderResourceBinding, QRhiShaderStage, QRhiTexture,
                           QRhiTextureSubresourceUploadDescription, QRhiTextureUploadDescription,
                           QRhiTextureUploadEntry, QRhiVertexInputAttribute, QRhiVertexInputBinding,
                           QRhiVertexInputLayout, QRhiViewport, QShader)
from PySide6.QtWidgets import QRhiWidget

from . import theme
from .renderer import MEMORY_SCALE, P_BASE, P_VISIBLE, TILE, level_for

HERE = os.path.dirname(os.path.abspath(__file__))
FLOATS_PER_VERTEX = 8          # x, y, u, v, r, g, b, a
TEX_BUDGET = int(320 * 1024 * 1024 * MEMORY_SCALE)  # Texturspeicher für Kacheln (bei Intel-Grafik im Arbeitsspeicher)
MAX_UPLOADS = 24               # neue Kachel-Texturen pro Bild (Rest im nächsten Bild)
SCROLLBAR = 6                  # Breite des Bildlaufbalkens (logische Pixel), beim Überfahren breiter
SCROLLBAR_HOVER = 10
SCROLLBAR_MARGIN = 3
SCROLLBAR_MIN = 36

APIS = {"d3d11": QRhiWidget.Api.Direct3D11, "d3d12": QRhiWidget.Api.Direct3D12}


def _shader(name):
    with open(os.path.join(HERE, "shaders", name), "rb") as f:
        return QShader.fromSerialized(f.read())


def _premul(c: QColor, alpha=1.0):
    a = c.alphaF() * alpha
    return (c.redF() * a, c.greenF() * a, c.blueF() * a, a)


WHITE = (1.0, 1.0, 1.0, 1.0)
BLACK = (0.0, 0.0, 0.0, 1.0)

# Video: Y'CbCr -> RGB (Kr, Kb je Farbraum); Ecken (oben links, oben rechts, unten rechts, unten links) je Drehung
_YUV_K = {"601": (0.299, 0.114), "709": (0.2126, 0.0722), "2020": (0.2627, 0.0593)}
_UV_CORNERS = {0: ((0, 0), (1, 0), (1, 1), (0, 1)), 90: ((0, 1), (0, 0), (1, 0), (1, 1)),
               180: ((1, 1), (0, 1), (0, 0), (1, 0)), 270: ((1, 0), (1, 1), (0, 1), (0, 0))}


def video_color_matrix(space, full_range, nv12):
    """Uniform-Daten für video.frag: Farbmatrix (spaltenweise, std140) + NV12-Schalter."""
    kr, kb = _YUV_K.get(space, _YUV_K["709"])
    kg = 1.0 - kr - kb
    rv = 2 * (1 - kr)
    bu = 2 * (1 - kb)
    gu, gv = -bu * kb / kg, -rv * kr / kg
    ys, cs = (1.0, 1.0) if full_range else (255 / 219, 255 / 224)
    yo, co = (0.0, -128 / 255 * cs) if full_range else (-16 / 255 * ys, -128 / 255 * cs)
    cols = [(ys, ys, ys, 0.0), (0.0, gu * cs, bu * cs, 0.0), (rv * cs, gv * cs, 0.0, 0.0),
            (yo + rv * co, yo + (gu + gv) * co, yo + bu * co, 1.0)]
    return array("f", [x for c in cols for x in c] + [1.0 if nv12 else 0.0, 0.0, 0.0, 0.0]).tobytes()


def log(msg):
    """Kurze Notiz ins Diagnoseprotokoll (qt.log, siehe main.py) – nur Haupt-Thread."""
    try:
        os.write(2, (time.strftime("%H:%M:%S ") + msg + "\n").encode("utf-8", "replace"))
    except OSError:
        pass


class GpuViewport(QRhiWidget):
    # Grafikgerät verloren (Treiber-Neustart): die Ansicht schaltet auf normale Darstellung um
    device_lost = Signal()

    def __init__(self, view, api="d3d11"):
        super().__init__()
        self.view = view
        self.setApi(APIS.get(api, QRhiWidget.Api.Direct3D11))
        self.setAttribute(Qt.WA_AcceptTouchEvents, True)
        self.setMouseTracking(True)
        self._rhi = None
        self._ps = None
        self._textures: OrderedDict = OrderedDict()  # cacheKey -> [tex, srb_nearest, srb_linear, bytes]
        self._tex_bytes = 0
        self._vbuf = None
        self._vbuf_size = 0
        self._overlay = None        # [tex, srb, QSize]
        self._ruler = {}            # "body"/"label" -> [Bildschlüssel, tex, srb, QSize] (Lineal)
        # Strich-Ebenen je Deckkraft (Stift 1.0, Marker 0.4): {"tex", "srb", "img", "drawn": {id(strich): n}}
        self._layers = {}
        self._layer_geo = None      # (Szenenpunkt oben links, Maßstab, Größe, Epoche) der Ebenen
        self._shown = {}            # Kachel -> zuletzt gezeigter Eintrag (falls die neue Textur noch fehlt)
        self._uploads_left = 0
        self._pending_init = None
        self.frame_count = 0
        self._video_ps = None
        self.video_sources = []      # abspielende Videos (MediaPlayerWidget), gezeichnet auf der Grafikkarte
        self._videos = {}            # id(Quelle) -> Texturen/Bindungen des Videobilds
        self._bars = {}              # id(Quelle) -> Textur der Bedienleiste
        self._zombies = []           # Ressourcen eines verlorenen Geräts: nicht mehr anfassen, nur festhalten
        self._lost_reported = False
        # Gerät alle 2 s prüfen: Ohne neues Bild (z. B. Dialog offen) fiele ein Verlust sonst erst spät auf
        self._lost_check = QTimer(self)
        self._lost_check.setInterval(2000)
        self._lost_check.timeout.connect(self._check_device)
        self._lost_check.start()

    # ------------------------------------------------------------ Grundgerüst
    def releaseResources(self):
        """Qt gibt die Grafikkarten-Schnittstelle auf – Gerät verloren (Treiber-Neustart, „Bildschirm kurz schwarz“)
        oder das Widget kommt in ein anderes Fenster. Vorher alle eigenen Grafik-Ressourcen freigeben: Sonst zeigen
        sie auf ein gelöschtes Direct3D-Gerät, und ihr späteres Benutzen oder Aufräumen bringt das Programm zum
        Absturz bzw. Hängen (z. B. beim Speichern). initialize() baut danach alles neu auf."""
        log(f"GPU: releaseResources (Gerät verloren: {self._device_lost()})")
        res = []
        for entry in self._textures.values():
            res += [entry[2], entry[1], entry[0]]
        if self._overlay is not None:
            res += [self._overlay[1], self._overlay[0]]
        for st in self._ruler.values():
            res += [st[2], st[1]]
        for layer in self._layers.values():
            res += [layer["srb"], layer["tex"]]
        for st in self._videos.values():
            res += [st["srb"], *st["texs"], st.get("cbuf")]
        for st in self._bars.values():
            res += [st["srb"], st["tex"]]
        for name in ("_white_srb", "_video_template", "_ps", "_video_ps", "_vbuf", "_ubuf", "_video_cbuf",
                     "_white_tex", "_nearest", "_linear"):
            res.append(getattr(self, name, None))
        done = set()
        for r in res:
            if r is not None and id(r) not in done:
                done.add(id(r))
                try:
                    r.destroy()
                except Exception:
                    pass
        self._reset()
        for name in ("_white_srb", "_video_template", "_vbuf", "_ubuf", "_video_cbuf", "_white_tex",
                     "_nearest", "_linear"):
            setattr(self, name, None)
        self._pending_init = None  # Stapel des alten Geräts
        self._rhi = None

    def _check_device(self):
        if self._rhi is None or self._lost_reported:
            return
        if self._device_lost():
            self._lost_reported = True
            log("GPU: Gerät verloren (Prüfung)")
            self.device_lost.emit()
        elif self.isVisible():
            self.update()  # Qt merkt einen Geräteverlust erst beim nächsten Bild (kostet ~3 ms alle 2 s)

    def _device_ok(self):
        """Darf man Ressourcen jetzt noch zerstören? Nur mit dem aktuellen, intakten Gerät."""
        rhi = self._rhi
        try:
            return rhi is not None and rhi is self.rhi() and not rhi.isDeviceLost()
        except RuntimeError:
            return False

    def _device_lost(self):
        try:
            return self._rhi is not None and self._rhi.isDeviceLost()
        except RuntimeError:
            return True

    def _discard(self, resources):
        """Ressourcen freigeben – bei verlorenem Gerät nur festhalten (Freigeben stürzte dann ab)."""
        resources = [r for r in resources if r is not None and r is not getattr(self, "_white_tex", None)]
        if self._device_ok():
            for r in resources:
                r.destroy()
        else:
            self._zombies.extend(resources)

    def _reset(self):
        self._ps = None
        self._textures.clear()
        self._tex_bytes = 0
        self._vbuf = None
        self._vbuf_size = 0
        self._overlay = None
        self._ruler = {}
        self._layers = {}
        self._layer_geo = None
        self._shown = {}
        self._video_ps = None
        self._videos = {}
        self._bars = {}

    def initialize(self, cb):
        rhi = self.rhi()
        if rhi is not self._rhi:
            log(f"GPU: initialize mit {'neuem' if self._rhi is not None or self._zombies else 'erstem'} Gerät")
            self._reset()
            self._zombies = []  # gehörten zum alten Gerät, das Qt inzwischen abgebaut hat
            self._lost_reported = False
            self._rhi = rhi
        if self._ps is not None:
            return
        self._nearest = rhi.newSampler(QRhiSampler.Nearest, QRhiSampler.Nearest, QRhiSampler.None_,
                                       QRhiSampler.ClampToEdge, QRhiSampler.ClampToEdge)
        self._nearest.create()
        self._linear = rhi.newSampler(QRhiSampler.Linear, QRhiSampler.Linear, QRhiSampler.None_,
                                      QRhiSampler.ClampToEdge, QRhiSampler.ClampToEdge)
        self._linear.create()
        self._ubuf = rhi.newBuffer(QRhiBuffer.Dynamic, QRhiBuffer.UniformBuffer, 64)
        self._ubuf.create()
        white = QImage(4, 4, QImage.Format_RGBA8888)
        white.fill(Qt.white)
        self._white_tex = rhi.newTexture(QRhiTexture.RGBA8, white.size())
        self._white_tex.create()
        self._white_srb = self._make_srb(self._white_tex, self._nearest)
        self._pending_init = rhi.nextResourceUpdateBatch()
        self._pending_init.uploadTexture(self._white_tex, white)

        ps = rhi.newGraphicsPipeline()
        ps.setShaderStages([QRhiShaderStage(QRhiShaderStage.Vertex, _shader("page.vert.qsb")),
                            QRhiShaderStage(QRhiShaderStage.Fragment, _shader("page.frag.qsb"))])
        layout = QRhiVertexInputLayout()
        layout.setBindings([QRhiVertexInputBinding(FLOATS_PER_VERTEX * 4)])
        layout.setAttributes([QRhiVertexInputAttribute(0, 0, QRhiVertexInputAttribute.Float2, 0),
                              QRhiVertexInputAttribute(0, 1, QRhiVertexInputAttribute.Float2, 8),
                              QRhiVertexInputAttribute(0, 2, QRhiVertexInputAttribute.Float4, 16)])
        ps.setVertexInputLayout(layout)
        blend = QRhiGraphicsPipeline.TargetBlend()
        blend.enable = True  # vormultipliziertes Alpha (Schatten, Überlagerung)
        blend.srcColor = QRhiGraphicsPipeline.One
        blend.dstColor = QRhiGraphicsPipeline.OneMinusSrcAlpha
        blend.srcAlpha = QRhiGraphicsPipeline.One
        blend.dstAlpha = QRhiGraphicsPipeline.OneMinusSrcAlpha
        ps.setTargetBlends([blend])
        ps.setShaderResourceBindings(self._white_srb)
        ps.setRenderPassDescriptor(self.renderTarget().renderPassDescriptor())
        ps.create()
        self._ps = ps
        self._video_ps = self._make_video_pipeline(layout, blend)

    def _make_video_pipeline(self, layout, blend):
        """Zweite Pipeline: Videobild aus Y/U/V-Texturen (Umrechnung nach RGB im Shader)."""
        rhi = self._rhi
        if not (rhi.isTextureFormatSupported(QRhiTexture.R8) and rhi.isTextureFormatSupported(QRhiTexture.RG8)):
            return None
        self._video_cbuf = rhi.newBuffer(QRhiBuffer.Dynamic, QRhiBuffer.UniformBuffer, 80)
        self._video_cbuf.create()
        srb = self._video_srb([self._white_tex] * 3, self._video_cbuf)
        ps = rhi.newGraphicsPipeline()
        ps.setShaderStages([QRhiShaderStage(QRhiShaderStage.Vertex, _shader("page.vert.qsb")),
                            QRhiShaderStage(QRhiShaderStage.Fragment, _shader("video.frag.qsb"))])
        ps.setVertexInputLayout(layout)
        ps.setTargetBlends([blend])
        ps.setShaderResourceBindings(srb)
        ps.setRenderPassDescriptor(self.renderTarget().renderPassDescriptor())
        ps.create()
        self._video_template = srb
        return ps

    def _video_srb(self, texs, cbuf):
        B = QRhiShaderResourceBinding
        srb = self._rhi.newShaderResourceBindings()
        srb.setBindings([B.uniformBuffer(0, B.VertexStage, self._ubuf),
                         B.sampledTexture(1, B.FragmentStage, texs[0], self._linear),
                         B.sampledTexture(2, B.FragmentStage, texs[1], self._linear),
                         B.sampledTexture(3, B.FragmentStage, texs[2], self._linear),
                         B.uniformBuffer(4, B.FragmentStage, cbuf)])
        srb.create()
        return srb

    def _make_srb(self, tex, sampler):
        srb = self._rhi.newShaderResourceBindings()
        srb.setBindings([
            QRhiShaderResourceBinding.uniformBuffer(0, QRhiShaderResourceBinding.VertexStage, self._ubuf),
            QRhiShaderResourceBinding.sampledTexture(1, QRhiShaderResourceBinding.FragmentStage, tex, sampler)])
        srb.create()
        return srb

    # ------------------------------------------------------------ Texturen
    def _cached_srb(self, key, linear):
        """Bereits hochgeladene Textur zum Bildschlüssel (oder None) – ohne neu hochzuladen."""
        entry = self._textures.get(key)
        if entry is None:
            return None
        i = 2 if linear else 1
        if entry[i] is None:
            entry[i] = self._make_srb(entry[0], self._linear if linear else self._nearest)
        return entry[i]

    def _texture(self, img: QImage, batch, linear):
        """Textur für ein Kachelbild (einmal hochgeladen, danach aus dem Speicher). None = noch nicht da."""
        key = img.cacheKey()
        entry = self._textures.get(key)
        if entry is None:
            if self._uploads_left <= 0:
                return None
            self._uploads_left -= 1
            tex = self._rhi.newTexture(QRhiTexture.RGBA8, img.size())
            tex.create()
            batch.uploadTexture(tex, img)
            nbytes = img.width() * img.height() * 4
            entry = [tex, None, None, nbytes]
            self._textures[key] = entry
            self._tex_bytes += nbytes
        else:
            self._textures.move_to_end(key)
        i = 2 if linear else 1
        if entry[i] is None:
            entry[i] = self._make_srb(entry[0], self._linear if linear else self._nearest)
        return entry[i]

    def _evict(self, keep):
        while self._tex_bytes > TEX_BUDGET and self._textures:
            key, entry = next(iter(self._textures.items()))
            if key in keep:
                break
            self._textures.popitem(last=False)
            self._tex_bytes -= entry[3]
            for r in entry[2::-1]:
                if r is not None:
                    r.destroy()

    def drop_textures(self):
        """Alle Kachel-Texturen verwerfen (z. B. nach einem neuen Dokument)."""
        res = []
        for entry in self._textures.values():
            res += entry[2::-1]
        self._discard(res)
        self._textures.clear()
        self._tex_bytes = 0
        self._shown.clear()

    # ------------------------------------------------------------ Bild aufbauen
    def render(self, cb):
        t_frame = time.perf_counter()
        view = self.view
        rhi = self._rhi
        if rhi is None or rhi is not self.rhi() or self._ps is None:
            return  # Gerät gewechselt: Qt baut es neu auf (releaseResources, initialize)
        if rhi.isDeviceLost():
            if not self._lost_reported:  # Treiber-Neustart: nicht weiter mit dem toten Gerät arbeiten
                self._lost_reported = True
                log("GPU: Gerät verloren (Grafiktreiber neu gestartet?)")
                QTimer.singleShot(0, self.device_lost.emit)
            return
        size = self.renderTarget().pixelSize()
        w, h = size.width(), size.height()
        dpr = w / max(1, self.width())
        batch = self._pending_init or rhi.nextResourceUpdateBatch()
        self._pending_init = None
        self._uploads_left = MAX_UPLOADS
        self.frame_count += 1
        verts = array("f")
        draws = []  # (srb, Anzahl Ecken, Pipeline)

        def quad(srb, x, y, qw, qh, u0=0.0, v0=0.0, u1=1.0, v1=1.0, color=WHITE, ps=None, corners=None):
            r, g, b, a = color
            x1, y1 = x + qw, y + qh
            if corners is None:
                tl, tr, br, bl = (u0, v0), (u1, v0), (u1, v1), (u0, v1)
            else:  # freie Texturecken (gedrehtes Video)
                tl, tr, br, bl = corners
            verts.extend((x, y, *tl, r, g, b, a, x, y1, *bl, r, g, b, a, x1, y, *tr, r, g, b, a,
                          x1, y, *tr, r, g, b, a, x, y1, *bl, r, g, b, a, x1, y1, *br, r, g, b, a))
            if draws and draws[-1][0] is srb:
                draws[-1][1] += 6
            else:
                draws.append([srb, 6, ps or self._ps])

        def poly(srb, pts, color=WHITE):
            """Freies Viereck (gedrehtes Lineal): Ecken oben links, oben rechts, unten rechts, unten links."""
            r, g, b, a = color
            (x0, y0), (x1, y1), (x2, y2), (x3, y3) = pts
            verts.extend((x0, y0, 0.0, 0.0, r, g, b, a, x3, y3, 0.0, 1.0, r, g, b, a, x1, y1, 1.0, 0.0, r, g, b, a,
                          x1, y1, 1.0, 0.0, r, g, b, a, x3, y3, 0.0, 1.0, r, g, b, a, x2, y2, 1.0, 1.0, r, g, b, a))
            if draws and draws[-1][0] is srb:
                draws[-1][1] += 6
            else:
                draws.append([srb, 6, self._ps])

        incomplete = self._pages(view, dpr, batch, quad)
        self._draw_videos(dpr, batch, quad)
        self._draw_overlay(view, dpr, batch, quad)
        self._draw_ruler(view, dpr, batch, quad, poly)
        self._draw_stroke(view, dpr, batch, quad, size)
        self._draw_scrollbars(view, dpr, quad)

        # Eckdaten hochladen
        data = verts.tobytes()
        if data:
            if self._vbuf is None or self._vbuf_size < len(data):
                self._vbuf_size = max(len(data), self._vbuf_size * 2, 64 * 1024)
                self._vbuf = rhi.newBuffer(QRhiBuffer.Dynamic, QRhiBuffer.VertexBuffer, self._vbuf_size)
                self._vbuf.create()
            batch.updateDynamicBuffer(self._vbuf, 0, len(data), data)
        mvp = QMatrix4x4()
        mvp.ortho(0, w, h, 0, -1, 1)
        mvp = rhi.clipSpaceCorrMatrix() * mvp
        batch.updateDynamicBuffer(self._ubuf, 0, 64, array("f", mvp.data()).tobytes())

        cb.beginPass(self.renderTarget(), theme.CANVAS, QRhiDepthStencilClearValue(1.0, 0), batch)
        if data:
            first = 0
            current = None
            for srb, n, ps in draws:
                if ps is not current:  # Pipeline wechseln (Seiten/Überlagerung bzw. Video)
                    current = ps
                    cb.setGraphicsPipeline(ps)
                    cb.setViewport(QRhiViewport(0, 0, w, h))
                    cb.setVertexInput(0, [(self._vbuf, 0)])
                cb.setShaderResources(srb)
                cb.draw(n, 1, first, 0)
                first += n
        cb.endPass()
        self._evict(set())
        view._prefetch()
        # dem Renderer sagen, wann das nächste Bild fällig ist (Kacheln nur in der freien Zeit dazwischen)
        screen = self.screen()
        hz = screen.refreshRate() if screen is not None else 60.0
        view.renderer.next_frame_at = t_frame + 1.0 / (hz if 30 <= hz <= 360 else 60.0)
        if incomplete:
            self.update()  # weitere Kacheln im nächsten Bild hochladen

    # ---- Video
    def add_video_source(self, src):
        if src not in self.video_sources:
            self.video_sources.append(src)
        self.update()

    def remove_video_source(self, src):
        if src in self.video_sources:
            self.video_sources.remove(src)
        self._drop_video(id(src))
        bar = self._bars.pop(id(src), None)
        if bar is not None:
            self._discard([bar["srb"], bar["tex"]])
        self.update()

    def _drop_video(self, key):
        st = self._videos.pop(key, None)
        if st is not None:
            self._discard([st["srb"], *st["texs"], st.get("cbuf")])

    def _draw_videos(self, dpr, batch, quad):
        """Aktuelles Bild jedes laufenden Videos: Ebenen hochladen (nur bei neuem Bild) und zeichnen."""
        for src in list(self.video_sources):
            if not src.isVisible():
                continue
            rect = src.video_rect()
            if rect is not None and not rect.isEmpty():
                self._draw_video(src, rect, dpr, batch, quad)
            self._draw_bar(src, dpr, batch, quad)

    def _draw_bar(self, src, dpr, batch, quad):
        """Bedienleiste des Players (selbst gezeichnetes Bild, neu hochgeladen nur bei Änderung)."""
        r = src.bar_rect()
        if r.isEmpty():
            return
        st = self._bars.get(id(src))
        if st is None or st["serial"] != src.bar.serial or st["dpr"] != dpr:
            img = src.bar.image(dpr)
            if st is None or st["size"] != img.size():
                if st is not None:
                    st["srb"].destroy()
                    st["tex"].destroy()
                tex = self._rhi.newTexture(QRhiTexture.RGBA8, img.size())
                tex.create()
                st = {"tex": tex, "srb": self._make_srb(tex, self._linear), "size": img.size()}
                self._bars[id(src)] = st
            batch.uploadTexture(st["tex"], img)
            st["serial"], st["dpr"] = src.bar.serial, dpr
        quad(st["srb"], r.x() * dpr, r.y() * dpr, st["size"].width(), st["size"].height())

    def _draw_video(self, src, rect, dpr, batch, quad):
        """Schwarze Bildfläche, darauf das Videobild (Seitenverhältnis bleibt, Drehung aus den Metadaten)."""
        x, y, rw, rh = rect.x() * dpr, rect.y() * dpr, rect.width() * dpr, rect.height() * dpr
        quad(self._white_srb, x, y, rw, rh, color=BLACK)
        frame = src.current_frame()
        if frame is None:
            return
        st = self._videos.get(id(src))
        if st is None or st["serial"] != src.frame_serial:
            st = self._upload_video(src, frame, batch, st)
            if st is None:
                return
        fw, fh = st["size"]
        if st["rot"] in (90, 270):
            fw, fh = fh, fw
        s = min(rw / max(1, fw), rh / max(1, fh))
        dw, dh = fw * s, fh * s
        corners = _UV_CORNERS[st["rot"]]
        if st["mirror"]:
            corners = tuple((1 - u, v) for u, v in corners)
        quad(st["srb"], x + (rw - dw) / 2, y + (rh - dh) / 2, dw, dh, ps=st["ps"], corners=corners)

    def _upload_video(self, src, data, batch, st):
        """Ebenen des neuesten Bilds hochladen (aufbereitet im Hintergrund, siehe media.video_planes)."""
        w, h, planes = data["w"], data["h"], data["planes"]
        cw, ch = (w + 1) // 2, (h + 1) // 2
        if planes is not None and self._video_ps is not None:
            layout = [(QRhiTexture.R8, w, h), (QRhiTexture.RG8, cw, ch)] if len(planes) == 2 else \
                [(QRhiTexture.R8, w, h), (QRhiTexture.R8, cw, ch), (QRhiTexture.R8, cw, ch)]
        else:
            layout = None
            if data["image"] is None:
                return None
        key = (data["fmt"], w, h, data["space"], data["full"], layout is None)
        if st is None or st["key"] != key:
            self._drop_video(id(src))
            rhi = self._rhi
            if layout is None:
                tex = rhi.newTexture(QRhiTexture.RGBA8, QSize(w, h))
                tex.create()
                st = {"texs": [tex], "srb": self._make_srb(tex, self._linear), "cbuf": None, "ps": self._ps}
            else:
                texs = []
                for f, pw, ph in layout:
                    t = rhi.newTexture(f, QSize(pw, ph))
                    t.create()
                    texs.append(t)
                cbuf = rhi.newBuffer(QRhiBuffer.Dynamic, QRhiBuffer.UniformBuffer, 80)
                cbuf.create()
                batch.updateDynamicBuffer(cbuf, 0, 80, video_color_matrix(data["space"], data["full"], len(texs) == 2))
                bound = texs + [texs[1]] * (3 - len(texs))
                if data["fmt"] == "Format_YV12":
                    bound = [texs[0], texs[2], texs[1]]  # YV12: V-Ebene vor U
                st = {"texs": texs, "srb": self._video_srb(bound, cbuf), "cbuf": cbuf, "ps": self._video_ps}
            st.update(key=key, size=(w, h))
            self._videos[id(src)] = st
        st["rot"], st["mirror"], st["serial"] = data["rot"], data["mirror"], src.frame_serial
        if layout is None:
            batch.uploadTexture(st["texs"][0], data["image"])
            return st
        for tex, (buf, stride), (_f, pw, ph) in zip(st["texs"], planes, layout):
            row = pw * (2 if _f == QRhiTexture.RG8 else 1)
            if stride < row or buf.size() < stride * (ph - 1) + row:
                return None  # unvollständiges Bild: lieber auslassen als über das Ende hinaus lesen
        for tex, (buf, stride) in zip(st["texs"], planes):
            desc = QRhiTextureSubresourceUploadDescription(buf)
            desc.setDataStride(stride)
            batch.uploadTexture(tex, QRhiTextureUploadDescription(QRhiTextureUploadEntry(0, 0, desc)))
        return st

    # ---- Seiten
    def _pages(self, view, dpr, batch, quad):
        """Sichtbare Seiten als Kacheln. Gibt True zurück, wenn Texturen noch fehlen."""
        r = view.renderer
        vt = view.viewportTransform()
        s = vt.m11()
        dev = s * dpr
        level = level_for(dev)
        exact = abs(level - dev) < 1e-6 and abs(vt.m12()) < 1e-9
        visible = view.visible_scene_rect()
        center = visible.center()
        edge = _premul(theme.PAGE_EDGE)
        incomplete = False
        white = self._white_srb
        for item in view.items_:
            br = item.sceneBoundingRect()
            if not visible.intersects(br):
                if br.top() > visible.bottom():
                    break
                continue
            idx = item.index
            o = vt.map(item.pos())
            ox, oy = o.x() * dpr, o.y() * dpr
            pw, ph = item.w * dev, item.h * dev
            # dünner Rand rund um die Seite (1 Gerätepixel außerhalb, auch schwarze Seiten bleiben erkennbar)
            ex, ey, ew, eh = round(ox), round(oy), round(ox + pw) - round(ox), round(oy + ph) - round(oy)
            d1 = max(1.0, round(dpr))
            quad(white, ex - d1, ey - d1, ew + 2 * d1, d1, color=edge)
            quad(white, ex - d1, ey + eh, ew + 2 * d1, d1, color=edge)
            quad(white, ex - d1, ey, d1, eh, color=edge)
            quad(white, ex + ew, ey, d1, eh, color=edge)
            page_rect = item.page_rect()
            exposed = visible.translated(-item.pos()).intersected(page_rect)
            if exposed.isEmpty():
                continue

            step = TILE / level
            tx0, tx1 = int(exposed.left() // step), int((exposed.right() - 1e-6) // step)
            ty0, ty1 = int(exposed.top() // step), int((exposed.bottom() - 1e-6) // step)
            pcenter = center - item.pos()
            ready, missing = [], []
            for ty in range(ty0, ty1 + 1):
                for tx in range(tx0, tx1 + 1):
                    entry = r.get_tile((idx, level, tx, ty))
                    trect = QRectF(tx * step, ty * step, step, step).intersected(page_rect)
                    if entry is not None:
                        ready.append((entry, trect, (idx, level, tx, ty)))
                        if not r.is_stale(idx, entry[1]):
                            continue
                    else:
                        missing.append(trect)
                    if not r.zooming:
                        r.request(("tile", idx, level, tx, ty), (P_VISIBLE, (trect.center() - pcenter).manhattanLength()))
            base = r.get_base(idx)
            if base is None or r.is_stale(idx, base[1]):
                r.request(("base", idx), (P_BASE, 0.0))

            # Texturen der fertigen Kacheln vorab holen (Hochladen begrenzt -> Rest wie Lücken behandeln)
            tiles = []
            for entry, trect, tkey in ready:
                srb = self._texture(entry[0], batch, not exact)
                if srb is None:
                    incomplete = True
                    # neue Fassung der Kachel (z. B. nach einem Strich) noch nicht hochgeladen: die vorige
                    # weiter zeigen statt kurz die unscharfe Vorschau (sonst flackert es beim Schreiben)
                    prev = self._shown.get(tkey)
                    psrb = self._cached_srb(prev[0], not exact) if prev is not None else None
                    if psrb is None:
                        missing.append(trect)
                    else:
                        tiles.append((prev[1], psrb))
                else:  # nur Schlüssel und Lage merken (keine Bilder festhalten)
                    self._shown[tkey] = (entry[0].cacheKey(), (entry[0].size(), entry[1], entry[2], entry[3]))
                    tiles.append((entry, srb))
            if len(self._shown) > 4096:
                self._shown.clear()

            def dev_rect(pr: QRectF):
                return ox + pr.x() * dev, oy + pr.y() * dev, pr.width() * dev, pr.height() * dev

            if missing:
                for m in missing:  # Papier
                    quad(white, *dev_rect(m))
                if base is not None:
                    bimg = base[0]
                    bsrb = self._texture(bimg, batch, True)
                    if bsrb is not None:
                        for m in missing:
                            quad(bsrb, *dev_rect(m), m.left() / item.w, m.top() / item.h,
                                 m.right() / item.w, m.bottom() / item.h)
                    else:
                        incomplete = True
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
                                srb = self._texture(entry[0], batch, True)
                                if srb is not None:
                                    quad(srb, *dev_rect(entry[2]))

            if exact:  # 1:1 auf Gerätepixel (wie in der Software-Darstellung)
                rx, ry = round(ox), round(oy)
                for entry, srb in tiles:
                    img = entry[0]
                    px, py = entry[3]
                    quad(srb, rx + px, ry + py, img.width(), img.height())
            else:
                for entry, srb in tiles:
                    quad(srb, *dev_rect(entry[2]))
        return incomplete

    # ---- Überlagerung (Striche in Arbeit, Auswahl, Textfeld, Suchtreffer …)
    def _draw_overlay(self, view, dpr, batch, quad):
        img_info = view._overlay_image(dpr)
        if img_info is None:
            return
        img, scene_tl, fresh = img_info
        if fresh or self._overlay is None:
            ov = self._overlay
            if ov is None or ov[2] != img.size():
                if ov is not None:
                    ov[1].destroy()
                    ov[0].destroy()
                tex = self._rhi.newTexture(QRhiTexture.RGBA8, img.size())
                tex.create()
                self._overlay = ov = [tex, self._make_srb(tex, self._nearest), img.size()]
            batch.uploadTexture(ov[0], img)
        p = view.viewportTransform().map(scene_tl)
        quad(self._overlay[1], round(p.x() * dpr), round(p.y() * dpr), img.width(), img.height())

    # ---- Lineal: ungedrehtes Bild, gedreht aufgelegt (Drehen/Verschieben ohne neues Hochladen)
    def _ruler_texture(self, name, img, batch, linear):
        st = self._ruler.get(name)
        if st is None or st[0] != img.cacheKey():
            if st is None or st[3] != img.size():
                if st is not None:
                    self._discard([st[2], st[1]])
                tex = self._rhi.newTexture(QRhiTexture.RGBA8, img.size())
                tex.create()
                st = [None, tex, self._make_srb(tex, self._linear if linear else self._nearest), img.size()]
                self._ruler[name] = st
            batch.uploadTexture(st[1], img)
            st[0] = img.cacheKey()
        return st[2]

    def _draw_ruler(self, view, dpr, batch, quad, poly):
        r = getattr(view, "ruler", None)
        if r is None or not r.visible:
            return
        body = r.body_image(view.transform().m11(), dpr)
        srb = self._ruler_texture("body", body, batch, linear=True)  # gedreht: weich filtern
        poly(srb, [(p.x() * dpr, p.y() * dpr) for p in r.corners()])
        lab = r.label_image(dpr)
        srb = self._ruler_texture("label", lab, batch, linear=False)
        c = r.label_center()
        quad(srb, round(c.x() * dpr - lab.width() / 2), round(c.y() * dpr - lab.height() / 2), lab.width(),
             lab.height())

    # ---- Striche: Ebene bleibt über mehrere Striche bestehen; nur neue Teilstücke zeichnen und hochladen
    def _draw_stroke(self, view, dpr, batch, quad, size):
        strokes = view._layer
        removed, view._layer_removed = view._layer_removed, []
        if not strokes and not self._layers:
            return
        vt = view.viewportTransform()
        scale = vt.m11() * dpr  # Gerätepixel je Szeneneinheit
        anchor_now = vt.inverted()[0].map(QPointF(0, 0))
        geo = self._layer_geo
        fresh_stroke = any(id(s) not in self._layers.get(s["opacity"], {}).get("drawn", {}) for s in strokes)
        reset = (geo is None or abs(geo[1] - scale) > 1e-9 or geo[2] != size or geo[3] != view._layer_epoch
                 # neuer Strich nach dem Scrollen: Ebene neu an der jetzigen Ansicht ausrichten
                 or (fresh_stroke and (abs(geo[0].x() - anchor_now.x()) > 1e-6 or abs(geo[0].y() - anchor_now.y()) > 1e-6)))
        if reset:
            for layer in self._layers.values():
                if layer["img"].size() != size:
                    layer["srb"].destroy()
                    layer["tex"].destroy()
            self._layers = {k: v for k, v in self._layers.items() if v["img"].size() == size}
            for layer in self._layers.values():
                layer["img"].fill(Qt.transparent)
                layer["drawn"] = {}
                layer["dirty"] = QRect(QPoint(0, 0), size)
            self._layer_geo = geo = (anchor_now, scale, size, view._layer_epoch)
        anchor = geo[0]
        ax, ay = anchor.x(), anchor.y()

        def to_img(s, p):
            ip = s["item"].pos()
            return QPointF((p[0] + ip.x() - ax) * scale, (p[1] + ip.y() - ay) * scale)

        def bounds(s, pts_img):
            pad = max(1.0, s["width"] * scale) / 2 + 2
            xs = [q.x() for q in pts_img]
            ys = [q.y() for q in pts_img]
            return QRect(int(min(xs) - pad), int(min(ys) - pad), int(max(xs) - min(xs) + 2 * pad) + 2,
                         int(max(ys) - min(ys) + 2 * pad) + 2)

        def paint(layer, s, pts_img, clip=None):
            c = QColor(s["color"])
            c.setAlpha(255)  # Deckkraft (Marker) erst beim Zusammensetzen -> keine dunkleren Überlappungen
            p = QPainter(layer["img"])
            p.setRenderHint(QPainter.Antialiasing)
            if clip is not None:
                p.setClipRect(clip)
            p.setPen(QPen(c, max(1.0, s["width"] * scale), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            if len(pts_img) == 1:
                p.drawPoint(pts_img[0])
            else:
                path = QPainterPath(pts_img[0])
                for q in pts_img[1:]:
                    path.lineTo(q)
                p.drawPath(path)
            p.end()

        def dirty(layer, r):
            layer["dirty"] = r if layer.get("dirty") is None else layer["dirty"].united(r)

        # entfernte Striche (Seite fertig gerendert): ihre Stelle leeren, überlappende Striche nachzeichnen
        if not reset:
            for s in removed:
                layer = self._layers.get(s["opacity"])
                if layer is None or id(s) not in layer["drawn"] or not s["pts"]:
                    continue
                del layer["drawn"][id(s)]
                r = bounds(s, [to_img(s, q) for q in s["pts"]]).intersected(QRect(QPoint(0, 0), size))
                if r.isEmpty():
                    continue
                p = QPainter(layer["img"])
                p.setCompositionMode(QPainter.CompositionMode_Clear)
                p.fillRect(r, Qt.transparent)
                p.end()
                for o in strokes:
                    if o["opacity"] == s["opacity"] and id(o) in layer["drawn"] and o["pts"]:
                        pts_img = [to_img(o, q) for q in o["pts"][:layer["drawn"][id(o)]]]
                        if bounds(o, pts_img).intersects(r):
                            paint(layer, o, pts_img, r)
                dirty(layer, r)

        # neue Teilstücke
        for s in strokes:
            pts = s["pts"]
            if not pts:
                continue
            layer = self._layers.get(s["opacity"])
            if layer is None:
                tex = self._rhi.newTexture(QRhiTexture.RGBA8, size)
                tex.create()
                img = QImage(size, QImage.Format_RGBA8888_Premultiplied)
                img.fill(Qt.transparent)
                layer = self._layers[s["opacity"]] = {"tex": tex, "srb": self._make_srb(tex, self._nearest),
                                                      "img": img, "drawn": {}, "dirty": QRect(QPoint(0, 0), size)}
            n = len(pts)
            done = layer["drawn"].get(id(s), 0)
            if n > done:
                seg = [to_img(s, q) for q in pts[max(0, done - 1):]]
                paint(layer, s, seg)
                layer["drawn"][id(s)] = n
                dirty(layer, bounds(s, seg))

        # geänderte Ausschnitte hochladen und Ebenen zeichnen (Marker unter dem Stift)
        o = vt.map(anchor)
        for opacity in sorted(self._layers, key=lambda a: a == 1.0):
            layer = self._layers[opacity]
            r = layer.get("dirty")
            layer["dirty"] = None
            if r is not None:
                r = r.intersected(QRect(QPoint(0, 0), size))
                if not r.isEmpty():
                    desc = QRhiTextureSubresourceUploadDescription(layer["img"])
                    desc.setSourceTopLeft(r.topLeft())
                    desc.setSourceSize(r.size())
                    desc.setDestinationTopLeft(r.topLeft())
                    batch.uploadTexture(layer["tex"], QRhiTextureUploadDescription(QRhiTextureUploadEntry(0, 0, desc)))
            if layer["drawn"]:
                a = opacity
                quad(layer["srb"], round(o.x() * dpr), round(o.y() * dpr), size.width(), size.height(),
                     color=(a, a, a, a))

    # ---- Bildlaufbalken
    def _draw_scrollbars(self, view, dpr, quad):
        for orient in (Qt.Vertical, Qt.Horizontal):
            geo = view._scroll_thumb(orient)
            if geo is None:
                continue
            rect, hot = geo
            c = theme.TEXT if hot else theme.TEXT_DIM
            color = _premul(c, 0.85 if hot else 0.6)
            x, y, w, h = rect.x() * dpr, rect.y() * dpr, rect.width() * dpr, rect.height() * dpr
            r = min(w, h) / 2
            # abgerundete Enden: Mittelteil + zwei Kappen aus schmalen Streifen (keine eigene Form nötig)
            self._rounded(quad, x, y, w, h, r, color)

    def _rounded(self, quad, x, y, w, h, r, color):
        white = self._white_srb
        steps = 3  # drei Streifen je Kappe reichen bei wenigen Pixeln Breite
        if w <= h:  # senkrecht: Kappen oben/unten
            quad(white, x, y + r, w, h - 2 * r, color=color)
            for i in range(steps):
                t0, t1 = i / steps, (i + 1) / steps
                dy = r * (1 - t1)
                half = math.sqrt(max(0.0, r * r - (r * (1 - (t0 + t1) / 2)) ** 2))
                cx = x + w / 2
                quad(white, cx - half, y + dy, 2 * half, r / steps, color=color)
                quad(white, cx - half, y + h - dy - r / steps, 2 * half, r / steps, color=color)
        else:
            quad(white, x + r, y, w - 2 * r, h, color=color)
            for i in range(steps):
                t0, t1 = i / steps, (i + 1) / steps
                dx = r * (1 - t1)
                half = math.sqrt(max(0.0, r * r - (r * (1 - (t0 + t1) / 2)) ** 2))
                cy = y + h / 2
                quad(white, x + dx, cy - half, r / steps, 2 * half, color=color)
                quad(white, x + w - dx - r / steps, cy - half, r / steps, 2 * half, color=color)


def render_overlay_image(view, region_vp: QRectF, dpr):
    """Szene ohne Seiten (nur Überlagerungen) für einen Viewport-Bereich in ein Bild zeichnen."""
    src = region_vp.toAlignedRect()
    img = QImage(QSize(max(1, math.ceil(src.width() * dpr)), max(1, math.ceil(src.height() * dpr))),
                 QImage.Format_RGBA8888_Premultiplied)
    img.setDevicePixelRatio(dpr)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
    view._overlay_pass = True
    try:
        view.render(p, QRectF(0, 0, src.width(), src.height()), src, Qt.IgnoreAspectRatio)
    finally:
        view._overlay_pass = False
        p.end()
    return img, src


def scroll_thumb_rect(view, orient, hot):
    """Lage des Bildlaufbalkens (logische Viewport-Koordinaten) oder None, wenn nicht nötig."""
    sb = view.verticalScrollBar() if orient == Qt.Vertical else view.horizontalScrollBar()
    rng = sb.maximum() - sb.minimum()
    vp = view.viewport()
    scene = view.sceneRect()
    content = (scene.height() if orient == Qt.Vertical else scene.width()) * view.transform().m11()
    if rng <= 0 or content <= (vp.height() if orient == Qt.Vertical else vp.width()) + 1:
        return None  # passt ganz in die Ansicht (Qt meldet dann teils trotzdem einen Bereich)
    thick = SCROLLBAR_HOVER if hot else SCROLLBAR
    length = (vp.height() if orient == Qt.Vertical else vp.width()) - 2 * SCROLLBAR_MARGIN
    page = sb.pageStep()
    thumb = max(SCROLLBAR_MIN, length * page / (rng + page))
    pos = SCROLLBAR_MARGIN + (length - thumb) * (sb.value() - sb.minimum()) / rng
    if orient == Qt.Vertical:
        return QRectF(vp.width() - SCROLLBAR_MARGIN - thick, pos, thick, thumb)
    return QRectF(pos, vp.height() - SCROLLBAR_MARGIN - thick, thumb, thick)


