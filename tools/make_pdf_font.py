"""Erzeugt pdfeditor/fonts/PDFSans.ttf aus der Helvetica-Ersatzschrift von MuPDF (Nimbus Sans, CFF).

Windows lädt reine CFF-Schriften nicht (und zeichnet auch in OpenType verpackte CFF-Umrisse aus dieser Quelle
nicht). Darum werden die Umrisse in TrueType-Kurven umgerechnet (Abweichung < 1 Schrifteinheit). So zeigt
das Textfeld beim Bearbeiten dieselbe Schrift mit denselben Zeichenbreiten wie das fertige Textfeld.
Nur nötig, wenn PyMuPDF eine andere Schrift mitbringt:
    python -m pip install fonttools
    python tools/make_pdf_font.py
"""
import os
import sys
from io import BytesIO

import pymupdf as fitz
from fontTools.agl import toUnicode
from fontTools.cffLib import CFFFontSet
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "pdfeditor", "fonts", "PDFSans.ttf")
FAMILY = "PDF Sans"  # kein Markenname: „Helvetica“ ist eine geschützte Marke


def main():
    data = fitz.Font("helv").buffer
    cff = CFFFontSet()
    cff.decompile(BytesIO(data), TTFont())
    name = cff.fontNames[0]
    top = cff[name]
    charstrings = top.CharStrings
    glyph_order = list(top.charset)
    glyphs, metrics, cmap = {}, {}, {}
    for g in glyph_order:
        cs = charstrings[g]
        pen = TTGlyphPen(None)
        cs.draw(Cu2QuPen(pen, max_err=0.5, reverse_direction=True))  # CFF: gegen den Uhrzeigersinn
        glyph = pen.glyph()
        glyphs[g] = glyph
        metrics[g] = (int(round(cs.width)), 0)
        uni = toUnicode(g)
        if uni and len(uni) == 1:
            cmap.setdefault(ord(uni), g)
    upem = int(round(1 / top.FontMatrix[0]))
    bbox = [int(v) for v in top.FontBBox]
    fb = FontBuilder(upem, isTTF=True)
    fb.setupGlyphOrder(glyph_order)
    fb.setupCharacterMap(cmap)
    fb.setupGlyf(glyphs)
    glyf = fb.font["glyf"]
    for g in glyph_order:  # linke Seitenränder aus den Umrissen
        gl = glyf[g]
        gl.recalcBounds(glyf)
        metrics[g] = (metrics[g][0], getattr(gl, "xMin", 0))
    fb.setupHorizontalMetrics(metrics)
    fb.setupHorizontalHeader(ascent=bbox[3], descent=bbox[1])
    fb.setupNameTable({"familyName": FAMILY, "styleName": "Regular",
                       "uniqueFontIdentifier": f"{FAMILY} Regular (MuPDF {name})",
                       "fullName": f"{FAMILY} Regular", "psName": "PDFSans-Regular",
                       "version": "Version 1.0",
                       "copyright": "Nimbus Sans (URW++), aus MuPDF; GNU AGPL / Font-Ausnahme"})
    fb.setupOS2(sTypoAscender=bbox[3], sTypoDescender=bbox[1], usWinAscent=bbox[3], usWinDescent=abs(bbox[1]),
                sxHeight=523, sCapHeight=718, fsType=0)
    fb.setupPost()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    fb.save(OUT)
    old = os.path.join(os.path.dirname(OUT), "PDFHelvetica.otf")
    if os.path.exists(old):
        os.remove(old)
    print(f"{OUT}: {len(glyph_order)} Zeichen, {len(cmap)} Unicode-Zuordnungen, upem {upem}")


if __name__ == "__main__":
    sys.exit(main())
