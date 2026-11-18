"""Feedback lesen (nur der Entwickler): holt die verschlüsselten Issues aus dem privaten Feedback-Repository,
entschlüsselt sie mit dem privaten Schlüssel und legt jede Nachricht als Ordner ab.

Aufruf (im Projektordner):  python tools/feedback_lesen.py            (neue Nachrichten)
                            python tools/feedback_lesen.py --alle     (auch schon gelesene)
Voraussetzungen: angemeldete GitHub-Kommandozeile (gh) und der private Schlüssel
(%USERPROFILE%\\PDF Editor Schlüssel\\privater_schluessel.pem, s. tools/feedback_schluessel.py).

Ergebnis: Dokumente\\PDF Editor Feedback\\<Datum> #<Nr>\\ mit feedback.pdf (alles zusammen: Nachricht, Systemdaten,
Bilder, Fehlerprotokoll), nachricht.txt, system.txt, fehlerprotokoll.txt und den Bildern. Gelesene Issues werden auf
GitHub geschlossen (Inhalt bleibt dort verschlüsselt erhalten). Bequemer: „Feedback lesen.bat“ im Projektordner.
"""
import base64
import html
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cryptography.hazmat.primitives.serialization import load_pem_private_key  # noqa: E402

from pdfeditor import feedback, updates  # noqa: E402

KEY_FILE = feedback.KEY_FILE
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
    images = []
    for n, img in enumerate(payload.get("images") or [], 1):
        name = os.path.basename(img.get("name") or f"bild{n}.jpg")
        stem, ext = os.path.splitext(name)
        fname = f"{n}_{stem}{ext or '.jpg'}"
        with open(os.path.join(folder, fname), "wb") as f:
            f.write(base64.b64decode(img["data"]))
        images.append((fname, img.get("size") or [0, 0]))
    make_pdf(folder, issue, payload, images)
    return folder


def make_pdf(folder, issue, payload, images):
    """Alles zu einer Nachricht in einer PDF (A4): Kopf, Nachricht, Systemdaten, Bilder, Fehlerprotokoll."""
    import pymupdf as fitz
    esc = html.escape
    page = fitz.paper_rect("a4")
    area = page + (48, 48, -48, -48)

    def rows(pairs):
        return "".join(f"<tr><td class='k'>{esc(str(k))}</td><td>{esc(str(v))}</td></tr>" for k, v in pairs)

    parts = [f"<h1>Feedback #{issue['number']}</h1>",
             "<table>" + rows([("Von", payload.get("name") or "(ohne Namen)"), ("Gesendet", payload.get("sent")),
                               ("Programmversion", payload.get("version"))]) + "</table>",
             "<h2>Nachricht</h2>",
             "<p>" + esc(payload.get("message", "")).replace("\n", "<br/>") + "</p>"]
    if payload.get("system"):
        parts += ["<h2>Systeminformationen</h2>", "<table>" + rows(
            (k, ", ".join(v) if isinstance(v, list) else v) for k, v in payload["system"].items()) + "</table>"]
    for fname, (w, h) in images:
        # Bild auf die Seite einpassen (ein zu hohes Bild würde sonst nirgends hinpassen)
        scale = min(area.width / w, (area.height - 60) / h, 1.0) if w and h else 1.0
        parts += [f"<h2>Bild: {esc(fname)}</h2>",
                  f"<p><img src='{esc(fname)}' width='{w * scale:.0f}' height='{h * scale:.0f}'/></p>"]
    if payload.get("log"):
        parts += ["<h2>Fehlerprotokoll</h2>", f"<div class='log'>{esc(payload['log'])}</div>"]
    css = ("body {font-family: sans-serif; font-size: 10pt;} h1 {font-size: 18pt; margin-bottom: 6pt;} "
           "h2 {font-size: 12pt; margin-top: 14pt; color: #335;} td {padding: 1pt 8pt 1pt 0; vertical-align: top;} "
           "td.k {font-weight: bold; white-space: nowrap;} "
           ".log {font-family: monospace; font-size: 7pt; white-space: pre-wrap;}")
    story = fitz.Story(html="".join(parts), user_css=css, archive=folder)
    path = os.path.join(folder, "feedback.pdf")
    writer = fitz.DocumentWriter(path)
    more = True
    while more:
        dev = writer.begin_page(page)
        more, _ = story.place(area)
        story.draw(dev)
        writer.end_page()
    writer.close()
    return path


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
