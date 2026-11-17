"""Erkennung von Stiftgesten (reine Geometrie, ohne Qt).

* Durchkritzeln       -> Löschen
* gerade Linie + Halten -> gerade Linie
* Kreis/Ellipse bzw. Rechteck + Halten -> saubere Form

Alle Punkte sind (x, y)-Tupel in Seitenkoordinaten (pt).
"""
from __future__ import annotations

import math

MIN_SIZE = 12.0          # kleinere Striche werden nie umgewandelt (pt)
LINE_TOLERANCE = 0.07    # max. Abweichung von der Geraden relativ zur Länge
CLOSE_TOLERANCE = 0.3    # Abstand Start/Ende relativ zur Größe, damit eine Form als geschlossen gilt
SHAPE_TOLERANCE = 0.16   # mittlere Abweichung von Ellipse/Rechteck (relativ)


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def path_length(pts):
    return sum(_dist(a, b) for a, b in zip(pts, pts[1:]))


def bbox(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _resample(pts, step):
    """Punkte in gleichmäßigem Abstand (glättet ungleichmäßige Eingaberaten)."""
    if len(pts) < 2:
        return list(pts)
    out = [pts[0]]
    carry = 0.0
    for a, b in zip(pts, pts[1:]):
        seg = _dist(a, b)
        if seg == 0:
            continue
        t = step - carry
        while t <= seg:
            f = t / seg
            out.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
            t += step
        carry = (carry + seg) % step
    if _dist(out[-1], pts[-1]) > step * 0.3:
        out.append(pts[-1])
    return out


# ---------------------------------------------------------------- Durchkritzeln
def _reversal_points(pts):
    """Indizes der Wendepunkte (Richtungsumkehr um mehr als ca. 120°)."""
    x0, y0, x1, y1 = bbox(pts)
    size = max(x1 - x0, y1 - y0, 1.0)
    rs = _resample(pts, max(1.5, size / 40))
    k = 2  # Richtungen über mehrere Schritte vergleichen (robust bei runden Spitzen)
    idx = []
    i = k
    while i < len(rs) - k:
        a, b, c = rs[i - k], rs[i], rs[i + k]
        v1 = (b[0] - a[0], b[1] - a[1])
        v2 = (c[0] - b[0], c[1] - b[1])
        n1, n2 = math.hypot(*v1), math.hypot(*v2)
        if n1 and n2 and (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2) < -0.5:
            idx.append(i)
            i += k + 1
        else:
            i += 1
    return rs, idx


def is_scribble(pts) -> bool:
    """Hin-und-her-Kritzeln: viele Wendungen, die weit über dieselbe Stelle zurücklaufen.

    Handschrift (z. B. 'mmmm') hat zwar auch viele Wendungen, kommt dabei aber stetig
    voran. Entscheidend ist deshalb das Verhältnis von Ausschlag zu Vorankommen."""
    if len(pts) < 8:
        return False
    x0, y0, x1, y1 = bbox(pts)
    w, h = x1 - x0, y1 - y0
    if max(w, h) < 8:
        return False
    if path_length(pts) < 2.5 * math.hypot(w, h):
        return False
    rs, rev = _reversal_points(pts)
    if len(rev) < 4:
        return False
    marks = [rs[0]] + [rs[i] for i in rev] + [rs[-1]]
    segs = [(b[0] - a[0], b[1] - a[1]) for a, b in zip(marks, marks[1:])]
    # Hauptrichtung des Hin und Her (jedes zweite Segment umdrehen und mitteln)
    ux = sum(s[0] * (1 if i % 2 == 0 else -1) for i, s in enumerate(segs))
    uy = sum(s[1] * (1 if i % 2 == 0 else -1) for i, s in enumerate(segs))
    n = math.hypot(ux, uy) or 1.0
    ux, uy = ux / n, uy / n
    amplitude = sum(abs(s[0] * ux + s[1] * uy) for s in segs[1:-1]) / max(1, len(segs) - 2)
    # Vorankommen quer dazu, je Hin- oder Rückweg
    proj = [p[0] * -uy + p[1] * ux for p in rs]
    progress = (max(proj) - min(proj)) / len(segs)
    return amplitude >= 3.0 * max(progress, 1.0)


# ---------------------------------------------------------------- Linie
def straight_line(pts):
    """(start, ende), wenn der Strich ziemlich gerade ist, sonst None."""
    if len(pts) < 2:
        return None
    a, b = pts[0], pts[-1]
    length = _dist(a, b)
    if length < MIN_SIZE:
        return None
    dx, dy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
    max_dev = max(abs((p[0] - a[0]) * dy - (p[1] - a[1]) * dx) for p in pts)
    if max_dev > LINE_TOLERANCE * length + 1.5:
        return None
    # Der Strich darf nicht über die Endpunkte hinaus "zurücklaufen"
    if path_length(pts) > 1.35 * length + 4:
        return None
    return a, b


# ---------------------------------------------------------------- Kreis / Rechteck
def closed_shape(pts):
    """('ellipse' | 'rect', (x0, y0, x1, y1)) für eine annähernd geschlossene Form, sonst None."""
    if len(pts) < 8:
        return None
    x0, y0, x1, y1 = bbox(pts)
    w, h = x1 - x0, y1 - y0
    if max(w, h) < MIN_SIZE or min(w, h) < MIN_SIZE * 0.5:
        return None
    size = (w + h) / 2
    if _dist(pts[0], pts[-1]) > CLOSE_TOLERANCE * size:
        return None
    length = path_length(pts)
    perimeter_est = 2 * (w + h)
    if length < 0.6 * perimeter_est * 0.785 or length > 1.6 * perimeter_est:
        return None  # zu kurz (Bogen) oder viel zu lang (mehrfach umkreist/gekritzelt)
    if is_scribble(pts):
        return None
    rs = _resample(pts, max(1.0, size / 60))
    cx, cy, a, b = (x0 + x1) / 2, (y0 + y1) / 2, w / 2, h / 2
    # Ellipse: radiale Abweichung
    e_err = sum(abs(math.hypot((p[0] - cx) / a, (p[1] - cy) / b) - 1) for p in rs) / len(rs)
    # Rechteck: Abstand zur nächsten Kante, relativ zur halben Größe
    half = min(w, h) / 2
    r_err = sum(min(abs(p[0] - x0), abs(p[0] - x1), abs(p[1] - y0), abs(p[1] - y1)) for p in rs) / len(rs) / half
    # Ecken prüfen: ein Rechteck hat Punkte nahe allen vier Ecken, eine Ellipse nicht
    corner_d = [min(math.hypot(p[0] - cxr, p[1] - cyr) for p in rs) / half
                for cxr, cyr in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
    corners_hit = sum(d < 0.35 for d in corner_d)
    if corners_hit >= 3 and r_err < SHAPE_TOLERANCE:
        return "rect", (x0, y0, x1, y1)
    if e_err < SHAPE_TOLERANCE and corners_hit <= 2:
        return "ellipse", (x0, y0, x1, y1)
    if r_err < SHAPE_TOLERANCE * 0.8 and r_err < e_err:
        return "rect", (x0, y0, x1, y1)
    return None


SNAP_DEGREES = 6.0  # so nah an waagerecht/senkrecht rastet eine Linie ein (Voreinstellung)
_snap = None


def snap_degrees() -> float:
    """Einrastwinkel in Grad (Datei → Einstellungen → Bedienung); 0 = nie einrasten."""
    global _snap
    if _snap is None:
        try:
            from PySide6.QtCore import QSettings
            _snap = float(QSettings("PDFEditor", "PDFEditor").value("input/snap_degrees", SNAP_DEGREES))
        except (TypeError, ValueError, ImportError):
            _snap = SNAP_DEGREES
    return _snap


def set_snap_degrees(value):
    global _snap
    _snap = float(value)


def snap_line(a, b, degrees=None):
    """Endpunkt b so verschieben, dass die Linie fast waagerechte/senkrechte Lagen exakt trifft."""
    degrees = snap_degrees() if degrees is None else degrees
    dx, dy = b[0] - a[0], b[1] - a[1]
    if (dx == 0 and dy == 0) or degrees <= 0:
        return b
    angle = abs(math.degrees(math.atan2(dy, dx))) % 180  # 0 = waagerecht, 90 = senkrecht
    if angle <= degrees or angle >= 180 - degrees:
        return (b[0], a[1])
    if abs(angle - 90) <= degrees:
        return (a[0], b[1])
    return b


def scale_box(box, grab, anchor, pen, min_size=4.0):
    """Form beim Weiterziehen skalieren.

    box: (x0, y0, x1, y1) beim Erkennen; grab: Stiftposition beim Erkennen; anchor: fester
    Punkt (gegenüber vom Stift); pen: aktuelle Stiftposition. Liegt der Griffpunkt nahe einer
    Ecke, werden Breite und Höhe getrennt skaliert, sonst gleichmäßig."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    gx, gy = grab[0] - anchor[0], grab[1] - anchor[1]
    px, py = pen[0] - anchor[0], pen[1] - anchor[1]
    if abs(gx) > 0.3 * w and abs(gy) > 0.3 * h:
        sx, sy = px / gx, py / gy
    else:
        g = math.hypot(gx, gy) or 1.0
        s = math.hypot(px, py) / g
        sx = sy = s
    sx = max(abs(sx), min_size / max(w, 1e-6))
    sy = max(abs(sy), min_size / max(h, 1e-6))
    nx0 = anchor[0] + (x0 - anchor[0]) * sx
    nx1 = anchor[0] + (x1 - anchor[0]) * sx
    ny0 = anchor[1] + (y0 - anchor[1]) * sy
    ny1 = anchor[1] + (y1 - anchor[1]) * sy
    return min(nx0, nx1), min(ny0, ny1), max(nx0, nx1), max(ny0, ny1)


def recognize(pts):
    """Erkennung beim Verweilen des Stifts: ('line', (a, b)) | ('rect'|'ellipse', bbox) | None."""
    line = straight_line(pts)
    if line is not None:
        return "line", line
    return closed_shape(pts)
