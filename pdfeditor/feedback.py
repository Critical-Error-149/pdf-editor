"""Feedback an den Entwickler – verschlüsselt über GitHub.

Nutzer brauchen kein GitHub-Konto, und niemand außer dem Entwickler kann die Nachrichten lesen:
- Nachricht, optionaler Name, optionale Systemdaten/Fehlerprotokoll und Bilder werden zu einem Paket gepackt und mit
  dem öffentlichen Schlüssel des Entwicklers verschlüsselt (X25519 + HKDF-SHA256 + AES-256-GCM). Den privaten
  Schlüssel gibt es nur auf seinem PC (tools/feedback_schluessel.py legt ihn an, tools/feedback_lesen.py liest).
- Das verschlüsselte Paket landet als Issue im privaten Repository REPO; was nicht in ein Issue passt (Bilder), wird
  auf Kommentare verteilt (GitHub erlaubt 65 536 Zeichen je Text).
- Zum Anlegen steckt ein Token in der .exe, das nur Issues in diesem einen Repository darf. Wer es herausliest, sieht
  dort nur verschlüsselte Pakete; er könnte höchstens Unsinn anlegen. Das Token steht nicht im Git-Repository:
  build.py setzt es beim Bauen ein (_secrets.py), beim Start aus dem Quellcode wird TOKEN_FILE gelesen.
"""
from __future__ import annotations

import base64
import datetime
import json
import os
import platform
import secrets
import sys
import zlib

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QUrl, Signal, Qt
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

FORMAT = "PDF-Editor-Feedback/1"
REPO = "Critical-Error-149/pdf-editor-feedback"
# Öffentlicher Schlüssel des Entwicklers (X25519, 32 Byte) – darf jeder kennen
PUBLIC_KEY_B64 = "ZVn281MxUiWZw4zpI4r6Qh686KKCX77hCHSgYhT41iU="
CHUNK = 60_000          # Zeichen je Issue/Kommentar (GitHub: höchstens 65 536)
IMAGE_MAX_PX = 1600     # Bilder werden verkleinert (lange Seite), als JPEG …
IMAGE_QUALITY = 82      # … in dieser Qualität: ein Fensterbild hat so meist 150–300 KB
MAX_IMAGES = 4
LOG_CHARS = 6000


def _data_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "PDF Editor", "feedback")


TOKEN_FILE = os.path.join(_data_dir(), "token.txt")
# Privater Schlüssel (nur Entwickler-PC) bewusst NICHT unter AppData: Store-Apps wie die Claude-App leiten Schreibzugriffe
# auf AppData in einen eigenen, versteckten Bereich um – dort fände man ihn weder zum Sichern noch beim Lesen
KEY_DIR = os.path.join(os.path.expanduser("~"), "PDF Editor Schlüssel")
KEY_FILE = os.path.join(KEY_DIR, "privater_schluessel.pem")


def token():
    """Token zum Anlegen der Issues: in der .exe eingebaut, sonst (Quellcode) aus TOKEN_FILE."""
    try:
        from ._secrets import feedback_token  # von build.py erzeugt, nicht im Git-Repository
        return feedback_token()
    except ImportError:
        pass
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def available():
    return bool(PUBLIC_KEY_B64) and bool(token())


# ------------------------------------------------------------------ Verschlüsselung
def _hkdf(shared, eph_pub, recipient_pub):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    return HKDF(hashes.SHA256(), 32, salt=eph_pub + recipient_pub, info=FORMAT.encode()).derive(shared)


def encrypt(payload, public_key_b64=None):
    """Paket (dict) → verschlüsselter Text (Base64). Aufbau: 0x01 | Einmal-Schlüssel (32) | Nonce (12) | Daten."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    recipient = base64.b64decode(public_key_b64 or PUBLIC_KEY_B64)
    eph = X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    key = _hkdf(eph.exchange(X25519PublicKey.from_public_bytes(recipient)), eph_pub, recipient)
    nonce = secrets.token_bytes(12)
    data = zlib.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"), 9)
    blob = b"\x01" + eph_pub + nonce + AESGCM(key).encrypt(nonce, data, FORMAT.encode())
    return base64.b64encode(blob).decode("ascii")


def decrypt(text, private_key):
    """Gegenstück (nur tools/feedback_lesen.py): verschlüsselter Text + privater Schlüssel → Paket (dict)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    blob = base64.b64decode(text)
    if blob[:1] != b"\x01":
        raise ValueError("Unbekanntes Format")
    eph_pub, nonce, ct = blob[1:33], blob[33:45], blob[45:]
    own_pub = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    key = _hkdf(private_key.exchange(X25519PublicKey.from_public_bytes(eph_pub)), eph_pub, own_pub)
    return json.loads(zlib.decompress(AESGCM(key).decrypt(nonce, ct, FORMAT.encode())).decode("utf-8"))


def split_parts(text):
    """Verschlüsselten Text auf Issue + Kommentare verteilen (jeweils mit Kopfzeile „Teil i/n“)."""
    chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)] or [""]
    n = len(chunks)
    return [f"{FORMAT}\nTeil {i}/{n}\n\n{c}" for i, c in enumerate(chunks, 1)]


def join_parts(bodies):
    """Gegenstück zu split_parts (Reihenfolge egal). Fehlt ein Teil → ValueError."""
    parts = {}
    total = None
    for body in bodies:
        lines = (body or "").replace("\r\n", "\n").split("\n")
        if len(lines) < 4 or lines[0] != FORMAT or not lines[1].startswith("Teil "):
            continue
        i, n = (int(x) for x in lines[1][5:].split("/"))
        total = n
        parts[i] = "".join(lines[3:]).strip()
    if total is None or sorted(parts) != list(range(1, total + 1)):
        raise ValueError("Unvollständig (Teile fehlen)")
    return "".join(parts[i] for i in range(1, total + 1))


# ------------------------------------------------------------------ Inhalt
def system_info(window=None):
    """Technische Angaben ohne persönliche Daten (keine Datei- oder Benutzernamen)."""
    from PySide6 import __version__ as pyside_version
    from PySide6.QtCore import qVersion
    from PySide6.QtWidgets import QApplication
    import pymupdf
    from . import __version__
    from .renderer import ram_gb
    info = {"PDF Editor": __version__, "Windows": platform.platform(), "Python": platform.python_version(),
            "Qt": qVersion(), "PySide6": pyside_version, "PyMuPDF": pymupdf.VersionBind,
            "MuPDF": pymupdf.VersionFitz, "Prozessor": platform.processor() or platform.machine(),
            "Arbeitsspeicher": f"{ram_gb():.0f} GB", "Als .exe": bool(getattr(sys, "frozen", False))}
    app = QApplication.instance()
    if app is not None:
        info["Bildschirme"] = [f"{s.size().width()}×{s.size().height()} @ {s.devicePixelRatio():g}x, "
                               f"{s.refreshRate():.0f} Hz" for s in app.screens()]
    if window is not None:
        try:
            info["Grafikkarten-Darstellung"] = bool(window.view.gpu)
            info["Offene Tabs"] = len(window.editor.tabs())
            info["Werkzeug"] = window.view.tool
        except Exception:
            pass
    return info


def error_log():
    """Letzte Einträge des Fehlerprotokolls – Pfade mit dem Benutzernamen werden unkenntlich gemacht."""
    folder = os.path.dirname(_data_dir())
    texts = []
    for name in ("fehler.log", "absturz.log"):
        try:
            with open(os.path.join(folder, name), encoding="utf-8", errors="replace") as f:
                t = f.read()[-LOG_CHARS:]
        except OSError:
            continue
        if t.strip():
            texts.append(f"--- {name} ---\n{t}")
    text = "\n".join(texts)
    home = os.path.expanduser("~")
    for variant in {home, home.replace("\\", "/"), home.replace("\\", "\\\\")}:
        text = text.replace(variant, "%USERPROFILE%")
    user = os.environ.get("USERNAME")
    if user and len(user) > 2:
        text = text.replace(user, "<Benutzer>")
    return text


def image_jpeg(img, name):
    """QImage → {"name", "size", "data"} als verkleinertes JPEG (Base64)."""
    if max(img.width(), img.height()) > IMAGE_MAX_PX:
        img = img.scaled(IMAGE_MAX_PX, IMAGE_MAX_PX, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "JPEG", IMAGE_QUALITY)
    buf.close()
    data = bytes(ba)
    return {"name": name, "size": [img.width(), img.height()], "data": base64.b64encode(data).decode("ascii"),
            "bytes": len(data)}


def window_screenshot(window):
    """Bild des Programmfensters (inkl. Grafikkarten-Ansicht). Das Fenster wird selbst gerendert statt den Bildschirm
    abzufotografieren – darüberliegende Dialoge (z. B. die Einstellungen) sind so nicht mit drauf."""
    img = window.grab().toImage()
    return img if not img.isNull() else None


def build_payload(message, name="", system=None, log=None, images=()):
    from . import __version__
    return {"format": 1, "version": __version__,
            "sent": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "name": name.strip() or None, "message": message.strip(), "system": system, "log": log or None,
            "images": list(images)}


# ------------------------------------------------------------------ Senden
class FeedbackSender(QObject):
    """Legt das Issue an und hängt weitere Teile als Kommentare an. progress(fertig, gesamt),
    finished(erfolgreich, Meldung)."""
    progress = Signal(int, int)
    finished = Signal(bool, str)

    API = "https://api.github.com"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._net = QNetworkAccessManager(self)
        self._parts = []
        self._issue = None
        self._done = 0

    def send(self, payload):
        try:
            self._parts = split_parts(encrypt(payload))
        except Exception as e:  # noqa: BLE001 – Verschlüsselung fehlgeschlagen: verständlich melden
            self.finished.emit(False, f"Verschlüsseln fehlgeschlagen: {e}")
            return
        self._done = 0
        title = f"Feedback {datetime.date.today().isoformat()} · {secrets.token_hex(3)}"  # verrät nichts
        self._post(f"/repos/{REPO}/issues", {"title": title, "body": self._parts[0]}, self._issue_created)

    def _post(self, path, data, handler):
        req = QNetworkRequest(QUrl(self.API + path))
        req.setRawHeader(b"Authorization", f"Bearer {token()}".encode())
        req.setRawHeader(b"Accept", b"application/vnd.github+json")
        req.setRawHeader(b"X-GitHub-Api-Version", b"2022-11-28")
        req.setHeader(QNetworkRequest.ContentTypeHeader, "application/json")
        req.setTransferTimeout(30000)
        reply = self._net.post(req, json.dumps(data).encode("utf-8"))
        reply.finished.connect(lambda r=reply: handler(r))

    def _check(self, reply):
        reply.deleteLater()
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        if reply.error() == QNetworkReply.NoError and status in (200, 201):
            return json.loads(bytes(reply.readAll()).decode("utf-8"))
        if status in (401, 403):
            msg = "Zugang abgelehnt (Token abgelaufen oder gesperrt) – bitte die neueste Version installieren."
        elif status == 404:
            msg = "Feedback-Ablage nicht gefunden."
        elif reply.error() in (QNetworkReply.HostNotFoundError, QNetworkReply.TimeoutError,
                               QNetworkReply.OperationCanceledError):
            msg = "Keine Verbindung zu GitHub (Internet?)."
        else:
            msg = reply.errorString()
        self.finished.emit(False, msg)
        return None

    def _issue_created(self, reply):
        data = self._check(reply)
        if data is None:
            return
        self._issue = data["number"]
        self._next()

    def _next(self, reply=None):
        if reply is not None and self._check(reply) is None:
            return
        self._done += 1
        self.progress.emit(self._done, len(self._parts))
        if self._done >= len(self._parts):
            self.finished.emit(True, "Danke! Deine Nachricht wurde verschlüsselt gesendet.")
            return
        self._post(f"/repos/{REPO}/issues/{self._issue}/comments", {"body": self._parts[self._done]}, self._next)
