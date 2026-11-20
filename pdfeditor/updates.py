"""Nach Updates suchen – über die Releases des öffentlichen GitHub-Repositorys, also ohne Anmeldung.

build.py --release legt jede Version als Release „v<Version>“ an: Setup, portables ZIP, Quellcode-ZIP und eine kleine
version.json, alle mit festem Namen. So zeigt „…/releases/latest/download/<Datei>“ immer auf die neueste Version
(Download-Links zum Weitergeben, VERSION_URL). Die Prüfung liest nur version.json (QtNetwork mit der Windows-eigenen
Verschlüsselung „Schannel“ – OpenSSL ist in der .exe nicht enthalten; GitHubs Weiterleitungen folgt Qt von selbst) und
vergleicht die Versionsnummern; die Links in version.json zeigen auf genau diese Version (passend zur Prüfsumme).

Brücke für alte Versionen: Bis 0.2.8 lag alles im Download-Gist (GIST_*); diese Versionen lesen ihre version.json
weiterhin dort. build.py schreibt deshalb auch die Gist-version.json – sie verweist auf die Dateien im Release.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

REPO = "Critical-Error-149/pdf-editor"   # öffentlich
RELEASES_URL = f"https://github.com/{REPO}/releases"
DOWNLOAD_PAGE = f"{RELEASES_URL}/latest"  # Download-Seite der neuesten Version (zum Weitergeben)

# Release-Dateien (fester Name in jedem Release)
SETUP_EXE = "PDF-Editor-Setup.exe"         # Installationsprogramm (Inno Setup)
PORTABLE_ZIP = "PDF-Editor-portable.zip"   # portable Version
SOURCE_ZIP = "PDF-Editor-Quellcode.zip"    # Quellcode getrennt (AGPL 6 d: am selben Ort wie das Programm)
VERSION_JSON = "version.json"
LATEST = f"{RELEASES_URL}/latest/download"  # + Dateiname: immer die neueste Version
SETUP_URL = f"{LATEST}/{SETUP_EXE}"
PORTABLE_URL = f"{LATEST}/{PORTABLE_ZIP}"
SOURCE_URL = f"{LATEST}/{SOURCE_ZIP}"
VERSION_URL = f"{LATEST}/{VERSION_JSON}"


def release_file_url(version, name):
    """Datei eines bestimmten Releases (für version.json: passt immer zur angegebenen Prüfsumme)."""
    return f"{RELEASES_URL}/download/v{version}/{name}"


# Brücke: Versionen bis 0.2.8 lesen ihre version.json aus dem früheren Download-Gist
GIST_USER = "Critical-Error-149"
GIST_ID = "79b10e874ee6302909641519633e18c5"
GIST_PAGE = f"https://gist.github.com/{GIST_USER}/{GIST_ID}"
TIMEOUT_MS = 15000


SKIP_KEY = "updates/skipped"  # übersprungene Version: die Prüfung beim Start meldet genau diese nicht mehr


def skipped_version():
    from PySide6.QtCore import QSettings
    return str(QSettings("PDFEditor", "PDFEditor").value(SKIP_KEY, "") or "")


def set_skipped_version(version):
    """Version überspringen ("" = nichts überspringen). Eine neuere Version wird wieder gemeldet."""
    from PySide6.QtCore import QSettings
    qs = QSettings("PDFEditor", "PDFEditor")
    if version:
        qs.setValue(SKIP_KEY, version)
    else:
        qs.remove(SKIP_KEY)


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
    Ergebnis: "newer", "current", "none" (noch keine Versionsinfo im Release) oder "error"."""
    finished = Signal(str, str, str)

    def __init__(self, current_version, parent=None):
        super().__init__(parent)
        self.current = current_version
        self._net = QNetworkAccessManager(self)
        self._reply = None
        self.data = {}  # vollständige version.json (Änderungsliste, Prüfsumme, Download) nach erfolgreicher Prüfung

    def start(self):
        # Zeitstempel in der Adresse: an keinem Zwischenspeicher hängen bleiben
        req = QNetworkRequest(QUrl(f"{VERSION_URL}?t={int(time.time())}"))
        req.setAttribute(QNetworkRequest.RedirectPolicyAttribute, QNetworkRequest.NoLessSafeRedirectPolicy)
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
                self.finished.emit("none", "", DOWNLOAD_PAGE)
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
            self.finished.emit("error", "", "Unerwartete Antwort von GitHub.")
            return
        page = data.get("page") or DOWNLOAD_PAGE
        self.data = data
        newer = parse_version(latest) > parse_version(self.current)
        self.finished.emit("newer" if newer else "current", latest, page)
