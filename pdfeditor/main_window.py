"""Hauptfenster: Menüs, Werkzeugleisten, Seitenleiste und Statusleiste."""
from __future__ import annotations

import os
import sys

import pymupdf as fitz
from PySide6.QtCore import QSettings, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import (QAction, QActionGroup, QColor, QDesktopServices, QImage, QKeySequence,
                           QPalette)
from PySide6.QtWidgets import (QApplication, QFileDialog, QInputDialog, QLabel,
                               QHBoxLayout, QLineEdit, QMainWindow, QMenu, QMessageBox, QSizePolicy, QSpinBox,
                               QStackedWidget, QToolBar, QToolButton, QWidget, QWidgetAction)

from . import APP_NAME, __version__
from . import presets as P
from .autosave import Autosave, AutosaveDialog, FILE_MODES
from .file_browser import FileBrowser
from .side_panel import CollapsiblePanel, GripSplitter
from . import icons, prefs, theme
from .ribbon import Ribbon, RibbonButton
from .widgets import ValueButton

from .document import PAGE_FORMATS, PasswordRequired, PdfDocument, fs_path
from . import images
from .document import PAGE_TEMPLATES
from .image_page import ImagePageDialog, default_spec
from .insert_dialog import InsertPagesDialog
from .settings_dialog import SettingsDialog, scan_batch_enabled, start_panels
from .page_view import PageView
from .tabs import DocTab, EditorArea
from .thumbnails import ThumbnailPanel

OPEN_FILTER = ("Dokumente (*.pdf *.xps *.oxps *.epub *.cbz *.fb2 *.png *.jpg *.jpeg *.tif *.tiff *.bmp);;"
               "PDF (*.pdf);;Alle Dateien (*)")

TOOL_DEFS = [
    # id, Beschriftung, Tastenkürzel, Tooltip
    ("select", "Lassoauswahl", "V", "Objekte umfahren und auswählen, Antippen wählt ein einzelnes Objekt"),
    ("pan", "Maus", "H", "Links anklicken; Ziehen verschiebt die Ansicht"),
    ("pen", "Stift", "P", "Freihand schreiben und zeichnen"),
    ("highlighter", "Marker", "M", "Halbtransparenter Freihand-Marker"),
    ("markup", "Markieren", "U", "Text im Bereich hervorheben"),
    ("eraser", "Radierer", "E", "Annotationen wegradieren (auch Stift-Radierer)"),
    ("text", "Text", "T", "Textfeld einfügen"),
    ("note", "Notiz", "N", "Notizzettel einfügen"),
    ("line", "Linie", "L", "Linie zeichnen"),
    ("arrow", "Pfeil", "A", "Pfeil zeichnen"),
    ("rect", "Rechteck", "R", "Rechteck zeichnen"),
    ("ellipse", "Ellipse", "O", "Ellipse zeichnen"),
    ("image", "Bild", "I", "Bild auf Seite platzieren"),
]

SHAPE_TOOLS = ("line", "arrow", "rect", "ellipse")

DEFAULTS = {
    "pen": ("#1f4fd1", 1.5),
    "highlighter": ("#f2c200", 12.0),
    "markup": ("#f2c200", 1.0),
    "eraser": ("#000000", 12.0),  # Durchmesser des Radierer-Kreises (wie die Strichstärke beim Stift)
    "text": ("#000000", 1.0),
    "line": ("#d92b2b", 2.0),
    "arrow": ("#d92b2b", 2.0),
    "rect": ("#d92b2b", 2.0),
    "ellipse": ("#d92b2b", 2.0),
}


def _no_index(index):
    """Kein Seitenindex übergeben? (QAction.triggered liefert ein bool 'checked')"""
    return index is None or isinstance(index, bool)


class ToolSettings:
    """Farbe/Stärke je Werkzeug, dauerhaft in QSettings gespeichert."""

    def __init__(self, qsettings: QSettings):
        self.qs = qsettings
        self.font_size = int(qsettings.value("font_size", 12))
        self.fill_shapes = qsettings.value("fill_shapes", "false") == "true"
        # Formen übernehmen Farbe/Dicke des zuletzt gewählten Stifts (liefert ein Preset-dict)
        self.shape_style = None

    def _shape_preset(self, tool):
        if tool in SHAPE_TOOLS and self.shape_style is not None:
            return self.shape_style()
        return None

    def color_hex(self, tool):
        p = self._shape_preset(tool)
        if p is not None:
            return p["color"]
        return self.qs.value(f"tool/{tool}/color", DEFAULTS.get(tool, ("#000000", 1))[0])

    def color(self, tool):
        c = QColor(self.color_hex(tool))
        return (c.redF(), c.greenF(), c.blueF())

    def set_color(self, tool, hexcolor):
        self.qs.setValue(f"tool/{tool}/color", hexcolor)

    def width(self, tool):
        p = self._shape_preset(tool)
        if p is not None:
            return float(p["width"])
        return float(self.qs.value(f"tool/{tool}/width", DEFAULTS.get(tool, ("#000000", 1.0))[1]))

    def opacity(self, tool):
        """Marker (und Formen mit Marker-Eigenschaften) sind halbtransparent."""
        p = self._shape_preset(tool)
        kind = p["kind"] if p is not None else tool
        return 0.4 if kind == "highlighter" else 1.0

    def set_width(self, tool, w):
        self.qs.setValue(f"tool/{tool}/width", w)

    def fill(self):
        return None  # Formen werden ungefüllt gezeichnet

    def save(self):
        self.qs.setValue("font_size", self.font_size)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.qs = QSettings("PDFEditor", "PDFEditor")
        self.settings = ToolSettings(self.qs)
        self.tab: DocTab | None = None  # aktiver Tab (Dokument + Ansicht)
        self._tool = "pan"
        self.last_template = self.qs.value("last_template", "Leer")
        self._search_results = []
        self._search_index = -1
        self._search_text = ""
        self.presets = P.load_presets(self.qs)
        self.active_preset: int | None = None
        self._last_preset = {}
        self._shape_preset: int | None = None  # Stift, dessen Eigenschaften Formen bekommen
        self.preset_actions: list[QAction] = []
        self._focus_mode = False
        self._pre_focus = {}

        self.settings.shape_style = self._shape_style
        self.files = FileBrowser(self.qs)
        self.files.open_requested.connect(self.open_file)
        self.files.reopen_requested.connect(self.open_file_again)
        self.files.rename_requested.connect(self.rename_file)
        self.files.is_open = lambda p: self._find_tab(p) is not None
        self.thumbs_stack = QStackedWidget()  # Miniaturen des jeweils aktiven Tabs
        self.editor = EditorArea()
        self.editor.current_changed.connect(self._on_tab_activated)
        self.editor.close_requested.connect(self.close_tab)
        self.editor.duplicate_requested.connect(self.duplicate_tab)
        self.editor.rename_requested.connect(lambda t: self.rename_file(t.document.path))
        self.editor.open_requested.connect(self.open_dialog)
        self.editor.layout_changed.connect(self._on_tabs_layout_changed)
        # Zoomanzeige gesammelt aktualisieren (während des Zoomens würde jedes Neuzeichnen ein Bild kosten)
        self._zoom_label_timer = QTimer(self)
        self._zoom_label_timer.setSingleShot(True)
        self._zoom_label_timer.setInterval(150)
        self._zoom_label_timer.timeout.connect(lambda: self.zoom_label.setText(f"{self.view.zoom * 100:.0f} %"))
        first = self._create_tab()
        self.tab = first  # schon beim Aufbau der Leisten verfügbar (angezeigt wird er erst unten)
        # Seitenleisten; eingeklappt bleibt je ein Knopf links in der Tableiste (Dateien links, Seiten daneben)
        self.files_panel = CollapsiblePanel(icons.icon("panel_files"), "Dateien", self.files, 260, "F3")
        self.thumbs_panel = CollapsiblePanel(icons.icon("panel_pages"), "Seiten", self.thumbs_stack, 200, "F4")
        self.editor.set_leading_widgets([self.files_panel.expand_button, self.thumbs_panel.expand_button])
        for panel in (self.files_panel, self.thumbs_panel):
            panel.visibility_changed.connect(self._place_panel_buttons)
        splitter = GripSplitter()
        splitter.addWidget(self.files_panel)
        splitter.addWidget(self.thumbs_panel)
        splitter.addWidget(self.editor)
        splitter.setStretchFactor(2, 1)
        # zuletzt eingestellte Leistenbreiten (auch über einen Neustart hinweg)
        fw = self._saved_width("files_width", 260, self.files_panel)
        tw = self._saved_width("thumbs_width", 200, self.thumbs_panel)
        self.files_panel._expanded_width, self.thumbs_panel._expanded_width = fw, tw
        splitter.setSizes([fw, tw, 1000])
        splitter.splitterMoved.connect(lambda *_: self._remember_panel_widths())
        self.splitter = splitter
        self.setCentralWidget(splitter)

        self._build_actions()
        self._build_menus()
        self._build_toolbars()
        self._build_statusbar()

        theme.apply(QApplication.instance())
        self.setAcceptDrops(True)
        self.resize(1300, 900)
        geo = self.qs.value("geometry")
        if geo:
            self.restoreGeometry(geo)
        self.editor.add_tab(first)
        self._refresh_tab_ui(first)
        self.select_tool("pan")  # Start immer im Maus-Werkzeug (kein Stift-Zeiger ohne Stift in der Hand)
        self.document.new()

    # ================================================================ Tabs
    # Aktiver Tab: alle bisherigen Zugriffe auf Dokument/Ansicht beziehen sich auf ihn
    @property
    def document(self):
        return self.tab.document

    @property
    def view(self):
        return self.tab.view

    @property
    def thumbs(self):
        return self.tab.thumbs

    @property
    def autosave(self):
        return self.tab.autosave

    def _create_tab(self, source: DocTab | None = None):
        """Neuen Tab anlegen – noch nicht angezeigt. Mit `source`: weitere Ansicht desselben Dokuments
        (Dokument, Rückgängig, Miniaturen und Autosave werden geteilt, Position/Zoom sind eigenständig)."""
        if source is not None:
            doc, thumbs, autosave = source.document, source.thumbs, source.autosave
        else:
            doc = PdfDocument()
            thumbs = ThumbnailPanel(doc)
            autosave = None
        view = PageView(doc, self.settings)
        tab = DocTab(doc, view, thumbs, autosave, self)
        if source is None:
            tab.autosave = Autosave(self.qs, doc, can_save=lambda: not self._doc_busy(doc),
                                    do_save=lambda: self._autosave_save(doc), parent=self)
            self.thumbs_stack.addWidget(thumbs)
            # Miniatur antippen: aktive Ansicht dieses Dokuments springt hin
            thumbs.page_clicked.connect(lambda i: self.view.goto_page(i) if self.tab.thumbs is thumbs else None)
        active = lambda: tab is self.tab  # noqa: E731 – Signale nur vom aktiven Tab auswerten
        tab.connections = []  # Verbindungen zu (evtl. geteilten) Objekten, beim Schließen trennen

        def link(signal, slot):
            signal.connect(slot)
            tab.connections.append((signal, slot))

        view.interacted.connect(lambda: self.editor.activate(tab) if tab.group is not None else None)
        view.current_page_changed.connect(lambda i: self._on_page_changed(i) if active() else None)
        view.zoom_changed.connect(lambda z: self._zoom_label_timer.start() if active() else None)
        view.status_message.connect(lambda m: self.statusBar().showMessage(m, 4000))
        view.selection_changed.connect(lambda s: self._on_selection_changed(s) if active() else None)
        view.tool_requested.connect(lambda t: self.select_tool(t) if active() else None)
        view.text_edit_started.connect(lambda size: self._sync_font_spin(size) if active() else None)
        view.context_changed.connect(lambda k: self._on_context_changed(k) if active() else None)
        view.text_selection_changed.connect(lambda _: self._update_copy_actions() if active() else None)
        link(doc.changed, lambda s: (self._update_tab_title(tab), self._on_doc_changed(s) if active() else None))
        link(doc.page_changed, lambda _: self._update_actions() if active() else None)
        link(doc.modified_changed, lambda _: (self._update_tab_title(tab),
                                              self._update_title() if active() else None))
        link(tab.autosave.status_changed, lambda t: self.autosave_label.setText(t) if active() else None)
        view.set_tool(self._tool)
        return tab

    def _siblings(self, tab):
        """Andere Tabs mit demselben Dokument."""
        return [t for t in self.editor.tabs() if t.document is tab.document and t is not tab]

    def _doc_busy(self, doc):
        return any(t.view.is_busy() for t in self.editor.tabs() if t.document is doc)

    def duplicate_tab(self, tab=None):
        """Dasselbe Dokument in einem weiteren Tab direkt daneben öffnen (eigene Position und Zoom)."""
        tab = tab if isinstance(tab, DocTab) else self.tab
        if tab is None:
            return
        tab.view.commit_text_edit()
        new = self._create_tab(source=tab)
        new.view.rebuild()
        group = tab.group
        self.editor.add_tab(new, group, group.tabs.index(tab) + 1)
        page, zoom, fit = tab.view.current_page(), tab.view.zoom, tab.view._fit_mode

        def restore():  # nach dem Einblenden (Größe der Ansicht steht erst dann fest)
            if fit:
                new.view.fit(fit)
            else:
                new.view.set_zoom(zoom)
            new.view.goto_page(page)
        QTimer.singleShot(0, restore)
        for t in [tab, *self._siblings(tab)]:
            self._update_tab_title(t)
        return new

    def open_file_again(self, path):
        """Bereits geöffnete Datei in einem weiteren Tab öffnen (sonst normal öffnen)."""
        existing = self._find_tab(path)
        if existing is None:
            self.open_file(path)
        else:
            self.duplicate_tab(existing)

    def _new_tab(self):
        tab = self._create_tab()
        self.editor.add_tab(tab)
        return tab

    def _tab_for_new_document(self):
        """Leeres Startdokument wiederverwenden, sonst neuen Tab anlegen."""
        if self.tab is not None and self.tab.is_pristine() and not self.view.is_editing_text():
            return self.tab, False
        return self._new_tab(), True

    def _find_tab(self, path):
        key = os.path.normcase(os.path.abspath(path))
        for t in self.editor.tabs():
            if t.document.path and os.path.normcase(os.path.abspath(t.document.path)) == key:
                return t
        return None

    def _on_tab_activated(self, tab):
        old = self.tab
        if old is tab:
            return
        if old is not None and old.view.is_editing_text():
            old.view.commit_text_edit()
        self.tab = tab
        self._refresh_tab_ui(tab)

    def _refresh_tab_ui(self, tab):
        """Leisten, Titel und Anzeigen an den (neu) aktiven Tab anpassen."""
        self.thumbs_stack.setCurrentWidget(tab.thumbs)
        tab.view.set_tool(self._tool)
        if hasattr(self, "act_ruler"):
            tab.view.set_ruler_visible(self.act_ruler.isChecked())
        if not hasattr(self, "page_spin"):
            return  # Aufbau des Fensters noch nicht fertig
        self._search_results, self._search_text = [], ""
        tab.view.clear_search()
        self._update_title()
        self._on_page_changed(tab.view.current_page())
        self.zoom_label.setText(f"{tab.view.zoom * 100:.0f} %")
        self._on_context_changed(tab.view.selection_kind())
        self._on_selection_changed(bool(tab.view._selected))
        tab.autosave.refresh()
        self.autosave_label.setText(tab.autosave.status_text())
        self.files.set_current_file(tab.document.path)
        self._update_split_actions()

    def _saved_width(self, key, default, panel):
        try:
            w = int(self.qs.value(key, default))
        except (TypeError, ValueError):
            w = default
        return max(panel.minimumSizeHint().width(), w)

    def _remember_panel_widths(self):
        """Mit dem Griff eingestellte Breite merken – gilt beim Wiederaufklappen und beim nächsten Start."""
        for panel, key in ((self.files_panel, "files_width"), (self.thumbs_panel, "thumbs_width")):
            if panel.isVisible() and panel.width() > 0:
                panel._expanded_width = panel.width()
                self.qs.setValue(key, panel.width())

    def _place_panel_buttons(self):
        """Dateien-Knopf steht immer ganz links: neben „‹ Seiten“, solange die Seitenleiste offen ist,
        sonst links in der Tableiste (vor dem Seiten-Knopf)."""
        fbtn, tbtn = self.files_panel.expand_button, self.thumbs_panel.expand_button
        tp = self.thumbs_panel
        in_pages = not tp.collapsed and not tp.suppressed
        if in_pages:
            if fbtn.parentWidget() is not tp:
                hidden = fbtn.isHidden()  # Umhängen blendet aus
                self.editor.set_leading_widgets([tbtn])
                tp.lead.addWidget(fbtn)
                fbtn.setVisible(not hidden)
        elif fbtn.parentWidget() is tp:
            fbtn._parked_hidden = fbtn.isHidden()
            self.editor.set_leading_widgets([fbtn, tbtn])

    def _on_tabs_layout_changed(self):
        self._update_split_actions()
        for t in self.editor.tabs():
            if self._siblings(t):
                self._update_tab_title(t)

    def _update_tab_title(self, tab):
        name = tab.title()
        same = [t for t in self.editor.tabs() if t.document is tab.document]
        if len(same) > 1 and tab in same:
            name += f" ({same.index(tab) + 1})"
        star = " •" if tab.document.is_modified() else ""
        self.editor.set_title(tab, name + star, tab.document.path or "Noch nicht gespeichert")

    def _update_split_actions(self):
        if hasattr(self, "act_split_right"):
            can = self.editor.can_split()
            self.act_split_right.setEnabled(can)
            self.act_split_down.setEnabled(can)
            self.act_unsplit.setEnabled(self.editor.is_split())
            self.act_next_tab.setEnabled(len(self.editor.tabs()) > 1)

    def close_tab(self, tab=None):
        tab = tab if isinstance(tab, DocTab) else self.tab
        if tab is None:
            return
        siblings = self._siblings(tab)
        if not siblings:  # letzter Tab dieses Dokuments: ggf. speichern
            if tab is not self.tab:
                self.editor.activate(tab)  # damit man sieht, worum es bei einer Rückfrage geht
            if not self._maybe_save(tab):
                return
        else:
            tab.view.commit_text_edit()
        for signal, slot in tab.connections:
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self.editor.remove_tab(tab)
        if not siblings:
            tab.autosave.timer.stop()
            self.thumbs_stack.removeWidget(tab.thumbs)
            tab.thumbs.deleteLater()
        if not self.editor.tabs():
            self.tab = None
            new = self._new_tab()
            new.document.new()
        tab.view.media.shutdown()  # laufendes Video zuerst anhalten (sonst kann das Schließen hängen)
        tab.view.deleteLater()
        tab.deleteLater()
        for t in siblings:
            self._update_tab_title(t)
        self._update_split_actions()

    # ================================================================ Umbenennen
    def rename_file(self, path):
        """Datei umbenennen; offene Tabs, „Zuletzt“-Liste und Autosave-Einstellung ziehen mit."""
        if not path or not os.path.isfile(path):
            return
        folder, name = os.path.split(path)
        stem, ext = os.path.splitext(name)
        dlg = QInputDialog(self)
        dlg.setWindowTitle("Umbenennen")
        dlg.setLabelText("Neuer Dateiname:")
        dlg.setTextValue(name)
        edit = dlg.findChild(QLineEdit)
        if edit is not None:  # nur den Namen ohne Endung markieren (wie im Explorer)
            QTimer.singleShot(0, lambda: edit.setSelection(0, len(stem)))
        if not dlg.exec():
            return
        new = dlg.textValue().strip()
        if not new or new == name:
            return
        if not os.path.splitext(new)[1]:
            new += ext  # Endung vergessen -> alte behalten
        if any(c in new for c in '\\/:*?"<>|'):
            QMessageBox.warning(self, APP_NAME, 'Dateinamen dürfen keines dieser Zeichen enthalten:  \\ / : * ? " < > |')
            return
        target = os.path.join(folder, new)
        same_file = os.path.normcase(target) == os.path.normcase(path)  # nur Groß-/Kleinschreibung geändert
        if os.path.exists(target) and not same_file:
            QMessageBox.warning(self, APP_NAME, f"„{new}“ gibt es in diesem Ordner schon.")
            return
        key = os.path.normcase(os.path.abspath(path))
        # große PDFs liest MuPDF direkt aus der Datei -> sie ist gesperrt; kurz freigeben und danach neu öffnen
        held = []
        for t in self.editor.tabs():
            d = t.document
            if d.is_large and d.path and os.path.normcase(os.path.abspath(d.path)) == key and d not in held:
                held.append(d)
        for d in held:
            if d.is_modified():
                r = QMessageBox.question(self, APP_NAME, f"„{name}“ hat ungespeicherte Änderungen. Vor dem "
                                         "Umbenennen speichern?", QMessageBox.Save | QMessageBox.Cancel)
                if r != QMessageBox.Save:
                    return
                try:
                    d.save()
                except Exception as e:
                    QMessageBox.critical(self, APP_NAME, f"Speichern fehlgeschlagen:\n{e}")
                    return
        for d in held:
            d.release_file()
        try:
            os.rename(path, target)
        except OSError as e:
            for d in held:
                d.reopen(path)
            QMessageBox.critical(self, APP_NAME, f"Umbenennen fehlgeschlagen:\n{e}")
            return
        for d in held:
            d.reopen(target)
        docs = []
        for t in self.editor.tabs():
            if t.document.path and os.path.normcase(os.path.abspath(t.document.path)) == key:
                if t.document not in docs:
                    docs.append(t.document)
                    t.document.path = target
        for t in self.editor.tabs():
            if t.document in docs:
                self._update_tab_title(t)
        if docs:
            mode = self.autosave.file_mode(path)
            if mode != "default":  # Autosave-Einstellung für diese Datei mitnehmen
                self.autosave.set_file_mode(mode, target)
                self.autosave.set_file_mode("default", path)
        recent = [target if os.path.normcase(p) == os.path.normcase(path) else p
                  for p in (self.qs.value("recent") or [])]
        self.qs.setValue("recent", recent)
        self._update_recent_menu()
        self.files.refresh_recent()
        self.files.set_current_file(self.document.path)
        self._update_title()
        self.statusBar().showMessage(f"Umbenannt in „{new}“.", 4000)

    # ================================================================ Aktionen
    def _act(self, text, slot, shortcut=None, tip=None):
        a = QAction(text, self)
        a.triggered.connect(slot)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if tip:
            a.setToolTip(tip)
        return a

    def _build_actions(self):
        A = self._act
        self.act_new = A("&Neu …", self.new_document, QKeySequence.New)
        self.act_open = A("Ö&ffnen …", self.open_dialog, QKeySequence.Open)
        self.act_save = A("&Speichern", self.save, QKeySequence.Save)
        self.act_save_as = A("Speichern &unter …", self.save_as, QKeySequence.SaveAs)
        self.act_print = A("&Drucken …", self.print_document, QKeySequence.Print)
        self.act_export_img = A("Seite als Bild exportieren …", self.export_page_image)
        self.act_quit = A("&Beenden", self.close, QKeySequence.Quit)

        self.act_undo = A("&Rückgängig", lambda: self.document.undo(), QKeySequence.Undo)
        self.act_redo = A("&Wiederholen", lambda: self.document.redo(), QKeySequence.Redo)
        self.act_delete_annot = A("Annotation &löschen", self.delete_current)
        self.act_delete_annot.setEnabled(False)
        self.act_edit_text = A("Text bearbeiten", lambda: self.view.edit_selected_text())
        self.act_done = A("Fertig", self.finish_selection)
        self.act_paste = A("&Einfügen", self.paste, QKeySequence.Paste,
                           "Kopierte Objekte, Bild oder Text aus der Zwischenablage einfügen (Strg+V)")
        self.act_copy = A("&Kopieren", self.copy, QKeySequence.Copy, "Auswahl kopieren (Strg+C)")
        self.act_cut = A("&Ausschneiden", lambda: self.copy(cut=True), QKeySequence.Cut, "Auswahl ausschneiden (Strg+X)")
        self.act_duplicate_sel = A("Duplizieren", lambda: self.view.duplicate_selection(), None,
                                   "Auswahl duplizieren (Strg+D)")
        self.act_insert_media = A("Video/Audio …", self.insert_media, None,
                                  "Video oder Audio einfügen – wird in die PDF eingebettet (keine Zusatzdateien)")
        self.act_play_media = A("Abspielen", lambda: self.view.play_selected(), None, "Video/Audio abspielen")
        self.act_capture_camera = A("Kamera …", lambda: self.capture("camera"), None,
                                    "Foto oder Video mit der Windows-Kamera-App aufnehmen und einfügen")
        self.act_capture_audio = A("Mikrofon", lambda: self.capture("audio"), None,
                                   "Tonaufnahme starten – das Audio-Symbol erscheint sofort auf der Seite, gesteuert "
                                   "wird über die Aufnahmeleiste (weiterarbeiten ist möglich)")
        self.act_rec_pause = A("Pause", lambda: self._rec_toggle_pause(), None, "Aufnahme anhalten/fortsetzen")
        self.act_rec_stop = A("Stopp && einfügen", lambda: self._rec_stop(False), None,
                              "Aufnahme beenden und an der Stelle des Symbols einfügen")
        self.act_rec_discard = A("Verwerfen", lambda: self._rec_stop(True), None, "Aufnahme beenden und wegwerfen")
        self._recorder = None
        self._rec_target = None  # (Tab, Platz) der laufenden Aufnahme
        self.act_paste_page = A("Bild aus Zwischenablage als &Seite einfügen …", self.paste_as_page, "Ctrl+Shift+V")
        self.act_crop = A("Bild &zuschneiden …", lambda: self.view.crop_selected(), "Ctrl+Shift+X")
        self.act_img_rot_l = A("Bild nach links drehen", lambda: self.view.rotate_selected(-90))
        self.act_img_rot_r = A("Bild nach rechts drehen", lambda: self.view.rotate_selected(90))
        for a in (self.act_crop, self.act_img_rot_l, self.act_img_rot_r):
            a.setEnabled(False)
        self.act_find = A("&Suchen …", self._focus_search, QKeySequence.Find)
        # nicht QKeySequence.FindNext: das ist unter Windows F3 und würde mit der Dateileiste (F3) kollidieren
        self.act_find_next = A("Weitersuchen", lambda: self._search_step(1), "Ctrl+G")
        self.act_find_prev = A("Rückwärts suchen", lambda: self._search_step(-1), "Ctrl+Shift+G")

        self.act_quick_insert = A("Leere Seite danach einfügen",
                                  lambda: self.quick_insert(self.view.current_page() + 1), "Ctrl+Shift+N",
                                  "Leere Seite im Format der aktuellen Seite nach dieser einfügen")
        self.act_insert_dialog = A("Seiten einfügen …", lambda: self.insert_pages_dialog(), "Ctrl+Shift+I")
        self.act_insert_pdf = A("PDF/Dokument einfügen …", lambda: self.insert_pdf())
        self.act_insert_image_page = A("Bild als neue Seite einfügen …", self.insert_image_page)
        self.act_edit_image_page = A("Bildseite bearbeiten …", lambda: self.edit_image_page(), None,
                                     "Format, Einpassung, Rand und Ausschnitt dieser Bildseite ändern")
        self.act_settings = A("Einstellungen …", self.settings_dialog, "Ctrl+,")
        self.act_dup = A("Seite duplizieren", self._duplicate, "Ctrl+D")
        self.act_del_page = A("Seite(n) löschen", lambda: self.delete_pages(self._target_pages()), "Ctrl+Del")
        self.act_rot_l = A("Nach links drehen", lambda: self.document.rotate_pages(self._target_pages(), -90), "Ctrl+Shift+L")
        self.act_rot_r = A("Nach rechts drehen", lambda: self.document.rotate_pages(self._target_pages(), 90), "Ctrl+Shift+R")
        self.act_move_page = A("Seite verschieben nach …", self.move_page_dialog)
        self.act_extract = A("Seiten als PDF exportieren …", lambda: self.extract_pages(self._target_pages()))

        self.act_zoom_in = A("Vergrößern", lambda: self.view.zoom_by(1.25), QKeySequence.ZoomIn)
        self.act_zoom_out = A("Verkleinern", lambda: self.view.zoom_by(0.8), QKeySequence.ZoomOut)
        self.act_zoom_100 = A("Originalgröße", lambda: self.view.set_zoom(1.0), "Ctrl+0")
        self.act_fit_width = A("Seitenbreite", lambda: self.view.fit("width"), "Ctrl+1")
        self.act_fit_page = A("Ganze Seite", lambda: self.view.fit("page"), "Ctrl+2")
        self.act_back = A("Zurück", lambda: self.view.go_back(), "Alt+Left",
                          "Zurück zur Stelle vor dem letzten Link (auch Maus-Zurücktaste)")
        self.act_files = A("Dateien", self._toggle_files, "F3", "Dateileiste ein-/ausblenden (F3)")
        self.act_files.setCheckable(True)
        start_files, start_thumbs = start_panels(self.qs)
        self.act_files.setChecked(start_files)
        self.act_autosave = A("Automatisch speichern …", self.autosave_dialog)
        self.act_sidebar = A("Seitenleiste", self._toggle_thumbs, "F4", "Seitenleiste ein-/ausblenden (F4)")
        self.act_sidebar.setCheckable(True)
        self.act_sidebar.setChecked(start_thumbs)
        self.act_fullscreen = A("Vollbild", self.toggle_focus_mode, "F11",
                                "Vollbild: nur Dokument und Werkzeuge (F11, Esc zum Beenden)")
        # nur aktiv im Vollbild; bewusst nicht unsichtbar schalten (sonst wirkt das Tastenkürzel nicht)
        self.act_exit_focus = A("Vollbild beenden", self.toggle_focus_mode, "Esc", "Vollbild beenden (Esc)")
        self.act_exit_focus.setEnabled(False)
        self.act_add_preset = A("Hinzufügen", self.add_preset, tip="Neuen Stift oder Marker anlegen")
        self.act_close_tab = A("Tab schließen", lambda: self.close_tab(), "Ctrl+W")
        self.act_next_tab = A("Nächster Tab", lambda: self.editor.cycle(1), "Ctrl+Tab")
        self.act_prev_tab = A("Vorheriger Tab", lambda: self.editor.cycle(-1), "Ctrl+Shift+Tab")
        self.act_split_right = A("Nebeneinander", lambda: self.editor.split(None, Qt.Horizontal), "Ctrl+\\",
                                 "Aktuellen Tab in einer Gruppe rechts daneben anzeigen (Strg+\\)")
        self.act_split_down = A("Untereinander", lambda: self.editor.split(None, Qt.Vertical), None,
                                "Aktuellen Tab in einer Gruppe darunter anzeigen")
        self.act_unsplit = A("Teilung aufheben", lambda: self.editor.unsplit(), None,
                             "Alle Tabs wieder in einer Gruppe zusammenführen")
        self.act_prev_page = A("Vorherige Seite", lambda: self.view.goto_page(self.view.current_page() - 1), "PgUp")
        self.act_next_page = A("Nächste Seite", lambda: self.view.goto_page(self.view.current_page() + 1), "PgDown")

        self.act_finger_draw = QAction(icons.icon("finger"), "Finger", self)
        self.act_finger_draw.setCheckable(True)
        self.act_finger_draw.setChecked(prefs.finger_draw())
        self.act_finger_draw.setToolTip("Mit dem Finger zeichnen: Ein Finger zeichnet mit dem gewählten Stift, Marker, "
                                        "Radierer bzw. der Form – mit zwei Fingern zoomen und verschieben")
        self.act_finger_draw.toggled.connect(self._set_finger_draw)

        self.act_ruler = QAction(icons.icon("ruler"), "Lineal", self)
        self.act_ruler.setCheckable(True)
        self.act_ruler.setShortcut(QKeySequence("Ctrl+R"))
        self.act_ruler.setToolTip("Lineal ein-/ausblenden (Strg+R): Stift oder Marker an der Kante zieht gerade Linien "
                                  "und zeigt ihre Länge; Skala in cm. Verschieben mit Finger, Stift oder Maus, "
                                  "Drehen mit zwei Fingern oder dem Mausrad")
        self.act_ruler.toggled.connect(self._set_ruler)
        self.addAction(self.act_ruler)

        self.tool_group = QActionGroup(self)
        self.tool_actions = {}
        for tid, label, key, tip in TOOL_DEFS:
            a = QAction(label, self)
            a.setCheckable(True)
            a.setShortcut(QKeySequence(key))
            name = label.split(" ", 1)[-1]
            a.setToolTip(f"{name} – {tip} ({key})")
            a.triggered.connect(lambda _=False, t=tid: self.select_tool(t))
            self.tool_group.addAction(a)
            self.tool_actions[tid] = a

    def _build_menus(self):
        """Menü der Schaltfläche „Datei“ im Menüband (keine klassische Menüleiste) – darin auch Hilfe und Feedback."""
        m = QMenu("&Datei", self)
        m.addActions([self.act_new, self.act_open])
        self.recent_menu = m.addMenu("Zuletzt geöffnet")
        self._update_recent_menu()
        m.addSeparator()
        m.addActions([self.act_save, self.act_save_as])
        m.addAction(self.act_close_tab)
        self.autosave_menu = m.addMenu("Automatisch speichern")
        self.autosave_menu.aboutToShow.connect(self._fill_autosave_menu)
        m.addSeparator()
        m.addActions([self.act_extract, self.act_export_img, self.act_print])
        m.addSeparator()
        self.act_remove_pw = m.addAction("Passwortschutz entfernen …", self.remove_password)
        self.act_forget_pw = m.addAction("Gespeichertes Passwort vergessen", self.forget_saved_password)
        m.aboutToShow.connect(self._update_password_actions)
        m.addSeparator()
        m.addAction(self.act_settings)
        m.addSeparator()
        h = QMenu("Hilfe", self)
        h.setIcon(icons.icon("help"))
        h.addAction("Tastenkürzel und Bedienung", self._show_help)
        h.addSeparator()
        h.addAction(f"Über {APP_NAME} …", self._show_about)
        self.help_menu = h
        m.addMenu(h)
        m.addAction(icons.icon("feedback"), "Feedback senden …", self.send_feedback)
        m.addSeparator()
        m.addAction(self.act_quit)
        self.file_menu = m
        # Tastenkürzel aller Aktionen gelten auch ohne Menüleiste
        self.addActions([a for a in self.findChildren(QAction) if not a.shortcut().isEmpty()])

    def _build_toolbars(self):
        I = icons.icon
        # Symbole und kurze Beschriftungen für das Menüband
        for act, ic, label in (
            (self.act_new, "new", "Neu"), (self.act_open, "open", "Öffnen"), (self.act_save, "save", "Speichern"),
            (self.act_save_as, "save_as", "Speichern unter"), (self.act_print, "print", "Drucken"),
            (self.act_export_img, "image_page", "Als Bild"), (self.act_extract, "export", "Als PDF"),
            (self.act_undo, "undo", "Rückgängig"), (self.act_redo, "redo", "Wiederholen"),
            (self.act_paste, "paste", "Einfügen"), (self.act_paste_page, "paste_page", "Als Seite"),
            (self.act_copy, "copy", "Kopieren"), (self.act_cut, "cut", "Ausschneiden"),
            (self.act_duplicate_sel, "duplicate", "Duplizieren"),
            (self.act_insert_media, "video", "Video/Audio"), (self.act_play_media, "play", "Abspielen"),
            (self.act_capture_camera, "camera", "Kamera"), (self.act_capture_audio, "mic", "Mikrofon"),
            (self.act_quick_insert, "page_add", "Leere Seite"),
            (self.act_insert_dialog, "pages_add", "Seiten …"), (self.act_insert_pdf, "pdf_in", "PDF einfügen"),
            (self.act_insert_image_page, "image_page", "Bild als Seite"), (self.act_dup, "duplicate", "Duplizieren"),
            (self.act_edit_image_page, "crop", "Bildseite"), (self.act_settings, "settings", "Einstellungen"),
            (self.act_move_page, "move", "Verschieben"), (self.act_rot_l, "rotate_l", "Links drehen"),
            (self.act_rot_r, "rotate_r", "Rechts drehen"), (self.act_del_page, "trash", "Löschen"),
            (self.act_zoom_in, "zoom_in", "Vergrößern"), (self.act_zoom_out, "zoom_out", "Verkleinern"),
            (self.act_fit_width, "fit_width", "Seitenbreite"), (self.act_fit_page, "fit_page", "Ganze Seite"),
            (self.act_back, "undo", "Zurück"),
            (self.act_files, "panel_files", "Dateien"), (self.act_sidebar, "panel_pages", "Seitenleiste"),
            (self.act_fullscreen, "fullscreen", "Vollbild"), (self.act_exit_focus, "exit_fullscreen", "Vollbild beenden"),
            (self.act_prev_page, "prev", "Vorherige"), (self.act_next_page, "next", "Nächste"),
            (self.act_add_preset, "plus", "Hinzufügen"), (self.act_edit_text, "edit", "Text bearbeiten"),
            (self.act_crop, "crop", "Zuschneiden"), (self.act_img_rot_l, "rotate_l", "Links drehen"),
            (self.act_img_rot_r, "rotate_r", "Rechts drehen"), (self.act_delete_annot, "trash", "Löschen"),
            (self.act_done, "check", "Fertig"),
            (self.act_split_right, "split_right", "Nebeneinander"), (self.act_split_down, "split_down", "Untereinander"),
            (self.act_unsplit, "unsplit", "Teilung aufheben"), (self.act_close_tab, "tab_close", "Tab schließen"),
        ):
            act.setIcon(I(ic))
            act.setIconText(label)
        tool_icons = {"select": "lasso", "pan": "select", "eraser": "eraser", "markup": "markup", "text": "text",
                      "note": "note", "image": "image", "line": "line", "arrow": "arrow", "rect": "rect",
                      "ellipse": "ellipse", "pen": "edit", "highlighter": "markup"}
        tool_labels = {"select": "Lassoauswahl", "pan": "Maus", "eraser": "Radierer", "markup": "Markieren",
                       "text": "Text", "note": "Notiz", "image": "Bild", "line": "Linie", "arrow": "Pfeil",
                       "rect": "Rechteck", "ellipse": "Ellipse"}
        for tid, a in self.tool_actions.items():
            a.setIcon(I(tool_icons[tid]))
            if tid in tool_labels:
                a.setIconText(tool_labels[tid])

        rb = Ribbon()
        self.ribbon = rb
        file_btn = QToolButton()
        file_btn.setText("Datei")
        file_btn.setPopupMode(QToolButton.InstantPopup)
        file_btn.setMenu(self.file_menu)
        file_btn.setAutoRaise(True)
        file_btn.setMinimumHeight(32)
        rb.add_left(file_btn)
        p_start = rb.add_page("Start")
        p_draw = rb.add_page("Zeichnen")
        p_insert = rb.add_page("Einfügen")
        p_pages = rb.add_page("Seiten")
        p_view = rb.add_page("Ansicht")
        self._ribbon_names = ["Start", "Zeichnen", "Einfügen", "Seiten", "Ansicht"]

        # --- Start
        g = p_start.add_group("Datei")
        for a in (self.act_new, self.act_open, self.act_save, self.act_save_as):
            g.add_action(a)
        g = p_start.add_group("Zwischenablage")
        g.add_action(self.act_paste)
        g.add_action(self.act_copy, always_compact=True)
        g.add_action(self.act_cut, always_compact=True)
        g.add_action(self.act_paste_page)
        g = p_start.add_group("Rückgängig")
        g.add_action(self.act_undo)
        g.add_action(self.act_redo)
        g = p_start.add_group("Leisten")
        g.add_action(self.act_files)
        g.add_action(self.act_sidebar)

        # --- Zeichnen
        g = p_draw.add_group("Rückgängig")
        g.add_action(self.act_undo, always_compact=True)
        g.add_action(self.act_redo, always_compact=True)
        g = p_draw.add_group("Auswahl")
        g.add_action(self.tool_actions["pan"])
        g.add_action(self.tool_actions["select"])
        g.add_action(self.act_finger_draw)
        g = p_draw.add_group("Zeichentools")
        self.preset_strip = QWidget()
        self._preset_row = QHBoxLayout(self.preset_strip)
        self._preset_row.setContentsMargins(0, 0, 0, 0)
        self._preset_row.setSpacing(1)
        g.add_widget(self.preset_strip)
        add_btn = g.add_action(self.act_add_preset)
        add_menu = QMenu(add_btn)
        add_menu.addAction("Neuer Stift oder Marker …", self.add_preset)
        add_menu.addAction("Stifte auf Standard zurücksetzen", self.reset_presets)
        add_btn.setMenu(add_menu)
        add_btn.setPopupMode(QToolButton.MenuButtonPopup)
        g = p_draw.add_group("Radieren")
        g.add_action(self.tool_actions["eraser"])
        g = p_draw.add_group("Markieren")
        g.add_action(self.tool_actions["markup"])
        g = p_draw.add_group("Formen")
        self.shapes_button = RibbonButton(self.tool_actions["line"])
        shapes_menu = QMenu(self.shapes_button)
        for t in SHAPE_TOOLS:
            shapes_menu.addAction(self.tool_actions[t])
        self.shapes_button.setMenu(shapes_menu)
        self.shapes_button.setPopupMode(QToolButton.MenuButtonPopup)
        self.shapes_button.setToolTip("Formen: Linie, Pfeil, Rechteck, Ellipse")
        g.add_widget(self.shapes_button)
        g = p_draw.add_group("Messen")
        g.add_action(self.act_ruler)
        g = p_draw.add_group("Stil")
        self._style_group = g
        self.current_color = QToolButton()
        self.current_color.setToolTip("Farbe")
        self.current_color.setIconSize(QSize(20, 20))
        self.current_color.setAutoRaise(True)
        self.current_color.setPopupMode(QToolButton.InstantPopup)
        color_menu = QMenu(self.current_color)
        wa = QWidgetAction(color_menu)
        wa.setDefaultWidget(P.ColorGrid(lambda c: (color_menu.close(), self._set_color(c))))
        color_menu.addAction(wa)
        self.current_color.setMenu(color_menu)
        g.add_widget(self.current_color)
        self.width_spin = ValueButton([0.5, 0.75, 1, 1.5, 2, 3, 4, 6, 8, 12, 16, 24, 32],
                                      tooltip="Strichstärke", minimum=0.25, maximum=60)
        self.width_spin.setAutoRaise(True)
        self.width_spin.valueChanged.connect(self._set_width)
        g.add_widget(self.width_spin)
        self.font_spin = ValueButton([8, 9, 10, 11, 12, 14, 16, 18, 20, 24, 28, 32, 40, 48, 64, 72],
                                     tooltip="Schriftgröße", decimals=0, minimum=4, maximum=144)
        self.font_spin.setAutoRaise(True)
        self.font_spin.setValue(self.settings.font_size)
        self.font_spin.valueChanged.connect(self._set_font_size)
        self.font_spin.setVisible(False)
        g.add_widget(self.font_spin)

        # --- Einfügen: Text, Notizen, Bilder, Video/Audio, Aufnahmen
        g = p_insert.add_group("Textfeld")  # Schriftfarbe/-größe stehen direkt beim Text-Werkzeug
        g.add_action(self.tool_actions["text"])
        self.ins_color = QToolButton()
        self.ins_color.setText("Schriftfarbe")
        self.ins_color.setToolTip("Schriftfarbe für neue Textfelder und das gerade bearbeitete")
        self.ins_color.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.ins_color.setIconSize(QSize(22, 22))
        self.ins_color.setAutoRaise(True)
        self.ins_color.setPopupMode(QToolButton.InstantPopup)
        ins_menu = QMenu(self.ins_color)
        iwa = QWidgetAction(ins_menu)
        iwa.setDefaultWidget(P.ColorGrid(lambda c: (ins_menu.close(), self._set_text_color(c))))
        ins_menu.addAction(iwa)
        self.ins_color.setMenu(ins_menu)
        self.ins_color.setIcon(P.text_color_icon(self.settings.color_hex("text")))
        g.add_widget(self.ins_color)
        self.ins_font = ValueButton([8, 9, 10, 11, 12, 14, 16, 18, 20, 24, 28, 32, 40, 48, 64, 72],
                                    tooltip="Schriftgröße für neue Textfelder und das gerade bearbeitete",
                                    decimals=0, minimum=4, maximum=144)
        self.ins_font.setAutoRaise(True)
        self.ins_font.setValue(self.settings.font_size)
        self.ins_font.valueChanged.connect(self._set_font_size)
        g.add_widget(self.ins_font)
        g = p_insert.add_group("Notiz")
        g.add_action(self.tool_actions["note"])
        g = p_insert.add_group("Bilder & Medien")
        g.add_action(self.tool_actions["image"])
        g.add_action(self.act_insert_media)
        g.add_action(self.act_paste)
        g = p_insert.add_group("Aufnehmen")
        g.add_action(self.act_capture_camera)
        g.add_action(self.act_capture_audio)

        # --- Seiten
        g = p_pages.add_group("Einfügen")
        for a in (self.act_quick_insert, self.act_insert_dialog, self.act_insert_pdf, self.act_insert_image_page,
                  self.act_paste_page):
            g.add_action(a)
        g = p_pages.add_group("Bearbeiten")
        g.add_action(self.act_edit_image_page)
        for a in (self.act_dup, self.act_move_page):
            g.add_action(a)
        g.add_action(self.act_rot_l, always_compact=True)  # Symbole sind eindeutig, spart Platz
        g.add_action(self.act_rot_r, always_compact=True)
        g.add_action(self.act_del_page)
        g = p_pages.add_group("Exportieren")
        for a in (self.act_extract, self.act_export_img, self.act_print):
            g.add_action(a)

        # --- Ansicht
        g = p_view.add_group("Zoom")
        g.add_action(self.act_zoom_out, always_compact=True)
        self.zoom_label = QLabel("100 %")
        self.zoom_label.setMinimumWidth(52)
        self.zoom_label.setAlignment(Qt.AlignCenter)
        g.add_widget(self.zoom_label)
        g.add_action(self.act_zoom_in, always_compact=True)
        g.add_action(self.act_fit_width)
        g.add_action(self.act_fit_page)
        g = p_view.add_group("Navigation")
        g.add_action(self.act_back, always_compact=True)
        g.add_action(self.act_prev_page, always_compact=True)
        self.page_spin = QSpinBox()
        self.page_spin.setMinimum(1)
        self.page_spin.setKeyboardTracking(False)
        self.page_spin.valueChanged.connect(lambda v: self.view.goto_page(v - 1))
        g.add_widget(self.page_spin)
        self.page_total = QLabel(" / 1 ")
        g.add_widget(self.page_total)
        g.add_action(self.act_next_page, always_compact=True)
        g = p_view.add_group("Leisten")
        g.add_action(self.act_files)
        g.add_action(self.act_sidebar)
        g = p_view.add_group("Tabs")
        g.add_action(self.act_split_right)
        g.add_action(self.act_split_down)
        g.add_action(self.act_unsplit)
        for page in (p_start, p_draw, p_pages, p_view):
            page.finish()

        # rechts in der Registerzeile: Suche und Vollbild (Hilfe und Feedback stehen im Menü „Datei“)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Suchen (Strg+F)")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setFixedWidth(240)
        self.search_edit.addAction(I("search", 16), QLineEdit.LeadingPosition)
        self.search_edit.returnPressed.connect(self._search)
        rb.add_right(self.search_edit)
        rb.add_right(RibbonButton(self.act_fullscreen))  # auf jeder Registerkarte sichtbar
        name = self.qs.value("start/ribbon_tab", "last")  # Einstellung: fester Start-Tab oder der zuletzt offene
        if name not in self._ribbon_names:
            name = self.qs.value("ribbon_tab_name")
        if name not in self._ribbon_names:  # früher als Nummer gespeichert (ohne „Einfügen“)
            old = int(self.qs.value("ribbon_tab", 1) or 1)
            name = ["Start", "Zeichnen", "Seiten", "Ansicht"][old] if 0 <= old < 4 else "Zeichnen"
        rb.set_current(self._ribbon_names.index(name))
        rb.tab_changed.connect(lambda i: self.qs.setValue("ribbon_tab_name", self._ribbon_names[i]))
        self.setMenuWidget(rb)

        # --- Vollbild: kompakte Leiste nur mit Rückgängig, Auswahl, Hand, Stiften und „Beenden“
        ft = QToolBar("Vollbild")
        ft.setObjectName("focus")
        ft.setMovable(False)
        ft.setIconSize(QSize(20, 20))
        ft.setToolButtonStyle(Qt.ToolButtonIconOnly)
        ft.addAction(self.act_undo)
        ft.addSeparator()
        ft.addAction(self.tool_actions["pan"])
        ft.addAction(self.tool_actions["select"])
        ft.addAction(self.act_ruler)
        ft.addSeparator()
        self.focus_preset_strip = QWidget()
        self._focus_preset_row = QHBoxLayout(self.focus_preset_strip)
        self._focus_preset_row.setContentsMargins(0, 0, 0, 0)
        self._focus_preset_row.setSpacing(1)
        ft.addWidget(self.focus_preset_strip)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        ft.addWidget(spacer)
        exit_btn = RibbonButton(self.act_exit_focus)
        ft.addWidget(exit_btn)
        self.addToolBar(Qt.TopToolBarArea, ft)
        ft.hide()
        self.focus_toolbar = ft
        self.addToolBarBreak()

        # Optionsleiste für das ausgewählte Objekt (erscheint nur bei Auswahl)
        cb = QToolBar("Auswahl")
        cb.setObjectName("context")
        cb.setMovable(False)
        cb.setIconSize(QSize(20, 20))
        cb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.context_label = QLabel()
        f = self.context_label.font()
        f.setBold(True)
        self.context_label.setFont(f)
        pal = self.context_label.palette()
        pal.setColor(QPalette.WindowText, theme.ACCENT_BRIGHT)
        self.context_label.setPalette(pal)
        self.context_label.setContentsMargins(12, 0, 12, 0)
        cb.addWidget(self.context_label)
        for a in (self.act_copy, self.act_cut, self.act_duplicate_sel):  # nur Symbole (Text als Tooltip)
            btn = QToolButton()
            btn.setDefaultAction(a)
            btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
            btn.setAutoRaise(True)
            btn.setIconSize(QSize(20, 20))
            cb.addWidget(btn)
        self.sel_color = QToolButton()
        self.sel_color.setToolTip("Farbe der Auswahl")
        self.sel_color.setIconSize(QSize(20, 20))
        self.sel_color.setAutoRaise(True)
        self.sel_color.setPopupMode(QToolButton.InstantPopup)
        sel_menu = QMenu(self.sel_color)
        swa = QWidgetAction(sel_menu)
        swa.setDefaultWidget(P.ColorGrid(lambda c: (sel_menu.close(), self._set_sel_color(c))))
        sel_menu.addAction(swa)
        self.sel_color.setMenu(sel_menu)
        self._sel_color_act = cb.addWidget(self.sel_color)
        self.sel_width = ValueButton([0.5, 0.75, 1, 1.5, 2, 3, 4, 6, 8, 12, 16, 24], tooltip="Strichstärke der Auswahl",
                                     minimum=0.25, maximum=60)
        self.sel_width.setAutoRaise(True)
        self.sel_width.valueChanged.connect(lambda w: self.view.apply_style(width=w))
        self._sel_width_act = cb.addWidget(self.sel_width)
        self.sel_font = ValueButton([8, 9, 10, 11, 12, 14, 16, 18, 20, 24, 28, 32, 40, 48, 64, 72],
                                    tooltip="Schriftgröße der Auswahl", decimals=0, minimum=4, maximum=144)
        self.sel_font.setAutoRaise(True)
        self.sel_font.valueChanged.connect(lambda s: self.view.apply_style(fontsize=s))
        self._sel_font_act = cb.addWidget(self.sel_font)
        self.sel_opacity = ValueButton([100, 85, 70, 50, 40, 25], suffix=" %", tooltip="Deckkraft der Auswahl",
                                       decimals=0, minimum=5, maximum=100)
        self.sel_opacity.setAutoRaise(True)
        self.sel_opacity.valueChanged.connect(lambda o: self.view.apply_style(opacity=o / 100))
        self._sel_opacity_act = cb.addWidget(self.sel_opacity)
        cb.addActions([self.act_play_media, self.act_edit_text, self.act_crop, self.act_img_rot_l,
                       self.act_img_rot_r, self.act_delete_annot])
        spacer2 = QWidget()
        spacer2.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        cb.addWidget(spacer2)
        cb.addAction(self.act_done)
        self.addToolBar(Qt.TopToolBarArea, cb)
        del_btn = cb.widgetForAction(self.act_delete_annot)
        if del_btn is not None:  # „Löschen“ in Rot
            pal = del_btn.palette()
            pal.setColor(QPalette.ButtonText, theme.ACCENT_PINK)
            del_btn.setPalette(pal)
            del_btn.setIcon(icons.colored("trash", theme.ACCENT_PINK))
        cb.hide()
        self.context_toolbar = cb

        # --- Aufnahmeleiste: nur während einer Tonaufnahme sichtbar
        rb_ = QToolBar("Aufnahme")
        rb_.setObjectName("recording")
        rb_.setMovable(False)
        rb_.setIconSize(QSize(20, 20))
        self.rec_label = QLabel("  ● Aufnahme 0:00  ")
        self.rec_label.setStyleSheet("color:#ff5a52;font-weight:bold")
        rb_.addWidget(self.rec_label)
        for a in (self.act_rec_pause, self.act_rec_stop, self.act_rec_discard):
            rb_.addAction(a)
        self.addToolBar(Qt.TopToolBarArea, rb_)
        rb_.hide()
        self.recording_toolbar = rb_
        self.addAction(self.act_exit_focus)  # Esc beendet das Vollbild auch ohne sichtbaren Knopf
        self._rebuild_preset_actions()

    def _build_statusbar(self):
        self.autosave_label = QLabel()
        self.autosave_label.setToolTip("Doppelklick: Einstellungen für automatisches Speichern")
        self.autosave_label.mouseDoubleClickEvent = lambda e: self.autosave_dialog()
        self.statusBar().addPermanentWidget(self.autosave_label)
        self.size_label = QLabel()
        self.statusBar().addPermanentWidget(self.size_label)
        self.autosave_label.setText(self.autosave.status_text())
        # Leisten: Titelzeile/Knopf in der Tableiste und Menü/F3/F4 halten sich gegenseitig aktuell
        self.files_panel.collapsed_changed.connect(lambda c: self._panel_changed(self.act_files, "files_visible", c))
        self.thumbs_panel.collapsed_changed.connect(
            lambda c: self._panel_changed(self.act_sidebar, "thumbs_visible", c))
        self.files_panel.set_collapsed(not self.act_files.isChecked())
        self.thumbs_panel.set_collapsed(not self.act_sidebar.isChecked())

    # ================================================================ Werkzeuge
    def select_tool(self, tool):
        if tool in P.KINDS:
            # Stift/Marker über Menü oder Tastenkürzel -> zuletzt benutzten passenden Stift nehmen
            idx = self._last_preset.get(tool)
            if idx is None or idx >= len(self.presets) or self.presets[idx]["kind"] != tool:
                idx = next((i for i, p in enumerate(self.presets) if p["kind"] == tool), None)
            if idx is not None:
                self.activate_preset(idx)
                return
        self.active_preset = None
        self._apply_tool(tool)
        self.tool_actions[tool].setChecked(True)

    def _set_finger_draw(self, on):
        """„Finger“ gehört zu den Modi neben Maus und Lasso: Einschalten wählt das zuletzt benutzte
        Zeichenwerkzeug (in Maus/Lasso zeichnet der Finger nicht), Maus bzw. Lasso schalten ihn wieder aus."""
        from .page_view import FINGER_DRAW_TOOLS
        prefs.set_finger_draw(on)
        if on and self.view.tool not in FINGER_DRAW_TOOLS:
            self.select_tool(getattr(self, "_last_draw_tool", None) or "pen")
        self.statusBar().showMessage("Fingermodus an: ein Finger zeichnet, zwei Finger zoomen/verschieben." if on else
                                     "Fingermodus aus: ein Finger scrollt.", 4000)

    def _set_ruler(self, on):
        self.view.set_ruler_visible(on)
        if on:
            self.statusBar().showMessage("Lineal: an der Kante entlang zeichnen ergibt eine gerade Linie; "
                                         "zwei Finger oder Mausrad drehen.", 5000)

    def _apply_tool(self, tool):
        from .page_view import FINGER_DRAW_TOOLS
        if tool in FINGER_DRAW_TOOLS:
            self._last_draw_tool = tool
        elif tool in ("pan", "select") and self.act_finger_draw.isChecked():
            self.act_finger_draw.blockSignals(True)  # Maus/Lasso gewählt: Fingermodus aus (wie ein Modusknopf)
            self.act_finger_draw.setChecked(False)
            self.act_finger_draw.blockSignals(False)
            prefs.set_finger_draw(False)
        self._tool = tool
        self.view.set_tool(tool)
        if tool in SHAPE_TOOLS:
            self.shapes_button.setDefaultAction(self.tool_actions[tool])
        has_color = tool in ("pen", "highlighter", "markup", "text") + SHAPE_TOOLS
        has_width = tool in ("pen", "highlighter", "eraser") + SHAPE_TOOLS
        self.current_color.setEnabled(has_color)
        self.width_spin.setEnabled(has_width)
        if self.width_spin.isHidden() != (tool == "text"):
            self.width_spin.setVisible(tool != "text")
            self.font_spin.setVisible(tool == "text")
            self._style_group.title.setText("Schrift" if tool == "text" else "Stil")
            self.ribbon.relayout()
        self.current_color.setToolTip("Schriftfarbe" if tool == "text" else "Farbe")
        if has_color:
            self.current_color.setIcon(self._color_icon(tool, self.settings.color_hex(tool)))
        self.width_spin.blockSignals(True)
        self.width_spin.setValue(self.settings.width(tool))
        self.width_spin.blockSignals(False)

    @staticmethod
    def _color_icon(tool, hexc):
        return P.text_color_icon(hexc) if tool == "text" else P.color_icon(hexc, 22)

    def _set_color(self, hexc):
        tool = self.view.tool
        self.settings.set_color(tool, hexc)
        self.current_color.setIcon(self._color_icon(tool, hexc))
        self._update_active_preset(color=hexc)
        if tool == "text":
            c = QColor(hexc)
            self.view.set_text_style(color=(c.redF(), c.greenF(), c.blueF()))
            self.ins_color.setIcon(P.text_color_icon(hexc))
        self.view.update_cursor()

    def _set_width(self, w):
        self.settings.set_width(self.view.tool, w)
        self._update_active_preset(width=w)
        self.view.update_cursor()

    def _sync_font_spin(self, size):
        self.font_spin.setValue(round(size))
        self.ins_font.setValue(round(size))

    # ================================================================ Optionsleiste
    CONTEXT_LABELS = {"image": "Bild", "text": "Textfeld", "note": "Notiz", "annot": "Zeichnung", "media": "Video/Audio",
                      "editing": "Text bearbeiten"}

    def _update_copy_actions(self):
        """Kopieren: ausgewählte Objekte, Textfeld in Bearbeitung oder mit der Maus markierter Seitentext (sonst
        wäre auch Strg+C gesperrt); Ausschneiden nur Objekte/Textfeld – Seitentext lässt sich nicht ausschneiden."""
        objects = getattr(self, "_copy_objects", False)
        self.act_copy.setEnabled(objects or bool(self.view.selected_text()))
        self.act_cut.setEnabled(objects)

    def _on_context_changed(self, kind):
        if kind == "group":
            self.context_label.setText(f"{self.view.selection_count()} Objekte")
        else:
            self.context_label.setText(self.CONTEXT_LABELS.get(kind, ""))
        self.act_edit_text.setVisible(kind in ("text", "note"))
        self.act_play_media.setVisible(kind == "media")
        objects = kind not in ("", "editing")
        self._copy_objects = objects or kind == "editing"
        self._update_copy_actions()
        self.act_duplicate_sel.setEnabled(objects)
        st = self.view.selection_style() if objects else None
        for act, key in ((self._sel_color_act, "color"), (self._sel_width_act, "width"),
                         (self._sel_font_act, "fontsize"), (self._sel_opacity_act, "opacity")):
            act.setVisible(bool(st) and st.get(key) is not None)
        if st:
            if st["color"] is not None:
                c = QColor.fromRgbF(*st["color"])
                self.sel_color.setIcon(P.color_icon(c.name(), 22))
            for btn, key, k in ((self.sel_width, "width", 1), (self.sel_font, "fontsize", 1), (self.sel_opacity, "opacity", 100)):
                if st.get(key) is not None:
                    btn.setValue(st[key] * k)
        for a in (self.act_crop, self.act_img_rot_l, self.act_img_rot_r):
            a.setVisible(kind == "image")
            a.setEnabled(kind == "image")
        self.act_delete_annot.setEnabled(bool(kind))
        self._set_context_bar(bool(kind))

    def _set_context_bar(self, visible):
        bar = self.context_toolbar
        if bar.isVisible() == visible:
            return
        # Ansicht so nachführen, dass das Dokument beim Ein-/Ausblenden nicht springt
        vp = self.view.viewport()
        before = vp.mapToGlobal(vp.rect().topLeft()).y()
        bar.setVisible(visible)
        self.layout().activate()
        after = vp.mapToGlobal(vp.rect().topLeft()).y()
        sb = self.view.verticalScrollBar()
        sb.setValue(sb.value() + (after - before))

    def _set_sel_color(self, hexc):
        c = QColor(hexc)
        self.sel_color.setIcon(P.color_icon(hexc, 22))
        self.view.apply_style(color=(c.redF(), c.greenF(), c.blueF()))

    def insert_media(self):
        """Video/Audio auswählen und in die aktuelle Seite einbetten (Vorschaubild = erstes Videobild)."""
        from . import media
        if not media.available():
            QMessageBox.warning(self, APP_NAME, "Video/Audio wird von dieser Version nicht unterstützt (Qt Multimedia fehlt).")
            return
        path, _ = QFileDialog.getOpenFileName(self, "Video oder Audio einfügen", self.qs.value("last_dir", ""),
                                              media.FILE_FILTER)
        if path:
            self._insert_media_file(path, os.path.basename(path))

    def _insert_media_file(self, path, name, data=None, place=None):
        """Video-/Audiodatei einbetten (Datei, Aufnahme): Vorschaubild erzeugen, auf der Seite platzieren.
        data: bereits eingelesener Inhalt (wird dann genau so eingebettet). Rückgabe: eingefügt?"""
        from . import media
        kind, _mime = media.kind_of(name)
        if kind is None:
            QMessageBox.warning(self, APP_NAME, "Dieses Format wird nicht unterstützt.")
            return False
        size = len(data) if data is not None else os.path.getsize(fs_path(path))
        if size > 200 * 1024 * 1024:
            r = QMessageBox.question(self, APP_NAME, f"„{name}“ ist {size / 2 ** 20:.0f} MB groß – die PDF wird "
                                     "entsprechend größer. Trotzdem einfügen?")
            if r != QMessageBox.Yes:
                return False
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            frame, duration = media.probe(path)
            poster = media.poster_png(frame, kind, name, duration)
            if data is None:
                with open(fs_path(path), "rb") as f:
                    data = f.read()
        finally:
            QApplication.restoreOverrideCursor()
        img = QImage.fromData(poster)
        aspect = img.width() / img.height() if not img.isNull() and img.height() else 16 / 9
        try:
            self.view.insert_media(data, name, poster, aspect, place)
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, f"Einfügen fehlgeschlagen:\n{e}")
            return False
        self.statusBar().showMessage("Eingefügt – Abspielen: Doppelklick, im Maus-Werkzeug antippen oder Kontextmenü.", 6000)
        return True

    def capture(self, mode):
        """Foto/Video mit der Windows-Kamera-App bzw. Ton mit dem Mikrofon aufnehmen und direkt einbetten."""
        from . import capture, media
        self.view.commit_text_edit()
        if mode == "camera":
            self._capture_camera_app()
            return
        if not media.available():
            QMessageBox.warning(self, APP_NAME, "Aufnehmen wird von dieser Version nicht unterstützt (Qt Multimedia fehlt).")
            return
        if self._recorder is not None:  # Mikrofon erneut gedrückt: beenden und einfügen
            self._rec_stop(False)
            return
        place = self.view.media_place("Aufnahme.m4a", 1.0)
        if place is None:
            return
        try:
            rec = capture.AudioRecorder(self)
        except Exception as e:
            QMessageBox.warning(self, APP_NAME, f"Aufnahme nicht möglich:\n{e}")
            return
        rec.duration_changed.connect(self._rec_duration)
        rec.state_changed.connect(self._rec_state)
        rec.failed.connect(self._rec_failed)
        rec.finished.connect(self._rec_finished)
        self._recorder = rec
        self._rec_target = (self.tab, place)
        self.view.show_recording_marker(place)
        self.act_capture_audio.setText("Stopp")
        self.act_rec_pause.setText("Pause")
        self.rec_label.setText("  ● Aufnahme 0:00  ")
        self._set_toolbar_visible(self.recording_toolbar, True)
        rec.start()
        self.statusBar().showMessage("Aufnahme läuft – du kannst weiterarbeiten. Beenden über die Aufnahmeleiste.", 5000)

    def _rec_duration(self, ms):
        s = ms // 1000
        dot = "●" if not (self._recorder and self._recorder.paused) else "❚❚"
        self.rec_label.setText(f"  {dot} Aufnahme {s // 60}:{s % 60:02d}  ")

    def _rec_state(self, state):
        self.act_rec_pause.setText("Fortsetzen" if state == "paused" else "Pause")
        if self._recorder is not None:
            self._rec_duration(self._recorder.recorder.duration())

    def _rec_toggle_pause(self):
        rec = self._recorder
        if rec is not None:
            rec.resume() if rec.paused else rec.pause()

    def _rec_stop(self, discard):
        if self._recorder is not None:
            self.rec_label.setText("  Wird gespeichert …  " if not discard else "  Wird verworfen …  ")
            self._recorder.stop(discard)

    def _rec_failed(self, msg):
        QMessageBox.warning(self, APP_NAME, f"Aufnahme fehlgeschlagen:\n{msg}")

    def _rec_finished(self, path):
        """Aufnahme fertig: an der Stelle des Platzhalters einbetten (im Tab, in dem sie begonnen wurde)."""
        from . import media
        rec, self._recorder = self._recorder, None
        target, self._rec_target = self._rec_target, None
        self.act_capture_audio.setText("Mikrofon")
        self._set_toolbar_visible(self.recording_toolbar, False)
        tab, place = target if target else (None, None)
        if tab is not None and tab in self.editor.tabs():
            tab.view.remove_recording_marker()
        if not path:
            self.statusBar().showMessage("Aufnahme verworfen.", 4000)
            return
        if tab is None or tab not in self.editor.tabs():
            self.statusBar().showMessage("Das Dokument der Aufnahme wurde geschlossen – Aufnahme verworfen.", 6000)
        else:
            if tab is not self.tab:
                self.editor.activate(tab)
            ok = self._insert_media_file(path, rec.name if rec else os.path.basename(path), place=place)
            if ok:
                self.statusBar().showMessage("Aufnahme eingefügt – antippen zum Abspielen.", 5000)
        if os.path.dirname(os.path.abspath(path)) == os.path.abspath(media.temp_dir()):
            try:
                os.remove(path)  # eigene Aufnahme im Temp-Ordner: steckt jetzt in der PDF
            except OSError:
                pass

    def _set_toolbar_visible(self, bar, visible):
        """Leiste ein-/ausblenden, ohne dass das Dokument springt."""
        if bar.isVisible() == visible:
            return
        vp = self.view.viewport()
        before = vp.mapToGlobal(vp.rect().topLeft()).y()
        bar.setVisible(visible)
        self.layout().activate()
        after = vp.mapToGlobal(vp.rect().topLeft()).y()
        sb = self.view.verticalScrollBar()
        sb.setValue(sb.value() + (after - before))

    def _capture_camera_app(self):
        """Kamera-App öffnen, neue Fotos/Videos aus „Eigene Aufnahmen“ einbetten und auf Wunsch löschen."""
        from PySide6.QtCore import QBuffer, QByteArray
        from PySide6.QtGui import QImageReader
        from . import capture, media
        delete = self.qs.value("camera/delete_after", "true") in (True, "true")
        dlg = capture.CameraAppDialog(self, delete)
        accepted = dlg.exec()
        delete = dlg.delete_after.isChecked()
        self.qs.setValue("camera/delete_after", "true" if delete else "false")
        if not accepted:
            return
        inserted = removed = 0
        for shot in dlg.session.shots():
            if shot.kind == "photo":
                buf = QBuffer()
                buf.setData(QByteArray(shot.data))
                buf.open(QBuffer.ReadOnly)
                reader = QImageReader(buf)
                reader.setAutoTransform(True)  # Hochformat-Fotos richtig herum (EXIF)
                img = reader.read()
                ok = not img.isNull()
                if ok:
                    self.view.paste_image(img, inserted=True)
            elif not media.available():
                QMessageBox.warning(self, APP_NAME, "Videos werden von dieser Version nicht unterstützt "
                                                    "(Qt Multimedia fehlt).")
                ok = False
            else:
                ok = self._insert_media_file(shot.path, shot.name, data=shot.data)
            inserted += bool(ok)
            if ok and delete:
                removed += dlg.session.delete(shot)  # nur genau diese, unveränderte Datei
        msg = f"{inserted} Aufnahme{'n' if inserted != 1 else ''} eingefügt"
        if delete:
            msg += f", {removed} aus „Eigene Aufnahmen“ gelöscht (Papierkorb)"
            if removed < inserted:
                msg += f" – {inserted - removed} nicht gelöscht, da verändert oder nicht eindeutig"
        self.statusBar().showMessage(msg + ".", 8000)

    def copy(self, cut=False):
        if self.view.is_editing_text():
            self.view.editor_copy(cut)
        elif not cut and self.view.selected_text():
            self.view.copy_selected_text()  # mit der Maus markierter Text
        elif not self.view.copy_selection(cut):
            self.statusBar().showMessage("Nichts ausgewählt – mit der Lassoauswahl Objekte antippen oder umfahren.", 4000)

    def _duplicate(self):
        """Strg+D: Auswahl duplizieren, sonst die aktuelle Seite."""
        if self.view.selection_count():
            self.view.duplicate_selection()
        else:
            self.document.duplicate_page(self.view.current_page())

    def delete_current(self):
        if self.view.is_editing_text():
            self.view.delete_editing_text()
        else:
            self.view.delete_selected()

    def finish_selection(self):
        self.view.commit_text_edit()
        self.view.clear_selection()

    def _set_font_size(self, size):
        size = int(round(size))
        self.settings.font_size = size
        for spin in (self.font_spin, getattr(self, "ins_font", None)):  # Zeichnen- und Einfügen-Tab abgleichen
            if spin is not None and abs(spin.value() - size) > 0.01:
                spin.blockSignals(True)
                spin.setValue(size)
                spin.blockSignals(False)
        self.view.set_text_style(fontsize=size)

    def _set_text_color(self, hexc):
        """Schriftfarbe (Einfügen-Tab) – gilt für neue Textfelder und das gerade bearbeitete."""
        self.settings.set_color("text", hexc)
        self.ins_color.setIcon(P.text_color_icon(hexc))
        if self.view.tool == "text":
            self.current_color.setIcon(P.text_color_icon(hexc))
        c = QColor(hexc)
        self.view.set_text_style(color=(c.redF(), c.greenF(), c.blueF()))

    # ================================================================ Eigene Stifte
    def _rebuild_preset_actions(self):
        for row in (self._preset_row, self._focus_preset_row):
            while row.count():
                w = row.takeAt(0).widget()
                if w is not None:
                    w.hide()
                    w.setParent(None)
                    w.deleteLater()
        for a in self.preset_actions:
            self.tool_group.removeAction(a)
            a.deleteLater()
        self.preset_actions = []
        for i, p in enumerate(self.presets):
            a = QAction(P.preset_icon(p), P.KINDS[p["kind"]], self)
            a.setCheckable(True)
            a.setToolTip(P.preset_tooltip(p) + "\nRechtsklick oder lang drücken: bearbeiten")
            a.triggered.connect(lambda _=False, i=i: self.activate_preset(i))
            self.tool_group.addAction(a)
            for row in (self._preset_row, self._focus_preset_row):
                btn = RibbonButton(a, always_compact=True)
                btn.setIconSize(P.PEN_ICON)
                btn.setContextMenuPolicy(Qt.CustomContextMenu)
                btn.customContextMenuRequested.connect(
                    lambda pos, i=i, b=btn: self._preset_menu(i, b.mapToGlobal(pos)))
                row.addWidget(btn)
            self.preset_actions.append(a)
        if self.active_preset is not None and self.active_preset < len(self.preset_actions):
            self.preset_actions[self.active_preset].setChecked(True)
        P.save_presets(self.qs, self.presets)
        self.ribbon.relayout()

    def _shape_style(self):
        i = self._shape_preset
        if i is None or i >= len(self.presets):
            i = next((k for k, p in enumerate(self.presets) if p["kind"] == "pen"), None)
        if i is None:
            return {"kind": "pen", "color": "#000000", "width": 1.5}
        return self.presets[i]

    def activate_preset(self, i):
        p = self.presets[i]
        kind = p["kind"]
        self.settings.set_color(kind, p["color"])
        self.settings.set_width(kind, p["width"])
        self.active_preset = i
        self._shape_preset = i
        self._last_preset[kind] = i
        self._apply_tool(kind)
        self.preset_actions[i].setChecked(True)

    def _update_active_preset(self, color=None, width=None):
        if self.view.tool in SHAPE_TOOLS:
            # Formen-Werkzeug: Änderungen gelten für den Stift, dessen Eigenschaften die Formen nutzen
            i = self._shape_preset
            if i is None or i >= len(self.presets):
                i = next((k for k, p in enumerate(self.presets) if p["kind"] == "pen"), None)
            if i is None:
                return
            kind = self.presets[i]["kind"]
            if color is not None:
                self.settings.set_color(kind, color)
            if width is not None:
                self.settings.set_width(kind, width)
        else:
            i = self.active_preset
            if i is None or i >= len(self.presets) or self.presets[i]["kind"] != self.view.tool:
                return
        if color is not None:
            self.presets[i]["color"] = color
        if width is not None:
            self.presets[i]["width"] = width
        self.preset_actions[i].setIcon(P.preset_icon(self.presets[i]))
        self.preset_actions[i].setToolTip(P.preset_tooltip(self.presets[i]) + "\nRechtsklick: bearbeiten")
        P.save_presets(self.qs, self.presets)

    def _preset_menu(self, i, global_pos):
        m = QMenu(self)
        m.addAction("Bearbeiten …", lambda: self.edit_preset(i))
        m.addAction("Duplizieren", lambda: self._insert_preset(i + 1, dict(self.presets[i])))
        if i > 0:
            m.addAction("Nach links", lambda: self._swap_presets(i, i - 1))
        if i < len(self.presets) - 1:
            m.addAction("Nach rechts", lambda: self._swap_presets(i, i + 1))
        m.addSeparator()
        m.addAction("Löschen", lambda: self.delete_preset(i))
        m.exec(global_pos)

    def add_preset(self):
        base = self.presets[self.active_preset] if self.active_preset is not None else None
        dlg = P.PresetDialog(self, base)
        dlg.setWindowTitle("Neuer Stift")
        if dlg.exec():
            self._insert_preset(len(self.presets), dlg.result_preset())

    def _insert_preset(self, pos, preset):
        self.presets.insert(pos, preset)
        self._rebuild_preset_actions()
        self.activate_preset(pos)

    def edit_preset(self, i):
        dlg = P.PresetDialog(self, self.presets[i])
        if dlg.exec():
            self.presets[i] = dlg.result_preset()
            self._rebuild_preset_actions()
            self.activate_preset(i)

    def delete_preset(self, i):
        del self.presets[i]
        self._last_preset = {}
        self._shape_preset = None
        was_active = self.active_preset == i
        if self.active_preset is not None and self.active_preset > i:
            self.active_preset -= 1
        if was_active:
            self.active_preset = None
        self._rebuild_preset_actions()
        if was_active:
            self.select_tool("pen" if self.presets else "select")

    def _swap_presets(self, i, j):
        self.presets[i], self.presets[j] = self.presets[j], self.presets[i]
        if self.active_preset in (i, j):
            self.active_preset = j if self.active_preset == i else i
        if self._shape_preset in (i, j):
            self._shape_preset = j if self._shape_preset == i else i
        self._last_preset = {}
        self._rebuild_preset_actions()

    def reset_presets(self):
        self.presets = [dict(p) for p in P.DEFAULT_PRESETS]
        self.active_preset = None
        self._last_preset = {}
        self._shape_preset = None
        self._rebuild_preset_actions()
        self.select_tool("pen")

    # ================================================================ Bilder / Zwischenablage
    @staticmethod
    def clipboard_image() -> QImage:
        """Bild aus der Zwischenablage (auch eine kopierte Bilddatei aus dem Explorer) oder ein leeres Bild."""
        cb = QApplication.clipboard()
        md = cb.mimeData()
        img = cb.image()
        if img.isNull() and md is not None and md.hasUrls():
            for u in md.urls():
                if u.isLocalFile():
                    img = QImage(u.toLocalFile())
                    if not img.isNull():
                        break
        return img

    def paste(self):
        """Strg+V: kopierte Objekte, Bild oder Text (im geöffneten Textfeld: Text)."""
        self.view.paste_clipboard()

    def paste_as_page(self, index=None):
        """Bild aus der Zwischenablage als eigene Seite (z. B. eine kopierte Folie) – mit Format-Dialog."""
        img = self.clipboard_image()
        if img.isNull():
            self.statusBar().showMessage("Kein Bild in der Zwischenablage.", 3000)
            return
        self.insert_image_page(index, data=images.encode(img))

    def _on_selection_changed(self, selected):
        self.act_delete_annot.setEnabled(selected or self.view.is_editing_text())
        is_img = selected and self.view.selected_is_image()
        for a in (self.act_crop, self.act_img_rot_l, self.act_img_rot_r):
            a.setEnabled(is_img)

    # ================================================================ Datei
    def new_document(self):
        fmt, ok = QInputDialog.getItem(self, "Neues Dokument", "Seitenformat:", list(PAGE_FORMATS), 1, False)
        if not ok:
            return
        orient, ok = QInputDialog.getItem(self, "Neues Dokument", "Ausrichtung:", ["Hochformat", "Querformat"], 0, False)
        if not ok:
            return
        tpl, ok = QInputDialog.getItem(self, "Neues Dokument", "Vorlage:",
                                       ["Leer", "Liniert", "Kariert", "Punktraster"], 0, False)
        if not ok:
            return
        tab, _ = self._tab_for_new_document()
        tab.document.new(fmt, orient == "Querformat", tpl)
        self._update_tab_title(tab)

    def open_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Öffnen", self.qs.value("last_dir", ""), OPEN_FILTER)
        for path in paths:
            self.open_file(path)

    def open_external_link(self, link, doc_path):
        """Link nach außen (Webseite, andere Datei). Webseiten nur nach Rückfrage öffnen (wie Acrobat);
        Programme werden aus Sicherheitsgründen nie gestartet."""
        kind = link.get("kind")
        target = (link.get("uri") or link.get("file") or "").strip()
        if not target:
            return
        base = os.path.dirname(doc_path) if doc_path else ""
        local = target[7:] if target.lower().startswith("file://") else target
        local_path = local if os.path.isabs(local) else os.path.join(base, local)
        if kind in (fitz.LINK_GOTOR, fitz.LINK_LAUNCH) and local.lower().endswith(".pdf") and os.path.isfile(local_path):
            self.open_file(local_path)  # andere PDF: im Editor öffnen und zur Zielseite springen
            page = link.get("page", -1)
            if page is not None and page >= 0:
                self.view.goto_location(page, link.get("to"))
            return
        lowered = target.lower()
        if lowered.startswith(("http://", "https://", "mailto:")) or lowered.startswith("www."):
            url = target if not lowered.startswith("www.") else "https://" + target
            r = QMessageBox.question(self, APP_NAME, f"Link im Browser öffnen?\n\n{url}",
                                     QMessageBox.Open | QMessageBox.Cancel, QMessageBox.Open)
            if r == QMessageBox.Open:
                QDesktopServices.openUrl(QUrl(url))
            return
        self.statusBar().showMessage(f"Link auf „{target}“ wird aus Sicherheitsgründen nicht geöffnet.", 6000)

    def open_file(self, path, ask=True):
        """Datei in einem neuen Tab öffnen (bzw. zu ihrem Tab wechseln, falls schon offen)."""
        existing = self._find_tab(path)
        if existing is not None:
            self.editor.activate(existing)
            return
        tab, created = self._tab_for_new_document()
        try:
            tab.document.open(path)
        except PasswordRequired as e:
            if not self._unlock(tab, path, e.doc_id):
                if created:
                    self.close_tab(tab)
                return
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, f"Datei konnte nicht geöffnet werden:\n{e}")
            if created:
                self.close_tab(tab)
            return
        self.qs.setValue("last_dir", os.path.dirname(path))
        self._add_recent(path)
        self._update_tab_title(tab)
        if tab is self.tab:
            self._update_title()
            self.files.set_current_file(path)
        tab.autosave.refresh()

    def _unlock(self, tab, path, doc_id):
        """Geschützte PDF entsperren: erst ein gemerktes Passwort versuchen, sonst fragen – bei falschem Passwort
        gleich erneut (statt das Dokument zu schließen). False = abgebrochen."""
        from . import passwords
        from .password_dialog import PasswordDialog
        keys = passwords.keys_for(doc_id, path)
        stored = passwords.lookup(keys)
        if stored is not None:
            if tab.document.authenticate(path, stored):
                passwords.remember(keys, stored)  # auch unter dem (evtl. neuen) Pfad merken
                self.statusBar().showMessage("Gespeichertes Passwort verwendet.", 4000)
                self._note_owner_only(tab)
                return True
            passwords.forget(keys)  # Passwort wurde geändert: veralteten Eintrag entfernen
        error = False
        while True:
            pw, remember, ok = PasswordDialog.ask(self, os.path.basename(path), error)
            if not ok:
                return False
            if tab.document.authenticate(path, pw):
                if remember:
                    passwords.remember(keys, pw)
                self._note_owner_only(tab)
                return True
            error = True

    def _note_owner_only(self, tab):
        if (tab.document.encryption or {}).get("owner_only"):
            QMessageBox.information(self, APP_NAME, "Die PDF wurde mit dem Besitzer-Passwort geöffnet. Das "
                                    "Passwort zum Öffnen ist unbekannt – beim Speichern wird die PDF darum mit dem "
                                    "Besitzer-Passwort geschützt (es wird dann auch zum Öffnen verlangt).")

    def remove_password(self):
        """Passwortschutz des aktuellen Dokuments entfernen (wirkt beim nächsten Speichern)."""
        from . import passwords
        doc = self.document
        if not doc.encryption:
            QMessageBox.information(self, APP_NAME, "Dieses Dokument ist nicht mit einem Passwort geschützt.")
            return
        r = QMessageBox.question(self, APP_NAME, "Passwortschutz entfernen?\n\nBeim nächsten Speichern wird die "
                                 "PDF ohne Passwort gespeichert – jeder kann sie dann öffnen.")
        if r != QMessageBox.Yes:
            return
        doc.remove_password()
        passwords.forget(passwords.keys_for(doc.doc_id, doc.path))
        self.statusBar().showMessage("Passwortschutz entfernt – wird beim nächsten Speichern übernommen.", 6000)

    def forget_saved_password(self):
        from . import passwords
        passwords.forget(passwords.keys_for(self.document.doc_id, self.document.path))
        self.statusBar().showMessage("Gespeichertes Passwort für dieses Dokument gelöscht.", 4000)

    def _update_password_actions(self):
        from . import passwords
        doc = self.document
        self.act_remove_pw.setEnabled(bool(doc.encryption))
        self.act_forget_pw.setEnabled(passwords.lookup(passwords.keys_for(doc.doc_id, doc.path)) is not None)

    def _autosave_save(self, doc=None):
        """Automatisch speichern – ohne Dialoge, Fehler nur in der Statusleiste."""
        doc = doc or self.document
        try:
            doc.save()
        except Exception as e:
            self.statusBar().showMessage(f"Automatisches Speichern fehlgeschlagen: {e}", 8000)
            return False
        self.statusBar().showMessage("Automatisch gespeichert.", 2500)
        return True

    def save(self):
        self.view.commit_text_edit()
        if self.document.path is None or not self.document.path.lower().endswith(".pdf"):
            return self.save_as()
        try:
            self.document.save()
            self.statusBar().showMessage("Gespeichert.", 3000)
            return True
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, f"Speichern fehlgeschlagen:\n{e}")
            return False

    def save_as(self):
        self.view.commit_text_edit()
        start = self.document.path or os.path.join(self.qs.value("last_dir", ""), "Dokument.pdf")
        if not start.lower().endswith(".pdf"):
            start = os.path.splitext(start)[0] + ".pdf"
        path, _ = QFileDialog.getSaveFileName(self, "Speichern unter", start, "PDF (*.pdf)")
        if not path:
            return False
        try:
            self.document.save(path)
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, f"Speichern fehlgeschlagen:\n{e}")
            return False
        self._add_recent(path)
        self._update_title()
        self._update_tab_title(self.tab)
        self.files.set_current_file(path)
        self.autosave.refresh()
        self.statusBar().showMessage("Gespeichert.", 3000)
        return True

    def _maybe_save(self, tab=None):
        tab = tab or self.tab
        tab.view.commit_text_edit()
        if not tab.document.is_modified():
            return True
        if tab.autosave.save_now_if_enabled():  # Autosave aktiv: ohne Rückfrage speichern
            return True
        if tab is not self.tab:
            self.editor.activate(tab)
        r = QMessageBox.question(self, APP_NAME, f"„{tab.title()}“ wurde geändert. Änderungen speichern?",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if r == QMessageBox.Save:
            return bool(self.save())
        return r == QMessageBox.Discard

    def _add_recent(self, path):
        recent = [p for p in (self.qs.value("recent") or []) if p != path]
        recent.insert(0, path)
        self.qs.setValue("recent", recent[:15])
        self._update_recent_menu()
        if hasattr(self, "files"):
            self.files.refresh_recent()

    # ================================================================ Dateileiste / Autosave
    def _toggle_files(self, visible):
        self.files_panel.set_collapsed(not visible)

    def _toggle_thumbs(self, visible):
        self.thumbs_panel.set_collapsed(not visible)

    def _panel_changed(self, action, key, collapsed):
        action.blockSignals(True)
        action.setChecked(not collapsed)
        action.blockSignals(False)
        self.qs.setValue(key, "false" if collapsed else "true")

    def autosave_dialog(self):
        dlg = AutosaveDialog(self.autosave, self)
        if dlg.exec():
            dlg.apply()
            self._refresh_autosave_all()

    def _refresh_autosave_all(self):
        """Globale Autosave-Einstellung gilt für alle offenen Tabs."""
        for t in self.editor.tabs():
            t.autosave.refresh()
        self.autosave_label.setText(self.autosave.status_text())

    def _fill_autosave_menu(self):
        m = self.autosave_menu
        m.clear()
        a = m.addAction("Für alle Dateien")
        a.setCheckable(True)
        a.setChecked(self.autosave.global_enabled)
        a.toggled.connect(lambda on: (setattr(self.autosave, "global_enabled", on), self._refresh_autosave_all()))
        m.addSeparator()
        head = m.addAction("Für diese Datei:")
        head.setEnabled(False)
        group = QActionGroup(m)
        current = self.autosave.file_mode()
        for key, label in FILE_MODES.items():
            if key == "default":
                label += " (" + ("an" if self.autosave.global_enabled else "aus") + ")"
            act = m.addAction("    " + label)
            act.setCheckable(True)
            act.setChecked(key == current)
            act.setEnabled(self.autosave.savable())
            group.addAction(act)
            act.triggered.connect(lambda _=False, k=key: self.autosave.set_file_mode(k))
        m.addSeparator()
        m.addAction(self.act_autosave)

    def _update_recent_menu(self):
        self.recent_menu.clear()
        recent = self.qs.value("recent") or []
        if isinstance(recent, str):
            recent = [recent]
        for p in recent:
            self.recent_menu.addAction(p, lambda _=False, p=p: self.open_file(p))
        self.recent_menu.setEnabled(bool(recent))

    def print_document(self):
        from .print_dialog import PrintDialog
        self.view.commit_text_edit()
        selected = self.thumbs.selected_pages() if self.thumbs.isVisible() else []
        if PrintDialog.run(self, self.document, self.view.current_page(), selected):
            self.statusBar().showMessage("An den Drucker gesendet.", 4000)

    def export_page_image(self, index=None):
        self.view.commit_text_edit()
        idx = self.view.current_page() if _no_index(index) else index
        base = os.path.splitext(self.document.path or "Seite")[0]
        path, _ = QFileDialog.getSaveFileName(self, "Seite als Bild exportieren",
                                              f"{base}_Seite{idx + 1}.png", "PNG (*.png);;JPEG (*.jpg)")
        if path:
            with self.document.reading():
                pix = self.document.page(idx).get_pixmap(dpi=200, alpha=False)
            pix.save(path)

    # ================================================================ Seiten
    def _target_pages(self):
        pages = self.thumbs.selected_pages() if self.thumbs.hasFocus() else []
        return pages or [self.view.current_page()]

    def quick_insert(self, index):
        ref = self.document.page(min(max(index - 1, 0), self.document.page_count - 1))
        r = ref.rect
        self.document.insert_blank_pages(index, 1, r.width, r.height, self.last_template)
        self.view.goto_page(index)

    def insert_pages_dialog(self, default_pos=None):
        cur = self.view.current_page()
        r = self.document.page(cur).rect
        default_pos = cur + 1 if default_pos is None else default_pos
        dlg = InsertPagesDialog(self, self.document.page_count, cur, (r.width, r.height),
                                default_pos, self.last_template)
        if dlg.exec():
            pos, count, w, h, tpl = dlg.values()
            self.last_template = tpl
            self.qs.setValue("last_template", tpl)
            self.document.insert_blank_pages(pos, count, w, h, tpl)
            self.view.goto_page(pos)

    def insert_pdf(self, index=None):
        index = self.view.current_page() + 1 if _no_index(index) else index
        path, _ = QFileDialog.getOpenFileName(self, "Dokument einfügen", self.qs.value("last_dir", ""), OPEN_FILTER)
        if not path:
            return
        try:
            self.document.insert_pdf(index, path)
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, f"Einfügen fehlgeschlagen:\n{e}")
            return
        self.view.goto_page(index)

    def insert_image_page(self, index=None, data: bytes | None = None):
        index = self.view.current_page() + 1 if _no_index(index) else index
        if data is None:
            path, _ = QFileDialog.getOpenFileName(self, "Bild als Seite einfügen", self.qs.value("last_dir", ""),
                                                  "Bilder (*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff *.webp)")
            if not path:
                return
            with open(fs_path(path), "rb") as f:
                data = f.read()
        img = images.load(data)
        if img.isNull():
            QMessageBox.warning(self, APP_NAME, "Das Bild konnte nicht gelesen werden.")
            return
        # Standard: gleiche Breite wie die vorangehende Seite, Höhe nach dem Bild
        ref = self.document.page(max(0, min(index - 1, self.document.page_count - 1))).rect
        dlg = ImagePageDialog(self, img, default_spec(self.qs), (ref.width, ref.height), PAGE_FORMATS,
                              PAGE_TEMPLATES)
        if not dlg.exec():
            return
        try:
            self.document.insert_image_page(index, data, dlg.result_spec())
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, f"Einfügen fehlgeschlagen:\n{e}")
            return
        self.view.goto_page(index)

    def edit_image_page(self, index=None):
        """Format, Einpassung, Rand und Ausschnitt einer Bildseite nachträglich ändern."""
        index = self.view.current_page() if _no_index(index) else index
        spec = self.document.image_page_spec(index)
        data = self.document.image_page_image(index) if spec is not None else None
        img = images.load(data) if data else None
        if img is None or img.isNull():
            QMessageBox.information(self, APP_NAME, "Diese Seite ist keine Bildseite (ein Bild ohne Text).")
            return
        ref = self.document.page(index - 1 if index > 0 else index).rect
        dlg = ImagePageDialog(self, img, spec, (ref.width, ref.height), PAGE_FORMATS, PAGE_TEMPLATES,
                              title=f"Bildseite {index + 1} bearbeiten")
        if dlg.exec():
            self.document.update_image_page(index, dlg.result_spec())
            self.view.goto_page(index)

    def settings_dialog(self):
        dlg = SettingsDialog(self.qs, self.autosave, self.view.gpu, self)
        if not dlg.exec():
            return
        dlg.apply()
        batch = scan_batch_enabled(self.qs)
        for t in self.editor.tabs():
            t.view.renderer.batch_heavy = batch
        self._refresh_autosave_all()
        self.statusBar().showMessage("Einstellungen gespeichert.", 3000)

    def delete_pages(self, pages):
        if len(pages) >= self.document.page_count:
            QMessageBox.information(self, APP_NAME, "Das Dokument muss mindestens eine Seite behalten.")
            return
        txt = f"Seite {pages[0] + 1}" if len(pages) == 1 else f"{len(pages)} Seiten"
        if QMessageBox.question(self, APP_NAME, f"{txt} löschen?") == QMessageBox.Yes:
            self.document.delete_pages(pages)

    def move_page_dialog(self, index=None):
        cur = self.view.current_page() if _no_index(index) else index
        n = self.document.page_count
        target, ok = QInputDialog.getInt(self, "Seite verschieben", f"Seite {cur + 1} verschieben an Position:",
                                         cur + 1, 1, n)
        if ok:
            self.document.move_page(cur, target - 1)
            self.view.goto_page(target - 1)

    def extract_pages(self, pages):
        base = os.path.splitext(self.document.path or "Dokument")[0]
        path, _ = QFileDialog.getSaveFileName(self, "Seiten exportieren", f"{base}_Auszug.pdf", "PDF (*.pdf)")
        if path:
            self.document.extract_pages(pages, path)
            self.statusBar().showMessage(f"{len(pages)} Seite(n) exportiert.", 3000)

    # ================================================================ Suche
    def _focus_search(self):
        self.search_edit.setFocus()
        self.search_edit.selectAll()

    def _search(self):
        text = self.search_edit.text().strip()
        if not text:
            self.view.clear_search()
            return
        if text == self._search_text and self._search_results:
            self._search_step(1)
            return
        self._search_text = text
        self._search_results = self.document.search(text)
        if not self._search_results:
            self.statusBar().showMessage(f"„{text}“ nicht gefunden.", 3000)
            self.view.clear_search()
            return
        cur = self.view.current_page()
        self._search_index = next((i for i, (p, _) in enumerate(self._search_results) if p >= cur), 0)
        self._show_search()

    def _search_step(self, d):
        if not self._search_results:
            return self._search()
        self._search_index = (self._search_index + d) % len(self._search_results)
        self._show_search()

    def _show_search(self):
        self.view.show_search(self._search_results, self._search_index)
        self.statusBar().showMessage(f"Treffer {self._search_index + 1} von {len(self._search_results)}")

    # ================================================================ Status
    def _on_doc_changed(self, structural):
        self._search_results = []
        self._search_text = ""
        self._update_actions()
        self._update_title()
        self._on_page_changed(self.view.current_page())

    def _on_page_changed(self, idx):
        n = self.document.page_count
        self.page_spin.blockSignals(True)
        self.page_spin.setMaximum(max(1, n))
        self.page_spin.setValue(idx + 1)
        self.page_spin.blockSignals(False)
        self.page_total.setText(f" / {n} ")
        self.thumbs.set_current(idx)
        if hasattr(self, "act_edit_image_page"):
            self.act_edit_image_page.setEnabled(0 <= idx < n and self.document.image_page_spec(idx) is not None)
        if 0 <= idx < n:
            r = self.document.page(idx).rect
            self.size_label.setText(f"{r.width / 72 * 25.4:.0f} × {r.height / 72 * 25.4:.0f} mm")

    def _update_actions(self):
        self.act_undo.setEnabled(self.document.can_undo())
        self.act_redo.setEnabled(self.document.can_redo())

    def _update_title(self):
        name = os.path.basename(self.document.path) if self.document.path else "Unbenannt"
        star = "*" if self.document.is_modified() else ""
        self.setWindowTitle(f"{name}{star} – {APP_NAME}")
        self._update_actions()

    def toggle_focus_mode(self):
        """Vollbild wie in OneNote: nur Dokument und Werkzeugleiste. Ein schwebendes Fenster behält dabei
        seine Größe (nur die Leisten verschwinden), ein maximiertes wird zum echten Vollbild."""
        if not self._focus_mode:
            fullscreen = self.isMaximized() or self.isFullScreen()
            self._pre_focus = {"maximized": self.isMaximized(), "fullscreen": fullscreen,
                               "geometry": self.geometry()}
            self.files_panel.set_suppressed(True)
            self.thumbs_panel.set_suppressed(True)
            self.editor.set_chrome_visible(False)
            self.ribbon.hide()
            self.focus_toolbar.show()
            self.statusBar().hide()
            self.act_exit_focus.setEnabled(True)
            self._focus_mode = True
            if fullscreen:
                self.showFullScreen()
        else:
            self.focus_toolbar.hide()
            self.ribbon.show()
            self.editor.set_chrome_visible(True)
            self.thumbs_panel.set_suppressed(False)
            self.files_panel.set_suppressed(False)
            self.statusBar().show()
            self.act_exit_focus.setEnabled(False)
            self._focus_mode = False
            if self._pre_focus.get("fullscreen"):
                self.showMaximized() if self._pre_focus.get("maximized") else self.showNormal()

    def _show_about(self):
        """Über: Version und die Rechtshinweise, die die AGPL für Programme mit Bedienoberfläche verlangt
        (Urheber, Lizenz, keine Gewährleistung, wo der Quellcode liegt)."""
        from PySide6 import __version__ as pyside_version
        from . import COPYRIGHT, LICENSE_NAME
        box = QMessageBox(self)
        box.setWindowTitle(f"Über {APP_NAME}")
        box.setIcon(QMessageBox.Information)
        box.setTextFormat(Qt.RichText)
        box.setText(
            f"<b>{APP_NAME}</b> Version {__version__}<br>{COPYRIGHT}<br><br>"
            f"Freie Software unter der {LICENSE_NAME}: Du darfst das Programm benutzen, weitergeben und "
            "verändern – Weitergabe wieder unter dieser Lizenz und mit Quellcode. "
            "<b>Ohne jede Gewährleistung.</b><br><br>"
            "Der vollständige Quellcode liegt dem Programm im Ordner „Quellcode“ bei; Lizenztexte und "
            "Hinweise zu den verwendeten Bibliotheken stehen in „LIZENZEN.txt“.<br><br>"
            f"Python {sys.version.split()[0]} · Qt/PySide6 {pyside_version} (LGPL-3.0) · "
            f"PyMuPDF/MuPDF {fitz.__version__} (AGPL-3.0) · FFmpeg (LGPL-2.1)<br>"
            f"Darstellung: {'Grafikkarte (Direct3D)' if self.view.gpu else 'Software'}")
        lic = box.addButton("Lizenzen …", QMessageBox.ActionRole)
        box.addButton(QMessageBox.Ok)
        box.exec()
        if box.clickedButton() is lic:
            self._open_license_file()

    def _open_license_file(self):
        """LIZENZEN.txt öffnen (neben der .exe bzw. im Projektordner)."""
        base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else \
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(base, "LIZENZEN.txt")
        if os.path.exists(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        else:
            QMessageBox.information(self, APP_NAME, "LIZENZEN.txt wurde nicht gefunden.")

    def send_feedback(self):
        from .feedback import window_screenshot
        from .feedback_dialog import FeedbackDialog
        shot = window_screenshot(self)  # vor dem Dialog aufnehmen: zeigt das Programm, nicht den Dialog
        FeedbackDialog(self, shot).exec()

    def _show_help(self):
        lines = ["<b>Werkzeuge</b><br>"]
        lines += [f"{label} – {tip} <i>({key})</i><br>" for _, label, key, tip in TOOL_DEFS]
        lines += ["<br><b>Bedienung</b><br>",
                  "Stift schreibt immer · Finger scrollt (1 Finger) und zoomt (2 Finger)<br>",
                  "Radierer-Ende des Stifts: radieren · Stift-Seitentaste: Kontextmenü (wie Rechtsklick)<br>",
                  "<br><b>Stiftgesten</b> (Stift oder Marker)<br>",
                  "Gerade Linie: Strich ziehen und am Ende kurz still halten – danach verschiebt der Stift "
                  "den Endpunkt<br>",
                  "Kreis/Ellipse oder Rechteck: Form ungefähr zeichnen und am Ende kurz still halten – danach "
                  "skaliert der Stift die Form (gegenüberliegende Seite bleibt fest)<br>",
                  "Linien rasten nahe der Waagerechten/Senkrechten ein<br>",
                  "Seitenleisten: Klick auf die Titelzeile („‹ Dateien“, „‹ Seiten“) blendet sie aus, die Knöpfe links in der "
                  "Tableiste wieder ein (F3/F4); Breite über die Griffe zwischen den Leisten<br>",
                  "Speicherorte: ＋ Speicherort in der Dateileiste; lang drücken auf einen Speicherort zum "
                  "Umbenennen/Entfernen<br>",
                  "Löschen: über Zeichnungen hin und her kritzeln (Text und Bilder bleiben)<br>",
                  "Formen übernehmen Farbe und Dicke des zuletzt gewählten Stifts<br>",
                  "Suchen: Strg+F, nächster Treffer mit Enter oder Strg+G, vorheriger mit Strg+Umschalt+G<br>",
                  "Bild als Seite: Format, Rand (weiß/schwarz/Vorlage), Strecken/Füllen und Zuschneiden; später ändern "
                  "über Rechtsklick → Bildseite bearbeiten oder Seiten → Bildseite<br>",
                  "Einstellungen: Datei → Einstellungen (Strg+,)<br>",
                  "<br><b>Tabs</b><br>",
                  "Jede geöffnete Datei bekommt einen Tab · Strg+Tab wechselt · Strg+W oder Mittelklick schließt<br>",
                  "Tabs ziehen: neu anordnen oder auf eine andere Gruppe bzw. deren Seite ablegen<br>",
                  "Dasselbe Dokument mehrfach: Rechtsklick auf den Tab → Duplizieren, oder in der Dateileiste "
                  "Rechtsklick → Erneut öffnen · Umbenennen: Rechtsklick auf Tab oder Datei<br>",
                  "Teilen: Ansicht → Tabs → Nebeneinander/Untereinander, oder Rechtsklick auf einen Tab "
                  "(Strg+\\ teilt nebeneinander) · Klick in eine Ansicht macht sie aktiv<br>",
                  "<br><b>Dateien</b><br>",
                  "Dateien (F3): Ordner durchsuchen, Datei antippen zum Öffnen; 'Zuletzt' zeigt "
                  "zuletzt geöffnete Dateien<br>",
                  "Automatisch speichern: Datei → Automatisch speichern (für alle Dateien bzw. nur diese Datei)<br>",
                  "<br><b>Finger</b><br>",
                  "Wischen: scrollen · zwei Finger: zoomen · Tippen: auswählen/Textfeld setzen<br>",
                  "Lang antippen auf Bild/Textfeld/Zeichnung: greifen und direkt verschieben<br>",
                  "Ausgewähltes Objekt: ziehen verschiebt, Ecken ändern die Größe, erneut antippen bearbeitet Text; "
                  "Optionen (Zuschneiden, Drehen, Löschen, Fertig) in der blauen Leiste oben<br>",
                  "Lang antippen auf freie Fläche: Kontextmenü (öffnet beim Loslassen)<br>",
                  "Textfeld: Griff ✥ verschiebt, Griff ↔ ändert die Breite; daneben tippen übernimmt<br><br>",
                  "Strg + Mausrad: Zoomen · Mittlere Maustaste: Ansicht verschieben<br>",
                  "Maus-Werkzeug (H): Links anklicken · Alt+← oder Maus-Zurücktaste: zurück zur vorherigen Stelle<br>",
                  "Auswahl: Strg+C kopieren, Strg+X ausschneiden, Strg+D duplizieren · Strg+V oder lange auf die Seite "
                  "drücken: einfügen<br>",
                  "Rechtsklick auf Seite/Miniatur/Annotation: Seiten einfügen, Bild zuschneiden, löschen …<br>",
                  "Seitenleiste: wischen scrollt, tippen zeigt die Seite, lang antippen öffnet das Seitenmenü "
                  "(Einfügen, Verschieben, Drehen, Exportieren, Löschen)<br>",
                  "Eigene Stifte: „Hinzufügen“ im Menüband (Zeichnen); Rechtsklick auf einen Stift zum Bearbeiten<br>",
                  "Lassoauswahl: Objekte mit einer Schlinge umfahren wählt sie gemeinsam aus (verschieben, Entf "
                  "löscht); ein Objekt anklicken und ziehen verschiebt es, Ecken ändern die Größe; "
                  "Doppelklick bearbeitet Text bzw. schneidet Bilder zu; umfahrener Text ohne Objekte wird "
                  "kopiert<br>",
                  "Strg+V fügt ein Bild aus der Zwischenablage ein, Strg+Umschalt+V als eigene Seite · F11: Vollbild (im schwebenden Fenster ohne Vergrößern), Esc beendet<br>"]
        QMessageBox.information(self, "Hilfe", "".join(lines))

    # ================================================================ Fenster
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if not urls:
            return
        if event.modifiers() & Qt.ControlModifier:
            # Strg gedrückt: Frage, ob in das aktuelle Dokument einfügen oder in neuen Tabs öffnen
            r = QMessageBox.question(self, APP_NAME, "Datei(en) in das aktuelle Dokument einfügen?\n"
                                     "(Nein = in neuen Tabs öffnen)",
                                     QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if r == QMessageBox.Cancel:
                return
            if r == QMessageBox.Yes:
                idx = self.view.current_page() + 1
                for p in urls:
                    self.document.insert_pdf(idx, p)
                return
        for p in urls:
            self.open_file(p)

    def closeEvent(self, event):
        if self._recorder is not None:
            r = QMessageBox.question(self, APP_NAME, "Eine Tonaufnahme läuft noch. Aufnahme verwerfen und beenden?\n\n"
                                     "(„Nein“: zurück – dann über die Aufnahmeleiste „Stopp & einfügen“.)")
            if r != QMessageBox.Yes:
                event.ignore()
                return
            self._recorder.stop(True)
        asked = set()
        for tab in list(self.editor.tabs()):
            if id(tab.document) in asked:
                continue
            asked.add(id(tab.document))
            if not self._maybe_save(tab):
                event.ignore()
                return
        self.settings.save()
        self.qs.setValue("geometry", self.saveGeometry())
        for tab in self.editor.tabs():
            tab.view.media.shutdown()
        event.accept()
