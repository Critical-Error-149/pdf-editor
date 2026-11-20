"""Selbst-Update – portable Version (ZIP) und installierte Version (Setup).

Portable Version: Downloader lädt das Programm-ZIP aus dem Download-Gist (Prüfsumme SHA-256 und Größe aus
version.json), prepare() entpackt und prüft es, write_script() schreibt ein kleines PowerShell-Skript. Dieses startet
erst, wenn das Programm regulär geschlossen wurde (MainWindow.closeEvent – ungespeicherte Änderungen werden vorher
abgefragt), wartet, bis der Prozess beendet ist, legt die alten Programmdateien zur Seite, kopiert die neuen hinein
und startet das Programm neu. Geht beim Kopieren etwas schief, kommen die alten Dateien zurück.

Installierte Version (Inno Setup, erkennbar an unins000.exe neben der .exe): statt des ZIPs wird das Setup geladen und
nach dem Beenden still ausgeführt (write_setup_script) – so stimmen danach auch Versionsangabe und Deinstallation.
Einstellungen (Registry), Logs (AppData) und eigene Dateien im Programmordner bleiben in beiden Fällen unberührt.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

EXE = "PDF Editor.exe"
WORK = os.path.join(tempfile.gettempdir(), "PDF Editor Update")


def install_dir():
    """Ordner der laufenden .exe (None beim Start aus dem Quellcode)."""
    return os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else None


def is_installed():
    """Mit dem Installationsprogramm installiert (statt portabel entpackt)?"""
    folder = install_dir()
    return bool(folder) and os.path.isfile(os.path.join(folder, "unins000.exe"))


def update_source(data):
    """Was geladen wird: (Art „setup“/„zip“, URL, SHA-256, Größe). Installierte Version → Setup, sofern version.json
    eines anbietet (ältere Gist-Stände nicht – dann ersetzt das ZIP die Dateien, der Ordner ist ja beschreibbar)."""
    from . import updates
    if is_installed() and data.get("setup") and data.get("setup_sha256"):
        return "setup", data["setup"], data["setup_sha256"], data.get("setup_size")
    return "zip", data.get("download") or updates.PORTABLE_URL, data.get("sha256"), data.get("size")


def can_self_update(kind="zip"):
    """(True, "") oder (False, Grund für die Anzeige)."""
    folder = install_dir()
    if folder is None:
        return False, "Beim Start aus dem Quellcode aktualisiert man mit „git pull“."
    if kind == "setup":  # das Setup fragt bei Bedarf selbst nach Administratorrechten
        return True, ""
    try:
        fd, probe = tempfile.mkstemp(dir=folder, prefix=".schreibtest")
        os.close(fd)
        os.remove(probe)
    except OSError:
        return False, ("Der Programmordner ist schreibgeschützt (z. B. unter „Programme“) – bitte die neue Version "
                       "selbst herunterladen.")
    return True, ""


class Downloader(QObject):
    """Lädt ZIP oder Setup nach WORK und prüft Größe und SHA-256. progress(geladen, gesamt), finished(ok, Pfad/Fehler)."""
    progress = Signal(int, int)
    finished = Signal(bool, str)

    def __init__(self, url, sha256, size, parent=None, filename="update.zip"):
        super().__init__(parent)
        self.url, self.sha256, self.size = url, (sha256 or "").lower(), int(size or 0)
        self._net = QNetworkAccessManager(self)
        self._reply = None
        self._file = None
        self._hash = hashlib.sha256()
        self.path = os.path.join(WORK, filename)
        self._cancelled = False

    def start(self):
        shutil.rmtree(WORK, ignore_errors=True)
        os.makedirs(WORK, exist_ok=True)
        self._file = open(self.path, "wb")
        req = QNetworkRequest(QUrl(self.url))
        req.setTransferTimeout(60000)  # bricht ab, wenn 60 s lang nichts ankommt
        # GitHub-Releases leiten zum eigentlichen Speicherort weiter (https → https)
        req.setAttribute(QNetworkRequest.RedirectPolicyAttribute, QNetworkRequest.NoLessSafeRedirectPolicy)
        self._reply = self._net.get(req)
        self._reply.readyRead.connect(self._read)
        self._reply.downloadProgress.connect(lambda done, total: self.progress.emit(done, total or self.size))
        self._reply.finished.connect(self._done)

    def cancel(self):
        self._cancelled = True
        if self._reply is not None:
            self._reply.abort()

    def _read(self):
        data = bytes(self._reply.readAll())
        self._hash.update(data)
        self._file.write(data)

    def _done(self):
        reply, self._reply = self._reply, None
        self._read_rest(reply)
        self._file.close()
        # kein reply.deleteLater(): die Antwort gehört dem QNetworkAccessManager und verschwindet mit diesem Objekt
        # (vorgemerktes Löschen + Löschen des Managers durch Python brachte Qt zum Absturz)
        if self._cancelled:
            self.finished.emit(False, "Abgebrochen.")
        elif reply.error() != QNetworkReply.NoError:
            self.finished.emit(False, f"Download fehlgeschlagen: {reply.errorString()}")
        elif self.size and os.path.getsize(self.path) != self.size:
            self.finished.emit(False, "Download unvollständig (Größe stimmt nicht).")
        elif self.sha256 and self._hash.hexdigest() != self.sha256:
            self.finished.emit(False, "Download beschädigt (Prüfsumme stimmt nicht) – bitte später erneut versuchen.")
        else:
            self.finished.emit(True, self.path)

    def _read_rest(self, reply):
        data = bytes(reply.readAll())
        if data:
            self._hash.update(data)
            self._file.write(data)


def prepare(zip_path):
    """ZIP entpacken und prüfen. Rückgabe: Ordner mit den neuen Programmdateien (darin „PDF Editor.exe“)."""
    target = os.path.join(WORK, "neu")
    shutil.rmtree(target, ignore_errors=True)
    with zipfile.ZipFile(zip_path) as z:
        bad = z.testzip()
        if bad is not None:
            raise ValueError(f"Beschädigte Datei im ZIP: {bad}")
        for name in z.namelist():  # nur Pfade innerhalb des Zielordners (kein „../“)
            full = os.path.normpath(os.path.join(target, name))
            if not full.startswith(os.path.normpath(target) + os.sep):
                raise ValueError(f"Ungültiger Pfad im ZIP: {name}")
        z.extractall(target)
    src = os.path.join(target, "PDF Editor")
    if not (os.path.isfile(os.path.join(src, EXE)) and os.path.isdir(os.path.join(src, "_internal"))):
        raise ValueError("Das ZIP enthält kein vollständiges Programm.")
    return src


SETUP_FILE = "PDF-Editor-Setup.exe"


def prepare_setup(path):
    """Geladenes Setup prüfen (Windows-Programm). Rückgabe: Pfad."""
    with open(path, "rb") as f:
        if f.read(2) != b"MZ":
            raise ValueError("Die geladene Datei ist kein Installationsprogramm.")
    return path


def _ps(text):
    return "'" + text.replace("'", "''") + "'"


def write_script(src, dst, pid):
    """PowerShell-Skript, das nach dem Ende von Prozess pid die Dateien ersetzt und das Programm neu startet."""
    log = os.path.join(WORK, "update.log")
    script = f"""$ErrorActionPreference = 'Stop'
$src = {_ps(src)}
$dst = {_ps(dst)}
$log = {_ps(log)}
$exe = Join-Path $dst {_ps(EXE)}
function Log($m) {{ Add-Content -Path $log -Value ((Get-Date -Format 'HH:mm:ss') + ' ' + $m) -Encoding UTF8 }}
$backup = @(@('_internal', '_internal.alt'), @({_ps(EXE)}, {_ps(EXE + '.alt')}))
try {{
    Log 'Warte auf das Ende des Programms ({pid})'
    try {{ Wait-Process -Id {pid} -Timeout 120 }} catch {{ }}
    Start-Sleep -Milliseconds 700
    foreach ($b in $backup) {{
        $alt = Join-Path $dst $b[1]
        if (Test-Path $alt) {{ Remove-Item $alt -Recurse -Force }}
        $cur = Join-Path $dst $b[0]
        for ($i = 0; $i -lt 30 -and (Test-Path $cur); $i++) {{
            try {{ Rename-Item $cur $b[1] }} catch {{ Start-Sleep -Milliseconds 500 }}
        }}
        if (Test-Path $cur) {{ throw ('Datei noch in Benutzung: ' + $b[0]) }}
    }}
    try {{
        foreach ($n in @('licenses', 'Quellcode')) {{
            $p = Join-Path $dst $n
            if (Test-Path $p) {{ Remove-Item $p -Recurse -Force }}
        }}
        Copy-Item -Path (Join-Path $src '*') -Destination $dst -Recurse -Force
        foreach ($b in $backup) {{ Remove-Item (Join-Path $dst $b[1]) -Recurse -Force -ErrorAction SilentlyContinue }}
        Log 'Aktualisiert'
    }} catch {{
        Log ('Fehler beim Kopieren: ' + $_ + ' - alte Version wird wiederhergestellt')
        foreach ($b in $backup) {{
            $cur = Join-Path $dst $b[0]
            if (Test-Path $cur) {{ Remove-Item $cur -Recurse -Force -ErrorAction SilentlyContinue }}
            Rename-Item (Join-Path $dst $b[1]) $b[0] -ErrorAction SilentlyContinue
        }}
    }}
}} catch {{
    Log ('Fehler: ' + $_)
    foreach ($b in $backup) {{  # nichts kaputt hinterlassen
        $cur = Join-Path $dst $b[0]
        $alt = Join-Path $dst $b[1]
        if (-not (Test-Path $cur) -and (Test-Path $alt)) {{ Rename-Item $alt $b[0] -ErrorAction SilentlyContinue }}
    }}
}}
Start-Process -FilePath $exe
"""
    path = os.path.join(WORK, "aktualisieren.ps1")
    with open(path, "w", encoding="utf-8-sig") as f:  # mit BOM: Windows PowerShell 5.1 liest sonst ANSI
        f.write(script)
    return path


def write_setup_script(setup, dst, pid):
    """PowerShell-Skript, das nach dem Ende von Prozess pid das Setup still ausführt und das Programm neu startet.
    Das Setup übernimmt Ordner und Aufgaben der bisherigen Installation; schlägt es fehl, bleibt die alte Version."""
    log = os.path.join(WORK, "update.log")
    script = f"""$ErrorActionPreference = 'Stop'
$setup = {_ps(setup)}
$log = {_ps(log)}
$exe = Join-Path {_ps(dst)} {_ps(EXE)}
function Log($m) {{ Add-Content -Path $log -Value ((Get-Date -Format 'HH:mm:ss') + ' ' + $m) -Encoding UTF8 }}
try {{
    Log 'Warte auf das Ende des Programms ({pid})'
    try {{ Wait-Process -Id {pid} -Timeout 120 }} catch {{ }}
    Start-Sleep -Milliseconds 700
    Log 'Starte das Setup'
    $p = Start-Process -FilePath $setup -Wait -PassThru -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES',
        '/NORESTART', '/NOCANCEL', '/CLOSEAPPLICATIONS', ('/LOG="' + (Join-Path {_ps(WORK)} 'setup.log') + '"'))
    if ($p.ExitCode -eq 0) {{ Log 'Aktualisiert' }} else {{ Log ('Fehler: Setup endete mit Code ' + $p.ExitCode) }}
}} catch {{
    Log ('Fehler: ' + $_)
}}
Start-Process -FilePath $exe
"""
    path = os.path.join(WORK, "aktualisieren.ps1")
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(script)
    return path


def script_for(update, pid):
    """Passendes Hilfsskript: update ist das geladene Setup (.exe) oder der entpackte Programmordner."""
    if update.lower().endswith(".exe"):
        return write_setup_script(update, install_dir(), pid)
    return write_script(update, install_dir(), pid)


def launch(script):
    """Skript unabhängig vom Programm starten (läuft weiter, wenn das Programm endet)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                      "-File", script], creationflags=flags, close_fds=True)
