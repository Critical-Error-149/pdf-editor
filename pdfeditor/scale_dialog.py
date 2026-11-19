"""Nachfrage beim Skalieren einer Seite mit Zeichnungen (Drehen mit „Breite angleichen“, Vorlagenformat ändern):
Zeichnungen mitskalieren? Strichdicke mitskalieren? Die letzte Wahl wird als Vorgabe gemerkt."""
from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QLabel, QVBoxLayout


class ScaleDrawingsDialog(QDialog):
    def __init__(self, parent, text):
        super().__init__(parent)
        self.setWindowTitle("Zeichnungen anpassen")
        self.qs = QSettings("PDFEditor", "PDFEditor")
        lay = QVBoxLayout(self)
        label = QLabel(text)
        label.setWordWrap(True)
        lay.addWidget(label)
        self.annots = QCheckBox("Zeichnungen mitskalieren (Größe und Lage passen sich der Seite an)")
        self.annots.setChecked(self.qs.value("scale/annots", "true") == "true")
        lay.addWidget(self.annots)
        self.widths = QCheckBox("Strichdicke und Schriftgröße mitskalieren")
        self.widths.setToolTip("Aus: Striche bleiben so dick (und Text so groß) wie vorher, nur Lage und Länge "
                               "passen sich an")
        self.widths.setChecked(self.qs.value("scale/widths", "true") == "true")
        lay.addWidget(self.widths)
        self.annots.toggled.connect(self.widths.setEnabled)
        self.widths.setEnabled(self.annots.isChecked())
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def choice(self):
        annots = self.annots.isChecked()
        widths = annots and self.widths.isChecked()
        self.qs.setValue("scale/annots", "true" if annots else "false")
        self.qs.setValue("scale/widths", "true" if self.widths.isChecked() else "false")
        return annots, widths


def ask(parent, text):
    """(Zeichnungen mitskalieren, Strichdicke mitskalieren) – oder None bei „Abbrechen“."""
    dlg = ScaleDrawingsDialog(parent, text)
    if dlg.exec() != QDialog.Accepted:
        return None
    return dlg.choice()
