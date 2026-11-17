"""Kontextmenüs mit dem Finger bedienen.

Qt bedient Menüs mit dem Finger über Ersatz-Mausereignisse. Unter Windows schließt Qt ein Menü aber beim
Loslassen des Fingers, ohne den Eintrag auszulösen: Das Loslassen landet beim Hauptfenster und gilt als Klick
neben das Menü. Darum wird nur dieses Loslassen abgefangen, wenn der Finger auf einem Menü liegt; alles andere
verarbeitet Qt wie gewohnt. Hat Qt den Eintrag kurz danach nicht selbst ausgelöst (erkannt am Signal des
Eintrags), wird er hier ausgelöst bzw. ein Untermenü geöffnet. Ein Tipp neben das Menü schließt es wie bisher.

Diagnose: Umgebungsvariable PDFEDITOR_TOUCHLOG=1 schreibt alle Berührungen bei offenem Menü in
%LOCALAPPDATA%\\PDF Editor\\touch.log.
"""
from __future__ import annotations

import os
import time

from PySide6.QtCore import QEvent, QObject, QPoint, QTimer
from PySide6.QtWidgets import QApplication, QMenu

_TOUCH = (QEvent.TouchBegin, QEvent.TouchUpdate, QEvent.TouchEnd, QEvent.TouchCancel)
_MOUSE = (QEvent.MouseButtonPress, QEvent.MouseButtonRelease)
FALLBACK_MS = 150  # so lange hat Qts eigene Verarbeitung Zeit, bevor nachgeholfen wird
_LOG = None


def _log(text):
    global _LOG
    if _LOG is None:
        if not os.environ.get("PDFEDITOR_TOUCHLOG"):
            _LOG = False
        else:
            base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "PDF Editor")
            os.makedirs(base, exist_ok=True)
            _LOG = open(os.path.join(base, "touch.log"), "a", encoding="utf-8")
            _LOG.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    if _LOG:
        _LOG.write(f"{time.perf_counter():.3f} {text}\n")
        _LOG.flush()


def _open_menus():
    """Sichtbare Menüs, das zuletzt geöffnete (z. B. Untermenü) zuerst."""
    menus = [w for w in QApplication.topLevelWidgets() if isinstance(w, QMenu) and w.isVisible()]
    top = QApplication.activePopupWidget()
    menus.sort(key=lambda m: m is not top)
    return menus


def _menu_at(gp):
    menu = next((m for m in _open_menus() if m.geometry().contains(gp)), None)
    return menu, (menu.actionAt(menu.mapFromGlobal(gp)) if menu is not None else None)


def _name(act):
    return act.text().replace("&", "") if act is not None else "-"


class _MenuTouch(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._tracking = False   # Berührung hat bei offenem Menü begonnen
        self._last = None        # letzte Fingerposition (global)
        self._pending = None     # (Menü, Eintrag, ausgelöst?) nach dem Loslassen

    def eventFilter(self, obj, event):
        t = event.type()
        if t in _MOUSE:
            if self._tracking or self._pending is not None:
                _log(f"{t.name} -> {type(obj).__name__} Quelle={event.source().name} "
                     f"Menü offen={isinstance(QApplication.activePopupWidget(), QMenu)}")
            return False
        if t not in _TOUCH:
            return False
        points = event.points()
        popup = QApplication.activePopupWidget()
        if t == QEvent.TouchBegin:
            # nur Berührungen, die bei offenem Menü beginnen: Der Finger, der ein Menü gerade erst geöffnet hat
            # (z. B. „Datei“ öffnet beim Antippen), darf es beim Loslassen nicht wieder schließen
            was = self._tracking
            self._tracking = isinstance(popup, QMenu) and bool(points)
            if self._tracking:
                self._last = points[0].globalPosition().toPoint()
                if not was:
                    m, a = _menu_at(self._last)
                    _log(f"Finger an -> {type(obj).__name__} über {type(m).__name__}: {_name(a)}")
            return False
        if not self._tracking:
            return False
        if points:
            self._last = points[0].globalPosition().toPoint()
        if t == QEvent.TouchCancel:
            self._tracking = False
            _log("abgebrochen")
            return False
        if t != QEvent.TouchEnd:
            return False
        menu, act = _menu_at(QPoint(self._last))
        if self._pending is None or self._pending[0] is not menu:  # nur einmal je Loslassen (Fenster + Widget)
            _log(f"Finger ab -> {type(obj).__name__} über {type(menu).__name__}: {_name(act)}")
            if menu is None:
                self._tracking = False
                return False  # daneben getippt: Qt schließt das Menü wie bei einem Mausklick außerhalb
            pending = [menu, act, False, None]
            if act is not None:
                def mark(*_a, p=pending):
                    p[2] = True
                pending[3] = mark
                act.triggered.connect(mark)
            self._pending = pending
            QTimer.singleShot(FALLBACK_MS, self._fallback)
        event.accept()
        return True  # nicht als Klick neben das Menü werten

    def _fallback(self):
        pending, self._pending = self._pending, None
        self._tracking = False
        if pending is None:
            return
        menu, act, triggered, mark = pending
        if act is not None and mark is not None:
            try:
                act.triggered.disconnect(mark)  # nur die eigene Verbindung lösen
            except (RuntimeError, TypeError):
                pass
        if triggered or act is None or act.isSeparator() or not act.isEnabled():
            _log(f"Qt hat ausgelöst: {triggered}")
            return
        sub = act.menu()
        if sub is not None:
            if not sub.isVisible() and menu.isVisible():  # Untermenü neben dem Eintrag öffnen
                menu.setActiveAction(act)
                r = menu.actionGeometry(act)
                sub.popup(menu.mapToGlobal(QPoint(r.right(), r.top())))
            _log("Untermenü geöffnet")
            return
        for m in _open_menus():  # erst alle Menüs schließen, dann auslösen (die Aktion kann Dialoge öffnen)
            m.close()
        _log(f"selbst ausgelöst: {_name(act)}")
        act.trigger()


def install(app: QApplication):
    """Für alle Menüs des Programms; mehrfacher Aufruf ist unschädlich."""
    if getattr(app, "_menu_touch", None) is not None:
        return
    app._menu_touch = _MenuTouch(app)
    app.installEventFilter(app._menu_touch)
