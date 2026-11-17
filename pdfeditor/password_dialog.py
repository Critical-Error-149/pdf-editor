"""Passwortabfrage für geschützte PDFs – mit „Passwort auf diesem PC merken“."""
from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QToolButton,
                               QVBoxLayout)

from . import passwords


class PasswordDialog(QDialog):
    def __init__(self, parent, filename, error=False, remember=False):
        super().__init__(parent)
        self.setWindowTitle("Passwort")
        lay = QVBoxLayout(self)
        if error:
            err = QLabel("Falsches Passwort – bitte erneut eingeben.")
            err.setStyleSheet("color:#ff6b6b;font-weight:bold")
            lay.addWidget(err)
        info = QLabel(f"„{filename}“ ist mit einem Passwort geschützt.")
        info.setWordWrap(True)
        lay.addWidget(info)
        row = QHBoxLayout()
        self.edit = QLineEdit()
        self.edit.setEchoMode(QLineEdit.Password)
        self.edit.setPlaceholderText("Passwort")
        self.edit.setMinimumWidth(320)
        row.addWidget(self.edit, 1)
        show = QToolButton()
        show.setText("👁")
        show.setToolTip("Passwort anzeigen")
        show.setCheckable(True)
        show.toggled.connect(lambda on: self.edit.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        row.addWidget(show)
        lay.addLayout(row)
        self.remember = QCheckBox("Passwort auf diesem PC merken (verschlüsselt, nur für dein Windows-Konto)")
        self.remember.setToolTip("Beim nächsten Öffnen dieser PDF wird das Passwort automatisch verwendet. "
                                 "Löschen: Datei → Einstellungen bzw. Datei → „Gespeichertes Passwort vergessen“")
        self.remember.setChecked(remember)
        self.remember.setVisible(passwords.available())
        lay.addWidget(self.remember)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.edit.setFocus()

    @classmethod
    def ask(cls, parent, filename, error=False, remember=False):
        """(Passwort, merken?, OK?)"""
        dlg = cls(parent, filename, error, remember)
        ok = dlg.exec() == QDialog.Accepted
        return dlg.edit.text(), dlg.remember.isChecked(), ok
