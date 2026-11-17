"""Programmsymbol: weißes Blatt mit Eselsohr und Stiftstrich auf Akzent-Grund (zur Laufzeit gezeichnet).

Für die .exe wird daraus mit `write_ico()` eine .ico-Datei erzeugt (siehe build.py).
"""
from __future__ import annotations

import struct

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def render(size: int) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / 64.0, size / 64.0)  # Entwurf im 64er-Raster
    # Grund: abgerundetes Quadrat in der Akzentfarbe
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#5e6ad2"))
    p.drawRoundedRect(QRectF(2, 2, 60, 60), 14, 14)
    # Blatt mit Eselsohr
    page = QPainterPath(QPointF(17, 11))
    for pt in ((38, 11), (48, 21), (48, 53), (17, 53)):
        page.lineTo(QPointF(*pt))
    page.closeSubpath()
    p.setBrush(QColor("#ffffff"))
    p.drawPath(page)
    ear = QPainterPath(QPointF(38, 11))
    ear.lineTo(QPointF(38, 21))
    ear.lineTo(QPointF(48, 21))
    ear.closeSubpath()
    p.setBrush(QColor("#c9cdf5"))
    p.drawPath(ear)
    # Textzeilen
    p.setPen(QPen(QColor("#b4b9e8"), 2.6, Qt.SolidLine, Qt.RoundCap))
    for y in (28, 34):
        p.drawLine(QPointF(23, y), QPointF(41, y))
    # handschriftlicher Strich
    stroke = QPainterPath(QPointF(22, 45))
    stroke.cubicTo(QPointF(26, 38), QPointF(29, 50), QPointF(33, 43))
    stroke.cubicTo(QPointF(36, 38), QPointF(38, 48), QPointF(43, 42))
    p.setPen(QPen(QColor("#f2777a"), 3.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    p.drawPath(stroke)
    p.end()
    return img


def icon() -> QIcon:
    ic = QIcon()
    for s in SIZES:
        ic.addPixmap(QPixmap.fromImage(render(s)))
    return ic


def _png_bytes(img: QImage) -> bytes:
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(data.data())


def write_ico(path: str):
    """Windows-.ico mit allen Größen (PNG-komprimierte Einträge, ab Windows Vista unterstützt)."""
    images = [(s, _png_bytes(render(s))) for s in SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for s, png in images:
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
        blobs += png
    with open(path, "wb") as f:
        f.write(header + entries + blobs)
