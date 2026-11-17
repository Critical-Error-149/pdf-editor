"""Lineal zum Zeichnen und Messen (wie in OneNote).

Das Lineal liegt fest auf dem Bildschirm (scrollt nicht mit), lässt sich verschieben und drehen und zeigt eine Skala
in echten Zentimetern der Seite – passend zum aktuellen Zoom. Stift- und Markerstriche, die an einer Längskante
beginnen, werden an die Kante gezogen: gerade Linien in jedem Winkel, die Länge steht dabei auf dem Lineal.

Bedienung: ein Finger, Stift oder Maus auf dem Lineal verschiebt es, zwei Finger drehen es (rastet bei 0°, 45°,
90° … ein), das Mausrad über dem Lineal dreht in 1°-Schritten.

Gezeichnet wird das Lineal ungedreht in ein Bild (nur bei anderem Zoom neu); Drehen und Verschieben kosten so nichts
– die Grafikkarte legt das Bild gedreht auf (gpu_view), die normale Darstellung dreht beim Zeichnen.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPen

WIDTH = 64          # Breite in Bildschirmpixeln
PAD = 3             # durchsichtiger Rand im Bild: gedreht gezeichnet bleiben die Außenkanten glatt
EDGE_IN = 12        # Fangbereich an einer Längskante: so weit im Lineal …
EDGE_OUT = 32       # … und so weit davor beginnt ein Strich an der Kante
SNAP_DEG = 2.5      # Drehen mit zwei Fingern rastet bei Vielfachen von 45° ein
ZERO = 14           # Nullpunkt der Skala: Abstand vom linken Ende
PT_PER_CM = 72 / 2.54

BODY = QColor(248, 248, 248, 215)
BORDER = QColor(70, 70, 70, 230)
TICK = QColor(25, 25, 25)
LABEL_BG = QColor(32, 32, 32, 215)


def format_length(pt):
    """Länge in pt als „4,2 cm“ (unter 1 cm in mm)."""
    cm = pt / PT_PER_CM
    if cm < 1:
        return f"{cm * 10:.0f} mm"
    return f"{cm:.1f} cm".replace(".", ",")


class Ruler:
    def __init__(self):
        self.visible = False
        self.center = QPointF(400, 300)
        self.angle = 0.0            # Grad, im Uhrzeigersinn (Bildschirmkoordinaten)
        self._raw = 0.0             # ungerastete Drehung während einer Geste
        self.length = 900.0
        self.readout = None         # Messwert während eines Strichs an der Kante (sonst wird der Winkel gezeigt)
        self._drag = None
        self._body = (None, None)   # (Schlüssel, Bild)
        self._label = (None, None)

    # ------------------------------------------------------------ Lage
    def place(self, w, h):
        """Neu einblenden: waagerecht in der Mitte der Ansicht."""
        self.length = max(360.0, min(1800.0, 0.8 * max(w, h)))
        self.center = QPointF(w / 2, h / 2)
        self.angle = self._raw = 0.0

    def keep_inside(self, w, h):
        """Mittelpunkt in der Ansicht halten (z. B. nach Verkleinern des Fensters)."""
        self.center = QPointF(min(max(self.center.x(), 0.0), float(w)), min(max(self.center.y(), 0.0), float(h)))

    def axes(self):
        """(entlang, quer) als Einheitsvektoren; „quer“ zeigt bei 0° nach unten."""
        a = math.radians(self.angle)
        return QPointF(math.cos(a), math.sin(a)), QPointF(-math.sin(a), math.cos(a))

    def to_local(self, p: QPointF):
        u, n = self.axes()
        d = p - self.center
        return d.x() * u.x() + d.y() * u.y(), d.x() * n.x() + d.y() * n.y()

    def from_local(self, s, t):
        u, n = self.axes()
        return self.center + u * s + n * t

    def corners(self, pad=PAD):
        """Ecken des Linealbilds (oben links, oben rechts, unten rechts, unten links) in Ansichtskoordinaten."""
        hl, hw = self.length / 2 + pad, WIDTH / 2 + pad
        return [self.from_local(-hl, -hw), self.from_local(hl, -hw), self.from_local(hl, hw), self.from_local(-hl, hw)]

    def contains(self, p: QPointF):
        s, t = self.to_local(p)
        return abs(s) <= self.length / 2 and abs(t) <= WIDTH / 2

    def edge_at(self, p: QPointF):
        """-1 = obere, 1 = untere Längskante (Fangbereich), 0 = keine."""
        s, t = self.to_local(p)
        if abs(s) > self.length / 2:
            return 0
        if -WIDTH / 2 - EDGE_OUT <= t <= -WIDTH / 2 + EDGE_IN:
            return -1
        if WIDTH / 2 - EDGE_IN <= t <= WIDTH / 2 + EDGE_OUT:
            return 1
        return 0

    def project(self, p: QPointF, edge, offset):
        """Punkt auf die Kante ziehen; offset = Abstand außerhalb der Kante (halbe Strichdicke)."""
        s, _ = self.to_local(p)
        s = max(-self.length / 2, min(self.length / 2, s))
        return self.from_local(s, edge * (WIDTH / 2 + offset))

    # ------------------------------------------------------------ Verschieben / Drehen
    def begin_drag(self, p: QPointF):
        self._drag = self.center - p

    def drag_to(self, p: QPointF):
        if self._drag is not None:
            self.center = p + self._drag

    def end_drag(self):
        self._drag = None

    def begin_rotate(self):
        self._raw = self.angle

    def rotate_by(self, deg, pivot: QPointF | None = None, snap=True):
        pivot = QPointF(self.center) if pivot is None else pivot
        old = self.angle
        self._raw += deg
        self.angle = self._snapped(self._raw) if snap else self._raw
        d = math.radians(self.angle - old)
        v = self.center - pivot
        c, s = math.cos(d), math.sin(d)
        self.center = pivot + QPointF(v.x() * c - v.y() * s, v.x() * s + v.y() * c)

    def two_fingers(self, a0: QPointF, b0: QPointF, a1: QPointF, b1: QPointF):
        """Zwei Finger bewegt (alt a0/b0 → neu a1/b1): mitdrehen und mitverschieben."""
        v0, v1 = b0 - a0, b1 - a1
        if math.hypot(v0.x(), v0.y()) < 8 or math.hypot(v1.x(), v1.y()) < 8:
            return  # Finger zu dicht beieinander: Winkel unzuverlässig
        delta = math.degrees(math.atan2(v1.y(), v1.x()) - math.atan2(v0.y(), v0.x()))
        delta = (delta + 180) % 360 - 180
        m0, m1 = (a0 + b0) / 2, (a1 + b1) / 2
        self.rotate_by(delta, m0)
        self.center += m1 - m0

    @staticmethod
    def _snapped(raw):
        near = round(raw / 45) * 45
        return float(near) if abs(raw - near) <= SNAP_DEG else raw

    def angle_text(self):
        a = round(self.angle) % 180  # Lineal ist symmetrisch: 180° = 0°
        return f"{a}°"

    # ------------------------------------------------------------ Bilder
    def body_image(self, scale, dpr):
        """Ungedrehtes Linealbild mit Skala; scale = Bildschirmpixel je pt (Zoom)."""
        key = (round(self.length), round(scale, 6), dpr)
        if self._body[0] == key:
            return self._body[1]
        L, W = self.length, WIDTH
        img = QImage(math.ceil((L + 2 * PAD) * dpr), math.ceil((W + 2 * PAD) * dpr),
                     QImage.Format_RGBA8888_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        p.translate(PAD, PAD)
        p.setPen(QPen(BORDER, 1))
        p.setBrush(BODY)
        p.drawRoundedRect(QRectF(0.5, 0.5, L - 1, W - 1), 4, 4)
        cm = PT_PER_CM * scale  # Bildschirmpixel je Zentimeter
        step = 1 if cm / 10 >= 5 else 5 if cm / 2 >= 5 else 10  # Teilstriche in mm: 1, 5 oder 10
        label_every = next((k for k in (1, 2, 5, 10, 20, 50, 100) if cm * k >= 30), 100)
        font = QFont()
        font.setPixelSize(11)
        p.setFont(font)
        p.setPen(QPen(TICK, 1))
        mm = 0
        while True:
            x = ZERO + mm / 10 * cm
            if x > L - 3:
                break
            h = 16 if mm % 10 == 0 else 11 if mm % 5 == 0 else 6
            p.drawLine(QPointF(x, 0), QPointF(x, h))
            p.drawLine(QPointF(x, W), QPointF(x, W - h))
            if mm % 10 == 0 and (mm // 10) % label_every == 0:
                p.drawText(QRectF(x - 20, 17, 40, 14), Qt.AlignHCenter | Qt.AlignTop, str(mm // 10))
            mm += step
        p.end()
        self._body = (key, img)
        return img

    def label_center(self):
        return self.from_local(0, 12)  # unter der Zahlenreihe

    def label_image(self, dpr):
        """Aufrechtes Schild in der Mitte: Messwert beim Zeichnen an der Kante, sonst der Winkel."""
        text = self.readout or self.angle_text()
        key = (text, dpr)
        if self._label[0] == key:
            return self._label[1]
        font = QFont()
        font.setPixelSize(13)
        font.setBold(True)
        fm = QFontMetricsF(font)
        w, h = fm.horizontalAdvance(text) + 16, 22
        img = QImage(math.ceil(w * dpr), math.ceil(h * dpr), QImage.Format_RGBA8888_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(LABEL_BG)
        p.drawRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)
        p.setPen(Qt.white)
        p.setFont(font)
        p.drawText(QRectF(0, 0, w, h), Qt.AlignCenter, text)
        p.end()
        self._label = (key, img)
        return img

    def paint(self, painter: QPainter, scale, dpr):
        """Normale Darstellung: Lineal in Ansichtskoordinaten zeichnen (painter ohne Szenen-Transformation)."""
        painter.save()
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.translate(self.center)
        painter.rotate(self.angle)
        painter.drawImage(QPointF(-self.length / 2 - PAD, -WIDTH / 2 - PAD), self.body_image(scale, dpr))
        painter.restore()
        lab = self.label_image(dpr)
        c = self.label_center()
        painter.drawImage(c - QPointF(lab.width() / dpr / 2, lab.height() / dpr / 2), lab)
