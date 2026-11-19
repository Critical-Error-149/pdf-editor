"""Dialog „Update verfügbar“: Änderungen seit der installierten Version, „Jetzt aktualisieren“ (Selbst-Update),
„Download-Seite öffnen“, „Später“. Erscheint nach der automatischen Prüfung beim Start oder nach „Nach Updates
suchen“ in den Einstellungen."""
from __future__ import annotations

import html

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QMessageBox, QProgressBar, QPushButton,
                               QTextBrowser, QVBoxLayout)

from . import __version__, self_update, updates


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
                lis = "".join(f"<li>{html.escape(c)}</li>" for c in items)
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
        self.later_btn = buttons.addButton("Später", QDialogButtonBox.RejectRole)
        for b in self.findChildren(QPushButton):
            b.setAutoDefault(False)
        self.update_btn.setDefault(True)
        ok, reason = self_update.can_self_update()
        if ok and not data.get("sha256"):
            ok, reason = False, "Für diese Version fehlt die Prüfsumme – bitte selbst herunterladen."
        self.update_btn.setEnabled(ok)
        if not ok:
            self.update_btn.setToolTip(reason)
            self.status.setText(reason)
        self.update_btn.clicked.connect(self._update)
        self.page_btn.clicked.connect(self._open_page)
        self.later_btn.clicked.connect(self.reject)
        lay.addWidget(buttons)
        self._dl = None

    def _open_page(self):
        QDesktopServices.openUrl(QUrl(self.data.get("page") or updates.GIST_PAGE))

    def _update(self):
        self.update_btn.setEnabled(False)
        self.page_btn.setEnabled(False)
        self.later_btn.setText("Abbrechen")
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.status.setText("Lade die neue Version …")
        self._dl = self_update.Downloader(self.data.get("download") or updates.GIST_DOWNLOAD,
                                          self.data.get("sha256"), self.data.get("size"), self)
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
            src = self_update.prepare(info)
        except (OSError, ValueError) as e:
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
