"""Dialog „Feedback senden“: Nachricht, optional Name, Systemdaten, Fehlerprotokoll und Bilder (siehe feedback.py)."""
from __future__ import annotations

import json
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QVBoxLayout)

from . import feedback

THUMB = 96


class FeedbackDialog(QDialog):
    def __init__(self, window, screenshot: QImage | None):
        super().__init__(window)
        self.window_ = window
        self.setWindowTitle("Feedback senden")
        self._screenshot = screenshot
        self._files = []  # (Name, QImage) zusätzlich angehängter Bilder
        lay = QVBoxLayout(self)
        info = QLabel("Fehler gefunden, Idee oder Wunsch? Deine Nachricht wird <b>verschlüsselt</b> an den Entwickler "
                      "gesendet – nur er kann sie lesen, niemand sonst.")
        info.setWordWrap(True)
        lay.addWidget(info)
        self.message = QPlainTextEdit()
        self.message.setPlaceholderText("Was ist dir aufgefallen? Was wünschst du dir? "
                                        "(Bei Fehlern: Was hast du gemacht, was ist passiert?)")
        self.message.setMinimumSize(520, 160)
        self.message.textChanged.connect(self._update_send)
        lay.addWidget(self.message, 1)
        row = QHBoxLayout()
        row.addWidget(QLabel("Name (freiwillig):"))
        self.name = QLineEdit()
        self.name.setPlaceholderText("leer lassen = anonym")
        row.addWidget(self.name, 1)
        lay.addLayout(row)

        self.system = QCheckBox("Systeminformationen mitsenden (Windows-, Programm- und Bibliotheksversionen, "
                                "Bildschirm – keine persönlichen Daten)")
        self.system.setChecked(True)
        lay.addWidget(self.system)
        self.log = QCheckBox("Fehlerprotokoll mitsenden (letzte Einträge, Benutzername in Pfaden unkenntlich)")
        self.log.setEnabled(bool(feedback.error_log()))
        lay.addWidget(self.log)
        shot_row = QHBoxLayout()
        self.shot = QCheckBox("Bild des Programmfensters anhängen")
        self.shot.setToolTip("Achtung: Darauf ist zu sehen, was gerade geöffnet ist")
        self.shot.setEnabled(screenshot is not None)
        shot_row.addWidget(self.shot)
        self.shot_thumb = QLabel()
        if screenshot is not None:
            self.shot_thumb.setPixmap(QPixmap.fromImage(screenshot.scaled(
                THUMB * 2, THUMB, Qt.KeepAspectRatio, Qt.SmoothTransformation)))
        shot_row.addWidget(self.shot_thumb)
        shot_row.addStretch(1)
        lay.addLayout(shot_row)
        img_row = QHBoxLayout()
        add = QPushButton("Bild hinzufügen …")
        add.clicked.connect(self._add_image)
        img_row.addWidget(add)
        self.remove = QPushButton("Entfernen")
        self.remove.clicked.connect(self._remove_image)
        img_row.addWidget(self.remove)
        img_row.addStretch(1)
        lay.addLayout(img_row)
        self.images = QListWidget()
        self.images.setMaximumHeight(70)
        self.images.setVisible(False)
        lay.addWidget(self.images)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        lay.addWidget(self.progress)
        buttons = QDialogButtonBox()
        self.preview_btn = buttons.addButton("Vorschau: Was wird gesendet?", QDialogButtonBox.HelpRole)
        self.preview_btn.clicked.connect(self._preview)
        self.send_btn = buttons.addButton("Senden", QDialogButtonBox.AcceptRole)
        self.send_btn.clicked.connect(self._send)
        buttons.addButton("Abbrechen", QDialogButtonBox.RejectRole).clicked.connect(self.reject)
        lay.addWidget(buttons)
        self._update_send()
        self._sender = None

    # ------------------------------------------------------------ Bilder
    def _add_image(self):
        if len(self._files) >= feedback.MAX_IMAGES:
            QMessageBox.information(self, "Feedback", f"Höchstens {feedback.MAX_IMAGES} Bilder.")
            return
        path, _ = QFileDialog.getOpenFileName(self, "Bild anhängen", "", "Bilder (*.png *.jpg *.jpeg *.bmp *.gif)")
        if not path:
            return
        img = QImage(path)
        if img.isNull():
            QMessageBox.warning(self, "Feedback", "Das Bild konnte nicht gelesen werden.")
            return
        name = os.path.basename(path)
        self._files.append((name, img))
        item = QListWidgetItem(QPixmap.fromImage(img.scaled(48, 48, Qt.KeepAspectRatio, Qt.SmoothTransformation)),
                               name)
        self.images.addItem(item)
        self.images.setVisible(True)

    def _remove_image(self):
        row = self.images.currentRow()
        if row < 0 and self.images.count():
            row = self.images.count() - 1
        if row >= 0:
            self.images.takeItem(row)
            del self._files[row]
            self.images.setVisible(bool(self._files))

    # ------------------------------------------------------------ Inhalt
    def _update_send(self):
        self.send_btn.setEnabled(bool(self.message.toPlainText().strip()) and self._sender is None)

    def payload(self):
        images = []
        if self.shot.isChecked() and self._screenshot is not None:
            images.append(feedback.image_jpeg(self._screenshot, "programmfenster.jpg"))
        for name, img in self._files:
            images.append(feedback.image_jpeg(img, os.path.splitext(name)[0] + ".jpg"))
        return feedback.build_payload(self.message.toPlainText(), self.name.text(),
                                      feedback.system_info(self.window_) if self.system.isChecked() else None,
                                      feedback.error_log() if self.log.isChecked() else None, images)

    def _preview(self):
        p = self.payload()
        shown = dict(p, images=[f"{i['name']} ({i['size'][0]}×{i['size'][1]}, {i['bytes'] // 1024} KB)"
                                for i in p["images"]])
        box = QMessageBox(self)
        box.setWindowTitle("Vorschau")
        box.setText("Genau das wird verschlüsselt gesendet:")
        box.setDetailedText(json.dumps(shown, ensure_ascii=False, indent=2))
        box.exec()

    # ------------------------------------------------------------ Senden
    def _send(self):
        if not feedback.available():
            QMessageBox.warning(self, "Feedback", "Das Senden ist in dieser Version nicht eingerichtet.")
            return
        self._sender = feedback.FeedbackSender(self)
        self._sender.progress.connect(self._on_progress)
        self._sender.finished.connect(self._on_finished)
        for w in (self.message, self.name, self.system, self.log, self.shot, self.preview_btn):
            w.setEnabled(False)
        self._update_send()
        self.progress.setRange(0, 0)
        self.progress.setVisible(True)
        self._sender.send(self.payload())

    def _on_progress(self, done, total):
        self.progress.setRange(0, total)
        self.progress.setValue(done)

    def _on_finished(self, ok, msg):
        self.progress.setVisible(False)
        if ok:
            QMessageBox.information(self, "Feedback", msg)
            self.accept()
            return
        QMessageBox.warning(self, "Feedback", f"Senden fehlgeschlagen:\n{msg}\n\nDeine Nachricht ist noch da – "
                                              "du kannst es später erneut versuchen.")
        self._sender = None
        for w in (self.message, self.name, self.system, self.preview_btn):
            w.setEnabled(True)
        self.log.setEnabled(bool(feedback.error_log()))
        self.shot.setEnabled(self._screenshot is not None)
        self._update_send()
