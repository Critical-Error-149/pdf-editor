"""Bildseiten: ein Bild als ganze Seite – Format, Einpassung, Rand und Ausschnitt jederzeit änderbar.

Das Originalbild liegt nur einmal in der PDF. Zuschneiden und Füllen geschehen über einen
Clipping-Rahmen im Seiteninhalt (kein Neuberechnen, kein Qualitätsverlust). Die Einstellungen
stehen unsichtbar im Seitenobjekt (/PDFEditorImagePage), damit die Seite später erneut
bearbeitet werden kann. Seiten ohne diese Angabe, die nur aus einem Bild bestehen (z. B. Scans),
lassen sich ebenfalls bearbeiten.
"""
from __future__ import annotations

import json

import pymupdf as fitz
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QHBoxLayout, QLabel, QPushButton, QRadioButton, QVBoxLayout, QWidget)

from . import images

KEY = "PDFEditorImagePage"
MM = 72 / 25.4
FITS = {"contain": "Einpassen (mit Rand)", "stretch": "Strecken", "cover": "Füllen (zuschneiden)"}
BORDERS = {"white": "Weiß", "black": "Schwarz", "template": "Vorlage"}


def default_spec(qs=None):
    fit = str(qs.value("image_page/fit", "contain")) if qs is not None else "contain"
    border = str(qs.value("image_page/border", "white")) if qs is not None else "white"
    template = str(qs.value("image_page/template", "Kariert")) if qs is not None else "Kariert"
    return {"format": "image", "landscape": False, "fit": fit if fit in FITS else "contain",
            "border": border if border in BORDERS else "white", "template": template, "crop": None,
            "size": None}


# ---------------------------------------------------------------- Geometrie
def page_size(spec, crop_w, crop_h, ref_size, formats):
    """Seitengröße (pt) aus den Formatangaben."""
    f = spec["format"]
    if f == "image":  # Breite der vorigen Seite, Höhe nach Bildseitenverhältnis -> kein Rand
        w = ref_size[0]
        return w, w * crop_h / crop_w
    if f == "prev":
        return ref_size
    if f == "custom":
        return tuple(spec["size"])
    short, long_ = sorted(formats[f])  # Ausrichtung aus den Maßen (16:9 liegt in der Liste schon quer)
    return (long_, short) if spec.get("landscape") else (short, long_)


def placement(page_w, page_h, img_w, img_h, crop, fit):
    """(Rechteck des ganzen Bildes, Clipping-Rechteck) in Seitenkoordinaten."""
    cx, cy, cw, ch = crop
    if fit == "stretch":
        tx, ty, tw, th = 0, 0, page_w, page_h
    else:
        s = min(page_w / cw, page_h / ch) if fit == "contain" else max(page_w / cw, page_h / ch)
        tw, th = cw * s, ch * s
        tx, ty = (page_w - tw) / 2, (page_h - th) / 2
    sx, sy = tw / cw, th / ch
    full = fitz.Rect(tx - cx * sx, ty - cy * sy, tx - cx * sx + img_w * sx, ty - cy * sy + img_h * sy)
    clip = fitz.Rect(tx, ty, tx + tw, ty + th) & fitz.Rect(0, 0, page_w, page_h)
    return full, clip


# ---------------------------------------------------------------- Seite aufbauen
def draw(doc, page, spec, img_w, img_h, data=None, xref=0):
    """Hintergrund + (beschnittenes) Bild auf eine leere Seite zeichnen. Gibt die Bild-xref zurück."""
    from .document import draw_template
    w, h = page.rect.width, page.rect.height
    crop = spec["crop"] or [0, 0, img_w, img_h]
    if spec["border"] == "black":
        page.draw_rect(page.rect, color=None, fill=(0, 0, 0), overlay=True)
    elif spec["border"] == "template":
        draw_template(page, spec.get("template") or "Kariert")
    full, clip = placement(w, h, img_w, img_h, crop, spec["fit"])
    if data is not None:
        xref = page.insert_image(full, stream=data, keep_proportion=False)
    else:
        xref = page.insert_image(full, xref=xref, keep_proportion=False)
    # Ausschnitt: den eben angehängten Bild-Befehl in einen Clipping-Rahmen setzen
    cont = page.get_contents()[-1]
    stream = doc.xref_stream(cont) or b""
    box = f"q {clip.x0:.3f} {h - clip.y1:.3f} {clip.width:.3f} {clip.height:.3f} re W n\n".encode()
    doc.update_stream(cont, box + stream + b"\nQ\n")
    store = dict(spec, crop=list(crop))
    doc.xref_set_key(page.xref, KEY, _pdf_string(json.dumps(store)))
    return xref


def clear_contents(doc, page):
    """Seiteninhalt leeren (Notizen bleiben); Bild-Verweise entfernen, damit das Bild nicht doppelt eingetragen wird."""
    for x in page.get_contents():
        doc.update_stream(x, b" ")
    kind, _ = doc.xref_get_key(page.xref, "Resources")
    if kind == "xref":  # Ressourcen als eigenes Objekt
        res_xref = int(doc.xref_get_key(page.xref, "Resources")[1].split()[0])
        doc.xref_set_key(res_xref, "XObject", "<<>>")
    elif kind == "dict":
        doc.xref_set_key(page.xref, "Resources/XObject", "<<>>")


def _pdf_string(text):
    return "(" + text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ")"


# ---------------------------------------------------------------- Bildseite erkennen
def page_image(doc, index):
    """(xref, Breite, Höhe) des Seitenbildes einer Bildseite oder None."""
    page = doc[index]
    imgs = {i[0]: i for i in page.get_images(full=True)}  # gleiches Bild unter mehreren Namen zählt einmal
    imgs = list(imgs.values())
    if len(imgs) != 1:
        return None
    xref, w, h = imgs[0][0], imgs[0][2], imgs[0][3]
    return xref, w, h


def get_spec(doc, index):
    """Gespeicherte Einstellungen – oder aus einer reinen Bildseite abgeleitet. None, wenn keine Bildseite."""
    page = doc[index]
    info = page_image(doc, index)
    if info is None:
        return None
    xref, iw, ih = info
    kind, value = doc.xref_get_key(page.xref, KEY)
    if kind == "string":
        try:
            spec = json.loads(value)
            spec["size"] = [page.rect.width, page.rect.height]
            return spec
        except ValueError:
            pass
    # Seite ohne Angaben: nur übernehmen, wenn sie wirklich nur aus dem Bild besteht (z. B. Scan)
    if page.get_text().strip() or page.get_drawings():
        return None
    rects = page.get_image_rects(xref)
    if not rects:
        return None
    r = rects[0] & page.rect
    covers = r.width >= page.rect.width - 1 and r.height >= page.rect.height - 1
    spec = default_spec()
    spec.update(format="custom", size=[page.rect.width, page.rect.height],
                fit="stretch" if covers else "contain", border="white", crop=[0, 0, iw, ih])
    return spec


def image_ok(doc, xref):
    """Lässt sich das Bild laden (inkl. Farbprofil)?"""
    try:
        doc.extract_image(xref)
        fitz.Pixmap(doc, xref)
        return True
    except Exception:
        return False


def image_bytes(doc, xref):
    """Originalbild als Bytes – notfalls ohne Farbprofil gelesen (statt mit einem Fehler abzubrechen)."""
    try:
        info = doc.extract_image(xref)
        if info:
            return info["image"]
    except Exception:
        pass
    raw = doc.xref_stream_raw(xref)
    if raw and doc.xref_get_key(xref, "Filter")[1] == "/DCTDecode":
        return raw  # JPEG-Daten unverändert; das Farbprofil steckt im PDF-Objekt, nicht im Bild
    try:
        fitz.TOOLS.set_icc(False)
        return fitz.Pixmap(doc, xref).tobytes("png")
    except Exception:
        return None
    finally:
        fitz.TOOLS.set_icc(True)


# ---------------------------------------------------------------- Dialog
class ImagePageDialog(QDialog):
    """Format, Einpassung, Rand und Ausschnitt einer Bildseite, mit Vorschau."""

    def __init__(self, parent, img: QImage, spec, ref_size, formats, templates, title="Bild als Seite"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.img = img
        self.spec = dict(spec)
        if not self.spec.get("crop"):
            self.spec["crop"] = [0, 0, img.width(), img.height()]
        self.ref_size = ref_size
        self.formats = formats
        # kleines Vorschaubild (schnell, auch bei großen Scans)
        self._thumb = img.scaled(900, 900, Qt.KeepAspectRatio, Qt.SmoothTransformation) \
            if max(img.width(), img.height()) > 900 else img
        self._thumb_bytes = images.encode(self._thumb)

        lay = QHBoxLayout(self)
        left = QVBoxLayout()
        self.preview = QLabel()
        self.preview.setFixedSize(300, 380)
        self.preview.setAlignment(Qt.AlignCenter)
        left.addWidget(self.preview)
        crop_row = QHBoxLayout()
        b = QPushButton("Zuschneiden …")
        b.clicked.connect(self._crop)
        crop_row.addWidget(b)
        self.reset_crop = QPushButton("Ganzes Bild")
        self.reset_crop.clicked.connect(self._reset_crop)
        crop_row.addWidget(self.reset_crop)
        left.addLayout(crop_row)
        lay.addLayout(left)

        right = QVBoxLayout()
        form = QFormLayout()
        self.format = QComboBox()
        self.format.addItem("Passend zum Bild (Breite der vorigen Seite)", "image")
        self.format.addItem("Wie vorige Seite", "prev")
        for name in formats:
            self.format.addItem(name, name)
        self.format.addItem("Benutzerdefiniert", "custom")
        form.addRow("Format:", self.format)
        self.custom_w, self.custom_h = QDoubleSpinBox(), QDoubleSpinBox()
        size = spec.get("size") or ref_size
        for sbox, v in ((self.custom_w, size[0] / MM), (self.custom_h, size[1] / MM)):
            sbox.setRange(10, 5000)
            sbox.setDecimals(1)
            sbox.setSuffix(" mm")
            sbox.setValue(v)
        row = QHBoxLayout()
        row.addWidget(self.custom_w)
        row.addWidget(self.custom_h)
        self.custom_widget = _wrap(row)
        form.addRow("Breite × Höhe:", self.custom_widget)
        self.portrait, self.landscape = QRadioButton("Hochformat"), QRadioButton("Querformat")
        grp = QButtonGroup(self)
        grp.addButton(self.portrait)
        grp.addButton(self.landscape)
        (self.landscape if spec.get("landscape") else self.portrait).setChecked(True)
        row = QHBoxLayout()
        row.addWidget(self.portrait)
        row.addWidget(self.landscape)
        self.orient_widget = _wrap(row)
        form.addRow("Ausrichtung:", self.orient_widget)
        self.fit = QComboBox()
        for k, v in FITS.items():
            self.fit.addItem(v, k)
        form.addRow("Bild:", self.fit)
        self.border = QComboBox()
        for k, v in BORDERS.items():
            self.border.addItem(v, k)
        form.addRow("Rand:", self.border)
        self.template = QComboBox()
        self.template.addItems([t for t in templates if t != "Leer"])
        form.addRow("Vorlage:", self.template)
        right.addLayout(form)
        self.size_label = QLabel()
        right.addWidget(self.size_label)
        right.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        right.addWidget(buttons)
        lay.addLayout(right, 1)

        _select(self.format, spec.get("format", "image"))
        _select(self.fit, spec.get("fit", "contain"))
        _select(self.border, spec.get("border", "white"))
        self.template.setCurrentText(spec.get("template") or "Kariert")
        for w in (self.format, self.fit, self.border, self.template):
            w.currentIndexChanged.connect(self._update)
        for w in (self.custom_w, self.custom_h):
            w.valueChanged.connect(self._update)
        self.portrait.toggled.connect(self._update)
        self._update()

    # ---- Werte
    def result_spec(self):
        s = dict(self.spec)
        s["format"] = self.format.currentData()
        s["landscape"] = self.landscape.isChecked()
        s["fit"] = self.fit.currentData()
        s["border"] = self.border.currentData()
        s["template"] = self.template.currentText()
        s["size"] = [self.custom_w.value() * MM, self.custom_h.value() * MM]
        cw, ch = s["crop"][2], s["crop"][3]
        s["size"] = list(page_size(s, cw, ch, self.ref_size, self.formats)) if s["format"] != "custom" \
            else s["size"]
        return s

    def _crop(self):
        dlg = images.CropDialog(self.img, self)
        x, y, w, h = self.spec["crop"]
        dlg.widget.crop.setRect(x, y, w, h)
        if dlg.exec():
            r: QRect = dlg.crop_rect()
            self.spec["crop"] = [r.x(), r.y(), r.width(), r.height()]
            self._update()

    def _reset_crop(self):
        self.spec["crop"] = [0, 0, self.img.width(), self.img.height()]
        self._update()

    def _update(self):
        f = self.format.currentData()
        self.custom_widget.setEnabled(f == "custom")
        self.orient_widget.setEnabled(f in self.formats)
        fit = self.fit.currentData()
        self.border.setEnabled(fit == "contain")
        self.template.setEnabled(fit == "contain" and self.border.currentData() == "template")
        cropped = self.spec["crop"] != [0, 0, self.img.width(), self.img.height()]
        self.reset_crop.setEnabled(cropped)
        spec = self.result_spec()
        w, h = spec["size"]
        self.size_label.setText(f"Seite: {w / MM:.0f} × {h / MM:.0f} mm"
                                + (f" · Ausschnitt {spec['crop'][2]} × {spec['crop'][3]} px" if cropped else ""))
        self.preview.setPixmap(self._render_preview(spec))

    def _render_preview(self, spec):
        """Seite mit dem verkleinerten Bild in einem temporären Dokument aufbauen und zeigen."""
        k = self._thumb.width() / self.img.width()
        thumb_spec = dict(spec, crop=[c * k for c in spec["crop"]])
        w, h = spec["size"]
        doc = fitz.open()
        page = doc.new_page(width=w, height=h)
        draw(doc, page, thumb_spec, self._thumb.width(), self._thumb.height(), data=self._thumb_bytes)
        box = self.preview.size()
        z = min((box.width() - 8) / w, (box.height() - 8) / h) * self.devicePixelRatioF()
        pix = doc[0].get_pixmap(matrix=fitz.Matrix(z, z), alpha=False)
        img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888).copy()
        doc.close()
        p = QPainter(img)  # Seitenrand, damit auch eine schwarze Seite vom dunklen Dialog absticht
        p.setPen(QColor("#6b6f78"))
        p.drawRect(0, 0, img.width() - 1, img.height() - 1)
        p.end()
        img.setDevicePixelRatio(self.devicePixelRatioF())
        return QPixmap.fromImage(img)


def _select(combo, data):
    i = combo.findData(data)
    if i >= 0:
        combo.setCurrentIndex(i)


def _wrap(layout):
    w = QWidget()
    layout.setContentsMargins(0, 0, 0, 0)
    w.setLayout(layout)
    return w

