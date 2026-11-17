"""Video, Audio und animierte GIFs in PDFs.

* Video/Audio stecken als eingebettete Datei in einer Bildschirm-Anmerkung (PDF-Standard „Screen“ mit
  Rendition-Aktion) – alles in der PDF, keine Zusatzdateien. Adobe Acrobat/Reader spielt sie ebenfalls ab;
  andere Programme zeigen das Vorschaubild (und meist das Video als Dateianhang).
* Animierte GIFs: PDF kennt keine Animationen. Gespeichert wird das erste Bild (sieht jeder Betrachter), dazu
  die Original-GIF – im Editor läuft sie animiert (im Maus- und Auswahl-Werkzeug).

Abgespielt wird mit Qt Multimedia (FFmpeg). Die Bilder kommen über einen QVideoSink:
* mit Grafikkarten-Darstellung zeichnet die Seitenfläche (gpu_view) sie selbst – die Y/U/V-Ebenen werden als
  Texturen hochgeladen und im Shader nach RGB umgerechnet (1080p: ~2 ms statt ~30 ms je Bild, 30/60 Bilder/s);
* sonst rechnet Qt das jeweils neueste Bild im Haupt-Thread um (QVideoFrame.toImage, ~30 ms bei 1080p – in
  einem Hintergrund-Thread wartet toImage intern auf den Haupt-Thread und kann das Programm blockieren).
Ein natives Videofenster (QVideoWidget) scheidet aus: Es verträgt sich nicht mit der Grafikkarten-Zeichenfläche.
"""
from __future__ import annotations

import atexit
import hashlib
import os
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import (QBuffer, QByteArray, QEventLoop, QIODevice, QObject, QPoint, QPointF, QRect, QRectF,
                            Qt, QTimer, QUrl, Signal)
from PySide6.QtGui import QColor, QFont, QImage, QMovie, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

VIDEO = {"mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime", "webm": "video/webm",
         "mkv": "video/x-matroska", "avi": "video/x-msvideo", "wmv": "video/x-ms-wmv", "mpg": "video/mpeg",
         "mpeg": "video/mpeg"}
AUDIO = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/aac", "wav": "audio/wav", "ogg": "audio/ogg",
         "flac": "audio/flac", "wma": "audio/x-ms-wma", "opus": "audio/ogg"}
FILE_FILTER = ("Video und Audio (" + " ".join(f"*.{e}" for e in list(VIDEO) + list(AUDIO)) + ");;Alle Dateien (*)")
DRAW_TOOLS = ("pen", "highlighter", "eraser", "markup", "text", "line", "arrow", "rect", "ellipse", "note", "image")


def kind_of(name, mime=""):
    """("video"|"audio", MIME) nach Dateiendung bzw. MIME-Typ – oder (None, "")."""
    ext = os.path.splitext(name or "")[1].lstrip(".").lower()
    if ext in VIDEO:
        return "video", VIDEO[ext]
    if ext in AUDIO:
        return "audio", AUDIO[ext]
    mime = (mime or "").lower()
    if mime.startswith("video/"):
        return "video", mime
    if mime.startswith("audio/"):
        return "audio", mime
    return None, ""


def available():
    try:
        from PySide6 import QtMultimedia  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------- Abbau ohne Hänger
_GRAVE = None


def _grave():
    """Eltern-Objekt für Abspiel-Objekte (lebt so lange wie das Programm)."""
    global _GRAVE
    if _GRAVE is None:
        _GRAVE = QObject()
    return _GRAVE


def new_player_objects():
    """QMediaPlayer, QVideoSink, QAudioOutput – bewusst nicht als Kinder eines Widgets (siehe retire)."""
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
    g = _grave()
    return QMediaPlayer(g), QVideoSink(g), QAudioOutput(g)


def retire(player, *others):
    """Wiedergabe beenden und die Objekte abbauen – über die Ereignisschleife, nicht aus Python-Code heraus.

    Hintergrund (Programm hing beim Schließen eines Tabs mit laufendem Video): Beim Anhalten bzw. Löschen wartet
    Qt auf den Abspiel-Thread von FFmpeg. Der braucht kurz das Python-GIL (er sendet Signale an Objekte, die aus
    Python angelegt wurden). Geschieht das Anhalten/Löschen in Python-Code, hält der Haupt-Thread das GIL ->
    beide warten aufeinander. Aus der Ereignisschleife heraus ist das GIL frei."""
    from PySide6.QtCore import QMetaObject
    g = _grave()
    objs = [o for o in (player, *others) if o is not None]
    for o in objs:
        try:
            if o.parent() is not g:
                o.setParent(g)  # gehört jetzt Qt: Python-Aufräumen löscht es nicht mehr direkt
        except RuntimeError:
            pass
    if player is not None:  # None: nur abbauen (z. B. Mikrofon-Aufnahme)
        try:
            QMetaObject.invokeMethod(player, "stop", Qt.QueuedConnection)
        except RuntimeError:
            pass
    for o in objs:
        try:
            o.deleteLater()  # nach dem Anhalten, in dieser Reihenfolge (Player vor Bildempfänger)
        except RuntimeError:
            pass


# ---------------------------------------------------------------- Vorschaubild
def probe(path, timeout_ms=4000):
    """Erstes Bild (QImage oder None) und Dauer (ms) einer Video-/Audiodatei – ohne Ton abzuspielen."""
    from PySide6.QtMultimedia import QMediaPlayer
    player, sink, audio = new_player_objects()
    player.setVideoSink(sink)  # kein Audioausgang -> stumm
    audio.deleteLater()
    result = {"img": None}
    loop = QEventLoop()
    t_end = QTimer()
    t_end.setSingleShot(True)
    t_end.timeout.connect(loop.quit)

    def poll():
        # Bild im Haupt-Thread abholen: Ein Python-Slot an videoFrameChanged liefe im Abspiel-Thread von FFmpeg –
        # beim Anhalten wartet Qt auf diesen Thread, der auf Python wartet -> Programm hängt
        if player.mediaStatus() in (QMediaPlayer.EndOfMedia, QMediaPlayer.InvalidMedia) or \
                player.error() != QMediaPlayer.NoError:
            loop.quit()
            return
        frame = sink.videoFrame()
        if frame.isValid():
            img = frame.toImage()
            if not img.isNull():
                result["img"] = img.copy()
                loop.quit()

    t_poll = QTimer()
    t_poll.setInterval(15)
    t_poll.timeout.connect(poll)
    player.setSource(QUrl.fromLocalFile(path))
    player.play()
    t_poll.start()
    t_end.start(timeout_ms)
    loop.exec()
    t_poll.stop()
    t_end.stop()
    duration = player.duration()
    retire(player, sink)
    return result["img"], duration


def _fmt_time(ms):
    s = max(0, int(ms or 0) // 1000)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _play_badge(p, center, radius):
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(0, 0, 0, 150))
    p.drawEllipse(center, radius, radius)
    tri = QPainterPath()
    r = radius * 0.5
    tri.moveTo(center.x() - r * 0.6, center.y() - r)
    tri.lineTo(center.x() + r * 1.0, center.y())
    tri.lineTo(center.x() - r * 0.6, center.y() + r)
    tri.closeSubpath()
    p.setBrush(QColor(255, 255, 255, 235))
    p.drawPath(tri)


AUDIO_ICON_PT = 18  # Audio als kleines Symbol wie eine Notiz (Kantenlänge in Punkt)


def audio_icon_png(size=256):
    """Audio-Symbol (PNG-Bytes): abgerundetes Kästchen mit Lautsprecher – gleiche Machart wie das Notiz-Symbol."""
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    u = size / AUDIO_ICON_PT  # Pixel je Punkt
    p.setPen(QPen(QColor("#000000"), u))
    p.setBrush(QColor("#9fd0ff"))
    p.drawRoundedRect(QRectF(0.5 * u, 0.5 * u, size - u, size - u), 2.8 * u, 2.8 * u)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#000000"))
    body = QPainterPath()
    for i, (x, y) in enumerate([(3.6, 7.0), (6.4, 7.0), (9.6, 4.0), (9.6, 14.0), (6.4, 11.0), (3.6, 11.0)]):
        (body.moveTo if i == 0 else body.lineTo)(x * u, y * u)
    body.closeSubpath()
    p.drawPath(body)
    pen = QPen(QColor("#000000"), 1.2 * u)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    for r in (2.6, 5.0):  # Schallwellen
        p.drawArc(QRectF((9.6 - r) * u, (9.0 - r) * u, 2 * r * u, 2 * r * u), -45 * 16, 90 * 16)
    p.end()
    buf = QByteArray()
    io = QBuffer(buf)
    io.open(QIODevice.WriteOnly)
    img.save(io, "PNG")
    return bytes(buf)


def poster_png(frame, kind, name, duration_ms):
    """Vorschaubild (PNG-Bytes) für die PDF: erstes Videobild mit Abspiel-Symbol und Namen; Audio: kleines Symbol."""
    if kind == "audio":
        return audio_icon_png()
    if frame is not None and not frame.isNull():
        img = frame.convertToFormat(QImage.Format_RGB32)
        if img.width() > 960:
            img = img.scaledToWidth(960, Qt.SmoothTransformation)
    else:
        img = QImage(520, 292, QImage.Format_RGB32)
        img.fill(QColor("#23262d"))
    w, h = img.width(), img.height()
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    _play_badge(p, QPointF(w / 2, h / 2), min(w, h) * 0.16)
    bar = max(22, h // 9)
    p.fillRect(QRectF(0, h - bar, w, bar), QColor(0, 0, 0, 150))
    f = QFont("Segoe UI")
    f.setPixelSize(int(bar * 0.6))
    p.setFont(f)
    p.setPen(QColor(255, 255, 255))
    label = "Video: " + (name or "")
    if duration_ms and duration_ms > 0:
        label += f"  ({_fmt_time(duration_ms)})"
    p.drawText(QRectF(bar * 0.4, h - bar, w - bar * 0.8, bar), Qt.AlignVCenter | Qt.AlignLeft,
               p.fontMetrics().elidedText(label, Qt.ElideMiddle, int(w - bar * 0.8)))
    p.end()
    buf = QByteArray()
    io = QBuffer(buf)
    io.open(QIODevice.WriteOnly)
    img.save(io, "PNG")
    return bytes(buf)


# ---------------------------------------------------------------- Temporäre Kopien zum Abspielen
_TMP = None


def temp_dir():
    """Ordner für kurzlebige Kopien (Abspielen, Aufnahmen vor dem Einbetten) – wird beim Beenden gelöscht."""
    global _TMP
    if _TMP is None:
        _TMP = tempfile.mkdtemp(prefix="pdfeditor-medien-")
        atexit.register(lambda: shutil.rmtree(_TMP, ignore_errors=True))
    return _TMP


def temp_copy(data, name):
    """Eingebettete Datei zum Abspielen kurz als Datei ablegen (wird beim Beenden gelöscht)."""
    ext = os.path.splitext(name or "")[1] or ".bin"
    path = os.path.join(temp_dir(), hashlib.sha1(data).hexdigest()[:16] + ext)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(data)
    return path


# ---------------------------------------------------------------- Abspielen über der Seite
class _Screen(QLabel):
    """Bildfläche des Players; Antippen/Klicken hält an bzw. spielt weiter."""
    clicked = Signal()

    def mousePressEvent(self, e):
        e.accept()  # nicht an die Seite weiterreichen (dort hieße Antippen „Medium abspielen“)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(e)


def video_planes(frame):
    """Bilddaten eines Videobilds für die Grafikkarte (läuft im Hintergrund-Thread: map() holt das Bild aus dem
    Decoder und kostet bei 1080p ~7 ms). Y'CbCr-Ebenen als Bytes oder – bei anderen Formaten – ein QImage."""
    from PySide6.QtMultimedia import QVideoFrame, QVideoFrameFormat
    PF = QVideoFrameFormat.PixelFormat
    CS = QVideoFrameFormat.ColorSpace
    fmt = frame.pixelFormat()
    w, h = frame.width(), frame.height()
    sf = frame.surfaceFormat()
    out = {"w": w, "h": h, "fmt": fmt.name,
           "space": {CS.ColorSpace_BT601: "601", CS.ColorSpace_BT709: "709", CS.ColorSpace_BT2020: "2020"}.get(
               sf.colorSpace(), "709" if h >= 720 else "601"),
           "full": sf.colorRange() == QVideoFrameFormat.ColorRange.ColorRange_Full,
           "rot": {"Clockwise90": 90, "Clockwise180": 180, "Clockwise270": 270}.get(
               getattr(frame.rotation(), "name", ""), 0),
           "mirror": bool(frame.mirrored()), "planes": None, "image": None}
    rows = {PF.Format_NV12: (h, (h + 1) // 2), PF.Format_YUV420P: (h, (h + 1) // 2, (h + 1) // 2),
            PF.Format_YV12: (h, (h + 1) // 2, (h + 1) // 2)}.get(fmt)
    if rows is not None:
        f = QVideoFrame(frame)
        if f.map(QVideoFrame.MapMode.ReadOnly):
            try:
                out["planes"] = [(QByteArray(f.bits(i)[:f.bytesPerLine(i) * n].tobytes()), f.bytesPerLine(i))
                                 for i, n in enumerate(rows)]
            finally:
                f.unmap()
            return out
    img = frame.toImage()  # anderes Format: Qt rechnet um (langsamer)
    if img.isNull():
        return None
    out["image"] = img.convertToFormat(QImage.Format_RGBA8888_Premultiplied)
    return out


class _FrameConverter(QObject):
    """Videobilder für die Grafikkarte in einem Hintergrund-Thread aufbereiten (Ebenen kopieren, siehe
    video_planes). Immer nur das neueste Bild: Kommt der Thread nicht mit, werden Zwischenbilder übersprungen
    statt sich aufzustauen. Das Ergebnis liegt in take(); das Signal selbst trägt keine Python-Objekte über die
    Thread-Grenze (sicherer mit PySide)."""
    ready = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self._pool = ThreadPoolExecutor(1, thread_name_prefix="video")
        self._lock = threading.Lock()
        self._busy = False
        self._next = None
        self._result = None
        self._alive = True

    def submit(self, frame):
        if not self._alive:
            return
        with self._lock:
            if self._busy:
                self._next = frame
                return
            self._busy = True
        self._pool.submit(self._work, frame)

    def _work(self, frame):
        while True:
            try:
                result = video_planes(frame)
            except Exception:
                result = None
            if result is not None:
                with self._lock:
                    if not self._alive:
                        self._busy = False
                        return
                    self._result = result
                self.ready.emit()  # Haupt-Thread holt das Ergebnis mit take()
            with self._lock:
                if self._next is None or not self._alive:
                    self._busy = False
                    return
                frame = self._next
                self._next = None

    def take(self):
        with self._lock:
            result, self._result = self._result, None
        return result

    def stop(self):
        with self._lock:
            self._alive = False
            self._result = self._next = None
        self._pool.shutdown(wait=False, cancel_futures=True)


class _ControlBar(QWidget):
    """Bedienleiste (Abspielen/Pause, Position, Zeit, Schließen) – selbst gezeichnet in ein kleines Bild.

    Über der Grafikkarten-Seitenfläche zeichnet gpu_view dieses Bild als Textur; das Widget selbst malt dann nichts
    und nimmt nur Eingaben an. Grund: Jede Änderung an einem normalen Widget über der Grafikkarten-Fläche lässt
    Qt das ganze Fenster neu zusammensetzen (20-30 ms im Haupt-Thread) – mit einem Schieberegler, der sich
    mehrmals pro Sekunde bewegt, ruckelte dadurch das Video."""
    HEIGHT = 32
    BG = QColor("#1b1e24")
    FG = QColor("#e8e8e8")
    ACCENT = QColor("#5b6fd6")

    def __init__(self, owner, gpu):
        super().__init__(owner)
        self.owner = owner
        self.gpu = gpu
        self.setFixedHeight(self.HEIGHT)
        self.serial = 0
        self._key = None
        self._img = None
        self._img_dpr = None
        self._drag = False
        self._drag_pos = 0

    # ---- Aufteilung
    def _areas(self):
        w, h = self.width(), self.height()
        play = QRect(0, 0, 36, h)
        close = QRect(w - 34, 0, 34, h)
        time_r = QRect(close.left() - 92, 0, 88, h)
        track = QRect(play.right() + 8, 0, max(10, time_r.left() - play.right() - 16), h)
        return play, track, time_r, close

    def _state(self):
        from PySide6.QtMultimedia import QMediaPlayer
        pl = self.owner.player
        pos, dur = (self._drag_pos if self._drag else pl.position()), max(0, pl.duration())
        return pl.playbackState() == QMediaPlayer.PlayingState, pos, dur

    def refresh(self):
        """Nur wenn sich sichtbar etwas ändert: neues Bild (Grafikkarte) bzw. neu zeichnen."""
        playing, pos, dur = self._state()
        track = self._areas()[1]
        px = int(pos / dur * track.width()) if dur else 0
        key = (self.width(), playing, px, f"{_fmt_time(pos)} / {_fmt_time(dur)}")
        if key == self._key:
            return
        self._key = key
        self._img = None
        self.serial += 1
        if self.gpu:
            self.owner.parentWidget().update()
        else:
            self.update()

    # ---- Zeichnen
    def image(self, dpr):
        if self._img is not None and self._img_dpr == dpr:
            return self._img
        img = QImage(max(1, int(self.width() * dpr)), max(1, int(self.height() * dpr)),
                     QImage.Format_RGBA8888_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(self.BG)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        playing, pos, dur = self._state()
        play, track, time_r, close = self._areas()
        c = QPointF(play.center())
        p.setPen(Qt.NoPen)
        p.setBrush(self.FG)
        if playing:  # Pause-Zeichen
            p.drawRoundedRect(QRectF(c.x() - 6, c.y() - 7, 4, 14), 1, 1)
            p.drawRoundedRect(QRectF(c.x() + 2, c.y() - 7, 4, 14), 1, 1)
        else:  # Abspielen-Dreieck
            tri = QPainterPath()
            tri.moveTo(c.x() - 5, c.y() - 7)
            tri.lineTo(c.x() + 7, c.y())
            tri.lineTo(c.x() - 5, c.y() + 7)
            tri.closeSubpath()
            p.drawPath(tri)
        cy = track.center().y() + 0.5
        p.setBrush(QColor("#3a3f4b"))
        p.drawRoundedRect(QRectF(track.left(), cy - 2, track.width(), 4), 2, 2)
        x = track.left() + (pos / dur * track.width() if dur else 0)
        p.setBrush(self.ACCENT)
        p.drawRoundedRect(QRectF(track.left(), cy - 2, x - track.left(), 4), 2, 2)
        p.setBrush(self.FG)
        p.drawEllipse(QPointF(x, cy), 7, 7)
        f = QFont("Segoe UI")
        f.setPixelSize(13)
        p.setFont(f)
        p.setPen(self.FG)
        p.drawText(time_r, Qt.AlignVCenter | Qt.AlignRight, f"{_fmt_time(pos)} / {_fmt_time(dur)}")
        pen = QPen(self.FG, 1.8)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        k = QPointF(close.center())
        p.drawLine(k + QPointF(-5, -5), k + QPointF(5, 5))
        p.drawLine(k + QPointF(-5, 5), k + QPointF(5, -5))
        p.end()
        self._img, self._img_dpr = img, dpr
        return img

    def paintEvent(self, e):
        if self.gpu:
            return  # zeichnet die Grafikkarte (gpu_view)
        p = QPainter(self)
        p.drawImage(QRectF(self.rect()), self.image(self.devicePixelRatioF()))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.refresh()

    # ---- Eingaben
    def _seek_to(self, x):
        track = self._areas()[1]
        dur = self.owner.player.duration()
        frac = min(1.0, max(0.0, (x - track.left()) / max(1, track.width())))
        self._drag_pos = int(frac * dur)
        self.owner.player.setPosition(self._drag_pos)
        self.refresh()

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        play, track, _time_r, close = self._areas()
        pt = e.position().toPoint()
        if close.contains(pt):
            self.owner.close_player()
        elif play.contains(pt):
            self.owner.toggle()
        elif track.adjusted(-8, 0, 8, 0).contains(pt):
            self._drag = True
            self._seek_to(pt.x())

    def mouseMoveEvent(self, e):
        if self._drag:
            self._seek_to(e.position().x())

    def mouseReleaseEvent(self, e):
        if self._drag:
            self._seek_to(e.position().x())
            self._drag = False
            self.refresh()


class MediaPlayerWidget(QWidget):
    """Kleiner Player über dem Video/Audio-Objekt: Bild, Abspielen/Pause, Position, Schließen."""
    closed = Signal()

    def __init__(self, parent, path, kind, title):
        super().__init__(parent)
        self.kind = kind
        # Seitenfläche auf der Grafikkarte: sie zeichnet Video und Bedienleiste selbst (dieses Widget bleibt leer)
        self.gpu = hasattr(parent, "add_video_source")
        self.frame_serial = 0
        self._latest = None
        self._closed = False
        if not self.gpu:
            self.setAttribute(Qt.WA_StyledBackground, True)
            self.setStyleSheet("MediaPlayerWidget{background:#000;border:1px solid #5b6fd6;border-radius:4px}")
        # Abspiel-Objekte gehören nicht zu diesem Widget: Würde Qt sie mit dem Widget löschen (Tab schließen),
        # geschähe das evtl. mitten in Python-Code -> Hänger (siehe retire)
        self.player, sink, self.audio = new_player_objects()
        self.player.setAudioOutput(self.audio)
        self.sink = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0) if self.gpu else lay.setContentsMargins(1, 1, 1, 1)
        lay.setSpacing(0)
        self.screen_label = None
        self._frame = None
        self._converter = None
        if kind == "video":
            self.screen_label = _Screen(self)
            self.screen_label.setAlignment(Qt.AlignCenter)
            self.screen_label.setMinimumSize(1, 1)
            self.screen_label.clicked.connect(self.toggle)
            lay.addWidget(self.screen_label, 1)
            self.sink = sink
            self.player.setVideoSink(self.sink)
            # Neues Bild im Haupt-Thread abholen (alle 4 ms) statt videoFrameChanged an Python zu hängen: Das Signal
            # käme aus dem Abspiel-Thread von FFmpeg. Beim Schließen wartet Qt auf diesen Thread, während er auf
            # Python wartet – das Programm hing (Tab mit laufendem Video schließen).
            self._last_frame = None
            self._frame_timer = QTimer(self)
            self._frame_timer.setTimerType(Qt.PreciseTimer)
            self._frame_timer.setInterval(4)
            self._frame_timer.timeout.connect(self._poll_frame)
            self._frame_timer.start()
            if self.gpu:
                self._converter = _FrameConverter(self)
                self._converter.ready.connect(self._planes_ready)
            else:
                self._pending = None  # neuestes, noch nicht gezeigtes Bild (normale Darstellung)
        else:
            self.setToolTip(title)  # Audio: nur die Bedienleiste (unter dem Symbol)
            sink.deleteLater()
        self.destroyed.connect(self._ensure_retired)
        self.bar = _ControlBar(self, self.gpu)
        lay.addWidget(self.bar)
        # Position nur 5x pro Sekunde prüfen; neu gezeichnet wird nur bei sichtbarer Änderung
        self._pos_timer = QTimer(self)
        self._pos_timer.setInterval(200)
        self._pos_timer.timeout.connect(self.bar.refresh)
        self._pos_timer.start()
        # an Methoden dieses Widgets (nicht an lambdas): dann laufen sie sicher im Haupt-Thread
        self.player.playbackStateChanged.connect(self._state_changed)
        self.player.durationChanged.connect(self._state_changed)
        if self.gpu:
            parent.add_video_source(self)
        self.player.setSource(QUrl.fromLocalFile(path))
        self.setFocusPolicy(Qt.StrongFocus)
        self.player.play()

    def _state_changed(self, *_args):
        self.bar.refresh()

    # ---- Bilder
    def _poll_frame(self):
        frame = self.sink.videoFrame()
        if not frame.isValid() or (self._last_frame is not None and frame == self._last_frame):
            return
        self._last_frame = frame
        self._on_frame(frame)

    def _on_frame(self, frame):
        if not frame.isValid():
            return
        if self.gpu:
            self._converter.submit(frame)
        else:
            if self._pending is None:
                QTimer.singleShot(0, self._convert_pending)  # liegen schon Bilder an: nur das neueste umrechnen
            self._pending = frame

    def _convert_pending(self):
        frame, self._pending = self._pending, None
        if frame is None or self.screen_label is None or self._closed:
            return
        img = frame.toImage()
        if img.isNull():
            return
        size = self.screen_label.size() * self.devicePixelRatioF()
        self._show_image(img.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _planes_ready(self):
        data = self._converter.take() if not self._closed else None
        if data is None:
            return
        self._latest = data
        self.frame_serial += 1
        if self.parentWidget() is not None:
            self.parentWidget().update()  # nächstes Bild der Seitenfläche zeigt es

    def current_frame(self):
        return self._latest

    def _rect_in_parent(self, w):
        return QRect(w.mapTo(self.parentWidget(), QPoint(0, 0)), w.size())

    def video_rect(self):
        """Bildfläche in Koordinaten der Seitenfläche (für gpu_view); Audio: None."""
        return None if self.screen_label is None else self._rect_in_parent(self.screen_label)

    def bar_rect(self):
        return self._rect_in_parent(self.bar)

    def _show_image(self, img):
        self._frame = img
        self._show_frame()

    def _show_frame(self):
        if self._frame is None or self.screen_label is None:
            return
        pm = QPixmap.fromImage(self._frame)
        pm.setDevicePixelRatio(self.devicePixelRatioF())
        self.screen_label.setPixmap(pm)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._show_frame()

    def toggle(self):
        from PySide6.QtMultimedia import QMediaPlayer
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _ensure_retired(self, *_a):
        """Widget wurde gelöscht, ohne dass der Player geschlossen wurde: Wiedergabe trotzdem beenden."""
        if not self._closed:
            self._closed = True
            retire(self.player, self.sink, self.audio)

    def mousePressEvent(self, e):
        e.accept()  # Rand des Players: nicht an die Seite weiterreichen

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close_player()
        elif e.key() == Qt.Key_Space:
            self.toggle()
        else:
            super().keyPressEvent(e)

    def close_player(self):
        if self._closed:
            return
        self._closed = True
        self._pos_timer.stop()
        if self.screen_label is not None:
            self._frame_timer.stop()
            self._last_frame = None
        retire(self.player, self.sink, self.audio)
        if self.gpu and self.parentWidget() is not None:
            self.parentWidget().remove_video_source(self)
        if self._converter is not None:
            self._converter.stop()
        self.hide()
        self.closed.emit()
        self.deleteLater()


class MediaController:
    """Medien einer Seitenansicht: GIF-Animationen über den Bildern, Player für Video/Audio.
    Die Widgets sind Kinder der Zeichenfläche und folgen beim Scrollen/Zoomen der Seite."""

    def __init__(self, view):
        self.view = view
        self.gifs = {}       # (seite, xref) -> (QLabel, QMovie, QBuffer, QByteArray)
        self.player = None   # (seite, xref, MediaPlayerWidget)
        self._refresh_timer = QTimer()
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(60)
        self._refresh_timer.timeout.connect(self.refresh)

    # -------------------------------------------------- Lage
    def _vp_rect(self, index, rect):
        view = self.view
        if not (0 <= index < len(view.items_)):
            return None
        item = view.items_[index]
        return view.mapFromScene(QRectF(rect.x0, rect.y0, rect.width, rect.height).translated(item.pos())).boundingRect()

    def _visible_pages(self):
        view = self.view
        vis = view.visible_scene_rect()
        return [it.index for it in view.items_ if vis.intersects(it.sceneBoundingRect())]

    def reposition(self):
        """Beim Scrollen/Zoomen: vorhandene Widgets nachführen (günstig), neue Medien kurz danach anlegen."""
        for (idx, xref), entry in list(self.gifs.items()):
            r = self._rect_of(idx, xref)
            if r is not None:
                entry[0].setGeometry(r)
        if self.player is not None:
            idx, xref, w = self.player
            r = self._rect_of(idx, xref)
            if r is not None:
                w.setGeometry(self._player_rect(r, w.kind))
        self._refresh_timer.start()

    def _rect_of(self, idx, xref):
        for m in self._media(idx):
            if m["xref"] == xref:
                return self._vp_rect(idx, m["rect"])
        return None

    def _player_rect(self, r, kind):
        if kind == "audio":  # Bedienleiste direkt unter dem Audio-Symbol
            vp = self.view.viewport().rect()
            w, h = 300, 34
            x = min(max(2, r.left()), max(2, vp.width() - w - 2))
            y = r.bottom() + 4
            if y + h > vp.height() and r.top() - h - 4 >= 0:
                y = r.top() - h - 4  # unten kein Platz: darüber
            return QRect(x, y, w, h)
        return QRect(r.left(), r.top(), max(r.width(), 220), max(r.height(), 160))

    def _media(self, idx):
        try:
            return self.view.document.media_annots(idx)
        except Exception:
            return []

    # -------------------------------------------------- GIF-Animationen
    def gifs_enabled(self):
        return self.view.tool not in DRAW_TOOLS and self.view._editor is None

    def refresh(self):
        """Animierte GIFs auf den sichtbaren Seiten anzeigen, andere entfernen."""
        wanted = {}
        if self.gifs_enabled():
            for idx in self._visible_pages():
                for m in self._media(idx):
                    if m["kind"] == "gif":
                        wanted[(idx, m["xref"])] = m
        for key in list(self.gifs):
            if key not in wanted:
                self._drop_gif(key)
        for key, m in wanted.items():
            r = self._vp_rect(key[0], m["rect"])
            if r is None:
                continue
            if key not in self.gifs:
                data = self.view.document.gif_bytes(key[1])
                if not data:
                    continue
                arr = QByteArray(data)
                buf = QBuffer()
                buf.setData(arr)
                buf.open(QIODevice.ReadOnly)
                movie = QMovie()
                movie.setDevice(buf)
                label = QLabel(self.view.viewport())
                label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                label.setScaledContents(True)
                label.setMovie(movie)
                movie.start()
                label.show()
                self.gifs[key] = (label, movie, buf, arr)
            self.gifs[key][0].setGeometry(r)

    def _drop_gif(self, key):
        label, movie, buf, _arr = self.gifs.pop(key)
        movie.stop()
        label.hide()
        label.deleteLater()

    # -------------------------------------------------- Video/Audio
    def media_at(self, scene):
        """Video-/Audio-Objekt unter dem Punkt: (seite, eintrag) oder None."""
        item = self.view.page_at(scene)
        if item is None:
            return None
        local = scene - item.pos()
        pad = self.view._touch_tolerance()  # kleine Audio-Symbole auch mit dem Finger treffen
        for m in self._media(item.index):
            r = m["rect"]
            if m["kind"] in ("video", "audio") and r.x0 - pad <= local.x() <= r.x1 + pad and \
                    r.y0 - pad <= local.y() <= r.y1 + pad:
                return item.index, m
        return None

    def play(self, idx, xref):
        if not available():
            self.view.status_message.emit("Abspielen nicht möglich: Qt Multimedia fehlt.")
            return
        if self.player is not None and self.player[:2] == (idx, xref) and self.player[2].isVisible():
            self.player[2].toggle()  # erneut angetippt: Pause/weiter
            return
        self.stop()
        m = next((x for x in self._media(idx) if x["xref"] == xref), None)
        if m is None:
            return
        data, name = self.view.document.media_bytes(idx, xref)
        if not data:
            self.view.status_message.emit("Die Mediendatei ist nicht in der PDF enthalten.")
            return
        r = self._vp_rect(idx, m["rect"])
        w = MediaPlayerWidget(self.view.viewport(), temp_copy(data, name), m["kind"], name or "")
        w.setGeometry(self._player_rect(r, m["kind"]))
        w.closed.connect(lambda: self._closed(w))
        w.show()
        w.setFocus()
        self.player = (idx, xref, w)

    def _closed(self, w):
        if self.player is not None and self.player[2] is w:
            self.player = None

    def player_at(self, vpos):
        if self.player is None:
            return False
        return self.player[2].isVisible() and self.player[2].geometry().contains(vpos.toPoint())

    def stop(self):
        if self.player is not None:
            w = self.player[2]
            self.player = None
            w.close_player()

    def shutdown(self):
        """Ansicht wird geschlossen: Wiedergabe sauber beenden, bevor Qt die Widgets abbaut."""
        self._refresh_timer.stop()
        self.stop()
        for key in list(self.gifs):
            self._drop_gif(key)

    def reset(self):
        """Dokument neu aufgebaut: alles schließen (Lage kann sich geändert haben)."""
        self.stop()
        for key in list(self.gifs):
            self._drop_gif(key)
        self._refresh_timer.start()
