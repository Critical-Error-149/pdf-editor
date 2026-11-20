"""Striche (Freihand-Anmerkungen) direkt auf der Grafikkarte (nur Grafikkarten-Ansicht).

Statt Striche von MuPDF in die Kacheln rastern zu lassen, werden sie als Dreiecke gezeichnet: bei jeder Zoomstufe
sofort scharf, und nach einem neuen oder gelöschten Strich muss keine Kachel neu gerastert werden. Gezeichnet wird
genau, was MuPDF zeichnen würde: die Linienstücke aus dem Erscheinungsbild (Appearance Stream), das MuPDF beim Anlegen
schreibt – gerade Stücke, runde Enden und Ecken, eine Strichbreite, eine Farbe, eine Deckkraft.

Nur Striche, deren Erscheinungsbild genau diesem Muster folgt, kommen auf die Grafikkarte (fremde Striche mit eigenem
Erscheinungsbild, z. B. mit wechselnder Breite, bleiben bei MuPDF). Ebenso Striche, über denen eine spätere andere
Anmerkung liegt – die Grafikkarte zeichnet Striche über den Kacheln, die Reihenfolge stimmte sonst nicht.

Deckkraft: Jede Anmerkung bekommt eine eigene Tiefe; mit dem Tiefentest „ungleich“ deckt ein Strich jeden Pixel nur
einmal (Überlappungen innerhalb eines Strichs werden nicht dunkler), zwei verschiedene Striche übereinander aber
schon – wie im PDF. Darum werden pro Strich erst die Kernflächen, dann die weichen Ränder gezeichnet.

Form je Strich (einmal berechnet, in Seitenkoordinaten): Linienzug mit Gehrung an flachen Knicken, an scharfen Knicken
und an den Enden runde Scheiben. Jede Ecke trägt Mittelpunkt, Auslenkung (halbe Breite), Saum-Kennung, Tiefe und
Farbe; den Saum (1 Gerätepixel) rechnet der Shader (shaders/ink.vert).
"""
from __future__ import annotations

import hashlib
import math
import re
import time
from array import array

import pymupdf as fitz
from PySide6.QtCore import QTimer

FLOATS = 10                 # Mittelpunkt (2), Auslenkung (2), Saum + Tiefe (2), Farbe (4)
SHARP = math.cos(math.radians(35))   # stärker geknickt: runde Scheibe statt Gehrung
_TOKEN = re.compile(rb"/[^\s/\[\]()<>{}%]*|[-+]?(?:\d+\.?\d*|\.\d+)|[A-Za-z*'\"]+")
_ALLOWED = {b"q", b"Q", b"gs", b"w", b"RG", b"G", b"J", b"j", b"m", b"l", b"S"}


def _parse_ap(data: bytes):
    """Erscheinungsbild „q /H gs w RG J j (m l…)+ S Q“ zerlegen → (Breite, Farbe, [Linienzüge]) oder None."""
    nums, width, color, cap, join = [], None, None, None, None
    paths, cur = [], None
    for tok in _TOKEN.findall(data):
        c = tok[:1]
        if c == b"/":
            continue
        if c.isdigit() or c in b"-+.":
            nums.append(float(tok))
            continue
        if tok not in _ALLOWED:
            return None
        if tok == b"w" and len(nums) == 1:
            width = nums[0]
        elif tok == b"RG" and len(nums) == 3:
            color = tuple(nums)
        elif tok == b"G" and len(nums) == 1:
            color = (nums[0],) * 3
        elif tok == b"J" and len(nums) == 1:
            cap = nums[0]
        elif tok == b"j" and len(nums) == 1:
            join = nums[0]
        elif tok == b"m" and len(nums) == 2:
            cur = [(nums[0], nums[1])]
            paths.append(cur)
        elif tok == b"l" and len(nums) == 2 and cur is not None:
            cur.append((nums[0], nums[1]))
        elif tok == b"S" and not nums:
            if cur is None:
                return None
            cur = None
        elif tok in (b"q", b"Q") and not nums:
            pass
        elif tok == b"gs" and not nums:
            pass
        else:
            return None
        nums = []
    if nums or width is None or color is None or cap != 1 or join != 1 or not paths or cur is not None:
        return None
    return width, color, paths


_REF = re.compile(r"(\d+) 0 R")
_CA = re.compile(r"/CA\s+([\d.]+)")


def _gs_text(pdf, apx) -> str:
    """Grafikzustände (ExtGState) des Erscheinungsbilds als Text (aufgelöste Verweise eingeschlossen)."""
    kind, value = pdf.xref_get_key(apx, "Resources/ExtGState")
    if kind == "null":
        return ""
    texts = [pdf.xref_object(int(value.split()[0])) if kind == "xref" else value]
    for ref in _REF.findall(texts[0]):
        texts.append(pdf.xref_object(int(ref)))
    return "\n".join(texts)


def _stroke_alpha(states: str):
    """Deckkraft der Striche laut Grafikzustand (so zeichnet MuPDF) – None bei Mischmodus/weicher Maske."""
    if re.search(r"/SMask\s*(?!/None)\S|/BM\s*/(?!Normal|Compatible)", states):
        return None
    values = {float(v) for v in _CA.findall(states)}
    if len(values) > 1:
        return None
    return max(0.0, min(1.0, values.pop())) if values else 1.0


_CIRCLES = {}


def _circle(n):
    """Einheitskreis mit n Abschnitten (n + 1 Punkte, erster = letzter)."""
    c = _CIRCLES.get(n)
    if c is None:
        c = _CIRCLES[n] = [(math.cos(2 * math.pi * i / n), math.sin(2 * math.pi * i / n)) for i in range(n + 1)]
    return c


def _mesh(paths, hw, z, rgba):
    """Dreiecke eines Strichs: (Kern, Saum) als float-Arrays. Je Abschnitt ein einziger extend-Aufruf – in Python
    zählt jeder Funktionsaufruf (so etwa dreimal schneller als Ecke für Ecke)."""
    core, fringe = [], []    # erst Listen, am Ende einmal in float-Arrays (array("f", liste) ist am schnellsten)
    r, g, b, a = rgba

    def disc(cx, cy):
        circ = _circle(12 if hw < 1.5 else 16 if hw < 3 else 24)
        cv, fv = [], []
        px_, py_ = circ[0][0] * hw, circ[0][1] * hw
        for ux, uy in circ[1:]:
            ex, ey = ux * hw, uy * hw
            cv += (cx, cy, 0.0, 0.0, 0.0, z, r, g, b, a,
                   cx, cy, px_, py_, 0.0, z, r, g, b, a,
                   cx, cy, ex, ey, 0.0, z, r, g, b, a)
            fv += (cx, cy, px_, py_, 0.0, z, r, g, b, a,
                   cx, cy, px_, py_, 1.0, z, r, g, b, a,
                   cx, cy, ex, ey, 0.0, z, r, g, b, a,
                   cx, cy, ex, ey, 0.0, z, r, g, b, a,
                   cx, cy, px_, py_, 1.0, z, r, g, b, a,
                   cx, cy, ex, ey, 1.0, z, r, g, b, a)
            px_, py_ = ex, ey
        core.extend(cv)
        fringe.extend(fv)

    def run(pts, normals):
        """Linienzug ohne scharfe Knicke: Gehrung an den inneren Punkten."""
        last = len(pts) - 1
        offs = [(normals[0][0] * hw, normals[0][1] * hw)]
        for i in range(1, last):
            (ax, ay), (bx, by) = normals[i - 1], normals[i]
            mx, my = ax + bx, ay + by
            ml = math.hypot(mx, my)
            if ml < 1e-9:
                offs.append((ax * hw, ay * hw))
                continue
            mx, my = mx / ml, my / ml
            k = hw / max(mx * ax + my * ay, 0.5)
            offs.append((mx * k, my * k))
        offs.append((normals[-1][0] * hw, normals[-1][1] * hw))
        cv, fv = [], []
        for i in range(last):
            x0, y0 = pts[i]
            x1, y1 = pts[i + 1]
            e0x, e0y = offs[i]
            e1x, e1y = offs[i + 1]
            # Kern: Viereck zwischen den beiden Kanten
            cv += (x0, y0, e0x, e0y, 0.0, z, r, g, b, a,
                   x0, y0, -e0x, -e0y, 0.0, z, r, g, b, a,
                   x1, y1, e1x, e1y, 0.0, z, r, g, b, a,
                   x1, y1, e1x, e1y, 0.0, z, r, g, b, a,
                   x0, y0, -e0x, -e0y, 0.0, z, r, g, b, a,
                   x1, y1, -e1x, -e1y, 0.0, z, r, g, b, a)
            # Saum auf beiden Seiten
            fv += (x0, y0, e0x, e0y, 0.0, z, r, g, b, a,
                   x0, y0, e0x, e0y, 1.0, z, r, g, b, a,
                   x1, y1, e1x, e1y, 0.0, z, r, g, b, a,
                   x1, y1, e1x, e1y, 0.0, z, r, g, b, a,
                   x0, y0, e0x, e0y, 1.0, z, r, g, b, a,
                   x1, y1, e1x, e1y, 1.0, z, r, g, b, a,
                   x0, y0, -e0x, -e0y, 0.0, z, r, g, b, a,
                   x0, y0, -e0x, -e0y, 1.0, z, r, g, b, a,
                   x1, y1, -e1x, -e1y, 0.0, z, r, g, b, a,
                   x1, y1, -e1x, -e1y, 0.0, z, r, g, b, a,
                   x0, y0, -e0x, -e0y, 1.0, z, r, g, b, a,
                   x1, y1, -e1x, -e1y, 1.0, z, r, g, b, a)
        core.extend(cv)
        fringe.extend(fv)

    for raw in paths:
        pts = [raw[0]]
        for p in raw[1:]:
            if abs(p[0] - pts[-1][0]) > 1e-6 or abs(p[1] - pts[-1][1]) > 1e-6:
                pts.append(p)
        disc(*pts[0])
        if len(pts) == 1:
            continue
        normals = []
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            dx, dy = x1 - x0, y1 - y0
            ln = math.hypot(dx, dy)
            normals.append((-dy / ln, dx / ln))
        start = 0
        for i in range(1, len(pts) - 1):
            (ax, ay), (bx, by) = normals[i - 1], normals[i]
            if ax * bx + ay * by < SHARP:  # scharfer Knick: Linienzug teilen, runde Ecke
                run(pts[start:i + 1], normals[start:i])
                disc(*pts[i])
                start = i
        run(pts[start:], normals[start:])
        disc(*pts[-1])
    return array("f", core), array("f", fringe)


class InkLayer:
    """Welche Striche einer Seite zeichnet die Grafikkarte, und mit welchen Dreiecken?

    Zwei Stufen je Seite (zwischengespeichert bis zur nächsten Änderung an ihren Anmerkungen):
      * Plan: welche Striche (Erscheinungsbild zerlegt, Seitenkoordinaten) – billig; braucht auch der Renderer, um
        sie in den Kacheln wegzulassen
      * Dreiecke: je Strich einmal berechnet (~1,4 ms für 100 Punkte) – für Seiten knapp außerhalb der Ansicht
        vorab in kleinen Zeitscheiben (prepare), sonst spätestens beim ersten Zeichnen"""

    PREPARE_MS = 4

    def __init__(self, document, renderer, parent=None):
        self.document = document
        self.renderer = renderer
        self.enabled = True
        self._plans = {}      # Seite -> (Schlüssel, [Strich-Plan])
        self._pages = {}      # Seite -> (Schlüssel, frozenset(xrefs), bytes, Ecken)
        self._parsed = {}     # Strich-Schlüssel -> Plan-Eintrag oder None (nicht geeignet)
        self._sigs = {}       # Seite -> Fingerabdruck dessen, was MuPDF in die Kacheln zeichnet (tiles_unchanged)
        self._meshes = {}     # Strich-Schlüssel -> bytes
        self._todo = []
        self._timer = QTimer(parent)
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._work)

    def clear(self):
        self._plans.clear()
        self._pages.clear()
        self._sigs.clear()
        self._parsed.clear()
        self._meshes.clear()
        self._todo.clear()

    def _key(self, index):
        r = self.renderer
        return (r.gen[index], r.hidden.get(index), r.epoch)

    def xrefs(self, index) -> frozenset:
        """Striche dieser Seite, die die Grafikkarte zeichnet (MuPDF lässt sie in den Kacheln weg)."""
        if not self.enabled:
            return frozenset()
        return frozenset(item["xref"] for item in self._plan(index))

    def page(self, index):
        """(Schlüssel, xrefs, Dreiecke als bytes, Anzahl Ecken) der Seite."""
        key = self._key(index)
        cached = self._pages.get(index)
        if cached is not None and cached[0] == key:
            return cached
        plan = self._plan(index)
        parts = [self._mesh(item) for item in plan]
        data = b"".join(parts)
        entry = (key, frozenset(item["xref"] for item in plan), data, len(data) // (FLOATS * 4))
        self._pages[index] = entry
        return entry

    def prepare(self, index):
        """Dreiecke der Seite in der Leerlaufzeit vorbereiten (Seite gleich sichtbar)."""
        if not self.enabled or index in self._todo:
            return
        cached = self._pages.get(index)
        if cached is not None and cached[0] == self._key(index):
            return
        self._todo.append(index)
        if not self._timer.isActive():
            self._timer.start()

    def _work(self):
        deadline = time.perf_counter() + self.PREPARE_MS / 1000
        while self._todo and time.perf_counter() < deadline:
            index = self._todo[-1]
            if index >= self.document.page_count:
                self._todo.pop()
                continue
            for item in self._plan(index):
                if item["key"] not in self._meshes:
                    self._mesh(item)
                    if time.perf_counter() >= deadline:
                        break
            else:
                self._todo.pop()
        if self._todo:
            self._timer.start()

    # ------------------------------------------------------------ Plan
    def _plan(self, index):
        key = self._key(index)
        cached = self._plans.get(index)
        if cached is not None and cached[0] == key:
            return cached[1]
        try:
            with self.document.reading():
                plan, sig = self._make_plan(index)
        except Exception:
            plan, sig = [], None
        self._plans[index] = (key, plan)
        self._sigs[index] = sig
        return plan

    def tiles_unchanged(self, index) -> bool:
        """Nach einer Änderung an der Seite: Ist alles, was in den Kacheln steht (Seiteninhalt, Anmerkungen, die
        MuPDF zeichnet), gleich geblieben – betraf die Änderung also nur Striche der Grafikkarte? Dann müssen keine
        Kacheln neu gerastert werden. Vergleicht mit dem Stand beim letzten Plan (vor der Änderung)."""
        old = self._sigs.get(index)
        if old is None or not self.enabled:
            return False
        try:
            with self.document.reading():
                _plan, new = self._make_plan(index)
        except Exception:
            return False
        return new == old

    def _make_plan(self, index):
        doc = self.document
        pdf = doc.doc
        page = doc.page(index)
        hidden = self.renderer.hidden.get(index) or frozenset()
        to_page = page.transformation_matrix * page.rotation_matrix
        annots = list(page.annots())
        chosen = []          # (Position in der Reihenfolge, xref, Rechteck, Erscheinungsbild-xref)
        others = []          # (Position, Rechteck) der übrigen sichtbaren Anmerkungen
        for pos, a in enumerate(annots):
            if a.flags & (fitz.PDF_ANNOT_IS_HIDDEN | fitz.PDF_ANNOT_IS_NO_VIEW) or a.xref in hidden:
                continue
            if a.type[0] == fitz.PDF_ANNOT_INK:
                kind, value = pdf.xref_get_key(a.xref, "AP/N")
                if kind == "xref":
                    chosen.append((pos, a.xref, a.rect, int(value.split()[0])))
                    continue
            others.append((pos, a.rect))
        for w in page.widgets():
            others.append((len(annots), w.rect))
        plan = []
        gpu = set()
        for pos, xref, rect, apx in chosen:
            # liegt eine spätere andere Anmerkung darüber? Dann zeichnet MuPDF den Strich (Reihenfolge)
            if any(p > pos and rect.intersects(orect) for p, orect in others):
                continue
            item = self._parse(pdf, xref, apx, to_page)
            if item is not None:
                plan.append(item)
                gpu.add(xref)
        # Fingerabdruck der Kacheln: Inhaltsströme (xref + gespeicherte Länge – ohne sie zu entpacken), Drehung,
        # Größe, und jede Anmerkung, die MuPDF zeichnet (mit ihrem Erscheinungsbild)
        contents = tuple((x, pdf.xref_get_key(x, "Length")[1]) for x in page.get_contents())
        drawn = []
        for a in annots + list(page.widgets()):
            if a.xref in gpu or a.xref in hidden:
                continue
            kind, value = pdf.xref_get_key(a.xref, "AP/N")
            ap = hashlib.blake2b(pdf.xref_stream(int(value.split()[0])), digest_size=8).digest()                 if kind == "xref" else value
            drawn.append((a.xref, tuple(a.rect), a.flags, ap))
        sig = (contents, page.rotation, tuple(page.rect), tuple(drawn))
        return plan, sig

    def _parse(self, pdf, xref, apx, to_page):
        """Erscheinungsbild prüfen und zerlegen → Plan-Eintrag (zwischengespeichert je Inhalt) oder None."""
        kind, value = pdf.xref_get_key(apx, "Matrix")
        if kind != "null" and value.replace(" ", "") != "[100100]":
            return None
        if pdf.xref_get_key(apx, "BBox")[1] != pdf.xref_get_key(xref, "Rect")[1]:
            return None  # MuPDF würde das Erscheinungsbild auf das Rechteck verzerren
        data = pdf.xref_stream(apx)
        states = _gs_text(pdf, apx)
        key = (xref, hashlib.blake2b(data + states.encode(), digest_size=16).digest(), tuple(to_page))
        if key in self._parsed:
            return self._parsed[key]
        parsed = _parse_ap(data)
        alpha = _stroke_alpha(states) if parsed is not None else None
        item = None
        if parsed is not None and alpha is not None:
            width, (cr, cg, cb), paths = parsed
            item = {"key": key, "xref": xref, "hw": width / 2,
                    "paths": [[tuple(fitz.Point(x, y) * to_page) for x, y in p] for p in paths],
                    # Tiefe = Kennung der Anmerkung (eindeutig je Dokument)
                    "z": ((xref % 4_000_000) + 1) / 4_194_304,
                    "rgba": (cr * alpha, cg * alpha, cb * alpha, alpha)}
        if len(self._parsed) > 20000:
            self._parsed.clear()
        self._parsed[key] = item
        return item

    def _mesh(self, item):
        mesh = self._meshes.get(item["key"])
        if mesh is None:
            core, fringe = _mesh(item["paths"], item["hw"], item["z"], item["rgba"])
            mesh = core.tobytes() + fringe.tobytes()
            if len(self._meshes) > 20000:
                self._meshes.clear()
            self._meshes[item["key"]] = mesh
        return mesh
