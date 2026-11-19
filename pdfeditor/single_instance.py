"""Nur ein laufendes Programm: Weitere Starts (z. B. Doppelklick auf eine PDF im Explorer) geben ihre Dateien an das
laufende Programm weiter – es öffnet sie als Tabs und holt sein Fenster nach vorn – und beenden sich sofort.

Wer zuerst startet, bestimmt ein Windows-Mutex (auch wenn der Explorer für mehrere markierte Dateien mehrere Programme
gleichzeitig startet). Die Dateinamen gehen über eine lokale Verbindung (Named Pipe), die nur das eigene Windows-Konto
benutzen darf; das laufende Programm bestätigt mit „ok“. Kommt keine Bestätigung (Programm hängt), öffnet der neue
Start zur Sicherheit doch ein eigenes Fenster.
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import time

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

MAX_MESSAGE = 1_000_000  # Bytes – mehr ist keine Dateiliste
_mutex = None            # bleibt bis Programmende geöffnet (Windows gibt es danach selbst frei)
_running = None
RUNNING_MUTEX = "PDFEditorLaeuft"


def _name():
    if os.environ.get("PDFEDITOR_INSTANCE_NAME"):  # Tests: eigener Name, damit nichts beim echten Programm landet
        return os.environ["PDFEDITOR_INSTANCE_NAME"]
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "benutzer"
    return f"PDFEditor-{user}"


def is_primary():
    """True = erster Start in dieser Windows-Sitzung (dieses Programm nimmt künftig die Dateien an)."""
    global _mutex
    if sys.platform != "win32":
        return True
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    _mutex = k32.CreateMutexW(None, False, "Local\\" + _name())
    primary = not (_mutex and ctypes.get_last_error() == 183)  # ERROR_ALREADY_EXISTS
    # fester Name für das Deinstallationsprogramm (installer/PDF Editor.iss): erkennt so ein laufendes Programm
    global _running
    _running = k32.CreateMutexW(None, False, RUNNING_MUTEX)
    return primary


def send_to_running(paths, timeout=8.0):
    """Dateien an das laufende Programm geben. True = angenommen, dieser Start kann sich beenden."""
    deadline = time.monotonic() + timeout
    sock = QLocalSocket()
    while True:  # das erste Programm startet vielleicht gerade erst: einige Sekunden lang versuchen
        sock.connectToServer(_name())
        if sock.waitForConnected(300):
            break
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)
    if sys.platform == "win32":
        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY: es darf sein Fenster nach vorn holen
    sock.write((json.dumps(paths) + "\n").encode("utf-8"))
    sock.waitForBytesWritten(2000)
    answer = b""
    while time.monotonic() < deadline:
        if sock.bytesAvailable() or sock.waitForReadyRead(200):
            answer += bytes(sock.readAll())
            if b"ok" in answer:
                sock.disconnectFromServer()
                return True
        elif sock.state() == QLocalSocket.UnconnectedState:
            break
    return False


class Server(QObject):
    """Im ersten Programm: nimmt Dateilisten weiterer Starts an."""
    files_received = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.UserAccessOption)  # nur das eigene Windows-Konto
        QLocalServer.removeServer(_name())
        self.ok = self._server.listen(_name())
        self._server.newConnection.connect(self._accept)

    def _accept(self):
        while self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            state = {"buf": b"", "done": False}
            sock.readyRead.connect(lambda s=sock, st=state: self._read(s, st))
            sock.disconnected.connect(sock.deleteLater)
            self._read(sock, state)

    def _read(self, sock, state):
        if state["done"]:
            return
        state["buf"] += bytes(sock.readAll())
        if b"\n" not in state["buf"]:
            if len(state["buf"]) > MAX_MESSAGE:
                state["done"] = True
                sock.disconnectFromServer()
            return
        state["done"] = True
        try:
            paths = json.loads(state["buf"].split(b"\n", 1)[0].decode("utf-8"))
        except ValueError:
            paths = None
        if isinstance(paths, list) and all(isinstance(p, str) for p in paths):
            sock.write(b"ok\n")
            sock.flush()
            self.files_received.emit(paths)
        sock.disconnectFromServer()


def bring_to_front(win):
    """Fenster nach vorn holen (auch wenn es minimiert war)."""
    if win.isMinimized():  # wiederherstellen wie vorher (maximiert bleibt maximiert)
        win.setWindowState((win.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
        win.show()
    win.raise_()
    win.activateWindow()
    if sys.platform == "win32":
        ctypes.windll.user32.SetForegroundWindow(int(win.winId()))
