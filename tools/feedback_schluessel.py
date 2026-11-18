"""Schlüsselpaar für das verschlüsselte Feedback anlegen (einmalig, nur auf dem PC des Entwicklers).

Aufruf (im Projektordner):  python tools/feedback_schluessel.py

- Der PRIVATE Schlüssel kommt nach %USERPROFILE%\\PDF Editor Schlüssel\\privater_schluessel.pem – nie ins Repository,
  nie weitergeben. Unbedingt sichern (z. B. USB-Stick, Passwort-Manager): Ohne ihn lässt sich Feedback nicht mehr
  lesen. Auf einem weiteren PC zum Lesen einfach dorthin kopieren.
- Der ÖFFENTLICHE Schlüssel wird in pdfeditor/feedback.py eingetragen (PUBLIC_KEY_B64) – den darf jeder kennen.
Ein vorhandener privater Schlüssel wird nicht überschrieben (sonst wäre älteres Feedback unlesbar); mit --neu
trotzdem ein neues Paar anlegen.
"""
import base64
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import (Encoding, NoEncryption, PrivateFormat,  # noqa: E402
                                                          PublicFormat, load_pem_private_key)

from pdfeditor.feedback import KEY_FILE  # noqa: E402

FEEDBACK_PY = os.path.join(ROOT, "pdfeditor", "feedback.py")


def main():
    if os.path.exists(KEY_FILE) and "--neu" not in sys.argv:
        with open(KEY_FILE, "rb") as f:
            key = load_pem_private_key(f.read(), None)
        print(f"Privater Schlüssel vorhanden: {KEY_FILE}")
    else:
        os.makedirs(os.path.dirname(KEY_FILE), exist_ok=True)
        key = X25519PrivateKey.generate()
        with open(KEY_FILE, "wb") as f:
            f.write(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
        print(f"Neuer privater Schlüssel: {KEY_FILE}\n  → bitte sichern, nicht weitergeben!")
    pub = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode("ascii")
    text = open(FEEDBACK_PY, encoding="utf-8").read()
    new = re.sub(r'^PUBLIC_KEY_B64 = ".*"$', f'PUBLIC_KEY_B64 = "{pub}"', text, count=1, flags=re.M)
    if new != text:
        with open(FEEDBACK_PY, "w", encoding="utf-8") as f:
            f.write(new)
        print("Öffentlicher Schlüssel in pdfeditor/feedback.py eingetragen.")
    else:
        print("Öffentlicher Schlüssel in pdfeditor/feedback.py ist schon aktuell.")


if __name__ == "__main__":
    main()
