"""Textfeld, das direkt auf der Seite bearbeitet wird (statt in einem Dialog)."""
from __future__ import annotations

import os

import pymupdf as fitz
from PySide6.QtCore import QByteArray, QEvent, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (QBrush, QColor, QFont, QFontDatabase, QPainter, QPainterPath, QPen, QTextBlockFormat,
                           QTextCursor)
from PySide6.QtWidgets import QGraphicsItem, QGraphicsTextItem

# So setzt MuPDF den Text eines FreeText-Felds (gemessen, in Vielfachen der Schriftgröße):
BASELINE = 0.79     # erste Grundlinie unterhalb der Oberkante
LINE_STEP = 1.2     # Zeilenabstand
BASE_PX = 100.0     # interne Schriftgröße; skaliert wird über setScale()
WRAP_PAD = 1.0      # Zugabe (pt) zur Breite, damit MuPDF genauso umbricht wie die Vorschau
GRIP = 30           # Größe der Griffe in Bildschirmpixeln (fingertauglich)
PAGE_MARGIN = 8.0
TAB = "    "        # Tabulator -> Leerzeichen (MuPDF zeigt Tabs in Textfeldern nicht an)
CURSOR_W = 0.08     # Cursorbreite in Vielfachen der Schriftgröße
BLINK_MS = 530

_family = None
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")


def pdf_font_family():
    """Die Helvetica, mit der MuPDF fertige Textfelder zeichnet (Nimbus Sans) – damit sich die Schrift
    beim Verlassen des Textfelds nicht ändert."""
    global _family
    if _family is None:
        _family = "Arial"  # Rückfall: gleiche Zeichenbreiten wie Helvetica
        # MuPDF liefert die Schrift als reine CFF-Datei, die Windows nicht lädt -> als OpenType verpackt
        # mitgeliefert (erzeugt mit tools/make_pdf_font.py); andernfalls direkt versuchen (Linux/macOS)
        sources = [lambda: QFontDatabase.addApplicationFont(os.path.join(FONT_DIR, "PDFSans.ttf")),
                   lambda: QFontDatabase.addApplicationFontFromData(QByteArray(fitz.Font("helv").buffer))]
        for load in sources:
            try:
                families = QFontDatabase.applicationFontFamilies(load())
            except Exception:
                families = []
            if families:
                _family = families[0]
                break
    return _family


class _Grip(QGraphicsItem):
    """Runder Griff mit fester Bildschirmgröße (Verschieben bzw. Breite ändern)."""

    def __init__(self, kind, parent):
        super().__init__(parent)
        self.kind = kind
        self.setFlag(QGraphicsItem.ItemIgnoresTransformations)
        self.setZValue(25)
        # außerhalb des Textfelds (in Bildschirmpixeln), damit der Griff weder Text noch Cursor verdeckt
        d = GRIP / 2 + 4
        self.offset = QPointF(-d, -d) if kind == "move" else QPointF(d, 0)

    def boundingRect(self):
        return QRectF(-GRIP / 2 - 1, -GRIP / 2 - 1, GRIP + 2, GRIP + 2).translated(self.offset)

    def paint(self, p: QPainter, option, widget=None):
        p.translate(self.offset)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor("#ffffff"), 2))
        p.setBrush(QColor("#2f7de1"))
        p.drawEllipse(QRectF(-GRIP / 2, -GRIP / 2, GRIP, GRIP))
        p.setPen(QPen(QColor("#ffffff"), 2, Qt.SolidLine, Qt.RoundCap))
        a = GRIP * 0.28
        path = QPainterPath()
        if self.kind == "move":  # Kreuzpfeil
            path.moveTo(-a, 0); path.lineTo(a, 0)
            path.moveTo(0, -a); path.lineTo(0, a)
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                tip = QPointF(dx * a, dy * a)
                path.moveTo(tip + QPointF(-dy * 4 - dx * 4, -dx * 4 - dy * 4)); path.lineTo(tip)
                path.lineTo(tip + QPointF(dy * 4 - dx * 4, dx * 4 - dy * 4))
        else:  # Doppelpfeil links/rechts
            path.moveTo(-a, 0); path.lineTo(a, 0)
            for dx in (1, -1):
                tip = QPointF(dx * a, 0)
                path.moveTo(tip + QPointF(-dx * 4, -4)); path.lineTo(tip); path.lineTo(tip + QPointF(-dx * 4, 4))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)


class TextEditorItem(QGraphicsTextItem):
    def __init__(self, parent, anchor: QPointF, text: str, fontsize: float, color: QColor,
                 width: float | None = None):
        super().__init__(parent)
        self.page_item = parent
        self.anchor = QPointF(anchor)  # obere linke Ecke des PDF-Textfelds (Seitenkoordinaten)
        self.fixed_width = width       # Breite in pt, None = wächst mit dem Text
        font = QFont(pdf_font_family())
        font.setPixelSize(int(BASE_PX))
        self.setFont(font)
        self.document().setDocumentMargin(0)
        self.setDefaultTextColor(color)
        self.setPlainText(text.replace("\t", TAB))
        # eigener Cursor (der von Qt wäre nach dem Verkleinern nur Bruchteile eines Pixels breit und in der
        # hellen Textfarbe des Programms gezeichnet -> auf weißem Papier unsichtbar)
        self.document().documentLayout().setProperty("cursorWidth", 0)
        self._cursor_on = True
        self._blink = QTimer(self)
        self._blink.setInterval(BLINK_MS)
        self._blink.timeout.connect(self._toggle_cursor)
        self._apply_line_height()
        self.move_grip = _Grip("move", parent)
        self.size_grip = _Grip("size", parent)
        self.fontsize = float(fontsize)
        self.setScale(self.fontsize / BASE_PX)
        self.setTextInteractionFlags(Qt.TextEditorInteraction)
        self.setZValue(20)
        self.document().contentsChanged.connect(self._layout_changed)
        self._layout_changed()

    # ------------------------------------------------------------ Darstellung
    def _apply_line_height(self):
        cur = QTextCursor(self.document())
        cur.select(QTextCursor.Document)
        fmt = QTextBlockFormat()
        fmt.setLineHeight(BASE_PX * LINE_STEP, QTextBlockFormat.LineHeightTypes.FixedHeight.value)
        cur.mergeBlockFormat(fmt)

    def set_font_size(self, fontsize: float):
        self.fontsize = float(fontsize)
        self.setScale(self.fontsize / BASE_PX)
        self._layout_changed()

    def set_color(self, color: QColor):
        self.setDefaultTextColor(color)

    def set_anchor(self, p: QPointF):
        pw, ph = self.page_item.w, self.page_item.h
        self.anchor = QPointF(min(max(p.x(), 0.0), pw - self.fontsize),
                              min(max(p.y(), 0.0), ph - self.fontsize))
        self._layout_changed()

    def set_width(self, width_pt: float):
        """Breite festlegen (Griff rechts). Text wird dann umgebrochen."""
        max_w = self.page_item.w - self.anchor.x() - PAGE_MARGIN / 2
        self.fixed_width = max(2 * self.fontsize, min(width_pt, max_w))
        self._layout_changed()

    def _layout_changed(self):
        s = self.scale()
        avail = (self.page_item.w - self.anchor.x() - PAGE_MARGIN) / s
        if self.fixed_width is not None:
            tw = (self.fixed_width - WRAP_PAD) / s
        else:
            # mitwachsen, aber am Seitenrand umbrechen
            self.setTextWidth(-1)
            tw = avail if self.document().idealWidth() > avail else -1
        if abs(self.textWidth() - tw) > 0.01:
            self.setTextWidth(tw)
        self._reposition()

    def _reposition(self):
        block = self.document().firstBlock()
        layout = block.layout()
        base = BASE_PX * 0.9
        if layout is not None and layout.lineCount() > 0:
            line = layout.lineAt(0)
            base = line.y() + line.ascent()
        s = self.scale()
        self.setPos(self.anchor.x(), self.anchor.y() + BASELINE * self.fontsize - base * s)
        # Griffe: links oben (verschieben) und rechts mittig (Breite)
        r = self.mapRectToParent(self.boundingRect())
        self.move_grip.setPos(r.left(), r.top())
        self.size_grip.setPos(r.right(), r.center().y())

    def paint(self, painter, option, widget=None):
        r = self.boundingRect().adjusted(-4, -4, 4, 4)
        pen = QPen(QColor("#2f7de1"), 1.5, Qt.DashLine)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(QBrush(QColor(47, 125, 225, 18)))
        painter.drawRect(r)
        painter.save()  # Qt begrenzt den Malbereich auf die Textbreite -> Cursor am Ende wäre abgeschnitten
        super().paint(painter, option, widget)
        painter.restore()
        if self.hasFocus() and self._cursor_on:
            rect = self.cursor_rect()
            if rect is not None:
                painter.fillRect(rect, self.defaultTextColor())

    # ------------------------------------------------------------ Cursor
    def cursor_rect(self):
        """Rechteck des Textcursors (Elementkoordinaten) oder None."""
        cur = self.textCursor()
        block = cur.block()
        layout = block.layout()
        if layout is None or layout.lineCount() == 0:
            return QRectF(0, 0, BASE_PX * CURSOR_W, BASE_PX * LINE_STEP)
        pos = cur.positionInBlock()
        line = layout.lineForTextPosition(pos)
        if not line.isValid():
            line = layout.lineAt(layout.lineCount() - 1)
        x = line.cursorToX(pos)
        x = x[0] if isinstance(x, tuple) else x
        origin = layout.position()
        w = BASE_PX * CURSOR_W
        baseline = origin.y() + line.y() + line.ascent()
        return QRectF(origin.x() + x - w / 2, baseline - 0.82 * BASE_PX, w, 1.04 * BASE_PX)  # Ober- bis Unterlänge

    def _toggle_cursor(self):
        self._cursor_on = not self._cursor_on
        self.update()

    def show_cursor(self):
        """Cursor sofort zeigen und Blinken neu starten (nach Tippen/Klicken)."""
        self._cursor_on = True
        self._blink.start()
        self.update()

    def focusInEvent(self, e):
        super().focusInEvent(e)
        self.show_cursor()

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self._blink.stop()
        self.update()

    def sceneEvent(self, e):
        # Tab erreicht keyPressEvent nicht (Qt behandelt es vorher) -> hier abfangen: als Leerzeichen,
        # wie im fertigen Textfeld
        if e.type() == QEvent.KeyPress and e.key() == Qt.Key_Tab \
                and not e.modifiers() & (Qt.ControlModifier | Qt.AltModifier):
            self.textCursor().insertText(TAB)
            self.show_cursor()
            e.accept()
            return True
        return super().sceneEvent(e)

    def keyPressEvent(self, e):
        super().keyPressEvent(e)
        self.show_cursor()

    def inputMethodEvent(self, e):
        super().inputMethodEvent(e)
        self.show_cursor()

    def mousePressEvent(self, e):
        super().mousePressEvent(e)
        self.show_cursor()

    def boundingRect(self):
        r = super().boundingRect()
        # Leeres Feld trotzdem sichtbar/anklickbar halten; rechts Platz für den Cursor hinter dem letzten Zeichen
        return r.united(QRectF(0, 0, BASE_PX * 2, BASE_PX * LINE_STEP)).adjusted(0, 0, BASE_PX * CURSOR_W, 0)

    def remove(self):
        self._blink.stop()
        for it in (self.move_grip, self.size_grip, self):
            if it.scene():
                it.scene().removeItem(it)

    # ------------------------------------------------------------ Bedienung
    def grip_at(self, view, vpos: QPointF):
        for g in (self.move_grip, self.size_grip):
            gp = QPointF(view.mapFromScene(g.scenePos())) + g.offset
            if abs(gp.x() - vpos.x()) <= GRIP * 0.75 and abs(gp.y() - vpos.y()) <= GRIP * 0.75:
                return g.kind
        return None

    def place_cursor(self, scene_pos: QPointF):
        """Textcursor an eine angetippte Stelle setzen."""
        pos = self.mapFromScene(scene_pos)
        idx = self.document().documentLayout().hitTest(pos, Qt.FuzzyHit)
        cur = self.textCursor()
        cur.setPosition(max(0, idx))
        self.setTextCursor(cur)
        self.setFocus()
        self.show_cursor()

    # ------------------------------------------------------------ Ergebnis
    def text(self) -> str:
        return self.toPlainText().replace("\t", TAB).rstrip()

    def pdf_rect(self):
        """Rechteck für das PDF-Textfeld (Seitenkoordinaten), passend zum Inhalt."""
        doc = self.document()
        lines = 0
        b = doc.firstBlock()
        while b.isValid():
            lines += max(1, b.layout().lineCount() if b.layout() else 1)
            b = b.next()
        fs = self.fontsize
        if self.fixed_width is not None or self.textWidth() > 0:
            w = self.textWidth() * self.scale() + WRAP_PAD
        else:
            w = doc.idealWidth() * self.scale() + 0.6 * fs + 4
        h = lines * LINE_STEP * fs + 0.4 * fs
        x, y = self.anchor.x(), self.anchor.y()
        return (x, y, x + max(w, fs), y + h)

    def focus_end(self):
        self.setFocus()
        cur = self.textCursor()
        cur.movePosition(QTextCursor.End)
        self.setTextCursor(cur)
        self.show_cursor()
