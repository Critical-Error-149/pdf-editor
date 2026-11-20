"""Einstellungen (Datei → Einstellungen …): Darstellung, Bildseiten, automatisches Speichern."""
from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QMessageBox, QPushButton, QSlider, QSpinBox, QTabWidget,
                               QVBoxLayout, QWidget)

from . import __version__, gestures, passwords, prefs, touch_scroll, updates
from .document import PAGE_TEMPLATES
from .image_page import BORDERS, FITS


RIBBON_TABS = ["Start", "Zeichnen", "Einfügen", "Seiten", "Ansicht"]
START_PANELS = {"last": "wie zuletzt", "none": "keine", "files": "Dateien", "pages": "Seiten",
                "both": "Dateien und Seiten"}


def start_panels(qs):
    """(Dateileiste sichtbar, Seitenleiste sichtbar) beim Programmstart."""
    mode = str(qs.value("start/sidebar", "last"))
    if mode in START_PANELS and mode != "last":
        return mode in ("files", "both"), mode in ("pages", "both")
    return (qs.value("files_visible", "true") in (True, "true"),
            qs.value("thumbs_visible", "true") in (True, "true"))


def auto_update_enabled(qs):
    """Beim Programmstart automatisch nach Updates suchen? (Standard: ja)"""
    return str(qs.value("updates/auto", "true")).lower() in ("true", "1")


def open_last_enabled(qs):
    """Beim Programmstart die zuletzt geöffnete PDF öffnen? (Standard: ja)"""
    return str(qs.value("start/open_last", "true")).lower() in ("true", "1")


def scan_batch_enabled(qs):
    return qs.value("scan_batch", "true") in (True, "true")


class SettingsDialog(QDialog):
    def __init__(self, qs, autosave, gpu_active, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Einstellungen")
        self.qs = qs
        self.autosave = autosave
        lay = QVBoxLayout(self)
        # Registerkarten statt einer langen Liste: alle Bereiche untereinander waren höher als der Bildschirm
        # (Surface: 1032 px Dialog bei 912 px Platz über der Taskleiste)
        self.tabs = QTabWidget()
        pages = {}
        for name in ("Darstellung", "Bedienung", "Dokumente", "Updates & Feedback"):
            page = QWidget()
            pages[name] = QVBoxLayout(page)
            self.tabs.addTab(page, name.replace("&", "&&"))  # „&“ sonst als Tastenkürzel-Markierung gelesen
        lay.addWidget(self.tabs)

        # ---- Darstellung
        box = QGroupBox("Darstellung")
        form = QVBoxLayout(box)
        self.gpu = QCheckBox("Grafikkarte zum Zeichnen der Seiten verwenden (Direct3D, flüssigeres Scrollen)")
        self.gpu.setChecked(str(qs.value("renderer", "auto")) != "raster")
        form.addWidget(self.gpu)
        note = QLabel("Wirkt nach einem Neustart des Programms. Aktuell: "
                      + ("Grafikkarte" if gpu_active else "normale Darstellung") + ".")
        note.setWordWrap(True)
        note.setEnabled(False)
        form.addWidget(note)
        self.scan = QCheckBox("Scanseiten am Stück rendern (eingescannte Seiten und große Bilder werden "
                              "deutlich schneller scharf)")
        self.scan.setChecked(scan_batch_enabled(qs))
        form.addWidget(self.scan)
        pages["Darstellung"].addWidget(box)

        # ---- Bildseiten
        box = QGroupBox("Bild als Seite einfügen – Voreinstellung")
        f = QFormLayout(box)
        self.fit = QComboBox()
        for k, v in FITS.items():
            self.fit.addItem(v, k)
        self.fit.setCurrentIndex(max(0, self.fit.findData(str(qs.value("image_page/fit", "contain")))))
        f.addRow("Bild:", self.fit)
        self.border = QComboBox()
        for k, v in BORDERS.items():
            self.border.addItem(v, k)
        self.border.setCurrentIndex(max(0, self.border.findData(str(qs.value("image_page/border", "white")))))
        f.addRow("Rand:", self.border)
        self.template = QComboBox()
        self.template.addItems([t for t in PAGE_TEMPLATES if t != "Leer"])
        self.template.setCurrentText(str(qs.value("image_page/template", "Kariert")))
        f.addRow("Vorlage für den Rand:", self.template)
        pages["Dokumente"].addWidget(box)

        # ---- Bedienung
        box = QGroupBox("Bedienung")
        f = QFormLayout(box)
        self.long_press = QSpinBox()
        self.long_press.setRange(200, 1500)
        self.long_press.setSingleStep(50)
        self.long_press.setSuffix(" ms")
        self.long_press.setValue(touch_scroll.long_press_ms())
        self.long_press.setToolTip("So lange Finger oder Stift halten, bis das Kontextmenü erscheint bzw. ein Objekt "
                                   "gegriffen wird")
        f.addRow("Langes Drücken nach:", self.long_press)
        self.snap = QSpinBox()
        self.snap.setRange(0, 30)
        self.snap.setSuffix(" °")
        self.snap.setSpecialValueText("nie")
        self.snap.setValue(round(gestures.snap_degrees()))
        self.snap.setToolTip("Gerade Linie (Strich zeichnen und am Ende halten): Liegt sie so nah an waagerecht bzw. "
                             "senkrecht, wird sie exakt waagerecht bzw. senkrecht")
        f.addRow("Linie rastet waagerecht/senkrecht ein bis:", self.snap)
        row = QHBoxLayout()
        self.smoothing = QSlider(Qt.Horizontal)
        self.smoothing.setRange(0, 10)
        self.smoothing.setPageStep(1)
        self.smoothing.setTickPosition(QSlider.TicksBelow)
        self.smoothing.setValue(prefs.stroke_smoothing())
        self.smoothing.setToolTip("Wie stark Stift- und Markerstriche nach dem Absetzen geglättet werden. "
                                  "Aus: genau wie geschrieben. Stark: runde, ruhige Linien (kleine Details und "
                                  "spitze Ecken werden weicher). Wirkt bei jedem Zoom und Schreibtempo gleich.")
        smoothing_label = QLabel()
        smoothing_label.setMinimumWidth(smoothing_label.fontMetrics().horizontalAdvance("10 (Standard)"))

        def _label(v):
            smoothing_label.setText("aus" if v == 0 else f"{v}" + (" (Standard)" if v == prefs.STROKE_SMOOTHING_DEFAULT
                                                                    else ""))
        self.smoothing.valueChanged.connect(_label)
        _label(self.smoothing.value())
        row.addWidget(self.smoothing, 1)
        row.addWidget(smoothing_label)
        f.addRow("Glättung der Stiftstriche:", row)
        self.after_insert = QCheckBox("Nach dem Einfügen (Text, Notiz, Bild, Form, Video, Aufnahme) automatisch "
                                      "zum Maus-Werkzeug wechseln")
        self.after_insert.setToolTip("Aus: Eingefügtes wird ausgewählt (zum Verschieben/Vergrößern) bzw. das "
                                     "Werkzeug bleibt für weitere Einfügungen aktiv")
        self.after_insert.setChecked(prefs.after_insert_mouse())
        f.addRow(self.after_insert)
        self.pen_auto = QCheckBox("Mit dem Stift schreiben schaltet vom Maus-Werkzeug automatisch zum Stift")
        self.pen_auto.setToolTip("Im Maus-Werkzeug: Stift über die Seite ziehen wechselt zum zuletzt benutzten Stift "
                                 "bzw. Marker und zeichnet sofort. Tippen bleibt ein Klick (z. B. auf einen Link), "
                                 "Halten ein Rechtsklick.\nAus: Der Stift verschiebt im Maus-Werkzeug die Seite.")
        self.pen_auto.setChecked(prefs.pen_auto_draw())
        f.addRow(self.pen_auto)
        self.note_close = QCheckBox("Geöffnete Notiz schließt sich beim Tippen/Klicken daneben")
        self.note_close.setToolTip("Aus: Die Notiz bleibt offen, bis „Schließen“ gedrückt wird. "
                                   "Scrollen und Zoomen schließen sie nie.")
        self.note_close.setChecked(prefs.note_close_outside())
        f.addRow(self.note_close)
        pages["Bedienung"].addWidget(box)

        # ---- Beim Start
        box = QGroupBox("Beim Programmstart")
        f = QFormLayout(box)
        self.start_tab = QComboBox()
        self.start_tab.addItem("wie zuletzt", "last")
        for name in RIBBON_TABS:
            self.start_tab.addItem(name, name)
        self.start_tab.setCurrentIndex(max(0, self.start_tab.findData(str(qs.value("start/ribbon_tab", "last")))))
        f.addRow("Geöffneter Menütab:", self.start_tab)
        self.start_panel = QComboBox()
        for k, v in START_PANELS.items():
            self.start_panel.addItem(v, k)
        self.start_panel.setCurrentIndex(max(0, self.start_panel.findData(str(qs.value("start/sidebar", "last")))))
        f.addRow("Geöffnete Seitenleiste:", self.start_panel)
        self.open_last = QCheckBox("Zuletzt geöffnete PDF öffnen (statt einer leeren Seite)")
        self.open_last.setChecked(open_last_enabled(qs))
        f.addRow(self.open_last)
        pages["Darstellung"].addWidget(box)

        # ---- Gemerkte PDF-Passwörter
        if passwords.available():
            box = QGroupBox("Gemerkte PDF-Passwörter")
            row = QHBoxLayout(box)
            self.pw_label = QLabel()
            row.addWidget(self.pw_label, 1)
            clear = QPushButton("Alle löschen")
            clear.clicked.connect(self._forget_passwords)
            row.addWidget(clear)
            self._update_pw_label()
            pages["Dokumente"].addWidget(box)

        # ---- Updates (über den Download-Gist: ohne Anmeldung, auch bei anderen)
        box = QGroupBox("Updates")
        row = QHBoxLayout()
        self.update_label = QLabel(f"Installierte Version: {__version__}")
        self.update_label.setWordWrap(True)
        row.addWidget(self.update_label, 1)
        self.update_btn = QPushButton("Nach Updates suchen")
        self.update_btn.setToolTip("Neueste Version auf der Download-Seite (GitHub-Gist) prüfen; "
                                   "heruntergeladen wird im Browser")
        self.update_btn.clicked.connect(self._check_updates)
        row.addWidget(self.update_btn)
        outer = QVBoxLayout()
        outer.addLayout(row)
        self.auto_update = QCheckBox("Beim Programmstart automatisch nach Updates suchen")
        self.auto_update.setChecked(auto_update_enabled(qs))
        outer.addWidget(self.auto_update)
        box.setLayout(outer)
        pages["Updates & Feedback"].addWidget(box)

        # ---- Feedback (verschlüsselt an den Entwickler, siehe feedback.py)
        box = QGroupBox("Feedback")
        row = QHBoxLayout(box)
        label = QLabel("Fehler melden oder Wünsche äußern – verschlüsselt, nur der Entwickler kann es lesen.")
        label.setWordWrap(True)
        row.addWidget(label, 1)
        self.feedback_btn = QPushButton("Feedback senden …")
        self.feedback_btn.clicked.connect(self._feedback)
        self.feedback_btn.setEnabled(hasattr(parent, "send_feedback"))
        row.addWidget(self.feedback_btn)
        pages["Updates & Feedback"].addWidget(box)

        # ---- Automatisch speichern
        box = QGroupBox("Automatisch speichern")
        f = QFormLayout(box)
        self.auto = QCheckBox("Für alle Dateien (einzelne Dateien lassen sich abweichend einstellen)")
        self.auto.setChecked(autosave.global_enabled)
        f.addRow(self.auto)
        self.interval = QSpinBox()
        self.interval.setRange(5, 3600)
        self.interval.setSuffix(" s")
        self.interval.setValue(autosave.interval)
        f.addRow("Spätestens nach:", self.interval)
        pages["Dokumente"].addWidget(box)

        for page in pages.values():
            page.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        # „OK“ bleibt Standard-Knopf (sonst übernimmt ihn z. B. „Nach Updates suchen“ nach dem Klick, „OK“ wirkt grau)
        for b in self.findChildren(QPushButton):
            b.setAutoDefault(False)
        buttons.button(QDialogButtonBox.Ok).setDefault(True)

    def _update_pw_label(self):
        n = passwords.count()
        self.pw_label.setText(f"{n} Dokument{'e' if n != 1 else ''} – verschlüsselt, nur für dein Windows-Konto"
                              if n else "Keine gespeichert.")

    def _forget_passwords(self):
        passwords.forget_all()
        self._update_pw_label()

    def _feedback(self):
        # NICHT ausblenden: hide() beendet die exec()-Schleife dieses Dialogs – danach blieb das Hauptfenster gesperrt,
        # ohne dass ein Dialog zu sehen war. Das Fensterbild rendert das Hauptfenster selbst (ohne diesen Dialog).
        self.parent().send_feedback()

    def _check_updates(self):
        self.update_btn.setEnabled(False)
        self.update_label.setText(f"Installierte Version: {__version__} – suche …")
        self._update_check = updates.UpdateCheck(__version__, self)
        self._update_check.finished.connect(self._update_result)
        self._update_check.start()

    def _update_result(self, result, latest, info):
        self.update_btn.setEnabled(True)
        texts = {"newer": f"Version {latest} ist verfügbar (installiert: {__version__}).",
                 "current": f"Du hast die neueste Version ({__version__}).",
                 "none": "Auf der Download-Seite ist noch keine Version eingetragen.",
                 "error": f"Prüfen nicht möglich: {info}"}
        self.update_label.setText(texts[result])
        if result == "current":
            return
        if result == "newer" and hasattr(self.parent(), "show_update_dialog"):
            # Änderungen anzeigen und „Jetzt aktualisieren“ anbieten
            self.parent().show_update_dialog(latest, self._update_check.data, modal=True)
            return
        url = info if result in ("newer", "none") else updates.DOWNLOAD_PAGE
        ask = {"newer": "Die Download-Seite im Browser öffnen? Dort das ZIP herunterladen und den alten Ordner "
                        "„PDF Editor“ durch den neuen ersetzen (Einstellungen bleiben erhalten).",
               "none": "Die Download-Seite trotzdem im Browser öffnen?",
               "error": "Die Download-Seite im Browser öffnen und dort selbst nachsehen?"}[result]
        if QMessageBox.question(self, "Updates", f"{texts[result]}\n\n{ask}") == QMessageBox.Yes:
            QDesktopServices.openUrl(QUrl(url))

    def apply(self):
        qs = self.qs
        qs.setValue("renderer", "auto" if self.gpu.isChecked() else "raster")
        qs.setValue("scan_batch", "true" if self.scan.isChecked() else "false")
        qs.setValue("input/long_press_ms", self.long_press.value())
        touch_scroll.set_long_press_ms(self.long_press.value())
        qs.setValue("input/snap_degrees", self.snap.value())
        gestures.set_snap_degrees(self.snap.value())
        prefs.set_stroke_smoothing(self.smoothing.value())
        prefs.set_after_insert_mouse(self.after_insert.isChecked())
        prefs.set_pen_auto_draw(self.pen_auto.isChecked())
        prefs.set_note_close_outside(self.note_close.isChecked())
        qs.setValue("start/ribbon_tab", self.start_tab.currentData())
        qs.setValue("start/sidebar", self.start_panel.currentData())
        qs.setValue("start/open_last", "true" if self.open_last.isChecked() else "false")
        qs.setValue("updates/auto", "true" if self.auto_update.isChecked() else "false")
        qs.setValue("image_page/fit", self.fit.currentData())
        qs.setValue("image_page/border", self.border.currentData())
        qs.setValue("image_page/template", self.template.currentText())
        self.autosave.global_enabled = self.auto.isChecked()
        self.autosave.interval = self.interval.value()
