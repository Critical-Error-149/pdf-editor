"""Feedback lesen (nur der Entwickler): holt die verschlüsselten Issues aus dem privaten Feedback-Repository,
entschlüsselt sie mit dem privaten Schlüssel und legt jede Nachricht als Ordner ab.

Aufruf (im Projektordner):  python tools/feedback_lesen.py            (neue Nachrichten)
                            python tools/feedback_lesen.py --alle     (auch schon gelesene)
Voraussetzungen: angemeldete GitHub-Kommandozeile (gh) und der private Schlüssel
(%LOCALAPPDATA%\\PDF Editor\\feedback\\privater_schluessel.pem, s. tools/feedback_schluessel.py).

Ergebnis: Dokumente\\PDF Editor Feedback\\<Datum> #<Nr>\\ mit nachricht.txt, system.txt, fehlerprotokoll.txt und den
Bildern. Gelesene Issues werden auf GitHub geschlossen (Inhalt bleibt dort verschlüsselt erhalten).
"""
import base64
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cryptography.hazmat.primitives.serialization import load_pem_private_key  # noqa: E402

from pdfeditor import feedback, updates  # noqa: E402

KEY_FILE = os.path.join(feedback._data_dir(), "privater_schluessel.pem")
OUT = os.path.join(os.path.expanduser("~"), "Documents", "PDF Editor Feedback")


def gh(*args):
    exe = updates.gh_path()
    if exe is None:
        raise SystemExit("GitHub-Kommandozeile (gh) nicht gefunden.")
    r = subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"gh {' '.join(args[:2])} fehlgeschlagen:\n{r.stderr}")
    return r.stdout


def api_all(path):
    """Alle Seiten einer Listen-Abfrage (gh api --paginate liefert je Seite ein JSON-Array)."""
    out = gh("api", "--paginate", path)
    items = []
    dec = json.JSONDecoder()
    i = 0
    while i < len(out):
        while i < len(out) and out[i].isspace():
            i += 1
        if i >= len(out):
            break
        part, i = dec.raw_decode(out, i)
        items.extend(part)
    return items


def save(issue, payload):
    date = (payload.get("sent") or issue["created_at"])[:10]
    folder = os.path.join(OUT, f"{date} #{issue['number']}")
    os.makedirs(folder, exist_ok=True)
    lines = [f"Von: {payload.get('name') or '(ohne Namen)'}", f"Gesendet: {payload.get('sent')}",
             f"Programmversion: {payload.get('version')}", "", payload.get("message", "")]
    with open(os.path.join(folder, "nachricht.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    if payload.get("system"):
        with open(os.path.join(folder, "system.txt"), "w", encoding="utf-8") as f:
            for k, v in payload["system"].items():
                f.write(f"{k}: {', '.join(v) if isinstance(v, list) else v}\n")
    if payload.get("log"):
        with open(os.path.join(folder, "fehlerprotokoll.txt"), "w", encoding="utf-8") as f:
            f.write(payload["log"])
    for n, img in enumerate(payload.get("images") or [], 1):
        name = os.path.basename(img.get("name") or f"bild{n}.jpg")
        stem, ext = os.path.splitext(name)
        with open(os.path.join(folder, f"{n}_{stem}{ext or '.jpg'}"), "wb") as f:
            f.write(base64.b64decode(img["data"]))
    return folder


def main():
    if not os.path.exists(KEY_FILE):
        raise SystemExit(f"Privater Schlüssel fehlt: {KEY_FILE}")
    with open(KEY_FILE, "rb") as f:
        key = load_pem_private_key(f.read(), None)
    state = "all" if "--alle" in sys.argv else "open"
    issues = [i for i in api_all(f"repos/{feedback.REPO}/issues?state={state}&per_page=100")
              if "pull_request" not in i]
    if not issues:
        print("Keine neuen Nachrichten.")
        return
    for issue in sorted(issues, key=lambda i: i["number"]):
        bodies = [issue.get("body") or ""]
        if issue.get("comments"):
            bodies += [c.get("body") or "" for c in api_all(issue["comments_url"].split("api.github.com/")[1]
                                                           + "?per_page=100")]
        try:
            payload = feedback.decrypt(feedback.join_parts(bodies), key)
        except Exception as e:  # noqa: BLE001 – unlesbares Issue überspringen, aber melden
            print(f"#{issue['number']}: nicht lesbar ({e}) – bleibt offen")
            continue
        folder = save(issue, payload)
        print(f"#{issue['number']}: {payload.get('name') or '(ohne Namen)'} → {folder}")
        if issue["state"] == "open":
            gh("issue", "close", str(issue["number"]), "--repo", feedback.REPO)
    print(f"\nAblage: {OUT}")


if __name__ == "__main__":
    main()
