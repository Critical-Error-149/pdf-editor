"""Nach Updates suchen: neueste veröffentlichte Version (GitHub-Release) im eigenen Repository.

Das Repository ist privat – ohne Anmeldung zeigt GitHub es nicht. Geprüft wird darum über die GitHub-Kommandozeile
(gh), sofern sie installiert und angemeldet ist; heruntergeladen wird im Browser auf der Release-Seite (dort ist man
angemeldet). Ohne gh bleibt nur „Seite öffnen“.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading

from PySide6.QtCore import QObject, Signal

REPO = "Critical-Error-149/pdf-editor"
RELEASES_URL = f"https://github.com/{REPO}/releases"
TIMEOUT_S = 20


def parse_version(text):
    """„v0.2.10“ → (0, 2, 10); ohne Zahlen → ()."""
    return tuple(int(n) for n in re.findall(r"\d+", text or ""))


def gh_path():
    found = shutil.which("gh")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            p = os.path.join(base, "GitHub CLI", "gh.exe")
            if os.path.isfile(p):
                return p
    return None


def _run_gh(gh):
    """(Rückgabecode, Ausgabe, Fehlertext) von „gh release view“ – ohne Konsolenfenster."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    p = subprocess.run([gh, "release", "view", "--repo", REPO, "--json", "tagName,url,name"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=TIMEOUT_S, creationflags=flags)
    return p.returncode, p.stdout, p.stderr


class UpdateCheck(QObject):
    """Prüft im Hintergrund. finished(Ergebnis, neueste Version, Adresse oder Fehlertext);
    Ergebnis: "newer", "current", "none" (noch nichts veröffentlicht) oder "error"."""
    finished = Signal(str, str, str)

    def __init__(self, current_version, parent=None):
        super().__init__(parent)
        self.current = current_version

    def start(self):
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self):
        result = self._check()
        try:
            self.finished.emit(*result)  # wird im Hauptthread zugestellt (Objekt lebt dort)
        except RuntimeError:
            pass  # Dialog inzwischen geschlossen

    def _check(self):
        gh = gh_path()
        if gh is None:
            return "error", "", "Die GitHub-Kommandozeile (gh) ist auf diesem PC nicht installiert."
        try:
            code, out, err = _run_gh(gh)
        except subprocess.TimeoutExpired:
            return "error", "", "GitHub hat nicht rechtzeitig geantwortet (keine Internetverbindung?)."
        except OSError as e:
            return "error", "", f"gh konnte nicht gestartet werden: {e}"
        low = err.lower()
        if code != 0:
            if "release not found" in low:
                return "none", "", RELEASES_URL
            if "auth login" in low or "not logged" in low or "authentication" in low:
                return "error", "", "gh ist nicht bei GitHub angemeldet (im Terminal: gh auth login)."
            if "could not resolve" in low or "connect" in low or "timeout" in low:
                return "error", "", "Keine Verbindung zu GitHub."
            return "error", "", (err.strip().splitlines() or ["Unbekannter Fehler"])[0]
        try:
            data = json.loads(out)
        except ValueError:
            return "error", "", "Unerwartete Antwort von GitHub."
        tag = data.get("tagName", "")
        url = data.get("url") or RELEASES_URL
        newer = parse_version(tag) > parse_version(self.current)
        return ("newer" if newer else "current"), tag.lstrip("vV"), url
