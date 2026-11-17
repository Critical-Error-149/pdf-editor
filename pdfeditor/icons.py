"""Einheitliche Strich-Symbole (24×24-Raster, abgerundete Linien) für das Menüband.

Die Symbole werden zur Laufzeit gezeichnet: normal in Textfarbe, aktiv in der Akzentfarbe,
deaktiviert gedimmt. Keine Bilddateien nötig, gestochen scharf bei jeder Skalierung.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

from . import theme

_cache: dict = {}


def _p(*pts):
    """Polylinie aus Punkten (x, y) im 24er-Raster."""
    path = QPainterPath(QPointF(*pts[0]))
    for pt in pts[1:]:
        path.lineTo(QPointF(*pt))
    return path


def _rr(x, y, w, h, r):
    path = QPainterPath()
    path.addRoundedRect(QRectF(x, y, w, h), r, r)
    return path


def _circle(cx, cy, r):
    path = QPainterPath()
    path.addEllipse(QPointF(cx, cy), r, r)
    return path


def _arc(cx, cy, r, start, span):
    path = QPainterPath()
    rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
    path.arcMoveTo(rect, start)
    path.arcTo(rect, start, span)
    return path


def _lasso():
    """Gestrichelte Schlinge mit Seilende (Lassoauswahl)."""
    out = QPainterPath()
    rect = QRectF(3.5, 3.5, 17, 11.5)
    start = 250.0  # Lücke unten links, dort beginnt das Seil
    for k in range(9):
        a = start + 8 + k * 34
        out.arcMoveTo(rect, a)
        out.arcTo(rect, a, 20)
    rope = QPainterPath()
    rope.arcMoveTo(rect, start)
    p0 = rope.currentPosition()
    rope.cubicTo(QPointF(p0.x() - 1.5, p0.y() + 3), QPointF(p0.x() + 3, p0.y() + 4), QPointF(p0.x() + 1, p0.y() + 6.5))
    out.addPath(rope)
    out.addEllipse(QPointF(p0.x(), p0.y() + 0.2), 1.3, 1.3)
    return out


def _join(*paths):
    out = QPainterPath()
    for p in paths:
        out.addPath(p)
    return out



# Jede Funktion liefert (Pfad, Füllflächen) im 24er-Raster
SHAPES = {
    "undo": lambda: _join(_p((9, 14), (4, 9), (9, 4)), _p((4, 9), (14.5, 9)), _arc(14.5, 14.5, 5.5, 90, -180),
                          _p((14.5, 20), (9, 20))),
    "redo": lambda: _join(_p((15, 14), (20, 9), (15, 4)), _p((20, 9), (9.5, 9)), _arc(9.5, 14.5, 5.5, 90, 180),
                          _p((9.5, 20), (15, 20))),
    "select": lambda: _p((5, 3), (5, 19), (9.5, 14.5), (12.5, 21), (15, 20), (12, 13.5), (18, 13.5), (5, 3)),
    "lasso": _lasso,
    "finger": lambda: _join(_rr(9.5, 2.5, 5, 11.5, 2.5), _p((9.5, 10), (6.5, 12.5), (7, 15.5), (10, 19), (16.5, 19),
                                                            (17.5, 13), (14.5, 10.5)),
                            _p((3, 22), (7, 20.5), (11, 22), (15, 20.5), (21, 22))),
    "hand": lambda: _join(_p((12, 3), (12, 21)), _p((3, 12), (21, 12)), _p((9.5, 5.5), (12, 3), (14.5, 5.5)),
                          _p((9.5, 18.5), (12, 21), (14.5, 18.5)), _p((5.5, 9.5), (3, 12), (5.5, 14.5)),
                          _p((18.5, 9.5), (21, 12), (18.5, 14.5))),
    "eraser": lambda: _join(_p((7, 21), (20, 21)), _p((5.5, 16.5), (14.5, 7.5), (19.5, 12.5), (11, 21), (7.5, 21),
                                                      (4.5, 18), (5.5, 16.5)), _p((9.5, 12.5), (14.5, 17.5))),
    "markup": lambda: _join(_p((9, 11), (15.5, 4.5), (19.5, 8.5), (13, 15)), _p((9, 11), (7, 17), (13, 15), (9, 11)),
                            _p((6.5, 17.5), (4.5, 19.5), (8.5, 19.5)), _p((3, 21.5), (21, 21.5))),
    "text": lambda: _join(_p((5, 7), (5, 4.5), (19, 4.5), (19, 7)), _p((12, 4.5), (12, 19.5)), _p((9, 19.5), (15, 19.5))),
    "note": lambda: _join(_p((15, 20), (5, 20), (5, 4), (19, 4), (19, 16), (15, 20)), _p((15, 20), (15, 16), (19, 16)),
                          _p((8.5, 9), (15.5, 9)), _p((8.5, 12.5), (13, 12.5))),
    "image": lambda: _join(_rr(3.5, 4.5, 17, 15, 2.5), _circle(9, 9.5, 1.6), _p((20.5, 15.5), (15.5, 10.5), (6, 19.5))),
    "line": lambda: _p((5, 19), (19, 5)),
    "ruler": lambda: _join(_p((2.5, 15.5), (15.5, 2.5), (21.5, 8.5), (8.5, 21.5), (2.5, 15.5)),
                           _p((5.5, 12.5), (8, 15)), _p((8, 10), (9.5, 11.5)), _p((10.5, 7.5), (13, 10)),
                           _p((13, 5), (14.5, 6.5))),
    "arrow": lambda: _join(_p((5, 19), (19, 5)), _p((10, 5), (19, 5), (19, 14))),
    "rect": lambda: _rr(4, 5.5, 16, 13, 2),
    "ellipse": lambda: _circle(12, 12, 8),
    "shapes": lambda: _join(_rr(3.5, 10, 10, 10, 1.5), _circle(15.5, 8.5, 5)),
    "plus": lambda: _join(_p((12, 5), (12, 19)), _p((5, 12), (19, 12))),
    "folder": lambda: _p((3.5, 18.5), (3.5, 5.5), (9, 5.5), (11, 8), (20.5, 8), (20.5, 18.5), (3.5, 18.5)),
    "open": lambda: _join(_p((3.5, 18.5), (3.5, 5.5), (9, 5.5), (11, 8), (18.5, 8), (18.5, 10.5)),
                          _p((3.5, 18.5), (6.5, 11), (21.5, 11), (18.5, 18.5), (3.5, 18.5))),
    "save": lambda: _join(_p((5, 4), (16, 4), (20, 8), (20, 20), (4, 20), (4, 4)), _rr(8, 13.5, 8, 6.5, 1),
                          _p((8, 4), (8, 8.5), (14.5, 8.5), (14.5, 4))),
    "save_as": lambda: _join(_p((12, 20), (4, 20), (4, 4), (16, 4), (20, 8), (20, 11)), _p((8, 4), (8, 8.5), (14.5, 8.5),
                                                                                          (14.5, 4)),
                             _p((15, 21), (15, 18.5), (20, 13.5), (22, 15.5), (17, 20.5), (15, 21))),
    "new": lambda: _join(_p((14, 3.5), (6, 3.5), (6, 20.5), (18, 20.5), (18, 7.5), (14, 3.5)), _p((14, 3.5), (14, 7.5),
                                                                                                (18, 7.5))),
    "page_add": lambda: _join(_p((14, 3.5), (6, 3.5), (6, 20.5), (18, 20.5), (18, 7.5), (14, 3.5)),
                              _p((14, 3.5), (14, 7.5), (18, 7.5)), _p((12, 10.5), (12, 17)), _p((8.75, 13.75), (15.25, 13.75))),
    "pages_add": lambda: _join(_rr(8, 7, 12, 14, 2), _p((5, 17), (4, 17), (4, 3.5), (15, 3.5), (15, 4.5)),
                               _p((14, 11), (14, 17)), _p((11, 14), (17, 14))),
    "pdf_in": lambda: _join(_p((14, 3.5), (7, 3.5), (7, 9)), _p((7, 15.5), (7, 20.5), (18, 20.5), (18, 7.5), (14, 3.5)),
                            _p((14, 3.5), (14, 7.5), (18, 7.5)), _p((2.5, 12.25), (12, 12.25)), _p((9, 9.25), (12, 12.25), (9, 15.25))),
    "image_page": lambda: _join(_p((14, 3.5), (6, 3.5), (6, 20.5), (18, 20.5), (18, 7.5), (14, 3.5)),
                                _circle(10.5, 11, 1.4), _p((18, 17.5), (14.5, 14), (9, 20.5))),
    "duplicate": lambda: _join(_rr(8, 8, 12.5, 12.5, 2), _p((4, 15.5), (3.5, 15), (3.5, 5.5), (5.5, 3.5), (15, 3.5),
                                                          (15.5, 4)),
                               _p((14.25, 11.25), (14.25, 17.25)), _p((11.25, 14.25), (17.25, 14.25))),  # Plus: neu
    "move": lambda: _join(_p((12, 3.5), (12, 20.5)), _p((8.5, 7), (12, 3.5), (15.5, 7)), _p((8.5, 17), (12, 20.5), (15.5, 17))),
    "rotate_l": lambda: _join(_arc(12, 12, 7.5, 160, -250), _p((3.5, 6.5), (4.5, 10.5), (8.5, 9.5))),
    "rotate_r": lambda: _join(_arc(12, 12, 7.5, 20, 250), _p((20.5, 6.5), (19.5, 10.5), (15.5, 9.5))),
    "trash": lambda: _join(_p((4, 6.5), (20, 6.5)), _p((9, 6.5), (9, 4), (15, 4), (15, 6.5)),
                           _p((6, 6.5), (7, 20), (17, 20), (18, 6.5)), _p((10, 10.5), (10, 16)), _p((14, 10.5), (14, 16))),
    "export": lambda: _join(_p((14, 3.5), (6, 3.5), (6, 20.5), (11, 20.5)), _p((14, 3.5), (14, 7.5), (18, 7.5), (18, 11)),
                            _p((14, 3.5), (18, 7.5)), _p((13, 17), (21, 17)), _p((18, 14), (21, 17), (18, 20))),
    "print": lambda: _join(_p((7, 8), (7, 3.5), (17, 3.5), (17, 8)), _rr(3.5, 8, 17, 8.5, 2),
                           _rr(7, 13.5, 10, 7, 1)),
    "zoom_in": lambda: _join(_circle(10.5, 10.5, 6.5), _p((15.5, 15.5), (20.5, 20.5)), _p((10.5, 7.5), (10.5, 13.5)),
                             _p((7.5, 10.5), (13.5, 10.5))),
    "zoom_out": lambda: _join(_circle(10.5, 10.5, 6.5), _p((15.5, 15.5), (20.5, 20.5)), _p((7.5, 10.5), (13.5, 10.5))),
    "fit_width": lambda: _join(_p((3.5, 12), (20.5, 12)), _p((7, 8.5), (3.5, 12), (7, 15.5)), _p((17, 8.5), (20.5, 12),
                                                                                                   (17, 15.5)),
                               _p((3.5, 4), (3.5, 6)), _p((20.5, 4), (20.5, 6))),
    "fit_page": lambda: _join(_rr(6.5, 3.5, 11, 17, 1.5), _p((12, 7), (12, 17)), _p((10, 9), (12, 7), (14, 9)),
                              _p((10, 15), (12, 17), (14, 15))),
    "fullscreen": lambda: _join(_p((4, 9), (4, 4), (9, 4)), _p((15, 4), (20, 4), (20, 9)), _p((20, 15), (20, 20), (15, 20)),
                                _p((9, 20), (4, 20), (4, 15))),
    "exit_fullscreen": lambda: _join(_p((9, 4), (9, 9), (4, 9)), _p((15, 4), (15, 9), (20, 9)),
                                     _p((20, 15), (15, 15), (15, 20)), _p((4, 15), (9, 15), (9, 20))),
    "panel_files": lambda: _join(_rr(3.5, 4.5, 17, 15, 2.5), _p((9.5, 4.5), (9.5, 19.5))),
    "panel_pages": lambda: _join(_rr(3.5, 4.5, 17, 15, 2.5), _p((8.5, 4.5), (8.5, 19.5)), _p((5.5, 8), (6.5, 8)),
                                 _p((5.5, 11), (6.5, 11))),
    # Zwischenablage mit Seite davor (eingefügtes Bild wird eine eigene Seite)
    "paste_page": lambda: _join(_p((7, 5), (5, 5), (5, 20.5), (11, 20.5)), _rr(7.5, 3, 6, 4, 1.2),
                                _p((13, 9), (19.5, 9), (19.5, 21), (13, 21), (13, 9))),
    "copy": lambda: _join(_rr(9, 8.5, 11, 12.5, 2), _p((6, 16.5), (5, 16.5), (4, 15.5), (4, 4.5), (5, 3.5),
                                                       (14.5, 3.5), (15.5, 4.5), (15.5, 5.5))),
    "cut": lambda: _join(_circle(7, 17.5, 3), _circle(17, 17.5, 3), _p((9.2, 15.4), (17, 3.5)), _p((14.8, 15.4), (7, 3.5))),
    "video": lambda: _join(_rr(3, 6, 13.5, 12, 2.5), _p((16.5, 10.5), (21, 7.5), (21, 16.5), (16.5, 13.5))),
    "camera": lambda: _join(_p((3.5, 8.5), (3.5, 18.5), (20.5, 18.5), (20.5, 8.5), (16.5, 8.5), (15, 6), (9, 6), (7.5, 8.5),
                                (3.5, 8.5)), _circle(12, 13, 3.3)),
    "mic": lambda: _join(_rr(9.5, 3.5, 5, 10, 2.5), _arc(12, 10.5, 5.5, 180, 180), _p((12, 16), (12, 20)),
                         _p((9, 20), (15, 20))),
    "play": lambda: _p((8, 5), (19, 12), (8, 19), (8, 5)),
    "paste": lambda: _join(_p((8, 5), (6, 5), (6, 20.5), (18, 20.5), (18, 5), (16, 5)), _rr(8.5, 3, 7, 4, 1.2)),
    "search": lambda: _join(_circle(10.5, 10.5, 6.5), _p((15.5, 15.5), (20.5, 20.5))),
    "crop": lambda: _join(_p((6, 2.5), (6, 18), (21.5, 18)), _p((2.5, 6), (18, 6), (18, 21.5))),
    "check": lambda: _p((5, 12.5), (10, 17.5), (19, 7)),
    "edit": lambda: _join(_p((4, 20), (4.5, 16), (16, 4.5), (19.5, 8), (8, 19.5), (4, 20)), _p((13.5, 7), (17, 10.5))),
    "close": lambda: _join(_p((6, 6), (18, 18)), _p((18, 6), (6, 18))),
    "split_right": lambda: _join(_rr(3.5, 4.5, 17, 15, 2.5), _p((12, 4.5), (12, 19.5))),
    "split_down": lambda: _join(_rr(3.5, 4.5, 17, 15, 2.5), _p((3.5, 12), (20.5, 12))),
    "unsplit": lambda: _join(_rr(3.5, 4.5, 17, 15, 2.5), _p((7.5, 8.5), (16.5, 8.5))),
    "tab_close": lambda: _join(_rr(3.5, 4.5, 17, 15, 2.5), _p((9.5, 9.5), (14.5, 14.5)), _p((14.5, 9.5), (9.5, 14.5))),
    "prev": lambda: _p((14.5, 6), (8.5, 12), (14.5, 18)),
    "up": lambda: _join(_p((12, 19), (12, 5)), _p((6.5, 10.5), (12, 5), (17.5, 10.5))),
    "recent": lambda: _join(_circle(12, 12, 8), _p((12, 7.5), (12, 12), (15.5, 14))),
    "next": lambda: _p((9.5, 6), (15.5, 12), (9.5, 18)),
    "help": lambda: _join(_circle(12, 12, 8.5), _arc(12, 9.5, 2.75, 160, -230), _p((12, 12.5), (12, 14)),
                          _p((12, 17), (12, 17.1))),
    "settings": lambda: _join(_circle(12, 12, 3), _circle(12, 12, 8)),
    "menu": lambda: _join(_p((4, 7), (20, 7)), _p((4, 12), (20, 12)), _p((4, 17), (20, 17))),
}


def _render(name, color: QColor, size: int, dpr: float) -> QPixmap:
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / 24.0, size / 24.0)
    p.setPen(QPen(color, 1.75, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    p.drawPath(SHAPES[name]())
    p.end()
    return pm


def icon(name: str, size: int = 20) -> QIcon:
    """Symbol mit Zuständen: normal (Text), aktiv/eingeschaltet (Akzent), deaktiviert (gedimmt)."""
    key = (name, size)
    if key in _cache:
        return _cache[key]
    ic = QIcon()
    for dpr in (1.0, 2.0):
        ic.addPixmap(_render(name, theme.TEXT, size, dpr), QIcon.Normal, QIcon.Off)
        ic.addPixmap(_render(name, theme.ACCENT_BRIGHT, size, dpr), QIcon.Normal, QIcon.On)
        ic.addPixmap(_render(name, theme.ACCENT_BRIGHT, size, dpr), QIcon.Active, QIcon.On)
        ic.addPixmap(_render(name, theme.TEXT, size, dpr), QIcon.Active, QIcon.Off)
        ic.addPixmap(_render(name, theme.TEXT_DISABLED, size, dpr), QIcon.Disabled, QIcon.Off)
        ic.addPixmap(_render(name, theme.TEXT_DISABLED, size, dpr), QIcon.Disabled, QIcon.On)
    _cache[key] = ic
    return ic


def colored(name: str, color: QColor, size: int = 20) -> QIcon:
    ic = QIcon()
    for dpr in (1.0, 2.0):
        ic.addPixmap(_render(name, QColor(color), size, dpr))
    return ic

