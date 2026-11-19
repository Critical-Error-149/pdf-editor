"""Nach Updates suchen – über den (geheimen) Download-Gist, also ohne Anmeldung und auch bei anderen.

build.py --release legt bei jeder Veröffentlichung das ZIP (fester Name, der Download-Link bleibt gleich) und eine
kleine version.json in den Gist. Die Prüfung liest nur version.json (QtNetwork mit der Windows-eigenen
Verschlüsselung „Schannel“ – OpenSSL ist in der .exe nicht enthalten) und vergleicht die Versionsnummern; heruntergeladen
wird im Browser über die Gist-Seite mit Anleitung.

Die GitHub-Releases im privaten Repository (REPO) bleiben das Archiv aller Versionen; build.py veröffentlicht dort
mit der GitHub-Kommandozeile (gh_path).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

REPO = "Critical-Error-149/pdf-editor"
RELEASES_URL = f"https://github.com/{REPO}/releases"

GIST_USER = "Critical-Error-149"
GIST_ID = "79b10e874ee6302909641519633e18c5"
GIST_ZIP = "PDF-Editor-portable.zip"
GIST_PAGE = f"https://gist.github.com/{GIST_USER}/{GIST_ID}"
GIST_RAW = f"https://gist.githubusercontent.com/{GIST_USER}/{GIST_ID}/raw"
GIST_DOWNLOAD = f"{GIST_RAW}/{GIST_ZIP}"
GIST_SOURCE_ZIP = "PDF-Editor-Quellcode.zip"  # Quellcode getrennt (AGPL 6 d: am selben Ort wie das Programm)
GIST_SOURCE = f"{GIST_RAW}/{GIST_SOURCE_ZIP}"
VERSION_URL = f"{GIST_RAW}/version.json"
TIMEOUT_MS = 15000


def parse_version(text):
    """„v0.2.10“ → (0, 2, 10); ohne Zahlen → ()."""
    return tuple(int(n) for n in re.findall(r"\d+", text or ""))


def changes_since(data, installed):
    """Änderungen der Versionen, die neuer sind als die installierte: [(Version, Datum, [Einträge])]."""
    out = []
    for entry in data.get("changelog") or []:
        v = str(entry.get("version", ""))
        if parse_version(v) > parse_version(installed):
            out.append((v, entry.get("date", ""), list(entry.get("changes") or [])))
    return out


def gh_path():
    """GitHub-Kommandozeile (nur build.py --release braucht sie)."""
    found = shutil.which("gh")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            p = os.path.join(base, "GitHub CLI", "gh.exe")
            if os.path.isfile(p):
                return p
    return None


class UpdateCheck(QObject):
    """Prüft asynchron. finished(Ergebnis, neueste Version, Adresse der Download-Seite oder Fehlertext);
    Ergebnis: "newer", "current", "none" (noch keine Versionsinfo im Gist) oder "error"."""
    finished = Signal(str, str, str)

    def __init__(self, current_version, parent=None):
        super().__init__(parent)
        self.current = current_version
        self._net = QNetworkAccessManager(self)
        self._reply = None
        self.data = {}  # vollständige version.json (Änderungsliste, Prüfsumme, Download) nach erfolgreicher Prüfung

    def start(self):
        # Zeitstempel in der Adresse: GitHub liefert Gist-Dateien sonst evtl. noch minutenlang aus dem Zwischenspeicher
        req = QNetworkRequest(QUrl(f"{VERSION_URL}?t={int(time.time())}"))
        req.setTransferTimeout(TIMEOUT_MS)
        req.setHeader(QNetworkRequest.UserAgentHeader, f"PDF-Editor/{self.current}")
        self._reply = self._net.get(req)
        self._reply.finished.connect(self._done)

    def _done(self):
        reply, self._reply = self._reply, None
        reply.deleteLater()
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        if reply.error() != QNetworkReply.NoError:
            if status == 404:
                self.finished.emit("none", "", GIST_PAGE)
            elif reply.error() == QNetworkReply.OperationCanceledError:
                self.finished.emit("error", "", "GitHub hat nicht rechtzeitig geantwortet.")
            elif reply.error() in (QNetworkReply.HostNotFoundError, QNetworkReply.TimeoutError,
                                   QNetworkReply.TemporaryNetworkFailureError,
                                   QNetworkReply.NetworkSessionFailedError):
                self.finished.emit("error", "", "Keine Verbindung zu GitHub (Internet?).")
            else:
                self.finished.emit("error", "", reply.errorString())
            return
        try:
            data = json.loads(bytes(reply.readAll()).decode("utf-8"))
            latest = str(data["version"])
        except (ValueError, KeyError, TypeError):
            self.finished.emit("error", "", "Unerwartete Antwort vom Download-Gist.")
            return
        page = data.get("page") or GIST_PAGE
        self.data = data
        newer = parse_version(latest) > parse_version(self.current)
        self.finished.emit("newer" if newer else "current", latest, page)
