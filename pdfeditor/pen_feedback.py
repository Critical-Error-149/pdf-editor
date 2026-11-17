"""Visuelles Stift-Feedback von Windows für die eigenen Fenster abschalten.

Windows zeichnet beim Antippen mit dem Stift (Punkt, sehr kurzer Strich) eine Wellen-/Kreis-Animation,
beim Halten einen Ring („Rechtsklick“), dazu Rückmeldungen für die Stifttaste. Beim Schreiben stört das
und verdeckt die Stelle, an der man gerade schreibt. Das Programm erkennt langes Halten selbst
(Kontextmenü, Formerkennung) – die Windows-Rückmeldungen werden deshalb nicht gebraucht.
Nur das eigene Fenster ist betroffen, die Windows-Einstellungen bleiben unverändert.
"""
from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QWidget

# FEEDBACK_TYPE (winuser.h)
FEEDBACK_PEN_BARRELVISUALIZATION = 2
FEEDBACK_PEN_TAP = 3
FEEDBACK_PEN_DOUBLETAP = 4
FEEDBACK_PEN_PRESSANDHOLD = 5
FEEDBACK_PEN_RIGHTTAP = 6
FEEDBACK_TOUCH_PRESSANDHOLD = 9   # Finger: langes Antippen erkennt das Programm selbst
FEEDBACK_TOUCH_RIGHTTAP = 10
DISABLED = (FEEDBACK_PEN_BARRELVISUALIZATION, FEEDBACK_PEN_TAP, FEEDBACK_PEN_DOUBLETAP, FEEDBACK_PEN_PRESSANDHOLD,
            FEEDBACK_PEN_RIGHTTAP, FEEDBACK_TOUCH_PRESSANDHOLD, FEEDBACK_TOUCH_RIGHTTAP)

# ältere Schnittstelle (Tablet PC): Fenstereigenschaft mit Schaltern
TABLET_PROPERTY = "MicrosoftTabletPenServiceProperty"
TABLET_DISABLE_PRESSANDHOLD = 0x00000001
TABLET_DISABLE_PENTAPFEEDBACK = 0x00000008
TABLET_DISABLE_PENBARRELFEEDBACK = 0x00000010
TABLET_DISABLE_FLICKS = 0x00010000


def disable(widget: QWidget) -> bool:
    """Feedback für das (oberste) Fenster des Widgets abschalten. Gibt True zurück, wenn es geklappt hat."""
    if sys.platform != "win32":
        return False
    try:
        hwnd = ctypes.c_void_p(int(widget.winId()))
        user32 = ctypes.windll.user32
        off = ctypes.c_int(0)  # BOOL FALSE
        ok = True
        for kind in DISABLED:
            ok &= bool(user32.SetWindowFeedbackSetting(hwnd, kind, 0, ctypes.sizeof(off), ctypes.byref(off)))
        flags = (TABLET_DISABLE_PRESSANDHOLD | TABLET_DISABLE_PENTAPFEEDBACK | TABLET_DISABLE_PENBARRELFEEDBACK
                 | TABLET_DISABLE_FLICKS)
        user32.SetPropW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p)
        user32.SetPropW(hwnd, TABLET_PROPERTY, ctypes.c_void_p(flags))
        return ok
    except Exception:
        return False


class _CursorInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("flags", ctypes.c_uint32), ("hCursor", ctypes.c_void_p),
                ("x", ctypes.c_long), ("y", ctypes.c_long)]


# Qt-Zeigerform -> Windows-Standardzeiger (IDC_*)
_STANDARD = {Qt.ArrowCursor: 32512, Qt.IBeamCursor: 32513, Qt.WaitCursor: 32514, Qt.CrossCursor: 32515,
             Qt.SizeAllCursor: 32646, Qt.PointingHandCursor: 32649}
_std_handles = None


def _standard_handles():
    global _std_handles
    if _std_handles is None:
        load = ctypes.windll.user32.LoadCursorW
        load.restype = ctypes.c_void_p
        load.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        ids = list(_STANDARD.values()) + [32631, 32642, 32643, 32644, 32645, 32648, 32650, 32651]
        _std_handles = {i: load(None, ctypes.c_void_p(i)) for i in ids}
    return _std_handles


def cursor_is_wrong(shape) -> bool:
    """Zeigt Windows gerade einen anderen Zeiger als den, den das Programm gesetzt hat?

    Nach dem Scrollen mit dem Finger blendet Windows den Zeiger für den schwebenden Stift aus bzw. setzt den
    Standardpfeil; ohne Zeiger zeichnet Windows für den Stift ein kleines Fadenkreuz. Qt merkt davon nichts
    und setzt den (aus seiner Sicht unveränderten) Zeiger nicht neu – erst wenn der Stift den Bildschirm
    verlässt und zurückkommt."""
    if sys.platform != "win32" or shape == Qt.BlankCursor:
        return False
    try:
        info = _CursorInfo()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetCursorInfo(ctypes.byref(info)):
            return False
        if info.flags & 2:  # CURSOR_SUPPRESSED: Windows blendet ihn bei Fingereingabe absichtlich aus
            return False
        if not info.flags & 1 or not info.hCursor:
            return True  # kein Zeiger sichtbar
        handles = _standard_handles()
        if shape in _STANDARD:
            return info.hCursor != handles[_STANDARD[shape]]
        return info.hCursor in handles.values()  # eigener Zeiger erwartet, aber ein Standardzeiger sichtbar
    except Exception:
        return False


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]


class _IconInfo(ctypes.Structure):
    _fields_ = [("fIcon", ctypes.c_int), ("xHotspot", ctypes.c_uint32), ("yHotspot", ctypes.c_uint32),
                ("hbmMask", ctypes.c_void_p), ("hbmColor", ctypes.c_void_p)]


_native = {}  # Bild des Zeigers (cacheKey) -> Windows-Zeiger (HCURSOR)


def _make_hcursor(pixmap, hot_x, hot_y):
    """Windows-Zeiger aus einem Bild (32 Bit mit Transparenz)."""
    img = pixmap.toImage().convertToFormat(QImage.Format_ARGB32_Premultiplied)
    w, h = img.width(), img.height()
    gdi32, user32 = ctypes.windll.gdi32, ctypes.windll.user32
    gdi32.CreateDIBSection.restype = ctypes.c_void_p
    gdi32.CreateDIBSection.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
                                       ctypes.c_void_p, ctypes.c_uint32)
    gdi32.CreateBitmap.restype = ctypes.c_void_p
    gdi32.DeleteObject.argtypes = (ctypes.c_void_p,)
    user32.CreateIconIndirect.restype = ctypes.c_void_p
    bmi = _BitmapInfoHeader(ctypes.sizeof(_BitmapInfoHeader), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)  # von oben nach unten
    bits = ctypes.c_void_p()
    color = gdi32.CreateDIBSection(None, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    if not color:
        return None
    row = w * 4
    src = img.constBits()
    data = bytes(src)[: img.bytesPerLine() * h]
    for y in range(h):  # Zeilen können aufgefüllt sein
        ctypes.memmove(bits.value + y * row, data[y * img.bytesPerLine():y * img.bytesPerLine() + row], row)
    mask = gdi32.CreateBitmap(w, h, 1, 1, None)
    info = _IconInfo(0, int(hot_x), int(hot_y), mask, color)
    cursor = user32.CreateIconIndirect(ctypes.byref(info))
    gdi32.DeleteObject(color)
    gdi32.DeleteObject(mask)
    return cursor


def native_cursor(cursor):
    """Windows-Zeiger (HCURSOR) zu einem Qt-Zeiger: Standardzeiger oder aus dem Bild erzeugt (zwischengespeichert)."""
    shape = cursor.shape()
    if shape in _STANDARD:
        return _standard_handles()[_STANDARD[shape]]
    if shape != Qt.BitmapCursor:
        return None
    pm = cursor.pixmap()
    if pm.isNull():
        return None
    key = pm.cacheKey()
    handle = _native.get(key)
    if handle is None:
        if len(_native) > 16:  # alte Zeiger (andere Strichdicke/Farbe/Zoomstufe) freigeben
            for h in _native.values():
                ctypes.windll.user32.DestroyCursor(ctypes.c_void_p(h))
            _native.clear()
        dpr = pm.devicePixelRatio()
        hot = cursor.hotSpot()
        handle = _make_hcursor(pm, hot.x() * dpr, hot.y() * dpr)
        if handle:
            _native[key] = handle
    return handle


def force_cursor(cursor) -> bool:
    """Zeiger direkt bei Windows setzen (Qts eigener Aufruf kommt nach einer Fingerberührung nicht an)."""
    if sys.platform != "win32":
        return False
    try:
        handle = native_cursor(cursor)
        if not handle:
            return False
        ctypes.windll.user32.SetCursor.argtypes = (ctypes.c_void_p,)
        ctypes.windll.user32.SetCursor(handle)
        return True
    except Exception:
        return False


def is_disabled(widget: QWidget, kind=FEEDBACK_PEN_TAP) -> bool:
    """Prüfen (für Tests): ist die Rückmeldung für dieses Fenster aus?"""
    if sys.platform != "win32":
        return False
    try:
        hwnd = ctypes.c_void_p(int(widget.winId()))
        value = ctypes.c_int(1)
        size = ctypes.c_uint32(ctypes.sizeof(value))
        if not ctypes.windll.user32.GetWindowFeedbackSetting(hwnd, kind, 1, ctypes.byref(size), ctypes.byref(value)):
            return False  # GWFS_INCLUDE_ANCESTORS = 1
        return value.value == 0
    except Exception:
        return False


class _Filter(QObject):
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Show and isinstance(obj, QWidget) and obj.isWindow():
            disable(obj)
        return False


def install(app: QApplication):
    """Für alle Fenster des Programms (auch später geöffnete) abschalten; mehrfacher Aufruf ist unschädlich."""
    if getattr(app, "_pen_feedback", None) is not None:
        return
    app._pen_feedback = _Filter(app)
    app.installEventFilter(app._pen_feedback)
    for w in app.topLevelWidgets():
        if w.isVisible():
            disable(w)
