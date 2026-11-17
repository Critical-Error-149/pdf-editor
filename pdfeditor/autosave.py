"""Automatisches Speichern: global einstellbar und pro Datei überschreibbar."""
from __future__ import annotations

import hashlib
import os

from PySide6.QtCore import QObject, QTime, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel,
                               QVBoxLayout)

INTERVALS = [(10, "10 Sekunden"), (30, "30 Sekunden"), (60, "1 Minute"), (120, "2 Minuten"),
             (300, "5 Minuten"), (600, "10 Minuten")]
DEFAULT_INTERVAL = 30
FILE_MODES = {"default": "Wie global", "on": "Immer an", "off": "Aus"}
RETRY_MS = 3000  # wenn gerade geschrieben/getippt wird, kurz später erneut versuchen


def _file_key(path):
    norm = os.path.normcase(os.path.abspath(path))
    return "autosave_file/" + hashlib.sha1(norm.encode("utf-8")).hexdigest()


class Autosave(QObject):
    """Speichert spätestens 'interval' Sekunden nach der ersten ungespeicherten Änderung."""

    status_changed = Signal(str)

    def __init__(self, qsettings, document, can_save, do_save, parent=None):
        """can_save(): False, solange gerade geschrieben/getippt wird.
        do_save(): speichert ohne Rückfrage, gibt True bei Erfolg zurück."""
        super().__init__(parent)
        self.qs = qsettings
        self.document = document
        self.can_save = can_save
        self.do_save = do_save
        self.last_saved = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._fire)
        document.modified_changed.connect(self._on_modified)
        document.changed.connect(lambda _: self._on_modified(document.is_modified()))
        document.page_changed.connect(lambda _: self._on_modified(document.is_modified()))

    # ------------------------------------------------------------ Einstellungen
    @property
    def global_enabled(self):
        return self.qs.value("autosave/enabled", "true") in (True, "true")

    @global_enabled.setter
    def global_enabled(self, value):
        self.qs.setValue("autosave/enabled", "true" if value else "false")
        self.refresh()

    @property
    def interval(self):
        return int(self.qs.value("autosave/interval", DEFAULT_INTERVAL))

    @interval.setter
    def interval(self, seconds):
        self.qs.setValue("autosave/interval", int(seconds))
        self.refresh()

    def file_mode(self, path=None):
        path = path or self.document.path
        if not path:
            return "default"
        return self.qs.value(_file_key(path), "default")

    def set_file_mode(self, mode, path=None):
        path = path or self.document.path
        if not path:
            return
        if mode == "default":
            self.qs.remove(_file_key(path))
        else:
            self.qs.setValue(_file_key(path), mode)
        self.refresh()

    def savable(self):
        """Kann die aktuelle Datei überhaupt automatisch gespeichert werden?"""
        p = self.document.path
        return bool(p) and p.lower().endswith(".pdf")

    def enabled(self):
        """Gilt Autosave für das aktuelle Dokument?"""
        if not self.savable():
            return False
        mode = self.file_mode()
        if mode == "on":
            return True
        if mode == "off":
            return False
        return self.global_enabled

    # ------------------------------------------------------------ Ablauf
    def refresh(self):
        self._on_modified(self.document.is_modified())
        self.status_changed.emit(self.status_text())

    def _on_modified(self, modified):
        if modified and self.enabled():
            if not self.timer.isActive():
                self.timer.start(self.interval * 1000)
        else:
            self.timer.stop()
        self.status_changed.emit(self.status_text())

    def _fire(self):
        if not (self.enabled() and self.document.is_modified()):
            return
        if not self.can_save():
            self.timer.start(RETRY_MS)
            return
        if self.do_save():
            self.last_saved = QTime.currentTime()
        else:
            self.timer.start(self.interval * 1000)  # später nochmal versuchen
        self.status_changed.emit(self.status_text())

    def save_now_if_enabled(self):
        """Beim Schließen/Wechseln: ohne Rückfrage speichern, wenn Autosave gilt."""
        if self.enabled() and self.document.is_modified():
            if self.do_save():
                self.last_saved = QTime.currentTime()
                return True
        return False

    def status_text(self):
        if not self.document.path:
            return "Autosave: erst nach dem ersten Speichern"
        if not self.savable():
            return "Autosave: nur für PDF-Dateien"
        if not self.enabled():
            return "Autosave: aus"
        txt = "Autosave: an"
        if self.file_mode() == "on":
            txt += " (für diese Datei)"
        if self.last_saved is not None:
            txt += f" · gespeichert {self.last_saved.toString('HH:mm')}"
        elif self.document.is_modified():
            txt += " · Änderungen ausstehend"
        return txt


class AutosaveDialog(QDialog):
    """Globale Einstellungen + Einstellung für die aktuelle Datei."""

    def __init__(self, autosave: Autosave, parent=None):
        super().__init__(parent)
        self.autosave = autosave
        self.setWindowTitle("Automatisch speichern")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("<b>Für alle Dateien</b>"))
        form = QFormLayout()
        self.enabled = QCheckBox("Automatisch speichern")
        self.enabled.setChecked(autosave.global_enabled)
        form.addRow(self.enabled)
        self.interval = QComboBox()
        for sec, label in INTERVALS:
            self.interval.addItem(f"spätestens {label} nach einer Änderung", sec)
        idx = next((i for i, (sec, _) in enumerate(INTERVALS) if sec == autosave.interval), 1)
        self.interval.setCurrentIndex(idx)
        form.addRow("Zeitpunkt:", self.interval)
        lay.addLayout(form)

        lay.addSpacing(8)
        lay.addWidget(QLabel("<b>Nur für diese Datei</b>"))
        self.file_mode = QComboBox()
        for key, label in FILE_MODES.items():
            self.file_mode.addItem(label, key)
        self.file_mode.setCurrentIndex(list(FILE_MODES).index(autosave.file_mode()))
        if not autosave.savable():
            self.file_mode.setEnabled(False)
            hint = QLabel("Die aktuelle Datei muss zuerst als PDF gespeichert werden.")
            hp = hint.palette()
            hp.setColor(hp.ColorRole.WindowText, hp.color(hp.ColorRole.PlaceholderText))
            hint.setPalette(hp)
            lay.addWidget(hint)
        f2 = QFormLayout()
        f2.addRow("Diese Datei:", self.file_mode)
        lay.addLayout(f2)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def apply(self):
        self.autosave.qs.setValue("autosave/enabled", "true" if self.enabled.isChecked() else "false")
        self.autosave.qs.setValue("autosave/interval", int(self.interval.currentData()))
        if self.autosave.savable():
            self.autosave.set_file_mode(self.file_mode.currentData())
        self.autosave.refresh()
