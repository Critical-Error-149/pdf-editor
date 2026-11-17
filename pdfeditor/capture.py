"""Aufnehmen: Fotos/Videos mit der Windows-Kamera-App, Ton mit dem Mikrofon (Qt Multimedia, im Hintergrund).

Kamera: Die Windows-Kamera-App nimmt flüssig auf (auch Video), gibt ihr Ergebnis aber nicht an andere
Programme zurück – sie speichert in „Bilder\\Eigene Aufnahmen“. Der Editor merkt sich darum vor dem Start
den Inhalt dieses Ordners, erkennt danach neu entstandene Fotos/Videos, wartet bis sie fertig geschrieben
sind und bettet sie ein. Anschließend kann er genau diese Dateien wieder löschen (in den Papierkorb) –
aber nur, wenn sie nachweislich neu sind und ihr Inhalt Byte für Byte dem Eingebetteten entspricht.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import time
import uuid
from ctypes import wintypes as W
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout

from . import media

CAMERA_ROLL_ID = "{AB5FB87B-7CE2-4F83-915D-550846C9537B}"  # Known Folder „Eigene Aufnahmen“
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".heic", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".wmv"}
STABLE_S = 1.5  # so lange darf sich eine neue Datei nicht mehr ändern, bevor sie als fertig gilt


def _stamp():
    return time.strftime("%Y-%m-%d %H-%M-%S")


# ================================================================ Windows-Kamera-App
def camera_roll() -> str:
    """Ordner, in den die Kamera-App speichert („Bilder\\Eigene Aufnahmen“, auch wenn umgeleitet/OneDrive)."""
    class GUID(ctypes.Structure):
        _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]
    try:
        u = uuid.UUID(CAMERA_ROLL_ID)
        g = GUID(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8)(*u.bytes[8:]))
        p = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(p)) == 0:
            path = p.value
            ctypes.windll.ole32.CoTaskMemFree(p)
            if path:
                return path
    except (OSError, AttributeError):
        pass
    return os.path.join(os.path.expanduser("~"), "Pictures", "Camera Roll")


def _listing(folder):
    """{Dateiname (klein): (Größe, Änderungszeit ns, Erstellzeit ns)} – nur Dateien direkt im Ordner."""
    out = {}
    try:
        with os.scandir(folder) as it:
            for e in it:
                if e.is_file(follow_symlinks=False):
                    st = e.stat(follow_symlinks=False)
                    born = getattr(st, "st_birthtime_ns", None) or st.st_ctime_ns
                    out[e.name.lower()] = (e.name, st.st_size, st.st_mtime_ns, born)
    except OSError:
        pass
    return out


def _unlocked(path) -> bool:
    """Schreibt kein anderes Programm mehr an der Datei? (Öffnen ohne Schreib-Freigabe gelingt nur dann)"""
    k = ctypes.windll.kernel32
    k.CreateFileW.restype = W.HANDLE
    k.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, W.LPVOID, W.DWORD, W.DWORD, W.HANDLE]
    h = k.CreateFileW(path, 0x80000000, 0x1, None, 3, 0, None)  # GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING
    if h in (None, W.HANDLE(-1).value):
        return False
    k.CloseHandle(h)
    return True


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _to_recycle_bin(path) -> bool:
    """Datei in den Papierkorb verschieben (ohne Rückfrage)."""
    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", W.HWND), ("wFunc", W.UINT), ("pFrom", W.LPCWSTR), ("pTo", W.LPCWSTR),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", W.BOOL), ("hNameMappings", W.LPVOID),
                    ("lpszProgressTitle", W.LPCWSTR)]
    op = SHFILEOPSTRUCTW()
    op.wFunc = 3  # FO_DELETE
    op.pFrom = os.path.abspath(path) + "\0"  # doppelt nullterminiert (ctypes hängt die zweite Null an)
    op.fFlags = 0x0040 | 0x0010 | 0x0004 | 0x0400  # ALLOWUNDO | NOCONFIRMATION | SILENT | NOERRORUI
    r = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return r == 0 and not op.fAnyOperationsAborted and not os.path.exists(path)


@dataclass
class Shot:
    """Eine von der Kamera-App neu gespeicherte Datei."""
    path: str
    name: str
    kind: str          # "photo" oder "video"
    size: int
    mtime_ns: int
    data: bytes = b""
    sha256: str = ""

    def load(self):
        """Inhalt einlesen (danach wird genau dieser Inhalt eingebettet und beim Löschen verglichen)."""
        with open(self.path, "rb") as f:
            self.data = f.read()
        self.sha256 = hashlib.sha256(self.data).hexdigest()


class CameraAppSession:
    """Merkt sich den Ordnerinhalt vor dem Start der Kamera-App und findet danach neue, fertige Aufnahmen."""

    def __init__(self, folder=None):
        self.folder = os.path.realpath(folder or camera_roll())
        self.before = _listing(self.folder)  # Schnappschuss: diese Dateien werden nie angefasst
        self.t0_ns = time.time_ns() - 2_000_000_000
        self._watch = {}  # Name -> (Größe, Änderungszeit, seit wann unverändert)
        self.ready = {}   # Name -> Shot

    def launch(self):
        os.startfile("microsoft.windows.camera:")

    def poll(self):
        """Neue Dateien prüfen. Rückgabe: Anzahl noch nicht fertig geschriebener Aufnahmen."""
        now = time.monotonic()
        pending = 0
        for key, (name, size, mtime, born) in _listing(self.folder).items():
            ext = os.path.splitext(key)[1]
            if key in self.before or key in self.ready or ext not in PHOTO_EXT | VIDEO_EXT:
                continue
            if name.startswith(("~", ".")) or born < self.t0_ns:
                continue
            old = self._watch.get(key)
            if old is None or old[:2] != (size, mtime):
                self._watch[key] = (size, mtime, now)
                pending += 1
                continue
            path = os.path.join(self.folder, name)
            if size > 0 and now - old[2] >= STABLE_S and _unlocked(path):
                self.ready[key] = Shot(path, name, "photo" if ext in PHOTO_EXT else "video", size, mtime)
                self._watch.pop(key, None)
            else:
                pending += 1
        return pending

    def shots(self):
        return sorted(self.ready.values(), key=lambda s: s.mtime_ns)

    def delete(self, shot: Shot) -> bool:
        """Aufnahme löschen – nur wenn sie zweifelsfrei die eingebettete ist."""
        path = shot.path
        if os.path.normcase(os.path.realpath(os.path.dirname(path))) != os.path.normcase(self.folder):
            return False  # nicht (mehr) im Kamera-Ordner
        if shot.name.lower() in self.before or not shot.sha256:
            return False  # gab es schon vor dem Start der Kamera-App / nie eingelesen
        try:
            st = os.stat(path)
        except OSError:
            return False
        born = getattr(st, "st_birthtime_ns", None) or st.st_ctime_ns
        if born < self.t0_ns or st.st_size != shot.size or st.st_mtime_ns != shot.mtime_ns:
            return False  # inzwischen eine andere/veränderte Datei
        if st.st_size != len(shot.data) or _sha256(path) != shot.sha256:
            return False  # Inhalt weicht vom Eingebetteten ab
        return _to_recycle_bin(path)


class CameraAppDialog(QDialog):
    """Startet die Kamera-App und wartet auf neue Fotos/Videos. Ergebnis: self.session.shots()."""

    def __init__(self, parent, delete_default=True):
        super().__init__(parent)
        self.setWindowTitle("Foto oder Video aufnehmen")
        self.session = CameraAppSession()
        self._left = False
        lay = QVBoxLayout(self)
        info = QLabel("Die Windows-Kamera-App wird geöffnet. Nimm dort ein oder mehrere Fotos oder Videos auf "
                      "und wechsle danach zurück zum Editor – die Aufnahmen werden automatisch eingefügt.")
        info.setWordWrap(True)
        lay.addWidget(info)
        self.status = QLabel("Warte auf Aufnahmen …")
        f = self.status.font()
        f.setPointSizeF(f.pointSizeF() * 1.25)
        self.status.setFont(f)
        lay.addWidget(self.status)
        where = QLabel(f"Überwachter Ordner: {self.session.folder}")
        where.setWordWrap(True)
        where.setEnabled(False)
        lay.addWidget(where)
        self.delete_after = QCheckBox("Aufnahmen nach dem Einfügen aus „Eigene Aufnahmen“ löschen (Papierkorb)")
        self.delete_after.setChecked(delete_default)
        lay.addWidget(self.delete_after)
        row = QHBoxLayout()
        again = QPushButton("Kamera öffnen")
        again.clicked.connect(self._launch)
        row.addWidget(again)
        row.addStretch(1)
        self.insert_btn = QPushButton("Einfügen")
        self.insert_btn.setEnabled(False)
        self.insert_btn.setDefault(True)
        self.insert_btn.clicked.connect(self._finish)
        row.addWidget(self.insert_btn)
        cancel = QPushButton("Abbrechen")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        lay.addLayout(row)
        self.resize(QSize(520, 220))
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._poll)
        self.timer.start()
        QTimer.singleShot(0, self._launch)

    def _launch(self):
        try:
            self.session.launch()
        except OSError as e:
            QMessageBox.warning(self, "Kamera", f"Die Kamera-App konnte nicht gestartet werden:\n{e}")

    def _poll(self):
        pending = self.session.poll()
        shots = self.session.shots()
        photos = sum(1 for s in shots if s.kind == "photo")
        videos = len(shots) - photos
        parts = [f"{photos} Foto" + ("s" if photos != 1 else "")] if photos else []
        parts += [f"{videos} Video" + ("s" if videos != 1 else "")] if videos else []
        text = (" und ".join(parts) + " bereit") if parts else "Warte auf Aufnahmen …"
        if pending:
            text += " – wird noch gespeichert …"
        self.status.setText(text)
        self.insert_btn.setEnabled(bool(shots))
        self.insert_btn.setText(f"Einfügen ({len(shots)})" if shots else "Einfügen")
        if shots and not pending and self._left and self.isActiveWindow():
            self._finish()  # zurück im Editor: Aufnahmen einfügen

    def event(self, e):
        if e.type() == QEvent.WindowDeactivate:
            self._left = True  # Kamera-App ist im Vordergrund
        return super().event(e)

    def _finish(self):
        self.timer.stop()
        try:
            for s in self.session.shots():
                s.load()
        except OSError as e:
            QMessageBox.warning(self, "Kamera", f"Aufnahme konnte nicht gelesen werden:\n{e}")
            self.timer.start()
            return
        self.accept()

    def done(self, r):
        self.timer.stop()
        super().done(r)


# ================================================================ Mikrofon


class AudioRecorder(QObject):
    """Tonaufnahme im Hintergrund – man arbeitet weiter, gesteuert über die Aufnahmeleiste.
    Signale: duration_changed(ms), state_changed("recording"/"paused"), finished(Pfad; "" = verworfen/fehlgeschlagen),
    failed(Meldung). Die Qt-Multimedia-Objekte hängen nicht an Widgets und werden über media.retire abgebaut."""
    duration_changed = Signal(int)
    state_changed = Signal(str)
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        from PySide6.QtMultimedia import QAudioInput, QMediaCaptureSession, QMediaFormat, QMediaRecorder
        g = media._grave()
        self.session = QMediaCaptureSession(g)
        self.audio_in = QAudioInput(g)
        self.session.setAudioInput(self.audio_in)
        self.recorder = QMediaRecorder(g)
        self.session.setRecorder(self.recorder)
        fmt = QMediaFormat(QMediaFormat.FileFormat.Mpeg4Audio)
        fmt.setAudioCodec(QMediaFormat.AudioCodec.AAC)
        self.recorder.setMediaFormat(fmt)
        self.recorder.setQuality(QMediaRecorder.HighQuality)
        # an Methoden dieses Objekts (nicht an lambdas): laufen sicher im Haupt-Thread
        self.recorder.recorderStateChanged.connect(self._on_state)
        self.recorder.errorOccurred.connect(self._on_error)
        self.recorder.durationChanged.connect(self._on_duration)
        self.name = "Audio " + _stamp() + ".m4a"
        self.path = os.path.join(media.temp_dir(), self.name)
        self.paused = False
        self._stopping = False
        self._discard = False
        self._done = False

    def start(self):
        self.recorder.setOutputLocation(QUrl.fromLocalFile(self.path))
        self.recorder.record()

    def _invoke(self, slot):
        from PySide6.QtCore import QMetaObject
        QMetaObject.invokeMethod(self.recorder, slot, Qt.QueuedConnection)  # aus der Ereignisschleife (kein GIL)

    def pause(self):
        if not self._stopping:
            self._invoke("pause")

    def resume(self):
        if not self._stopping:
            self._invoke("record")

    def stop(self, discard=False):
        """Aufnahme beenden; discard: verwerfen statt einfügen."""
        if self._stopping:
            self._discard = self._discard or discard
            return
        self._stopping = True
        self._discard = discard
        self._invoke("stop")
        QTimer.singleShot(5000, self._finish)  # Sicherheitsnetz, falls kein Zustandswechsel kommt

    def _on_state(self, state):
        from PySide6.QtMultimedia import QMediaRecorder
        if state == QMediaRecorder.StoppedState and self._stopping:
            QTimer.singleShot(200, self._finish)  # Datei ist erst fertig, wenn der Rekorder angehalten hat
        elif state == QMediaRecorder.PausedState:
            self.paused = True
            self.state_changed.emit("paused")
        elif state == QMediaRecorder.RecordingState:
            self.paused = False
            self.state_changed.emit("recording")

    def _on_duration(self, ms):
        self.duration_changed.emit(int(ms))

    def _on_error(self, _err, msg):
        if msg:
            self.failed.emit(msg)
        self.stop(discard=True)

    def _finish(self):
        if self._done:
            return
        self._done = True
        loc = self.recorder.actualLocation().toLocalFile()
        if loc and os.path.exists(loc):
            self.path = loc
            self.name = os.path.basename(loc)
        media.retire(None, self.recorder, self.session, self.audio_in)
        ok = os.path.exists(self.path) and os.path.getsize(self.path) > 0
        if self._discard or not ok:
            try:
                if os.path.exists(self.path):
                    os.remove(self.path)  # eigene Datei im Temp-Ordner des Programms
            except OSError:
                pass
            self.finished.emit("")
        else:
            self.finished.emit(self.path)
