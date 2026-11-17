"""Dialog zum Einfügen leerer Seiten in beliebigem Format."""
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFormLayout, QHBoxLayout, QRadioButton, QSpinBox, QVBoxLayout,
                               QWidget)

from .document import PAGE_FORMATS, PAGE_TEMPLATES, format_size

MM = 72 / 25.4


class InsertPagesDialog(QDialog):
    def __init__(self, parent, page_count, current_index, current_size, default_pos, last_template):
        super().__init__(parent)
        self.setWindowTitle("Seiten einfügen")
        self.current_size = current_size
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.position = QComboBox()
        self.position.addItem(f"Nach aktueller Seite ({current_index + 1})", current_index + 1)
        self.position.addItem(f"Vor aktueller Seite ({current_index + 1})", current_index)
        self.position.addItem("Am Anfang", 0)
        self.position.addItem("Am Ende", page_count)
        self.position.addItem("Nach Seite …", -1)
        self.after_page = QSpinBox()
        self.after_page.setRange(0, page_count)
        self.after_page.setValue(max(0, default_pos))
        self.after_page.setEnabled(False)
        self.position.currentIndexChanged.connect(
            lambda: self.after_page.setEnabled(self.position.currentData() == -1))
        if default_pos != current_index + 1:
            self.position.setCurrentIndex(4)
        pos_row = QHBoxLayout()
        pos_row.addWidget(self.position, 1)
        pos_row.addWidget(self.after_page)
        form.addRow("Position:", _wrap(pos_row))

        self.count = QSpinBox()
        self.count.setRange(1, 500)
        form.addRow("Anzahl:", self.count)

        self.format = QComboBox()
        self.format.addItem("Wie aktuelle Seite", "current")
        for name in PAGE_FORMATS:
            self.format.addItem(name, name)
        self.format.addItem("Benutzerdefiniert", "custom")
        form.addRow("Format:", self.format)

        self.custom_w = QDoubleSpinBox()
        self.custom_h = QDoubleSpinBox()
        for sb, v in ((self.custom_w, current_size[0] / MM), (self.custom_h, current_size[1] / MM)):
            sb.setRange(10, 5000)
            sb.setDecimals(1)
            sb.setSuffix(" mm")
            sb.setValue(v)
        custom_row = QHBoxLayout()
        custom_row.addWidget(self.custom_w)
        custom_row.addWidget(self.custom_h)
        self.custom_widget = _wrap(custom_row)
        form.addRow("Breite × Höhe:", self.custom_widget)

        self.portrait = QRadioButton("Hochformat")
        self.landscape = QRadioButton("Querformat")
        self.portrait.setChecked(True)
        grp = QButtonGroup(self)
        grp.addButton(self.portrait)
        grp.addButton(self.landscape)
        orient_row = QHBoxLayout()
        orient_row.addWidget(self.portrait)
        orient_row.addWidget(self.landscape)
        self.orient_widget = _wrap(orient_row)
        form.addRow("Ausrichtung:", self.orient_widget)

        self.template = QComboBox()
        self.template.addItems(PAGE_TEMPLATES)
        self.template.setCurrentText(last_template)
        form.addRow("Vorlage:", self.template)

        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.format.currentIndexChanged.connect(self._update)
        self._update()

    def _update(self):
        f = self.format.currentData()
        self.custom_widget.setEnabled(f == "custom")
        self.orient_widget.setEnabled(f not in ("current", "custom"))

    def values(self):
        pos = self.position.currentData()
        if pos == -1:
            pos = self.after_page.value()
        f = self.format.currentData()
        if f == "current":
            w, h = self.current_size
        elif f == "custom":
            w, h = self.custom_w.value() * MM, self.custom_h.value() * MM
        else:
            w, h = format_size(f, self.landscape.isChecked())
        return pos, self.count.value(), w, h, self.template.currentText()


def _wrap(layout):
    w = QWidget()
    layout.setContentsMargins(0, 0, 0, 0)
    w.setLayout(layout)
    return w
