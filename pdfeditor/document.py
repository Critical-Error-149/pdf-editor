"""Dokumentmodell: kapselt pymupdf.Document mit Undo/Redo und Seitenoperationen."""
from __future__ import annotations

import os
import re

import pymupdf as fitz
from pymupdf import mupdf
from PySide6.QtCore import QObject, Signal

from . import image_page, images

# Seitenformate in PDF-Punkten (1 pt = 1/72 Zoll), Hochformat
PAGE_FORMATS = {
    "A3": (842, 1191),
    "A4": (595, 842),
    "A5": (420, 595),
    "A6": (298, 420),
    "Letter": (612, 792),
    "Legal": (612, 1008),
    "16:9 Folie": (720, 405),
}

PAGE_TEMPLATES = ["Leer", "Liniert", "Kariert", "Punktraster"]


def _have_operation(pdf):
    """Ersatz für pymupdf.JM_have_operation (PyMuPDF 1.28): die Originalprüfung meldet ausgerechnet
    *während* eines Journal-Schritts einen Fehler („Can't undo/redo during an operation“) – dadurch brachen
    insert_text, get_new_xref, update_object … bei aktivem Rückgängig-Journal ab."""
    if not pdf.m_internal.journal:
        return 1
    try:
        mupdf.pdf_undoredo_step(pdf, 0)
    except Exception:
        return 1  # MuPDF verweigert die Abfrage nur, solange ein Schritt läuft -> Änderungen erlaubt
    return 0


fitz.JM_have_operation = _have_operation

# Notiz-Symbol: MuPDF zeichnet ein eckiges Kästchen („0.5 0.5 15 15 re“) – stattdessen leicht abgerundet
_NOTE_BOX = b"0.5 0.5 15 15 re"


def _rounded_box(x0=0.5, y0=0.5, x1=15.5, y1=15.5, r=2.5):
    k = r * 0.5523  # Bézier-Näherung für Viertelkreise
    return (f"{x0 + r} {y0} m {x1 - r} {y0} l {x1 - r + k} {y0} {x1} {y0 + r - k} {x1} {y0 + r} c "
            f"{x1} {y1 - r} l {x1} {y1 - r + k} {x1 - r + k} {y1} {x1 - r} {y1} c "
            f"{x0 + r} {y1} l {x0 + r - k} {y1} {x0} {y1 - r + k} {x0} {y1 - r} c "
            f"{x0} {y0 + r} l {x0} {y0 + r - k} {x0 + r - k} {y0} {x0 + r} {y0} c h").encode()


class _ReadGuard:
    """Lesezugriff (Rendern, Text, Suche) in einem „impliziten“ Journal-Schritt.

    MuPDF repariert beim Lesen mancher PDFs nebenbei Kleinigkeiten (z. B. fehlende Rückverweise im
    Strukturbaum getaggter Word-PDFs). Mit aktivem Rückgängig-Journal und ohne laufenden Schritt beschädigt das
    den Speicher – das Programm stürzte beim Öffnen solcher Dateien ab. In einem impliziten Schritt sind die
    Änderungen erlaubt; MuPDF hängt sie an den vorigen Schritt an (kein zusätzlicher Rückgängig-Schritt)."""

    def __init__(self, doc):
        self.doc = doc
        self.pdf = None

    def __enter__(self):
        try:
            pdf = mupdf.pdf_document_from_fz_document(self.doc.this)
            if pdf.m_internal and pdf.m_internal.journal:
                mupdf.pdf_begin_implicit_operation(pdf)
                self.pdf = pdf
        except Exception:
            self.pdf = None
        return self.doc

    def __exit__(self, *exc):
        if self.pdf is not None:
            mupdf.pdf_end_operation(self.pdf)
            self.pdf = None
        return False


def read_guard(doc):
    return _ReadGuard(doc)


def repair_struct_tree(doc):
    """Fehlende Rückverweise (/P) im Strukturbaum getaggter PDFs ergänzen – genau das, was MuPDF sonst beim
    ersten Rendern einer Seite nachholt. Vor dem Einschalten des Journals aufrufen. Gibt die Anzahl zurück."""
    try:
        kind, val = doc.xref_get_key(doc.pdf_catalog(), "StructTreeRoot")
    except Exception:
        return 0
    if kind != "xref":
        return 0
    root = int(val.split()[0])
    fixed = 0
    seen = set()

    def refs(text):
        return [int(m) for m in re.findall(r"(\d+) 0 R", text or "")]

    def is_elem_without_parent(x):
        return doc.xref_get_key(x, "S")[0] != "null" and doc.xref_get_key(x, "P")[0] == "null"

    stack = [(root, None)]
    while stack:  # Baum über /K: Eltern = übergeordnetes Element
        x, parent = stack.pop()
        if x in seen:
            continue
        seen.add(x)
        if parent is not None:
            if doc.xref_get_key(x, "S")[0] == "null":
                continue  # kein Strukturelement (Verweis auf Seiteninhalt)
            if doc.xref_get_key(x, "P")[0] == "null":
                doc.xref_set_key(x, "P", f"{parent} 0 R")
                fixed += 1
        kind, k = doc.xref_get_key(x, "K")
        if kind in ("xref", "array"):
            stack += [(r, x) for r in refs(k)]
    # Elemente, die nur über die Seitenzuordnung (ParentTree) erreichbar sind: Eltern = Wurzel (wie MuPDF)
    kind, val = doc.xref_get_key(root, "ParentTree")
    todo = refs(val) if kind == "xref" else []
    done = set()
    while todo:
        x = todo.pop()
        if x in done:
            continue
        done.add(x)
        for key in ("Kids", "Nums"):
            kind, v = doc.xref_get_key(x, key)
            if kind == "xref":
                v, kind = doc.xref_object(int(v.split()[0]), compressed=True), "array"
            if kind != "array":
                continue
            for r in refs(v):
                if key == "Kids":
                    todo.append(r)
                    continue
                obj = doc.xref_object(r, compressed=True)
                for e in (refs(obj) if obj.startswith("[") else [r]):  # Liste der Elemente einer Seite
                    if e not in seen and is_elem_without_parent(e):
                        doc.xref_set_key(e, "P", f"{root} 0 R")
                        fixed += 1
                    seen.add(e)
    return fixed

MARKUP_TYPES = (fitz.PDF_ANNOT_HIGHLIGHT, fitz.PDF_ANNOT_UNDERLINE, fitz.PDF_ANNOT_STRIKE_OUT,
                fitz.PDF_ANNOT_SQUIGGLY)
_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def doc_bytes(doc) -> bytes:
    """Dokument als PDF-Bytes. Schreibt direkt in einen MuPDF-Puffer – etwa viermal schneller als
    doc.tobytes(), das jedes Teilstück einzeln an Python übergibt (Rückgängig-Schnappschuss nach jedem Strich)."""
    try:
        buf = mupdf.fz_new_buffer(1 << 20)
        out = mupdf.FzOutput(buf)
        mupdf.pdf_write_document(mupdf.pdf_document_from_fz_document(doc.this), out, mupdf.PdfWriteOptions())
        out.fz_close_output()
        return buf.fz_buffer_extract()
    except Exception:
        return doc.tobytes()


FONT_BASE = {"Helv": "Helvetica", "TiRo": "Times-Roman", "Cour": "Courier", "ZaDb": "ZapfDingbats",
             "Symb": "Symbol"}


def repair_annot_fonts(doc, annot_xrefs=None):
    """Schriftverweise in Textfeldern prüfen und kaputte ersetzen (zeigen sie auf etwas anderes als eine
    Schrift, ersetzt MuPDF die Schrift durch eine Serifenschrift ohne Umlaute). Gibt die Anzahl zurück."""
    if annot_xrefs is None:
        annot_xrefs = [a.xref for page in doc for a in page.annots() if a.type[0] == fitz.PDF_ANNOT_FREE_TEXT]
    fixed = 0
    n = doc.xref_length()
    for ax in annot_xrefs:
        ap = doc.xref_get_key(ax, "AP/N")
        if ap[0] != "xref":
            continue
        apx = int(ap[1].split()[0])
        fonts = doc.xref_get_key(apx, "Resources/Font")
        if fonts[0] != "dict":
            continue
        for name, ref in re.findall(r"/([\w#.+-]+)\s+(\d+)\s+0\s+R", fonts[1]):
            fx = int(ref)
            ok = 0 < fx < n and doc.xref_get_key(fx, "Type")[1] == "/Font"
            if not ok:
                nx = doc.get_new_xref()
                doc.update_object(nx, f"<</Type/Font/Subtype/Type1/BaseFont/{FONT_BASE.get(name, 'Helvetica')}"
                                      "/Encoding/WinAnsiEncoding>>")
                doc.xref_set_key(apx, f"Resources/Font/{name}", f"{nx} 0 R")
                n = doc.xref_length()
                fixed += 1
    return fixed


def format_size(name, landscape=False):
    """Seitengröße (pt) eines benannten Formats; Hoch-/Querformat ergibt sich aus den Maßen
    (die Liste enthält auch von Haus aus quer liegende Formate wie 16:9)."""
    short, long_ = sorted(PAGE_FORMATS[name])
    return (long_, short) if landscape else (short, long_)

# ab dieser Größe wird eine PDF direkt von der Platte gelesen (nicht ganz in den Arbeitsspeicher) und
# beim Speichern nur um die Änderungen ergänzt (inkrementell) – z. B. ein 700-MB-Fachbuch
LARGE_FILE = 64 * 1024 * 1024

BLOCKED_MSG = ("Windows hat den Zugriff auf „{name}“ verweigert.\n\n"
               "Mögliche Ursachen:\n"
               "• Die Datei ist in einem anderen Programm geöffnet.\n"
               "• OneDrive synchronisiert sie gerade oder sie liegt nur online vor.\n"
               "• „Überwachter Ordnerzugriff“ (Windows-Sicherheit → Viren- & Bedrohungsschutz → "
               "Ransomware-Schutz) blockiert den PDF Editor – dort kann er als zulässige App hinzugefügt werden.")


class PasswordRequired(Exception):
    """PDF ist mit einem Passwort geschützt. doc_id: feste Dokumentkennung (für gemerkte Passwörter)."""

    def __init__(self, doc_id=None):
        super().__init__("Passwort erforderlich")
        self.doc_id = doc_id


def _encryption_method(doc):
    """Verschlüsselungsverfahren der geöffneten PDF (für das Speichern mit demselben Schutz)."""
    info = str((doc.metadata or {}).get("encryption") or "")
    if "AES" in info:
        return fitz.PDF_ENCRYPT_AES_128 if "128" in info else fitz.PDF_ENCRYPT_AES_256
    if "RC4" in info:
        return fitz.PDF_ENCRYPT_RC4_40 if "40" in info else fitz.PDF_ENCRYPT_RC4_128
    return fitz.PDF_ENCRYPT_AES_256


class FileBlocked(PermissionError):
    """Windows verweigert den Zugriff auf eine Datei (mit verständlicher Meldung)."""

    def __init__(self, path):
        super().__init__(BLOCKED_MSG.format(name=os.path.basename(path)))


def _new_page(doc, index, width, height):
    """Leere Seite einfügen (MuPDF direkt: PyMuPDFs new_page bricht mit aktivem Journal ab)."""
    pdf = mupdf.pdf_document_from_fz_document(doc.this)
    obj = mupdf.pdf_add_page(pdf, mupdf.FzRect(0, 0, width, height), 0, mupdf.pdf_new_dict(pdf, 1),
                             mupdf.fz_new_buffer(0))
    at = index if 0 <= index < doc.page_count else doc.page_count
    mupdf.pdf_insert_page(pdf, at, obj)
    doc._reset_page_refs()
    return doc[at]


def _graft_pages(doc, src, from_page, to_page, index):
    """Seiten aus src übernehmen (wie insert_pdf, das mit aktivem Journal abbricht), samt Anmerkungen.
    Links und Formularfelder werden nicht übernommen (sie verweisen auf Seiten des anderen Dokuments)."""
    pdf = mupdf.pdf_document_from_fz_document(doc.this)
    spdf = mupdf.pdf_document_from_fz_document(src.this)
    gmap = mupdf.pdf_new_graft_map(pdf)
    to_page = src.page_count - 1 if to_page < 0 else min(to_page, src.page_count - 1)
    at = index if 0 <= index < doc.page_count else doc.page_count
    for k, sp in enumerate(range(from_page, to_page + 1)):
        dest = at + k
        mupdf.pdf_graft_mapped_page(gmap, dest if dest < doc.page_count else -1, spdf, sp)
        doc._reset_page_refs()
        _graft_annots(doc, gmap, src, sp, dest)
    doc._reset_page_refs()


def _annot_refs(doc, page_xref):
    """xrefs im /Annots-Eintrag einer Seite (auch wenn das Array ein eigenes Objekt ist)."""
    kind, val = doc.xref_get_key(page_xref, "Annots")
    if kind == "xref":
        val = doc.xref_object(int(val.split()[0]), compressed=True)
    return [int(m) for m in re.findall(r"(\d+) 0 R", val or "")]


def _graft_annots(doc, gmap, src, sp, dest):
    """Anmerkungen von Seite sp (src) an Seite dest (doc) anhängen. src wird dabei verändert (Verweise auf die
    alte Seite gelöst) – nur mit einem eigens geöffneten Quelldokument aufrufen. Gibt die neuen xrefs zurück."""
    spdf = mupdf.pdf_document_from_fz_document(src.this)
    refs = []
    for x in _annot_refs(src, src[sp].xref):
        if src.xref_get_key(x, "Subtype")[1] in ("/Link", "/Popup", "/Widget"):
            continue
        # Verweise auf die alte Seite/Notiz lösen, sonst würde die ganze fremde Seite mitkopiert
        src.xref_set_key(x, "P", "null")
        src.xref_set_key(x, "Popup", "null")
        new = mupdf.pdf_graft_mapped_object(gmap, mupdf.pdf_new_indirect(spdf, x, 0))
        refs.append(mupdf.pdf_to_num(new))
    if refs:
        page_xref = doc[dest].xref
        old = _annot_refs(doc, page_xref)
        doc.xref_set_key(page_xref, "Annots", "[" + " ".join(f"{r} 0 R" for r in old + refs) + "]")
        for r in refs:
            doc.xref_set_key(r, "P", f"{page_xref} 0 R")
        doc._reset_page_refs()
    return refs


_RAW_SHIFT = (fitz.PDF_ANNOT_INK, fitz.PDF_ANNOT_LINE, fitz.PDF_ANNOT_POLY_LINE, fitz.PDF_ANNOT_POLYGON,
              fitz.PDF_ANNOT_SQUARE, fitz.PDF_ANNOT_CIRCLE, fitz.PDF_ANNOT_STAMP)


def _shift_raw(page, xref, d):
    """Verschieben direkt im PDF: Rechteck und Punktlisten um d versetzen, Erscheinungsbild bleibt (es wird auf das
    neue Rechteck abgebildet). Viel schneller als Neuerzeugen – und die xref bleibt gleich."""
    doc = page.parent
    m = page.transformation_matrix  # PDF-Koordinaten -> Seitenkoordinaten (y nach unten)
    o = fitz.Point(0, 0) * ~m
    dp = fitz.Point(d) * ~m - o  # Verschiebung in PDF-Koordinaten (y nach oben)

    def shifted(text):
        vals = [float(v) for v in _NUM.findall(text)]
        for k in range(0, len(vals) - 1, 2):
            vals[k] += dp.x
            vals[k + 1] += dp.y
        return " ".join(f"{v:g}" for v in vals)

    for key in ("Rect", "L", "Vertices", "QuadPoints", "CL"):
        kind, val = doc.xref_get_key(xref, key)
        if kind == "array":
            doc.xref_set_key(xref, key, "[" + shifted(val) + "]")
    kind, val = doc.xref_get_key(xref, "InkList")
    if kind == "array":
        strokes = re.findall(r"\[([^\[\]]*)\]", val)
        doc.xref_set_key(xref, "InkList", "[" + " ".join("[" + shifted(sv) + "]" for sv in strokes) + "]")
    return xref


def _shift_annot(page, annot, d):
    """Anmerkung um d (PDF-Seitenkoordinaten) verschieben. Gibt die (ggf. neue) xref zurück."""
    t = annot.type[0]
    if t in _RAW_SHIFT + MARKUP_TYPES and page.parent.xref_get_key(annot.xref, "AP/N")[0] == "xref":
        return _shift_raw(page, annot.xref, d)
    if t in (fitz.PDF_ANNOT_INK, fitz.PDF_ANNOT_LINE, fitz.PDF_ANNOT_POLY_LINE, fitz.PDF_ANNOT_POLYGON) + MARKUP_TYPES:
        return _recreate_transformed(page, annot, lambda x, y: (x + d.x, y + d.y))
    if t in _MEDIA_TYPES:  # Video/Audio: MuPDF kann deren Rechteck nicht setzen -> direkt im PDF
        _set_raw_rect(page.parent, page, annot.xref, annot.rect + (d.x, d.y, d.x, d.y))
        return annot.xref
    annot.set_rect(annot.rect + (d.x, d.y, d.x, d.y))
    annot.update()
    return annot.xref


def fs_path(path):
    """Windows: lange Pfade (ab 248 Zeichen, z. B. tief verschachtelte Studienordner) über das Präfix für lange
    Pfade ansprechen – sonst meldet Windows „Pfad nicht gefunden“. Kurze Pfade bleiben unverändert."""
    if os.name != "nt" or not path:
        return path
    full = os.path.abspath(path)
    if len(full) < 248 or full.startswith("\\\\?\\"):
        return path
    if full.startswith("\\\\"):  # Netzwerkpfad
        return "\\\\?\\UNC\\" + full[2:]
    return "\\\\?\\" + full


MEDIA_DESC = "PDF Editor Medien"  # markiert vom Editor eingebettete Video-/Audiodateien
_MEDIA_TYPES = (fitz.PDF_ANNOT_SCREEN, getattr(fitz, "PDF_ANNOT_RICH_MEDIA", -1), getattr(fitz, "PDF_ANNOT_MOVIE", -1))


def _pdf_text(s):
    """Python-Text als PDF-Textzeichenkette (Umlaute über UTF-16)."""
    if all(32 <= ord(c) < 127 for c in s):
        return "(" + s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ")"
    return "<FEFF" + s.encode("utf-16-be").hex().upper() + ">"


def _decode_pdf_text(v):
    v = (v or "").strip()
    if v.startswith("<") and v.endswith(">"):
        raw = bytes.fromhex(v[1:-1])
        return raw[2:].decode("utf-16-be", "replace") if raw[:2] == b"\xfe\xff" else raw.decode("latin-1")
    if v.startswith("(") and v.endswith(")"):
        return v[1:-1].replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\")
    return v


def _find_media(doc, xref, limit=40):
    """Eingebettete Mediendatei einer Anmerkung (Screen/RichMedia/Movie) suchen:
    (Datenstrom-xref, Filespec-xref, Name, MIME) oder None. Folgt Verweisen, nicht aber Seite/Eltern."""
    todo, seen = [xref], set()
    ct = ""
    found = None
    while todo and len(seen) < limit:
        x = todo.pop(0)
        if x in seen:
            continue
        seen.add(x)
        try:
            obj = doc.xref_object(x, compressed=True)
        except Exception:
            continue
        m = re.search(r"/CT\s*(\([^)]*\)|<[0-9A-Fa-f]*>)", obj)
        if m and not ct:
            ct = _decode_pdf_text(m.group(1))
        ef = re.search(r"/EF\s*<<[^>]*?/(?:F|UF)\s+(\d+)\s+0\s+R", obj)
        if ef and found is None:
            nm = re.search(r"/UF\s*(\([^)]*\)|<[0-9A-Fa-f]*>)", obj) or re.search(r"/F\s*(\([^)]*\)|<[0-9A-Fa-f]*>)", obj)
            found = (int(ef.group(1)), x, _decode_pdf_text(nm.group(1)) if nm else "")
        # weiter suchen – aber nicht über Seite, Eltern, sich selbst oder das Erscheinungsbild
        clean = re.sub(r"/(P|Parent|AN|AP|Popup|IRT)\s+\d+\s+0\s+R", "", obj)
        clean = re.sub(r"/AP\s*<<.*?>>", "", clean, flags=re.S)
        todo += [int(r) for r in re.findall(r"(\d+)\s+0\s+R", clean)]
    if found is None:
        return None
    return found[0], found[1], found[2], ct


def _set_raw_rect(doc, page, xref, rect):
    """Rechteck einer Anmerkung direkt setzen (MuPDF erlaubt das bei Bildschirm-Anmerkungen nicht über set_rect).
    rect in PDF-Seitenkoordinaten wie annot.rect."""
    r = (fitz.Rect(rect) * page.transformation_matrix).normalize()
    doc.xref_set_key(xref, "Rect", f"[{r.x0:g} {r.y0:g} {r.x1:g} {r.y1:g}]")


def _sync_media_attachments(doc):
    """Beim Speichern (in der Kopie): vom Editor eingebettete Videos/Audios zusätzlich als Dateianhänge
    eintragen – Betrachter ohne Medienwiedergabe (Browser, Sumatra …) bieten sie dann zum Öffnen an.
    Einträge gelöschter Medien fallen weg (die Datei schrumpft wieder)."""
    try:
        cat = doc.pdf_catalog()
        ours = []
        for page in doc:
            for x, t, _ in page.annot_xrefs():
                if t in _MEDIA_TYPES:
                    info = _find_media(doc, x)
                    if info and MEDIA_DESC in (doc.xref_get_key(info[1], "Desc")[1] or ""):
                        ours.append((info[2] or "Medien", info[1]))
        kind, names = doc.xref_get_key(cat, "Names")
        if kind == "null":
            if not ours:
                return
            doc.xref_set_key(cat, "Names", "<<>>")
            kind, names = doc.xref_get_key(cat, "Names")
        holder, prefix = (int(names.split()[0]), "") if kind == "xref" else (cat, "Names/")
        kind, ef = doc.xref_get_key(holder, prefix + "EmbeddedFiles")
        if kind == "xref":
            target, key = int(ef.split()[0]), "Names"
        else:
            target, key = holder, prefix + "EmbeddedFiles/Names"
        if doc.xref_get_key(target, key.replace("Names", "Kids") if key == "Names" else
                            prefix + "EmbeddedFiles/Kids")[0] != "null":
            return  # verzweigter Namensbaum: nicht anfassen
        kind, arr = doc.xref_get_key(target, key)
        pairs = re.findall(r"(\([^)]*\)|<[0-9A-Fa-f]*>)\s*(\d+)\s+0\s+R", arr if kind == "array" else "")
        keep = [(k, int(x)) for k, x in pairs if MEDIA_DESC not in (doc.xref_get_key(int(x), "Desc")[1] or "")]
        ours = [(f"(~Medien {i + 1:02d})", fs) for i, (_name, fs) in enumerate(ours)]  # Anzeigename steht im Filespec
        entries = keep + ours
        if not entries and kind == "null":
            return
        doc.xref_set_key(target, key, "[" + " ".join(f"{k} {x} 0 R" for k, x in entries) + "]")
    except Exception:
        pass  # Anhänge sind nur eine Zugabe – Speichern darf daran nicht scheitern


def _replace_file(tmp, path):
    try:
        os.replace(fs_path(tmp), fs_path(path))
    except PermissionError as e:
        try:
            os.remove(fs_path(tmp))
        except OSError:
            pass
        raise FileBlocked(path) from e


class PdfDocument(QObject):
    """Hält das geöffnete PDF.

    Normale PDFs werden aus Bytes geöffnet, damit die Datei auf der Platte nicht gesperrt ist und direkt
    überschrieben werden kann. Große PDFs (ab LARGE_FILE) liest MuPDF bei Bedarf direkt von der Platte;
    gespeichert wird dann inkrementell (nur die Änderungen werden angehängt).
    Rückgängig/Wiederholen nutzt MuPDFs Journal: Jede Änderung ist ein Schritt, gemerkt werden nur die
    geänderten Objekte (früher: Kopie des ganzen Dokuments je Schritt – bei 700 MB unbenutzbar).
    """

    # structural=True: Seiten hinzugefügt/entfernt/verschoben -> alles neu aufbauen
    changed = Signal(bool)
    # einzelne Seite hat sich geändert (z. B. Annotation)
    page_changed = Signal(int)
    modified_changed = Signal(bool)
    # vor jeder Änderung (z. B. damit ein offenes Textfeld vorher übernommen wird)
    about_to_modify = Signal()

    def __init__(self):
        super().__init__()
        self.doc: fitz.Document = fitz.open()
        self.path: str | None = None
        self.encryption = None     # Passwortschutz beim Speichern beibehalten: {method, user_pw, owner_pw, …}
        self.doc_id = None         # feste Dokumentkennung (/ID) – Schlüssel für gemerkte Passwörter
        self._modified = False
        self._backing = None       # Datei, aus der MuPDF direkt liest (große PDFs), sonst None
        self._journal = False      # Rückgängig über MuPDFs Journal verfügbar?
        self._op_open = False      # laufender Rückgängig-Schritt (bleibt bis zur nächsten Änderung offen)
        self._redo_floor = None    # Schritte ab hier nicht wiederherstellen (verworfene Textfeld-Bearbeitung)
        # Geometrie der Anmerkungen je Seite (Radierer, Antippen): einmal auslesen statt bei jedem Punkt
        self._geom: dict[int, list] = {}
        # während page_changed: betroffener Bereich (angezeigte Seitenkoordinaten) oder None = ganze Seite
        self.changed_rect = None
        self.page_changed.connect(lambda i: self._geom.pop(i, None))
        self.changed.connect(lambda _structural: self._geom.clear())
        self._media = {}  # Seite -> Video/Audio/GIF-Objekte (für die Wiedergabe)
        self._chars = {}  # Seite -> Zeichen mit Lage (Textauswahl mit der Maus); Seiteninhalt ändert sich selten
        self.changed.connect(lambda _structural: self._chars.clear())
        self.page_changed.connect(lambda i: self._media.pop(i, None))
        # zuerst verbunden: Notiz-Symbole abrunden, bevor neu gerendert wird
        self.page_changed.connect(self._round_notes)
        self.changed.connect(lambda _structural: self._media.clear())

    # ------------------------------------------------------------ Laden/Speichern
    def open(self, path: str):
        ext = os.path.splitext(path)[1].lstrip(".").lower() or "pdf"
        backing = None
        try:
            if ext == "pdf" and os.path.getsize(fs_path(path)) >= LARGE_FILE:
                doc = fitz.open(fs_path(path))  # große Datei: MuPDF liest nur, was gebraucht wird
                backing = path
            else:
                with open(fs_path(path), "rb") as f:
                    data = f.read()
                doc = fitz.open(stream=data, filetype=ext)
        except PermissionError as e:
            raise FileBlocked(path) from e
        if doc.needs_pass:
            from .passwords import doc_id_from_trailer
            doc_id = doc_id_from_trailer(doc)  # Trailer ist nicht verschlüsselt
            doc.close()
            raise PasswordRequired(doc_id)
        if not doc.is_pdf:
            # Bilder, XPS, EPUB ... in PDF umwandeln, damit wir bearbeiten können
            doc = fitz.open("pdf", doc.convert_to_pdf())
            backing = None
        repair_annot_fonts(doc)  # Textfelder aus früheren Versionen mit kaputtem Schriftverweis
        repair_struct_tree(doc)  # getaggte PDFs (z. B. aus Word): sonst Absturz beim Rendern mit Journal
        self._set_doc(doc, backing)
        self.path = path
        self.encryption = None
        from .passwords import doc_id_from_trailer
        self.doc_id = doc_id_from_trailer(doc)
        self.set_modified(False)
        self.changed.emit(True)

    def authenticate(self, path: str, password: str) -> bool:
        """Geschützte PDF mit Passwort öffnen. Bearbeitet wird entschlüsselt; beim Speichern kommt derselbe Schutz
        (Verfahren, Passwort, Rechte) wieder darauf – außer er wurde mit remove_password() entfernt."""
        with open(fs_path(path), "rb") as f:
            doc = fitz.open(stream=f.read(), filetype="pdf")
        from .passwords import doc_id_from_trailer
        doc_id = doc_id_from_trailer(doc)
        rc = doc.authenticate(password)
        if not rc:
            return False
        import secrets as _secrets
        owner = bool(rc & 4)
        user = bool(rc & 2)
        self.encryption = {
            "method": _encryption_method(doc),
            # Benutzer-Passwort (zum Öffnen). Mit dem Besitzer-Passwort geöffnet und Benutzer-Passwort unbekannt:
            # dann öffnet künftig das Besitzer-Passwort (der Schutz bleibt, aber mit diesem Passwort)
            "user_pw": password,
            # Besitzer-Passwort unbekannt: zufälliges, damit die Rechte (Drucken, Kopieren …) erhalten bleiben
            "owner_pw": password if owner else _secrets.token_urlsafe(24),
            "permissions": doc.permissions,
            "owner_only": owner and not user,
        }
        # Entschlüsselt weiterarbeiten
        doc = fitz.open("pdf", doc.tobytes(encryption=fitz.PDF_ENCRYPT_NONE))
        repair_struct_tree(doc)
        self._set_doc(doc)
        self.path = path
        self.doc_id = doc_id
        self.set_modified(False)
        self.changed.emit(True)
        return True

    def remove_password(self):
        """Passwortschutz entfernen: beim nächsten Speichern ohne Passwort."""
        if self.encryption is not None:
            self.encryption = None
            self.set_modified(True)

    def _encrypt_kwargs(self):
        e = self.encryption
        if not e:
            return {}
        return {"encryption": e["method"], "user_pw": e["user_pw"], "owner_pw": e["owner_pw"],
                "permissions": e["permissions"]}

    def new(self, fmt="A4", landscape=False, template="Leer"):
        doc = fitz.open()
        w, h = format_size(fmt, landscape)
        draw_template(_new_page(doc, 0, w, h), template)
        self._set_doc(doc)
        self.path = None
        self.encryption = None
        self.doc_id = None
        self.set_modified(False)
        self.changed.emit(True)

    @property
    def is_large(self) -> bool:
        """Direkt von der Platte gelesene (große) PDF?"""
        return self._backing is not None

    def release_file(self):
        """Große PDF: Datei freigeben (z. B. zum Umbenennen). Danach muss reopen() folgen.
        Ungespeicherte Änderungen gehen verloren – vorher speichern."""
        if self._backing is not None:
            self._end_op()
            self.doc.close()

    def reopen(self, path):
        """Nach release_file(): Datei (ggf. unter neuem Namen) wieder öffnen."""
        doc = fitz.open(fs_path(path))
        repair_struct_tree(doc)
        self.doc = doc
        self._enable_journal(doc)
        self._backing = path
        self.path = path
        self.set_modified(False)
        self.changed.emit(True)

    def save(self, path: str | None = None):
        path = path or self.path
        self._end_op()  # laufenden Rückgängig-Schritt abschließen (Journal: nicht während eines Schritts speichern)
        tmp = path + ".tmp"
        try:
            if self._backing is not None:
                self._save_large(path, tmp)
            else:
                # Aufräumen (garbage=3) nur in einer Kopie: im offenen Dokument würde MuPDF Objekte entfernen und
                # neu nummerieren, während es sich die alten Nummern merkt -> neue Textfelder zeigten danach auf
                # falsche „Schriften“ (Serifenschrift ohne Umlaute), eingefügte Bilder auf falsche Farbprofile
                copy = fitz.open("pdf", doc_bytes(self.doc))
                try:
                    _sync_media_attachments(copy)
                    data = copy.tobytes(garbage=3, deflate=True, **self._encrypt_kwargs())  # Schutz beibehalten
                finally:
                    copy.close()
                with open(fs_path(tmp), "wb") as f:
                    f.write(data)
                _replace_file(tmp, path)
        except PermissionError as e:
            if isinstance(e, FileBlocked):
                raise
            raise FileBlocked(path) from e
        self.path = path
        self.set_modified(False)

    def _save_large(self, path, tmp):
        """Große PDF: in die geöffnete Datei nur die Änderungen anhängen (Sekunden statt Minuten, kaum Speicher).
        Geht das nicht (anderer Dateiname, Datei repariert …), komplett schreiben und die neue Datei öffnen."""
        same = os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(self._backing))
        if same and self.doc.can_save_incrementally():
            self.doc.save(fs_path(path), incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
            return
        self.doc.save(fs_path(tmp))  # vollständig, ohne Aufräumen (würde im offenen Dokument Objektnummern verschieben)
        self.doc.close()    # Datei freigeben, damit sie ersetzt werden kann
        reopen = self._backing
        try:
            _replace_file(tmp, path)
            reopen = path
        finally:
            doc = fitz.open(fs_path(reopen))
            repair_struct_tree(doc)
            self.doc = doc
            self._enable_journal(doc)  # Rückgängig-Verlauf beginnt neu
            self._backing = reopen
            self.changed.emit(True)

    def reading(self):
        """Lesezugriff auf Seiten (Rendern, Text, Suche) absichern – siehe _ReadGuard."""
        return _ReadGuard(self.doc)

    def _set_doc(self, doc, backing=None):
        old = self.doc
        self.doc = doc
        self._backing = backing
        if old is not None and old is not doc:
            old.close()
        self._enable_journal(doc)

    def _enable_journal(self, doc):
        self._op_open = False
        self._redo_floor = None
        try:
            doc.journal_enable()
            self._journal = True
        except Exception:
            self._journal = False  # ohne Journal kein Rückgängig (z. B. bei beschädigten Dateien)

    # ------------------------------------------------------------ Status
    @property
    def page_count(self) -> int:
        return self.doc.page_count

    def page(self, index: int) -> fitz.Page:
        return self.doc[index]

    def is_modified(self) -> bool:
        return self._modified

    def set_modified(self, value: bool):
        if self._modified != value:
            self._modified = value
            self.modified_changed.emit(value)

    # ------------------------------------------------------------ Undo/Redo
    def checkpoint(self):
        """Vor jeder Änderung aufrufen: beginnt einen neuen Rückgängig-Schritt."""
        self.about_to_modify.emit()  # kann ein offenes Textfeld übernehmen (gehört noch zum vorigen Schritt)
        self._end_op()
        self._begin_op()
        self.set_modified(True)

    def _begin_op(self):
        if self._journal:
            self.doc.journal_start_op("Änderung")
            self._op_open = True
            self._redo_floor = None

    def _end_op(self):
        if self._op_open:
            self._op_open = False
            self.doc.journal_stop_op()

    def _ensure_op(self):
        """Änderung ohne vorherigen checkpoint() (z. B. Textfeld nach automatischem Speichern übernommen):
        MuPDF erlaubt Änderungen nur innerhalb eines Schritts."""
        if self._journal and not self._op_open:
            self._begin_op()

    def can_undo(self):
        return self._journal and (self._op_open or self.doc.journal_can_do().get("undo", False))

    def can_redo(self):
        if not self._journal or self._op_open or not self.doc.journal_can_do().get("redo", False):
            return False
        pos, _steps = self.doc.journal_position()
        return self._redo_floor is None or pos < self._redo_floor

    def drop_checkpoint(self, was_modified):
        """Letzten Schritt verwerfen (Änderung wurde ohne Wirkung abgebrochen)."""
        self._end_op()
        if self._journal and self.doc.journal_can_do().get("undo", False):
            self.doc.journal_undo()
            self._redo_floor = self.doc.journal_position()[0]  # nicht wiederherstellbar
        self.set_modified(was_modified)

    def undo(self):
        self.about_to_modify.emit()
        self._end_op()
        if not self.can_undo():
            return
        self.doc.journal_undo()
        self._after_history()

    def redo(self):
        self.about_to_modify.emit()
        self._end_op()
        if not self.can_redo():
            return
        self.doc.journal_redo()
        self._after_history()

    def _after_history(self):
        self.doc._reset_page_refs()
        self.set_modified(True)
        self.changed.emit(True)

    # ------------------------------------------------------------ Seitenoperationen
    def insert_blank_pages(self, index: int, count: int, width: float, height: float, template: str):
        self.checkpoint()
        for i in range(count):
            self._insert_blank(index + i, width, height, template)
        self.changed.emit(True)

    def _insert_blank(self, index, width, height, template):
        draw_template(_new_page(self.doc, index, width, height), template)

    def insert_pdf(self, index: int, path: str, from_page=0, to_page=-1):
        try:
            src = fitz.open(fs_path(path))
        except PermissionError as e:
            raise FileBlocked(path) from e
        if not src.is_pdf:
            src = fitz.open("pdf", src.convert_to_pdf())
        self.checkpoint()
        _graft_pages(self.doc, src, from_page, to_page, index)
        src.close()
        self.changed.emit(True)

    def insert_image_page(self, index: int, data: bytes, spec):
        """Bild als eigene Seite (Format/Einpassung/Rand/Ausschnitt laut spec, siehe image_page)."""
        img = images.load(data)
        if img.isNull():
            raise ValueError("Bild konnte nicht gelesen werden")
        if not (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"):
            data = images.encode(img)  # z. B. WebP/TIFF/BMP: als PNG/JPEG einbetten
        w, h = spec["size"]
        self.checkpoint()
        # Manche Fotos (z. B. Display-P3-Farbprofil vom Handy) kann MuPDF nicht immer laden -> prüfen und
        # notfalls ohne Farbprofil bzw. in sRGB umgerechnet einbetten
        for variant in (data, images.strip_icc(data), images.encode_srgb(img)):
            page = _new_page(self.doc, index, w, h)
            xref = image_page.draw(self.doc, page, spec, img.width(), img.height(), data=variant)
            if image_page.image_ok(self.doc, xref):
                break
            self.doc.delete_page(index)
        else:
            raise ValueError("Das Bild lässt sich nicht einbetten.")
        self.changed.emit(True)

    def image_page_spec(self, index: int):
        """Einstellungen einer Bildseite (oder None, wenn die Seite keine Bildseite ist)."""
        with self.reading():
            return image_page.get_spec(self.doc, index)

    def image_page_image(self, index: int):
        """Originalbild einer Bildseite als Bytes."""
        info = image_page.page_image(self.doc, index)
        return image_page.image_bytes(self.doc, info[0]) if info else None

    def update_image_page(self, index: int, spec):
        """Bildseite mit neuen Einstellungen neu aufbauen (Bild bleibt dasselbe, Notizen bleiben erhalten)."""
        info = image_page.page_image(self.doc, index)
        if info is None:
            return
        xref, iw, ih = info
        repaired = None
        if not image_page.image_ok(self.doc, xref):  # Bild nicht lesbar: lesbare Fassung neu einbetten
            raw = image_page.image_bytes(self.doc, xref)
            repaired = images.strip_icc(raw) if raw else None
        self.checkpoint()
        page = self.doc[index]
        page.set_rotation(0)
        image_page.clear_contents(self.doc, page)
        w, h = spec["size"]
        page.set_mediabox(fitz.Rect(0, 0, w, h))
        page = self.doc[index]
        if repaired is not None:
            image_page.draw(self.doc, page, spec, iw, ih, data=repaired)
        else:
            image_page.draw(self.doc, page, spec, iw, ih, xref=xref)
        self.changed.emit(True)

    def delete_pages(self, indices):
        indices = sorted(set(indices))
        if not indices or len(indices) >= self.page_count:
            return False
        self.checkpoint()
        self.doc.delete_pages(indices)
        self.changed.emit(True)
        return True

    def duplicate_page(self, index: int):
        self.checkpoint()
        # hinter der letzten Seite: anhängen (index + 1 wäre dort keine gültige Seitennummer)
        self.doc.fullcopy_page(index, index + 1 if index + 1 < self.doc.page_count else -1)
        self.changed.emit(True)

    def move_page(self, src: int, dst: int):
        """Verschiebt Seite src so, dass sie anschließend an Position dst steht."""
        if src == dst:
            return
        self.checkpoint()
        # pymupdf: move_page(pno, to) fügt VOR 'to' ein; -1 = ans Ende
        if dst >= self.page_count - 1:
            self.doc.move_page(src, -1)
        elif dst > src:
            self.doc.move_page(src, dst + 1)
        else:
            self.doc.move_page(src, dst)
        self.changed.emit(True)

    def rotate_pages(self, indices, degrees: int):
        self.checkpoint()
        for i in indices:
            p = self.doc[i]
            p.set_rotation((p.rotation + degrees) % 360)
        self.changed.emit(True)

    def extract_pages(self, indices, path: str):
        out = fitz.open()
        with self.reading():
            for i in sorted(indices):
                out.insert_pdf(self.doc, from_page=i, to_page=i)
        out.save(path, garbage=3, deflate=True)
        out.close()

    # ------------------------------------------------------------ Bilder
    # Bilder werden als Stempel-Annotation mit Bild eingefügt. So bleiben sie
    # verschieb-, skalier-, zuschneid- und löschbar und werden trotzdem von
    # jedem PDF-Reader angezeigt.

    def add_image(self, index: int, data: bytes, rect_display, keep_aspect=True, checkpoint=True):
        if not checkpoint:
            self._ensure_op()
        page = self.doc[index]
        img = images.load(data)
        if img.isNull():
            raise ValueError("Bild konnte nicht gelesen werden")
        r = fitz.Rect(rect_display)
        if keep_aspect:
            r = _fit_aspect(r, img.width() / img.height())
        # Seitendrehung ausgleichen, damit das Bild aufrecht erscheint
        stored = images.rotate(images.encode(img), -page.rotation)
        if checkpoint:
            self.checkpoint()
        a = page.add_stamp_annot((r * page.derotation_matrix).normalize(), stamp=stored)
        a.set_info(content="Bild", title="PDF Editor")
        a.update()
        if images.is_animated(data):  # PDF zeigt nur das erste Bild; Original für die Animation im Editor
            gx = self.doc.get_new_xref()
            self.doc.update_object(gx, "<<>>")
            self.doc.update_stream(gx, data, compress=0)
            self.doc.xref_set_key(a.xref, "PDFEditorGIF", f"{gx} 0 R")
        self._emit_page(index, [a.rect])
        return a.xref

    # ------------------------------------------------------------ Video/Audio
    def add_media(self, index, data, name, rect_display, poster):
        """Video/Audio als Bildschirm-Anmerkung (PDF-Standard) mit eingebetteter Datei und Vorschaubild einfügen.
        Alles steckt in der PDF. Gibt die xref zurück."""
        from .media import kind_of
        kind, mime = kind_of(name)
        if kind is None:
            raise ValueError("Unbekanntes Video-/Audioformat")
        self.checkpoint()
        page = self.doc[index]
        r = (fitz.Rect(rect_display) * page.derotation_matrix).normalize()
        # Vorschaubild: Erscheinungsbild eines Bild-Stempels übernehmen (Stempel selbst wieder entfernen)
        st = page.add_stamp_annot(r, stamp=images.rotate(poster, -page.rotation))
        ap = self.doc.xref_get_key(st.xref, "AP/N")[1]
        page.delete_annot(st)
        ef = self.doc.get_new_xref()
        self.doc.update_object(ef, "<<>>")
        self.doc.update_stream(ef, data, compress=0)  # Video/Audio ist schon komprimiert
        self.doc.xref_set_key(ef, "Type", "/EmbeddedFile")
        self.doc.xref_set_key(ef, "Subtype", "/" + mime.replace("/", "#2F"))
        self.doc.xref_set_key(ef, "Params", f"<</Size {len(data)}>>")
        fs = self.doc.get_new_xref()
        nm = _pdf_text(name)
        self.doc.update_object(fs, f"<</Type/Filespec/F{nm}/UF{nm}/EF<</F {ef} 0 R/UF {ef} 0 R>>"
                                   f"/Desc{_pdf_text(MEDIA_DESC)}>>")
        pp = mupdf.pdf_page_from_fz_page(page.this)
        sx = mupdf.pdf_to_num(mupdf.pdf_annot_obj(mupdf.pdf_create_annot(pp, mupdf.PDF_ANNOT_SCREEN)))
        _set_raw_rect(self.doc, page, sx, r)
        self.doc.xref_set_key(sx, "AP", f"<</N {ap}>>")
        self.doc.xref_set_key(sx, "T", nm)
        self.doc.xref_set_key(sx, "Contents", _pdf_text(("Video: " if kind == "video" else "Audio: ") + name))
        # Rendition-Aktion (so spielt auch Adobe Acrobat/Reader das Objekt ab)
        self.doc.xref_set_key(sx, "A", f"<</S/Rendition/OP 0/AN {sx} 0 R/R<</Type/Rendition/S/MR/N{nm}"
                                       f"/C<</Type/MediaClip/S/MCD/N{nm}/CT{_pdf_text(mime)}/D {fs} 0 R"
                                       f"/P<</TF(TEMPACCESS)>>>>/P<</BE<</C true>>>>>>>>")
        self.doc._reset_page_refs()
        self._emit_page(index, [r])
        return sx

    def media_annots(self, index):
        """Video/Audio-Objekte und animierte GIFs der Seite: [{"xref", "kind", "rect" (angezeigt)}]."""
        cached = self._media.get(index)
        if cached is not None:
            return cached
        out = []
        page = self.doc[index]
        rot = page.rotation_matrix
        for g in self._page_geometry(index):
            t = g["type"]
            kind = None
            if t in _MEDIA_TYPES:
                info = _find_media(self.doc, g["xref"])
                if info:
                    from .media import kind_of
                    kind = kind_of(info[2], info[3])[0]
            elif t == fitz.PDF_ANNOT_STAMP and self.doc.xref_get_key(g["xref"], "PDFEditorGIF")[0] == "xref":
                kind = "gif"
            if kind:
                out.append({"xref": g["xref"], "kind": kind, "rect": (fitz.Rect(g["rect"]) * rot).normalize()})
        self._media[index] = out
        return out

    def media_bytes(self, index, xref):
        """(Daten, Dateiname) der eingebetteten Video-/Audiodatei."""
        info = _find_media(self.doc, xref)
        if info is None:
            return None, ""
        try:
            return self.doc.xref_stream(info[0]), info[2]
        except Exception:
            return None, info[2]

    def gif_bytes(self, xref):
        kind, val = self.doc.xref_get_key(xref, "PDFEditorGIF")
        if kind != "xref":
            return None
        try:
            return self.doc.xref_stream(int(val.split()[0]))
        except Exception:
            return None

    def insert_image_on_page(self, index: int, path: str, rect_display):
        with open(fs_path(path), "rb") as f:
            return self.add_image(index, f.read(), rect_display)

    def image_bytes(self, index: int, xref: int):
        """Bilddaten einer Bild-Annotation (aufrecht, wie angezeigt) oder None."""
        page = self.doc[index]
        img_xref = self._ap_image_xref(xref)
        if img_xref is None:
            return None
        try:
            info = self.doc.extract_image(img_xref)
            data = info["image"]
            if info.get("smask"):  # Transparenz wieder anfügen
                pix = fitz.Pixmap(fitz.Pixmap(data), fitz.Pixmap(self.doc.extract_image(info["smask"])["image"]))
                data = pix.tobytes("png")
        except Exception:
            return None
        return images.rotate(data, page.rotation)

    def _ap_image_xref(self, annot_xref: int):
        """xref des Bildes im Erscheinungsbild einer Stempel-Annotation (oder None)."""
        try:
            ap = self.doc.xref_get_key(annot_xref, "AP/N")
            if ap[0] != "xref":
                return None
            ap_xref = int(ap[1].split()[0])
            xobjs = self.doc.xref_get_key(ap_xref, "Resources/XObject")[1]
            for m in re.finditer(r"(\d+) 0 R", xobjs):
                x = int(m.group(1))
                if self.doc.xref_get_key(x, "Subtype")[1] == "/Image":
                    return x
        except Exception:
            pass
        return None

    def annot_kind(self, index: int, xref: int):
        """'image', 'text' (Textfeld), 'note' (Notizzettel), 'annot' (sonstige) oder None."""
        page = self.doc[index]
        annot = _load_annot(page, xref)
        if annot is None:
            return None
        if self.is_image(annot):
            return "image"
        t = annot.type[0]
        if t in _MEDIA_TYPES and _find_media(self.doc, xref) is not None:
            return "media"
        if t == fitz.PDF_ANNOT_FREE_TEXT:
            return "text"
        if t == fitz.PDF_ANNOT_TEXT:
            return "note"
        return "annot"

    def is_image_xref(self, index: int, xref: int) -> bool:
        page = self.doc[index]  # Seite festhalten, sonst wird die Annotation ungültig
        return self.is_image(_load_annot(page, xref))

    def is_image(self, annot) -> bool:
        return (annot is not None and annot.type[0] == fitz.PDF_ANNOT_STAMP
                and self._ap_image_xref(annot.xref) is not None)

    def replace_image(self, index: int, xref: int, data: bytes, rect_display):
        """Ersetzt eine Bild-Annotation (Zuschneiden/Drehen). Gibt die neue xref zurück."""
        page = self.doc[index]
        annot = _load_annot(page, xref)
        if annot is None:
            return None
        self.checkpoint()
        page.delete_annot(annot)
        return self.add_image(index, data, rect_display, keep_aspect=False, checkpoint=False)

    def annot_rect_display(self, index: int, xref: int):
        page = self.doc[index]
        annot = _load_annot(page, xref)
        if annot is None:
            return None
        return annot.rect * page.rotation_matrix

    def resize_annotation(self, index: int, xref: int, rect_display):
        """Setzt das Rechteck einer Annotation neu; Freihand/Linien werden mitskaliert."""
        page = self.doc[index]
        annot = _load_annot(page, xref)
        if annot is None:
            return None
        new = (fitz.Rect(rect_display) * page.derotation_matrix).normalize()
        if new.width < 2 or new.height < 2:
            return xref
        self.checkpoint()
        annot = _load_annot(page, xref)
        if annot is None:
            return None
        old_rect = annot.rect
        t = annot.type[0]
        if t in (fitz.PDF_ANNOT_INK, fitz.PDF_ANNOT_LINE, fitz.PDF_ANNOT_POLY_LINE, fitz.PDF_ANNOT_POLYGON):
            # Annotationsrechteck enthält einen Rand für die Strichbreite -> Punkte innen skalieren
            v = annot.vertices
            pts = [p for s in v for p in s] if t == fitz.PDF_ANNOT_INK else v
            o = fitz.Rect(min(p[0] for p in pts), min(p[1] for p in pts),
                          max(p[0] for p in pts), max(p[1] for p in pts))
            m = max(0.0, o.x0 - annot.rect.x0)
            n = new + (m, m, -m, -m) if new.width > 2 * m + 1 and new.height > 2 * m + 1 else new
            sx = n.width / o.width if o.width else 1
            sy = n.height / o.height if o.height else 1
            fn = lambda x, y: (n.x0 + (x - o.x0) * sx, n.y0 + (y - o.y0) * sy)  # noqa: E731
            xref = _recreate_transformed(page, annot, fn)
        elif t in _MEDIA_TYPES:
            _set_raw_rect(self.doc, page, xref, new)
        else:
            annot.set_rect(new)
            annot.update()
        a = _load_annot(page, xref)
        self._emit_page(index, [old_rect, new, a.rect if a is not None else new])
        return xref

    # ------------------------------------------------------------ Annotationen
    # Alle Punkte/Rechtecke werden in *angezeigten* Seitenkoordinaten (inkl.
    # Seitendrehung) übergeben und hier in PDF-Koordinaten umgerechnet.

    def add_ink(self, index, strokes, color, width, opacity=1.0):
        page = self.doc[index]
        m = page.derotation_matrix
        pts = [[tuple(fitz.Point(x, y) * m) for x, y in s] for s in strokes if len(s) > 0]
        # Einzelpunkte (Tippen) sichtbar machen
        pts = [s if len(s) > 1 else [s[0], (s[0][0] + 0.01, s[0][1] + 0.01)] for s in pts]
        if not pts:
            return
        self.checkpoint()
        a = page.add_ink_annot(pts)
        a.set_colors(stroke=color)
        a.set_border(width=width)
        a.set_opacity(opacity)
        a.update()
        self._emit_page(index, [a.rect])
        return a.xref

    def add_shape(self, index, kind, p1, p2, color, width, fill=None, opacity=1.0, replace=None):
        """Form einfügen. replace: xref eines Freihandstrichs, den die Form ersetzt (erkannte Geste) – im selben
        Rückgängig-Schritt; „Rückgängig“ macht die Form so wieder zum Strich."""
        page = self.doc[index]
        m = page.derotation_matrix
        a_ = fitz.Point(p1) * m
        b_ = fitz.Point(p2) * m
        self.checkpoint()
        rects = []
        if replace is not None:
            old = _load_annot(page, replace)
            if old is not None:
                rects.append(old.rect)
                page.delete_annot(old)
        if kind in ("line", "arrow"):
            a = page.add_line_annot(a_, b_)
            if kind == "arrow":
                a.set_line_ends(fitz.PDF_ANNOT_LE_NONE, fitz.PDF_ANNOT_LE_OPEN_ARROW)
        else:
            r = fitz.Rect(a_, b_).normalize()
            a = page.add_rect_annot(r) if kind == "rect" else page.add_circle_annot(r)
        a.set_colors(stroke=color, fill=fill)
        a.set_border(width=width)
        if opacity < 1.0:
            a.set_opacity(opacity)
        a.update()
        self._emit_page(index, rects + [a.rect])

    def add_text(self, index, rect_display, text, fontsize, color, checkpoint=True):
        page = self.doc[index]
        if checkpoint:
            self.checkpoint()
        else:
            self._ensure_op()
        r = (fitz.Rect(rect_display) * page.derotation_matrix).normalize()
        a = page.add_freetext_annot(
            r, text, fontsize=fontsize, fontname="helv", text_color=color, rotate=page.rotation
        )
        a.update(text_color=color, fontsize=fontsize)
        repair_annot_fonts(self.doc, [a.xref])
        self.page_changed.emit(index)
        return a.xref

    # ---- Textfeld direkt auf der Seite bearbeiten
    def text_info(self, index, xref):
        """(Text, Schriftgröße, Farbe, Rechteck in Anzeigekoordinaten) eines Textfelds."""
        page = self.doc[index]
        annot = _load_annot(page, xref)
        if annot is None:
            return None
        da = self.doc.xref_get_key(xref, "DA")[1]
        m = re.search(r"([\d.]+)\s+Tf", da)
        size = float(m.group(1)) if m else 12.0
        m = re.search(r"([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+rg", da)
        color = tuple(float(v) for v in m.groups()) if m else (0.0, 0.0, 0.0)
        return annot.info.get("content", ""), size, color, annot.rect * page.rotation_matrix

    def begin_text_edit(self, index, xref):
        """Textfeld ausblenden, solange es auf der Seite bearbeitet wird. Gibt den alten Änderungsstatus zurück."""
        was_modified = self._modified
        page = self.doc[index]
        annot = _load_annot(page, xref)
        self.checkpoint()
        annot.set_flags(annot.flags | fitz.PDF_ANNOT_IS_HIDDEN)
        annot.update()
        self.page_changed.emit(index)
        return was_modified

    def end_text_edit(self, index, xref, text, rect_display, fontsize, color, unchanged, was_modified):
        self._ensure_op()
        page = self.doc[index]
        annot = _load_annot(page, xref)
        if annot is None:
            return None
        if unchanged:
            annot.set_flags(annot.flags & ~fitz.PDF_ANNOT_IS_HIDDEN)
            annot.update()
            self.drop_checkpoint(was_modified)
            self.page_changed.emit(index)
            return xref
        attached = self._attached_markups(index, annot)
        old_rect = annot.rect
        page.delete_annot(annot)
        if not text.strip():
            for x in attached:  # Text gelöscht -> seine Markierungen auch
                a = _load_annot(page, x)
                if a is not None:
                    page.delete_annot(a)
            self.page_changed.emit(index)
            return None
        new_xref = self.add_text(index, rect_display, text, fontsize, color, checkpoint=False)
        new = _load_annot(page, new_xref)
        if attached and new is not None:  # Textfeld am Griff verschoben: Markierungen wandern mit
            dx, dy = new.rect.x0 - old_rect.x0, new.rect.y0 - old_rect.y0
            if abs(dx) > 0.01 or abs(dy) > 0.01:
                rects = []
                for x in attached:
                    a = _load_annot(page, x)
                    if a is not None:
                        rects += [a.rect, a.rect + (dx, dy, dx, dy)]
                        _recreate_transformed(page, a, lambda px, py: (px + dx, py + dy))
                self._emit_page(index, rects)
        return new_xref

    def update_text(self, index, xref, text):
        page = self.doc[index]
        annot = _load_annot(page, xref)
        if annot is None:
            return
        self.checkpoint()
        annot.set_info(content=text)
        annot.update()
        self.page_changed.emit(index)

    def _round_notes(self, index):
        """Von MuPDF (neu) erzeugte Notiz-Symbole der Seite mit runden Ecken versehen. Nur während einer
        Änderung (gehört zum selben Rückgängig-Schritt) – nach Rückgängig/Wiederholen ist alles schon rund."""
        if self._journal and not self._op_open:
            return
        try:
            page = self.doc[index]
            xrefs = [x for x, t, _id in page.annot_xrefs() if t == fitz.PDF_ANNOT_TEXT]
        except Exception:
            return
        for x in xrefs:
            kind, val = self.doc.xref_get_key(x, "AP/N")
            if kind != "xref":
                continue
            ap = int(val.split()[0])
            try:
                stream = self.doc.xref_stream(ap)
            except Exception:
                continue
            if stream and _NOTE_BOX in stream:
                self.doc.update_stream(ap, stream.replace(_NOTE_BOX, _rounded_box(), 1))

    def add_note(self, index, point_display, text):
        page = self.doc[index]
        self.checkpoint()
        a = page.add_text_annot(fitz.Point(point_display) * page.derotation_matrix, text)
        a.update()
        self.page_changed.emit(index)

    # ------------------------------------------------------------ Textauswahl (wie in PDF-Betrachtern)
    def text_lines(self, index):
        """Textzeilen der Seite in Lesereihenfolge: [(Zeilenrechteck, [(Zeichenrechteck, Zeichen), ...])],
        Koordinaten wie angezeigt (Drehung berücksichtigt). Zwischengespeichert."""
        lines = self._chars.get(index)
        if lines is not None:
            return lines
        page = self.doc[index]
        rot = page.rotation_matrix
        lines = []
        with self.reading():
            # Ligaturen (ﬁ, ﬂ) aufgelöst: kopierter Text soll „fi“ enthalten
            raw = page.get_text("rawdict", flags=fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_MEDIABOX_CLIP)
        for block in raw.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                chars = []
                for span in line.get("spans", []):
                    for ch in span.get("chars", []):
                        r = (fitz.Rect(ch["bbox"]) * rot).normalize()
                        if not r.is_empty and not r.is_infinite:
                            chars.append(((r.x0, r.y0, r.x1, r.y1), ch["c"]))
                if chars:
                    lb = (fitz.Rect(line["bbox"]) * rot).normalize()
                    lines.append(((lb.x0, lb.y0, lb.x1, lb.y1), chars))
        self._chars[index] = lines
        return lines

    def text_at(self, index, point_display, pad=1.5):
        """Liegt an der Stelle Text? (für den Textcursor)"""
        x, y = point_display
        for (x0, y0, x1, y1), _chars in self.text_lines(index):
            if x0 - pad <= x <= x1 + pad and y0 - pad <= y <= y1 + pad:
                return True
        return False

    def _caret(self, lines, point):
        """Einfügestelle (Zeile, Zeichen) zum Punkt: Zeile, in der er liegt bzw. die am nächsten ist, darin das
        Zeichen nach der Mitte."""
        x, y = point
        best, best_d = None, None
        for li, ((x0, y0, x1, y1), _chars) in enumerate(lines):
            dy = 0.0 if y0 <= y <= y1 else min(abs(y - y0), abs(y - y1))
            dx = 0.0 if x0 <= x <= x1 else min(abs(x - x0), abs(x - x1))
            d = dy * 4 + dx  # senkrechter Abstand zählt stärker (Zeilen nebeneinander, z. B. Spalten)
            if best_d is None or d < best_d:
                best, best_d = li, d
        if best is None:
            return None
        chars = lines[best][1]
        for ci, ((cx0, _cy0, cx1, _cy1), _c) in enumerate(chars):
            if x < (cx0 + cx1) / 2:
                return best, ci
        return best, len(chars)

    def text_selection(self, index, a_display, b_display):
        """Auswahl zwischen zwei Punkten in Lesereihenfolge: (Rechtecke je Zeile, Text). Leer: ([], "")."""
        lines = self.text_lines(index)
        if not lines:
            return [], ""
        ca, cb = self._caret(lines, a_display), self._caret(lines, b_display)
        if ca is None or cb is None:
            return [], ""
        if cb < ca:
            ca, cb = cb, ca
        rects, parts = [], []
        for li in range(ca[0], cb[0] + 1):
            _lb, chars = lines[li]
            start = ca[1] if li == ca[0] else 0
            end = cb[1] if li == cb[0] else len(chars)
            sel = chars[start:end]
            if not sel:
                continue
            rects.append((min(c[0][0] for c in sel), min(c[0][1] for c in sel),
                          max(c[0][2] for c in sel), max(c[0][3] for c in sel)))
            parts.append("".join(c[1] for c in sel).rstrip())
        return rects, _join_accents("\n".join(parts))

    def highlight_rects(self, index, rects_display, color, kind="highlight"):
        """Textmarker (bzw. Unterstreichen/Durchstreichen) über die ausgewählten Zeilen legen."""
        if not rects_display:
            return False
        page = self.doc[index]
        m = page.derotation_matrix
        quads = [(fitz.Rect(r) * m).normalize().quad for r in rects_display]
        self.checkpoint()
        add = {"highlight": page.add_highlight_annot, "underline": page.add_underline_annot,
               "strikeout": page.add_strikeout_annot}[kind]
        a = add(quads)
        a.set_colors(stroke=color)
        a.update()
        self.page_changed.emit(index)
        return True

    def highlight_text(self, index, rect_display, color):
        """Markiert den Text, der im Rechteck liegt, als Textmarker-Annotation."""
        page = self.doc[index]
        r = fitz.Rect(rect_display) * page.derotation_matrix
        with self.reading():
            words = [fitz.Rect(w[:4]) for w in page.get_text("words") if fitz.Rect(w[:4]).intersects(r)]
        if not words:
            return False
        self.checkpoint()
        a = page.add_highlight_annot(words)
        a.set_colors(stroke=color)
        a.update()
        self.page_changed.emit(index)
        return True

    def _emit_page(self, index, rects):
        """page_changed mit dem betroffenen Bereich – nur dort muss neu gerendert werden.
        rects: Rechtecke in PDF-Seitenkoordinaten (vor und nach der Änderung)."""
        page = self.doc[index]
        u = None
        for r in rects:
            r = fitz.Rect(r)
            if r.is_empty or r.is_infinite:
                continue
            u = r if u is None else u | r
        if u is not None:
            u = (u * page.rotation_matrix).normalize() + (-3, -3, 3, 3)  # etwas Rand für Kantenglättung
        self.changed_rect = u
        try:
            self.page_changed.emit(index)
        finally:
            self.changed_rect = None

    def _page_geometry(self, index):
        """Lage aller Anmerkungen der Seite (PDF-Seitenkoordinaten), zwischengespeichert bis zur nächsten
        Änderung. Liest die Punkte direkt aus dem PDF – annot.vertices braucht für eine Seite mit vielen
        Strichen ~75 ms, und der Radierer prüft bei jeder Bewegung mehrere Punkte."""
        geom = self._geom.get(index)
        if geom is not None:
            return geom
        doc = self.doc
        page = doc[index]
        m = page.transformation_matrix
        a, b, c, d, e, f = m.a, m.b, m.c, m.d, m.e, m.f

        def pts(v):
            return [(a * v[i] + c * v[i + 1] + e, b * v[i] + d * v[i + 1] + f) for i in range(0, len(v) - 1, 2)]

        def raw(xref, key):
            kind, val = doc.xref_get_key(xref, key)
            if kind == "xref":  # indirekt gespeichert
                val = doc.xref_object(int(val.split()[0]), compressed=True)
            return val

        def nums(xref, key):
            return [float(x) for x in _NUM.findall(raw(xref, key))]

        geom = []
        for xref, t, _ in page.annot_xrefs():
            if t in (fitz.PDF_ANNOT_LINK, fitz.PDF_ANNOT_WIDGET, fitz.PDF_ANNOT_POPUP):
                continue  # wie page.annots()
            r = nums(xref, "Rect")
            if len(r) < 4:
                continue
            corners = pts(r[:4])
            xs, ys = [q[0] for q in corners], [q[1] for q in corners]
            w = nums(xref, "BS/W") or nums(xref, "Border")[2:3]
            g = {"xref": xref, "type": t, "rect": (min(xs), min(ys), max(xs), max(ys)),
                 "width": (w[0] if w and w[0] > 0 else 1.0)}
            if t == fitz.PDF_ANNOT_INK:
                g["strokes"] = [pts([float(x) for x in _NUM.findall(sv)])
                                for sv in re.findall(r"\[([^\[\]]*)\]", raw(xref, "InkList"))]
            elif t == fitz.PDF_ANNOT_LINE:
                g["verts"] = pts(nums(xref, "L"))
            elif t in (fitz.PDF_ANNOT_POLY_LINE, fitz.PDF_ANNOT_POLYGON):
                g["verts"] = pts(nums(xref, "Vertices"))
            geom.append(g)
        self._geom[index] = geom
        return geom

    def annotation_at(self, index, point_display, tolerance=4.0):
        """Liefert die oberste Annotation unter dem Punkt (oder None)."""
        page = self.doc[index]
        p = fitz.Point(point_display) * page.derotation_matrix
        found = None
        for g in self._page_geometry(index):
            if _geom_hit(g, p, tolerance):
                found = g
        # Seite festhalten: ohne Referenz wird die zurückgegebene Annotation ungültig
        self._hit_page = page
        return _load_annot(page, found["xref"]) if found is not None else None

    def delete_annotation(self, index, xref, checkpoint=True):
        page = self.doc[index]
        annot = _load_annot(page, xref)
        if annot is None:
            return
        if checkpoint:
            self.checkpoint()
            annot = _load_annot(page, xref)
            if annot is None:
                return
        else:
            self._ensure_op()
        attached = self._attached_markups(index, annot)
        rects = [annot.rect]
        page.delete_annot(annot)
        for x in attached:  # Markierungen im Textfeld gehören zum Text
            a = _load_annot(page, x)
            if a is not None:
                rects.append(a.rect)
                page.delete_annot(a)
        self._emit_page(index, rects)

    def erase_at(self, index, point_display, tolerance, checkpoint):
        """Radiergummi: löscht Annotationen unter dem Punkt. Gibt True zurück, wenn etwas gelöscht wurde.
        Formen (Rechteck, Ellipse, Vieleck) nur an ihrem Rand – im Inneren kann man radieren, ohne sie zu löschen."""
        page = self.doc[index]
        p = fitz.Point(point_display) * page.derotation_matrix
        if not any(_geom_hit(g, p, tolerance, outline=True) for g in self._page_geometry(index)):
            return False
        if checkpoint:
            self.checkpoint()  # kann ein offenes Textfeld übernehmen -> Geometrie danach neu holen
        else:
            self._ensure_op()
        geom = self._page_geometry(index)
        hits = {g["xref"] for g in geom if _geom_hit(g, p, tolerance, outline=True)}
        if not hits:
            return False
        for g in geom:
            if g["xref"] in hits and g["type"] == fitz.PDF_ANNOT_FREE_TEXT:
                hits.update(self._attached_markups(index, _load_annot(page, g["xref"])))
        rects = []
        for xref in hits:
            a = _load_annot(page, xref)
            if a is not None:
                rects.append(a.rect)
                page.delete_annot(a)
        remaining = [g for g in geom if g["xref"] not in hits]
        self._emit_page(index, rects)
        self._geom[index] = remaining  # übrige Anmerkungen sind unverändert (spart das Neuauslesen)
        return True

    def scribble_erase(self, index, points_display, tolerance, scribble=None):
        """Geste 'Durchkritzeln': löscht Zeichnungen (Freihand, Linien, Formen), die vom
        Kritzelpfad berührt werden. Bilder, Text und Notizen bleiben unangetastet.
        scribble = (Striche, Farbe, Breite, Deckkraft): das Gekritzel wird zuerst als eigener Schritt gespeichert
        und dann zusammen mit dem Durchgestrichenen gelöscht – „Rückgängig“ holt beides zurück.
        Gibt die Anzahl gelöschter Objekte zurück."""
        page = self.doc[index]
        m = page.derotation_matrix
        pts = [fitz.Point(p) * m for p in points_display]
        # Pfad fein abtasten, damit auch dünne Striche zwischen zwei Punkten getroffen werden
        samples = []
        for a, b in zip(pts, pts[1:]):
            n = max(1, int(abs(b - a) / max(1.0, tolerance / 2)))
            samples += [a + (b - a) * (k / n) for k in range(n)]
        samples += pts[-1:]
        drawing = (fitz.PDF_ANNOT_INK, fitz.PDF_ANNOT_LINE, fitz.PDF_ANNOT_POLY_LINE,
                   fitz.PDF_ANNOT_POLYGON, fitz.PDF_ANNOT_SQUARE, fitz.PDF_ANNOT_CIRCLE)
        hits = [g["xref"] for g in self._page_geometry(index)
                if g["type"] in drawing and any(_geom_hit(g, p, tolerance, outline=True) for p in samples)]
        if not hits:
            return 0
        n = len(hits)
        if scribble is not None:
            ink = self.add_ink(index, *scribble)  # eigener Schritt: das Gekritzel
            if ink is not None:
                hits.append(ink)
            page = self.doc[index]
        self.checkpoint()
        rects = []
        for xref in hits:
            a = _load_annot(page, xref)
            if a is not None:
                rects.append(a.rect)
                page.delete_annot(a)
        self._emit_page(index, rects)
        return n

    def _attached_markups(self, index, annot):
        """Textmarkierungen (Hervorheben, Unterstreichen …), die auf dem Text eines Textfelds liegen."""
        if annot is None or annot.type[0] != fitz.PDF_ANNOT_FREE_TEXT:
            return []
        box = annot.rect
        out = []
        for g in self._page_geometry(index):
            if g["type"] not in MARKUP_TYPES:
                continue
            r = fitz.Rect(g["rect"])
            inter = r & box
            if r.get_area() > 0 and not inter.is_empty and inter.get_area() >= 0.6 * r.get_area():
                out.append(g["xref"])
        return out

    def move_annotation(self, index, xref, dx_display, dy_display, checkpoint=True):
        moved = self._move_with_markups(index, xref, dx_display, dy_display, checkpoint)
        return moved[0] if moved else None

    def _move_with_markups(self, index, xref, dx_display, dy_display, checkpoint=True, emit=True):
        """Verschiebt eine Anmerkung (ein Textfeld samt seinen Textmarkierungen). Gibt die neuen xrefs zurück,
        die des verschobenen Objekts zuerst."""
        page = self.doc[index]
        annot = _load_annot(page, xref)
        if annot is None:
            return []
        if checkpoint:
            self.checkpoint()
            annot = _load_annot(page, xref)
            if annot is None:
                return []
        else:
            self._ensure_op()
        # Verschiebung in PDF-Koordinaten umrechnen (nur Drehanteil)
        d = fitz.Point(dx_display, dy_display) * page.derotation_matrix - fitz.Point(0, 0) * page.derotation_matrix
        attached = self._attached_markups(index, annot)
        rects = [annot.rect] + [fitz.Rect(g["rect"]) for g in self._page_geometry(index) if g["xref"] in attached]
        rects += [r + (d.x, d.y, d.x, d.y) for r in rects]

        out = [_shift_annot(page, annot, d)]
        for x in attached:
            a = _load_annot(page, x)
            if a is not None:
                out.append(_shift_annot(page, a, d))
        if emit:
            self._emit_page(index, rects)
        else:
            self._pending_rects.extend(rects)
        return out

    # ---- Kopieren / Einfügen / Stil der Auswahl
    def copy_annotations(self, index, xrefs):
        """Ausgewählte Anmerkungen als kleines PDF (für die Zwischenablage) – auch zwischen Dokumenten
        einfügbar; Textmarkierungen eines Textfelds kommen mit. None, wenn nichts zu kopieren ist."""
        page = self.doc[index]
        keys = set()
        for x in xrefs:
            a = _load_annot(page, x)
            if a is None:
                continue
            for b in [a] + [_load_annot(page, m) for m in self._attached_markups(index, a)]:
                if b is not None:
                    keys.add((b.type[0], tuple(round(v, 1) for v in b.rect)))
        if not keys:
            return None
        tmp = fitz.open()
        with self.reading():
            tmp.insert_pdf(self.doc, from_page=index, to_page=index, links=False, annots=True, widgets=False)
        tp = tmp[0]
        drop = [a.xref for a in tp.annots() if (a.type[0], tuple(round(v, 1) for v in a.rect)) not in keys]
        for x in drop:
            a = _load_annot(tp, x)
            if a is not None:
                tp.delete_annot(a)
        for x in tp.get_contents():  # Seiteninhalt wird nicht gebraucht (hält das Kopierte klein)
            tmp.update_stream(x, b" ")
        tmp.xref_set_key(tp.xref, "Resources", "<<>>")
        data = tmp.tobytes(garbage=3, deflate=True)
        tmp.close()
        return data

    @staticmethod
    def clip_bbox(data):
        """Umriss der kopierten Anmerkungen (Seitenkoordinaten der Ursprungsseite) oder None."""
        try:
            src = fitz.open("pdf", data)
            page = src[0]
            box = None
            for a in page.annots():
                r = a.rect * page.rotation_matrix
                box = r if box is None else box | r
            return box
        except Exception:
            return None

    def paste_annotations(self, index, data, at=None, offset=12.0):
        """Kopierte Anmerkungen auf Seite index einfügen: linke obere Ecke an at (angezeigte Seitenkoordinaten),
        sonst an der alten Stelle um offset versetzt. Gibt die neuen xrefs zurück."""
        src = fitz.open("pdf", data)
        if src.page_count < 1:
            return []
        self.checkpoint()
        pdf = mupdf.pdf_document_from_fz_document(self.doc.this)
        refs = _graft_annots(self.doc, mupdf.pdf_new_graft_map(pdf), src, 0, index)
        page = self.doc[index]
        annots = [a for a in (_load_annot(page, r) for r in refs) if a is not None]
        if not annots:
            return []
        box = None
        for a in annots:
            r = a.rect * page.rotation_matrix
            box = r if box is None else box | r
        if at is not None:
            dx, dy = at[0] - box.x0, at[1] - box.y0
        else:
            dx = dy = offset
        area = page.rect  # ganz auf der Seite lassen
        dx = max(area.x0 - box.x0, min(dx, area.x1 - box.x1)) if box.width < area.width else area.x0 - box.x0
        dy = max(area.y0 - box.y0, min(dy, area.y1 - box.y1)) if box.height < area.height else area.y0 - box.y0
        d = fitz.Point(dx, dy) * page.derotation_matrix - fitz.Point(0, 0) * page.derotation_matrix
        out, rects = [], []
        for a in annots:
            rects.append(a.rect + (d.x, d.y, d.x, d.y))
            out.append(_shift_annot(page, a, d) if (abs(d.x) > 1e-6 or abs(d.y) > 1e-6) else a.xref)
        texts = [x for x in out if self.doc.xref_get_key(x, "Subtype")[1] == "/FreeText"]
        if texts:
            repair_annot_fonts(self.doc, texts)
        self._emit_page(index, rects)
        return out

    def annot_style(self, index, xref):
        """Farbe (r, g, b), Strichbreite, Deckkraft und Schriftgröße einer Anmerkung (soweit vorhanden)."""
        page = self.doc[index]
        a = _load_annot(page, xref)
        if a is None:
            return None
        t = a.type[0]
        st = {"type": t, "color": None, "width": None, "fontsize": None,
              "opacity": a.opacity if a.opacity is not None and a.opacity >= 0 else 1.0}
        if t == fitz.PDF_ANNOT_FREE_TEXT:
            _text, size, color, _r = self.text_info(index, xref)
            st.update(color=color, fontsize=size)
        elif t != fitz.PDF_ANNOT_STAMP and t not in _MEDIA_TYPES:
            c = a.colors.get("stroke") or []
            st["color"] = tuple(c[:3]) if len(c) >= 3 else None
            if t not in MARKUP_TYPES + (fitz.PDF_ANNOT_TEXT,):
                st["width"] = (a.border or {}).get("width") or 1.0
        return st

    def set_annot_style(self, index, xrefs, color=None, width=None, opacity=None, fontsize=None):
        """Farbe/Strichbreite/Deckkraft/Schriftgröße ändern (ein Rückgängig-Schritt). Bilder bleiben unverändert."""
        page = self.doc[index]
        targets = [x for x in xrefs if (a := _load_annot(page, x)) is not None
                   and a.type[0] != fitz.PDF_ANNOT_STAMP and a.type[0] not in _MEDIA_TYPES]
        if not targets:
            return
        self.checkpoint()
        page = self.doc[index]
        bounds = (page.rect * page.derotation_matrix).normalize()
        rects = []
        for x in targets:
            a = _load_annot(page, x)
            if a is None:
                continue
            t = a.type[0]
            rects.append(a.rect)
            if t == fitz.PDF_ANNOT_FREE_TEXT:
                _text, size, col, _r = self.text_info(index, x)
                new_size = fontsize or size
                if fontsize and size and abs(fontsize - size) > 0.01:
                    k = fontsize / size  # Rahmen mitskalieren: Zeilenumbruch bleibt gleich
                    r = a.rect
                    a.set_rect(fitz.Rect(r.x0, r.y0, min(bounds.x1, r.x0 + r.width * k),
                                         min(bounds.y1, r.y0 + r.height * k)))
                if opacity is not None:
                    a.set_opacity(opacity)
                a.update(fontsize=new_size, text_color=color or col)
                repair_annot_fonts(self.doc, [x])
            else:
                if color is not None:
                    fill = a.colors.get("fill") or None
                    if t in (fitz.PDF_ANNOT_SQUARE, fitz.PDF_ANNOT_CIRCLE, fitz.PDF_ANNOT_POLYGON) and fill:
                        a.set_colors(stroke=color, fill=fill)
                    else:
                        a.set_colors(stroke=color)
                if width is not None and t not in MARKUP_TYPES + (fitz.PDF_ANNOT_TEXT,):
                    a.set_border(width=width, dashes=(a.border or {}).get("dashes") or None)
                if opacity is not None:
                    a.set_opacity(opacity)
                a.update()
            a = _load_annot(page, x)
            if a is not None:
                rects.append(a.rect)
        self._emit_page(index, rects)

    # ---- Lassoauswahl (mehrere Objekte)
    def annotations_in_lasso(self, index, polygon_display):
        """xrefs aller Objekte, die überwiegend in der Lasso-Fläche liegen (wie in OneNote)."""
        page = self.doc[index]
        m = page.derotation_matrix
        poly = [tuple(fitz.Point(p) * m) for p in polygon_display]
        found = []
        for a in page.annots():
            t = a.type[0]
            if t in (fitz.PDF_ANNOT_LINK, fitz.PDF_ANNOT_WIDGET, fitz.PDF_ANNOT_POPUP) or \
                    a.flags & fitz.PDF_ANNOT_IS_HIDDEN:
                continue
            pts = []
            if t in (fitz.PDF_ANNOT_INK, fitz.PDF_ANNOT_LINE, fitz.PDF_ANNOT_POLY_LINE, fitz.PDF_ANNOT_POLYGON):
                v = a.vertices or []
                pts = [tuple(p) for s_ in v for p in s_] if t == fitz.PDF_ANNOT_INK else [tuple(p) for p in v]
            if not pts:  # Rechteck-Objekte: Ecken, Kantenmitten und Mitte
                r = a.rect
                xs, ys = (r.x0, (r.x0 + r.x1) / 2, r.x1), (r.y0, (r.y0 + r.y1) / 2, r.y1)
                pts = [(x, y) for x in xs for y in ys]
            inside = sum(1 for p in pts if _point_in_polygon(p, poly))
            if inside >= 0.6 * len(pts):
                found.append(a.xref)
        return found

    def text_in_lasso(self, index, polygon_display):
        """Text der Wörter, deren Mitte in der Lasso-Fläche liegt (in Lesereihenfolge)."""
        page = self.doc[index]
        m = page.derotation_matrix
        poly = [tuple(fitz.Point(p) * m) for p in polygon_display]
        lines, last = [], None
        with self.reading():
            words = page.get_text("words", sort=True)
        for w in words:
            c = ((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)
            if not _point_in_polygon(c, poly):
                continue
            key = (w[5], w[6])  # Block, Zeile
            if key != last:
                lines.append([])
                last = key
            lines[-1].append(w[4])
        return "\n".join(" ".join(ws) for ws in lines)

    def move_annotations(self, index, xrefs, dx_display, dy_display):
        """Mehrere Objekte gemeinsam verschieben (ein Rückgängig-Schritt). Gibt die neuen xrefs zurück."""
        self.checkpoint()
        out = []
        moved = set()
        page = self.doc[index]
        for x in xrefs:  # Textfelder zuerst: ihre Markierungen wandern mit (und nicht doppelt)
            if self.annot_kind(index, x) == "text":
                moved.update(self._attached_markups(index, _load_annot(page, x)))
        self._pending_rects = []
        for x in sorted(xrefs, key=lambda x: self.annot_kind(index, x) != "text"):
            if x in moved:
                continue
            out += self._move_with_markups(index, x, dx_display, dy_display, checkpoint=False, emit=False)
        rects, self._pending_rects = self._pending_rects, []
        self._emit_page(index, rects)  # einmal für alle (sonst wird die Seite je Objekt neu vorbereitet)
        return out

    def delete_annotations(self, index, xrefs):
        page = self.doc[index]
        if not any(_load_annot(page, x) is not None for x in xrefs):
            return
        self.checkpoint()
        xrefs = set(xrefs)
        for x in list(xrefs):
            xrefs.update(self._attached_markups(index, _load_annot(page, x)))
        rects = []
        for x in xrefs:
            a = _load_annot(page, x)
            if a is not None:
                rects.append(a.rect)
                page.delete_annot(a)
        self._emit_page(index, rects)

    # ------------------------------------------------------------ Suche
    def search(self, text):
        results = []
        with self.reading():
            for i, page in enumerate(self.doc):
                for quad in page.search_for(text):
                    results.append((i, quad * page.rotation_matrix))
        return results


# ---------------------------------------------------------------- Hilfsfunktionen
def _point_in_polygon(p, poly):
    """Strahlverfahren: liegt Punkt p im (geschlossenen) Polygon?"""
    x, y = p
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _load_annot(page, xref):
    try:
        return page.load_annot(xref)
    except Exception:
        return None


def draw_template(page: fitz.Page, template: str):
    w, h = page.rect.width, page.rect.height
    color = (0.75, 0.82, 0.92)
    margin = 36
    if template == "Liniert":
        step = 24
        y = margin + step
        shape = page.new_shape()
        while y < h - margin:
            shape.draw_line((margin, y), (w - margin, y))
            y += step
        shape.finish(color=color, width=0.6)
        shape.commit()
    elif template == "Kariert":
        step = 14.17  # 5 mm
        shape = page.new_shape()
        x = step
        while x < w:
            shape.draw_line((x, 0), (x, h))
            x += step
        y = step
        while y < h:
            shape.draw_line((0, y), (w, y))
            y += step
        shape.finish(color=color, width=0.4)
        shape.commit()
    elif template == "Punktraster":
        step = 14.17
        shape = page.new_shape()
        y = step
        while y < h:
            x = step
            while x < w:
                shape.draw_circle((x, y), 0.6)
                x += step
            y += step
        shape.finish(color=(0.6, 0.65, 0.75), fill=(0.6, 0.65, 0.75), width=0)
        shape.commit()


def _seg_dist(p, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return ((p.x - ax) ** 2 + (p.y - ay) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((p.x - ax) * dx + (p.y - ay) * dy) / (dx * dx + dy * dy)))
    cx, cy = ax + t * dx, ay + t * dy
    return ((p.x - cx) ** 2 + (p.y - cy) ** 2) ** 0.5


_ACCENTS = {"\u00a8": "\u0308", "\u00b4": "\u0301", "`": "\u0300", "\u02dc": "\u0303"}


def _join_accents(text):
    """LaTeX-PDFs setzen Umlaute oft aus einzelnem Akzent und Buchstabe zusammen („k¨orper“) -> „körper“."""
    import unicodedata

    def fix(m):
        return unicodedata.normalize("NFC", m.group(2) + _ACCENTS[m.group(1)])
    return re.sub(r"([\u00a8\u00b4`\u02dc]) ?([A-Za-z])", fix, text)


def _geom_hit(g, p, tol, outline=False):
    """Liegt Punkt p (PDF-Seitenkoordinaten) auf der Anmerkung? g aus PdfDocument._page_geometry."""
    t = g["type"]
    tol = tol + g["width"] / 2
    x0, y0, x1, y1 = g["rect"]
    if not (x0 - tol <= p.x <= x1 + tol and y0 - tol <= p.y <= y1 + tol):
        return False  # schneller Vortest
    if t == fitz.PDF_ANNOT_INK:
        for stroke in g["strokes"]:
            if len(stroke) == 1 and _seg_dist(p, stroke[0], stroke[0]) <= tol:
                return True
            for a, b in zip(stroke, stroke[1:]):
                if _seg_dist(p, a, b) <= tol:
                    return True
        return False
    if t in (fitz.PDF_ANNOT_LINE, fitz.PDF_ANNOT_POLY_LINE):
        v = g["verts"]
        return any(_seg_dist(p, a, b) <= tol for a, b in zip(v, v[1:]))
    if outline and t == fitz.PDF_ANNOT_POLYGON and len(g.get("verts", ())) >= 2:
        v = g["verts"] + g["verts"][:1]  # geschlossen: auch die Kante zurück zum Anfang
        return any(_seg_dist(p, a, b) <= tol for a, b in zip(v, v[1:]))
    r = fitz.Rect(g["rect"])
    width = g["width"]
    if outline and t in (fitz.PDF_ANNOT_SQUARE, fitz.PDF_ANNOT_CIRCLE):
        # nur der Rand zählt (Kritzeln im Inneren eines Rahmens löscht den Rahmen nicht)
        inner = r + (width, width, -width, -width)
        if t == fitz.PDF_ANNOT_SQUARE:
            d = min(abs(p.x - inner.x0), abs(p.x - inner.x1), abs(p.y - inner.y0), abs(p.y - inner.y1))
            inside_band = inner.x0 - tol <= p.x <= inner.x1 + tol and inner.y0 - tol <= p.y <= inner.y1 + tol
            return inside_band and d <= tol
        a, b = max(inner.width / 2, 1), max(inner.height / 2, 1)
        c = (inner.tl + inner.br) / 2
        rr = (((p.x - c.x) / a) ** 2 + ((p.y - c.y) / b) ** 2) ** 0.5
        return abs(rr - 1) * min(a, b) <= tol
    return (r + (-tol, -tol, tol, tol)).contains(p)


def _recreate_transformed(page, annot, fn):
    """Erzeugt eine Vektor-Annotation neu, wobei jeder Punkt durch fn(x, y) abgebildet wird."""
    t = annot.type[0]
    colors = annot.colors
    border = annot.border
    opacity = annot.opacity
    line_ends = annot.line_ends
    verts = annot.vertices
    page.delete_annot(annot)
    if t == fitz.PDF_ANNOT_INK:
        new = page.add_ink_annot([[fn(x, y) for x, y in s] for s in verts])
    elif t == fitz.PDF_ANNOT_LINE:
        new = page.add_line_annot(fn(*verts[0]), fn(*verts[1]))
        if line_ends:
            new.set_line_ends(*line_ends)
    elif t == fitz.PDF_ANNOT_POLY_LINE:
        new = page.add_polyline_annot([fn(x, y) for x, y in verts])
    elif t in MARKUP_TYPES:
        quads = [fitz.Quad(*[fn(*verts[i + k]) for k in range(4)]) for i in range(0, len(verts) - 3, 4)]
        add = {fitz.PDF_ANNOT_HIGHLIGHT: page.add_highlight_annot, fitz.PDF_ANNOT_UNDERLINE: page.add_underline_annot,
               fitz.PDF_ANNOT_STRIKE_OUT: page.add_strikeout_annot, fitz.PDF_ANNOT_SQUIGGLY: page.add_squiggly_annot}[t]
        new = add(quads=quads)
        new.set_colors(stroke=colors.get("stroke"))
        if opacity is not None and opacity >= 0:
            new.set_opacity(opacity)
        new.update()
        return new.xref
    else:
        new = page.add_polygon_annot([fn(x, y) for x, y in verts])
    new.set_colors(stroke=colors.get("stroke"), fill=colors.get("fill"))
    if border:
        new.set_border(width=border.get("width", 1), dashes=border.get("dashes") or None)
    if opacity is not None and opacity >= 0:
        new.set_opacity(opacity)
    new.update()
    return new.xref


def _fit_aspect(r: fitz.Rect, aspect: float) -> fitz.Rect:
    """Größtes Rechteck mit Seitenverhältnis aspect (b/h) in r, oben links ausgerichtet."""
    if r.width / r.height > aspect:
        return fitz.Rect(r.x0, r.y0, r.x0 + r.height * aspect, r.y1)
    return fitz.Rect(r.x0, r.y0, r.x1, r.y0 + r.width / aspect)
