"""Bedien-Einstellungen, die an mehreren Stellen gebraucht werden (einmal gelesen, danach zwischengespeichert)."""
from __future__ import annotations

_cache = {}


def _value(key, default):
    if key not in _cache:
        from PySide6.QtCore import QSettings
        _cache[key] = QSettings("PDFEditor", "PDFEditor").value(key, default)
    return _cache[key]


def after_insert_mouse() -> bool:
    """Nach dem Einfügen (Text, Notiz, Bild, Form, Video …) automatisch zum Maus-Werkzeug wechseln?"""
    return str(_value("input/after_insert_mouse", "false")).lower() in ("true", "1")


def pen_auto_draw() -> bool:
    """Stift auf der Seite im Maus-Werkzeug ziehen: automatisch zum zuletzt benutzten Stift/Marker wechseln?
    (Tippen bleibt ein Klick, Halten ein Rechtsklick.)"""
    return str(_value("input/pen_auto_draw", "true")).lower() in ("true", "1")


def set_pen_auto_draw(on: bool):
    from PySide6.QtCore import QSettings
    QSettings("PDFEditor", "PDFEditor").setValue("input/pen_auto_draw", "true" if on else "false")
    _cache["input/pen_auto_draw"] = "true" if on else "false"


def finger_draw() -> bool:
    """Zeichnet ein einzelner Finger mit dem aktiven Zeichenwerkzeug (statt zu scrollen)?"""
    return str(_value("input/finger_draw", "false")).lower() in ("true", "1")


def set_finger_draw(on: bool):
    _set("input/finger_draw", on)


def note_close_outside() -> bool:
    """Schließt Tippen/Klicken neben eine geöffnete Notiz die Sprechblase?"""
    return str(_value("input/note_close_outside", "true")).lower() in ("true", "1")


def set_note_close_outside(on: bool):
    _set("input/note_close_outside", on)


# Glättung der Stiftstriche: Stufe 0 (aus) … 10 (stark) → Breite des Gauß-Mittels in Bildschirmpixeln
STROKE_SMOOTHING_PX = [0, 0.5, 0.8, 1.2, 1.7, 2.3, 3.0, 4.0, 5.2, 6.6, 8.5]
STROKE_SMOOTHING_DEFAULT = 3


def stroke_smoothing() -> int:
    try:
        return max(0, min(10, int(_value("input/stroke_smoothing", STROKE_SMOOTHING_DEFAULT))))
    except (TypeError, ValueError):
        return STROKE_SMOOTHING_DEFAULT


def stroke_smoothing_px() -> float:
    return STROKE_SMOOTHING_PX[stroke_smoothing()]


def set_stroke_smoothing(level: int):
    from PySide6.QtCore import QSettings
    QSettings("PDFEditor", "PDFEditor").setValue("input/stroke_smoothing", int(level))
    _cache["input/stroke_smoothing"] = int(level)


def rotate_fit_width() -> bool:
    """Gedrehte Seiten auf die Breite der übrigen Seiten skalieren? (Standard: ja)"""
    return str(_value("pages/rotate_fit_width", "true")).lower() in ("true", "1")


def set_rotate_fit_width(on: bool):
    _set("pages/rotate_fit_width", on)


def _set(key, on):
    from PySide6.QtCore import QSettings
    QSettings("PDFEditor", "PDFEditor").setValue(key, "true" if on else "false")
    _cache[key] = "true" if on else "false"


def set_after_insert_mouse(on: bool):
    from PySide6.QtCore import QSettings
    QSettings("PDFEditor", "PDFEditor").setValue("input/after_insert_mouse", "true" if on else "false")
    _cache["input/after_insert_mouse"] = "true" if on else "false"
