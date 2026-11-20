"""Dialog „Update verfügbar“: Änderungen seit der installierten Version, „Jetzt aktualisieren“ (Selbst-Update),
„Download-Seite öffnen“, „Diese Version überspringen“ (bzw. „Wieder melden“), „Später“. Erscheint nach der
automatischen Prüfung beim Start (nicht für eine übersprungene Version) oder nach „Nach Updates suchen“ in den
Einstellungen (dort immer)."""
from __future__ import annotations

import html
import re
import zipfile

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QMessageBox, QProgressBar, QPushButton,
                               QTextBrowser, QVBoxLayout)

from . import __version__, self_update, updates


_URL = re.compile(r"https://[^\s<>\"]+[^\s<>\".,;:)]")


def _linkify(text):
    """Adressen in der Änderungsliste anklickbar machen (Text ist schon HTML-maskiert)."""
    return _URL.sub(lambda m: f"<a href='{m.group(0)}'>{m.group(0)}</a>", text)


class UpdateDialog(QDialog):
    def __init__(self, window, latest, data):
        super().__init__(window)
        self.window_ = window
        self.data = data
        self.setWindowTitle("Update verfügbar")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"<b>Version {html.escape(latest)} ist verfügbar</b> (installiert: {__version__})."))
        changes = updates.changes_since(data, __version__)
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        if changes:
            parts = []
            for v, date, items in changes:
                lis = "".join(f"<li>{_linkify(html.escape(c))}</li>" for c in items)
                parts.append(f"<p><b>Version {html.escape(v)}</b> {html.escape(date)}</p><ul>{lis}</ul>")
            browser.setHtml("".join(parts))
        else:
            browser.setPlainText("(Keine Änderungsliste vorhanden.)")
        lay.addWidget(browser, 1)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        lay.addWidget(self.progress)
        self.status = QLabel()
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        buttons = QDialogButtonBox()
        self.update_btn = buttons.addButton("Jetzt aktualisieren", QDialogButtonBox.AcceptRole)
        self.page_btn = buttons.addButton("Download-Seite öffnen", QDialogButtonBox.ActionRole)
        # Überspringen: erst eine neuere Version wird beim Start wieder gemeldet
        self.latest = latest
        self.skipped = updates.skipped_version() == latest
        self.skip_btn = buttons.addButton("Wieder melden" if self.skipped else "Diese Version überspringen",
                                          QDialogButtonBox.ActionRole)
        self.skip_btn.setToolTip("Beim Programmstart wieder auf diese Version hinweisen" if self.skipped else
                                 "Beim Programmstart nicht mehr auf diese Version hinweisen – erst wieder bei der "
                                 "nächsten. „Nach Updates suchen“ zeigt sie weiterhin an.")
        self.later_btn = buttons.addButton("Später", QDialogButtonBox.RejectRole)
        for b in self.findChildren(QPushButton):
            b.setAutoDefault(False)
        self.update_btn.setDefault(True)
        # installierte Version: Setup laden und still ausführen, portable Version: ZIP entpacken und Dateien ersetzen
        self.kind, self.url, self.sha256, self.size = self_update.update_source(data)
        ok, reason = self_update.can_self_update(self.kind)
        if ok and not self.sha256:
            ok, reason = False, "Für diese Version fehlt die Prüfsumme – bitte selbst herunterladen."
        self.update_btn.setEnabled(ok)
        notes = []
        if not ok:
            self.update_btn.setToolTip(reason)
            notes.append(reason)
        if self.skipped:
            notes.append("Diese Version hast du übersprungen – beim Programmstart wird sie nicht gemeldet.")
        self.status.setText("\n".join(notes))
        self.update_btn.clicked.connect(self._update)
        self.page_btn.clicked.connect(self._open_page)
        self.skip_btn.clicked.connect(self._toggle_skip)
        self.later_btn.clicked.connect(self.reject)
        lay.addWidget(buttons)
        self._dl = None

    def _toggle_skip(self):
        if self.skipped:  # „Wieder melden“
            updates.set_skipped_version("")
            msg = f"Version {self.latest} wird beim Programmstart wieder gemeldet."
        else:
            updates.set_skipped_version(self.latest)
            msg = f"Version {self.latest} übersprungen – gemeldet wird erst die nächste neue Version."
        status = getattr(self.window_, "statusBar", None)
        if callable(status):
            status().showMessage(msg, 6000)
        self.reject()

    def _open_page(self):
        QDesktopServices.openUrl(QUrl(self.data.get("page") or updates.DOWNLOAD_PAGE))

    def _update(self):
        self.update_btn.setEnabled(False)
        self.page_btn.setEnabled(False)
        self.later_btn.setText("Abbrechen")
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.status.setText("Lade die neue Version …")
        name = self_update.SETUP_FILE if self.kind == "setup" else "update.zip"
        self._dl = self_update.Downloader(self.url, self.sha256, self.size, self, filename=name)
        self._dl.progress.connect(self._on_progress)
        self._dl.finished.connect(self._on_downloaded)
        self._dl.start()

    def reject(self):
        if self._dl is not None:
            self._dl.cancel()
        super().reject()

    def _on_progress(self, done, total):
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
            self.status.setText(f"Lade die neue Version … {done / 1048576:.0f} von {total / 1048576:.0f} MB")

    def _on_downloaded(self, ok, info):
        dl, self._dl = self._dl, None
        if dl is not None and dl.parent() is self:
            dl.deleteLater()
        if not ok:
            if self.isVisible():
                self._failed(info)
            return
        try:
            src = self_update.prepare_setup(info) if self.kind == "setup" else self_update.prepare(info)
        except (OSError, ValueError, zipfile.BadZipFile) as e:
            self._failed(str(e))
            return
        self.progress.setVisible(False)
        self.status.setText("Bereit.")
        QMessageBox.information(self, "Update", "Die neue Version ist geladen. Das Programm wird jetzt beendet und "
                                                "in der neuen Version neu gestartet.\n\n(Ungespeicherte Änderungen "
                                                "werden vorher abgefragt.)")
        self.accept()
        self.window_.install_update(src)

    def _failed(self, msg):
        self.progress.setVisible(False)
        self.status.setText(msg)
        self.later_btn.setText("Schließen")
        self.page_btn.setEnabled(True)
        self.update_btn.setEnabled(True)
