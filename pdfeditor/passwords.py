"""Gespeicherte PDF-Passwörter („Passwort auf diesem PC merken“).

Die Passwörter werden mit Windows DPAPI (CryptProtectData) verschlüsselt: Entschlüsseln kann sie nur dasselbe
Windows-Konto auf diesem PC – ein kopierter Registrierungsschlüssel nützt woanders nichts. Abgelegt sind sie in den
Programmeinstellungen (Registrierung, Gruppe „pdf_passwords“).

Zugeordnet werden sie über die feste Dokumentkennung der PDF (erster Teil von /ID im Trailer, auch ohne Passwort
lesbar) und zusätzlich über den Dateipfad – so klappt es auch, wenn die Datei verschoben oder kopiert wurde.
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import os
import re
import sys

GROUP = "pdf_passwords"
_ENTROPY = b"PDFEditor/PDF-Passwort/1"


def available() -> bool:
    return sys.platform == "win32"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _dpapi(data: bytes, protect: bool) -> bytes | None:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    inp, _keep1 = _blob(data)
    ent, _keep2 = _blob(_ENTROPY)
    out = _Blob()
    ui_forbidden = 0x1
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(inp), ctypes.c_wchar_p("PDF Editor"), ctypes.byref(ent), None, None,
                                      ui_forbidden, ctypes.byref(out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(inp), None, ctypes.byref(ent), None, None, ui_forbidden,
                                        ctypes.byref(out))
    if not ok:
        return None
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def doc_id_from_trailer(doc) -> str | None:
    """Erster Teil der Dokumentkennung /ID (bleibt beim Bearbeiten gleich), z. B. „780224C2…“."""
    try:
        kind, val = doc.xref_get_key(-1, "ID")
    except Exception:
        return None
    if kind != "array":
        return None
    m = re.search(r"<([0-9A-Fa-f]+)>|\(([^)]*)\)", val)
    if not m:
        return None
    return (m.group(1) or m.group(2) or "").upper() or None


def keys_for(doc_id: str | None, path: str | None) -> list[str]:
    """Schlüssel in den Einstellungen (nur Prüfsummen – Pfade und Kennungen stehen nicht im Klartext darin)."""
    keys = []
    if doc_id:
        keys.append("id_" + hashlib.sha256(doc_id.encode()).hexdigest()[:32])
    if path:
        norm = os.path.normcase(os.path.abspath(path))
        keys.append("path_" + hashlib.sha256(norm.encode("utf-8")).hexdigest()[:32])
    return keys


def _settings():
    from PySide6.QtCore import QSettings
    return QSettings("PDFEditor", "PDFEditor")


def lookup(keys) -> str | None:
    if not available():
        return None
    qs = _settings()
    for k in keys:
        raw = qs.value(f"{GROUP}/{k}")
        if not raw:
            continue
        try:
            plain = _dpapi(base64.b64decode(str(raw)), protect=False)
        except Exception:
            plain = None
        if plain is not None:
            return plain.decode("utf-8")
    return None


def remember(keys, password: str) -> bool:
    if not available() or not keys:
        return False
    blob = _dpapi(password.encode("utf-8"), protect=True)
    if blob is None:
        return False
    qs = _settings()
    for k in keys:
        qs.setValue(f"{GROUP}/{k}", base64.b64encode(blob).decode("ascii"))
    qs.sync()
    return True


def forget(keys):
    qs = _settings()
    for k in keys:
        qs.remove(f"{GROUP}/{k}")
    qs.sync()


def count() -> int:
    """Anzahl gemerkter Dokumente (je Dokument gibt es bis zu zwei Schlüssel: Kennung und Pfad)."""
    qs = _settings()
    qs.beginGroup(GROUP)
    keys = qs.childKeys()
    qs.endGroup()
    blobs = {str(qs.value(f"{GROUP}/{k}")) for k in keys}
    return len(blobs)


def forget_all():
    qs = _settings()
    qs.remove(GROUP)
    qs.sync()
