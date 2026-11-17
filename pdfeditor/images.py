"""Bildhilfen (Drehen, Zuschneiden, Kodieren) und der Zuschneide-Dialog."""
from __future__ import annotations

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QColorSpace, QImage, QPainter, QPainterPath, QPen, QTransform
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPushButton, QVBoxLayout, QWidget

from . import theme


def load(data: bytes) -> QImage:
    img = QImage()
    img.loadFromData(data)
    return img


def is_animated(data: bytes) -> bool:
    """Animiertes Bild (z. B. GIF mit mehreren Bildern)?"""
    from PySide6.QtGui import QImageReader
    buf = QBuffer()
    buf.setData(QByteArray(data))
    buf.open(QIODevice.ReadOnly)
    reader = QImageReader(buf)
    return reader.supportsAnimation() and reader.imageCount() > 1


def encode(img: QImage) -> bytes:
    """PNG bei Transparenz, sonst JPEG (deutlich kleiner bei Fotos)."""
    buf = QByteArray()
    io = QBuffer(buf)
    io.open(QIODevice.WriteOnly)
    if img.hasAlphaChannel():
        img.save(io, "PNG")
    else:
        img.convertToFormat(QImage.Format_RGB888).save(io, "JPEG", 92)
    return bytes(buf)


def strip_icc(data: bytes) -> bytes:
    """Eingebettetes Farbprofil verlustfrei entfernen (JPEG: APP2-Segmente „ICC_PROFILE“, PNG: iCCP)."""
    if data[:2] == b"\xff\xd8":
        out, i = bytearray(b"\xff\xd8"), 2
        while i + 4 <= len(data) and data[i] == 0xFF:
            marker = data[i + 1]
            if marker == 0xDA:  # Bilddaten beginnen: Rest unverändert übernehmen
                break
            length = int.from_bytes(data[i + 2:i + 4], "big")
            segment = data[i:i + 2 + length]
            if not (marker == 0xE2 and segment[4:16] == b"ICC_PROFILE\x00"):
                out += segment
            i += 2 + length
        return bytes(out + data[i:])
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        out, i = bytearray(data[:8]), 8
        while i + 8 <= len(data):
            length = int.from_bytes(data[i:i + 4], "big")
            chunk = data[i:i + 12 + length]
            if chunk[4:8] != b"iCCP":
                out += chunk
            i += 12 + length
        return bytes(out)
    return data


def encode_srgb(img: QImage) -> bytes:
    """Bild in sRGB umrechnen und ohne Farbprofil kodieren (letzter Ausweg bei unlesbaren Profilen)."""
    img = QImage(img)
    cs = img.colorSpace()
    if cs.isValid() and cs != QColorSpace(QColorSpace.SRgb):
        img.convertToColorSpace(QColorSpace(QColorSpace.SRgb))
    img.setColorSpace(QColorSpace())
    return encode(img)


def rotate(data: bytes, degrees: int) -> bytes:
    if degrees % 360 == 0:
        return data
    return encode(load(data).transformed(QTransform().rotate(degrees), Qt.SmoothTransformation))


def crop(data: bytes, rect: QRect) -> bytes:
    return encode(load(data).copy(rect))


# ---------------------------------------------------------------- Zuschneiden
HANDLE = 9


class CropWidget(QWidget):
    """Zeigt das Bild; ein Rahmen mit Anfassern legt den Ausschnitt fest."""

    def __init__(self, img: QImage, parent=None):
        super().__init__(parent)
        self.img = img
        self.crop = QRectF(0, 0, img.width(), img.height())  # in Bildpixeln
        self._drag = None  # (modus, startpunkt, startrechteck)
        self.setMinimumSize(300, 220)
        self.setMouseTracking(True)

    # Abbildung Bild <-> Widget
    def _frame(self):
        s = min((self.width() - 20) / self.img.width(), (self.height() - 20) / self.img.height())
        w, h = self.img.width() * s, self.img.height() * s
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h), s

    def _to_widget(self, r: QRectF):
        f, s = self._frame()
        return QRectF(f.x() + r.x() * s, f.y() + r.y() * s, r.width() * s, r.height() * s)

    def _to_img(self, p: QPointF):
        f, s = self._frame()
        return QPointF((p.x() - f.x()) / s, (p.y() - f.y()) / s)

    def _handles(self):
        r = self._to_widget(self.crop)
        pts = {
            "tl": r.topLeft(), "tr": r.topRight(), "bl": r.bottomLeft(), "br": r.bottomRight(),
            "t": QPointF(r.center().x(), r.top()), "b": QPointF(r.center().x(), r.bottom()),
            "l": QPointF(r.left(), r.center().y()), "r": QPointF(r.right(), r.center().y()),
        }
        return pts

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), theme.CANVAS)
        f, _ = self._frame()
        p.drawImage(f, self.img)
        r = self._to_widget(self.crop)
        shade = QPainterPath()
        shade.addRect(f)
        inner = QPainterPath()
        inner.addRect(r)
        p.fillPath(shade.subtracted(inner), QColor(0, 0, 0, 140))
        p.setPen(QPen(QColor("#ffffff"), 1.5, Qt.DashLine))
        p.drawRect(r)
        p.setPen(QPen(QColor("#2f7de1"), 1))
        p.setBrush(QColor("#ffffff"))
        for pt in self._handles().values():
            p.drawRect(QRectF(pt.x() - HANDLE / 2, pt.y() - HANDLE / 2, HANDLE, HANDLE))

    def _hit(self, pos: QPointF):
        for name, pt in self._handles().items():
            if abs(pos.x() - pt.x()) <= HANDLE and abs(pos.y() - pt.y()) <= HANDLE:
                return name
        if self._to_widget(self.crop).contains(pos):
            return "move"
        return "new"

    def mousePressEvent(self, e):
        mode = self._hit(e.position())
        start = self._to_img(e.position())
        if mode == "new":
            self.crop = QRectF(start, start)
            mode = "br"
        self._drag = (mode, start, QRectF(self.crop))

    def mouseMoveEvent(self, e):
        if self._drag is None:
            cursors = {"tl": Qt.SizeFDiagCursor, "br": Qt.SizeFDiagCursor, "tr": Qt.SizeBDiagCursor,
                       "bl": Qt.SizeBDiagCursor, "t": Qt.SizeVerCursor, "b": Qt.SizeVerCursor,
                       "l": Qt.SizeHorCursor, "r": Qt.SizeHorCursor, "move": Qt.SizeAllCursor}
            self.setCursor(cursors.get(self._hit(e.position()), Qt.CrossCursor))
            return
        mode, start, r0 = self._drag
        p = self._to_img(e.position())
        W, H = self.img.width(), self.img.height()
        px = min(max(p.x(), 0), W)
        py = min(max(p.y(), 0), H)
        r = QRectF(r0)
        if mode == "move":
            d = p - start
            dx = min(max(d.x(), -r0.left()), W - r0.right())
            dy = min(max(d.y(), -r0.top()), H - r0.bottom())
            r.translate(dx, dy)
        else:
            if "l" in mode:
                r.setLeft(px)
            if "r" in mode:
                r.setRight(px)
            if "t" in mode:
                r.setTop(py)
            if "b" in mode:
                r.setBottom(py)
        self.crop = r
        self.update()

    def mouseReleaseEvent(self, e):
        self._drag = None
        self.crop = self.crop.normalized()
        if self.crop.width() < 2 or self.crop.height() < 2:
            self.reset()
        self.update()

    def reset(self):
        self.crop = QRectF(0, 0, self.img.width(), self.img.height())
        self.update()

    def crop_rect(self) -> QRect:
        r = self.crop.normalized()
        return QRect(int(r.left()), int(r.top()), max(1, int(r.width())), max(1, int(r.height())))


class CropDialog(QDialog):
    def __init__(self, img: QImage, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Bild zuschneiden")
        self.resize(760, 600)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Rahmen an den Anfassern ziehen oder neuen Bereich aufziehen."))
        self.widget = CropWidget(img)
        lay.addWidget(self.widget, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        reset = QPushButton("Zurücksetzen")
        reset.clicked.connect(self.widget.reset)
        buttons.addButton(reset, QDialogButtonBox.ResetRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def crop_rect(self) -> QRect:
        return self.widget.crop_rect()
