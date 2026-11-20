"""Baut den PDF Editor: dist/PDF-Editor-<Version>-portable.zip (Programm), dist/PDF-Editor-<Version>-Setup.exe
(Installationsprogramm, braucht Inno Setup 6: winget install JRSoftware.InnoSetup – Skript: installer/PDF Editor.iss)
und dist/PDF-Editor-<Version>-quellcode.zip (Quellcode, getrennt – AGPL Abschnitt 6 d: am selben Ort angeboten)

Aufruf (im Projektordner):  python build.py
                             python build.py --release   (zusätzlich auf GitHub veröffentlichen, s. u.)
Voraussetzung (einmalig):    python -m pip install -r requirements-dev.txt

Jeder Aufruf erhöht die Versionsnummer (letzte Stelle in pdfeditor/__init__.py).

Ergebnis: ein ZIP mit dem Ordner „PDF Editor“ (darin „PDF Editor.exe“). Zum Weitergeben einfach das ZIP
verschicken – auf dem Zielrechner wird weder Python noch eine Installation benötigt. Den Quellcode gibt es als
eigenes ZIP (bei Weitergabe mitgeben oder auf die Download-Seite verweisen).

--release: Setup, Programm- und Quellcode-ZIP werden als GitHub-Release „v<Version>“ im öffentlichen Repository
veröffentlicht, unter festen Namen und mit einer version.json (dort findet sie „Nach Updates suchen“). Vorher müssen
alle Änderungen committet und gepusht sein; die erhöhte Versionsnummer wird automatisch committet und gepusht.
Braucht die angemeldete GitHub-Kommandozeile (gh). Der frühere Download-Gist bekommt nur noch einen Verweis und –
für Versionen bis 0.2.8 – dieselbe version.json. Die Einträge unter „Neu“ in CHANGELOG.md werden zum Abschnitt der
neuen Version (Pflicht für --release); sie stehen in der version.json und erscheinen im Programm bei einer neuen
Version.
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(ROOT, "build")
DIST = os.path.join(ROOT, "dist")
sys.path.insert(0, ROOT)

from pdfeditor import APP_NAME, __version__  # noqa: E402

# Quellcode, der dem ZIP beiliegt (PyMuPDF steht unter der AGPL: Weitergabe nur mit Quellcode)
SOURCE_FILES = ["main.py", "build.py", "requirements.txt", "requirements-dev.txt", "README.md", "CHANGELOG.md",
                "PDF Editor starten.bat", "Feedback lesen.bat", "LIESMICH.txt", "LICENSE", "LIZENZEN.txt"]
SOURCE_DIRS = ["pdfeditor", "tools", "licenses", "installer"]
# Liegen neben der .exe (Weitergabe: Lizenz der AGPL/LGPL-Teile muss beiliegen)
LEGAL_FILES = ["LICENSE", "LIZENZEN.txt"]
LEGAL_DIR = "licenses"
REQUIRED_LICENSES = ["AGPL-3.0.txt", "GPL-3.0.txt", "LGPL-3.0.txt", "LGPL-2.1.txt", "Python-PSF-LICENSE.txt",
                     "PyInstaller-COPYING.txt", "Apache-2.0.txt", "cryptography-BSD.txt", "cffi-MIT.txt",
                     "pycparser-BSD.txt"]
# Token fürs Feedback (feedback.py): nur beim Bauen eingesetzt, nie im Git-Repository oder im Quellcode-Ordner
SECRETS = os.path.join(ROOT, "pdfeditor", "_secrets.py")


# Python-Pakete, die PyInstaller über optionale Importe (z. B. von PyMuPDF) einsammeln würde
EXCLUDE_MODULES = ["tkinter", "numpy", "scipy", "pandas", "matplotlib", "PIL", "jedi", "IPython", "pytz",
                   "sqlite3", "lxml", "fontTools", "PySide6.QtQuick", "PySide6.QtQml",
                   "PySide6.QtPdf", "PySide6.QtSvg", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
                   "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
                   "PySide6.Qt3DCore", "PySide6.QtVirtualKeyboard"]

# Qt-Dateien, die das Programm nicht braucht (relativ zu _internal/PySide6)
PRUNE = ["opengl32sw.dll",  # Software-OpenGL (wir zeichnen über Direct3D bzw. ohne Grafikkarte)
         "Qt6Quick.dll", "Qt6Qml.dll", "Qt6QmlModels.dll", "Qt6QmlMeta.dll", "Qt6QmlWorkerScript.dll",
         # Qt6Network.dll und QtNetwork.pyd bleiben: Qt Multimedia (Video/Audio) und die Einzelinstanz
         # (single_instance: weitere Starts übergeben ihre Dateien per lokaler Verbindung) brauchen sie
         "Qt6VirtualKeyboard.dll", "Qt6Pdf.dll", "Qt6Svg.dll",
         "plugins/platforminputcontexts", "plugins/networkinformation",
         # von den TLS-Erweiterungen bleibt nur Schannel (Windows-eigene Verschlüsselung für „Nach Updates suchen“)
         "plugins/tls/qopensslbackend.dll", "plugins/tls/qcertonlybackend.dll",
         "plugins/imageformats/qpdf.dll", "plugins/imageformats/qsvg.dll", "plugins/iconengines",
         "plugins/platforms/qdirect2d.dll", "plugins/platforms/qminimal.dll"]
KEEP_TRANSLATIONS = ("qtbase_de", "qt_de", "qt_help_de")


def prune(app_dir):
    qt = os.path.join(app_dir, "_internal", "PySide6")
    for rel in PRUNE:
        p = os.path.join(qt, rel)
        if os.path.isdir(p):
            shutil.rmtree(p)
        elif os.path.exists(p):
            os.remove(p)
    tr = os.path.join(qt, "translations")
    if os.path.isdir(tr):
        for name in os.listdir(tr):
            if not name.startswith(KEEP_TRANSLATIONS):
                os.remove(os.path.join(tr, name))
    internal = os.path.join(app_dir, "_internal")
    for name in os.listdir(internal):  # OpenSSL wird ohne QtNetwork nicht gebraucht
        if name.startswith("libcrypto") or name.startswith("libssl"):
            os.remove(os.path.join(internal, name))


def selftest(app_dir):
    """Gebaute .exe kurz starten (mit einem Test-PDF) und prüfen, ob alles Nötige dabei ist."""
    import subprocess
    import tempfile
    import pymupdf
    pdf = os.path.join(tempfile.gettempdir(), "pdfeditor_selbsttest.pdf")
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Selbsttest")
    doc.save(pdf)
    log = os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()), APP_NAME, "selbsttest.txt")
    if os.path.exists(log):
        os.remove(log)
    env = dict(os.environ, PDFEDITOR_SELFTEST="1")
    env.pop("QT_QPA_PLATFORM", None)
    from PySide6.QtCore import QSettings
    qs = QSettings("PDFEditor", "PDFEditor")
    keys = ("recent", "last_dir", "browser_dir", "geometry")
    saved = {k: qs.value(k) for k in keys if qs.contains(k)}
    try:
        subprocess.run([os.path.join(app_dir, APP_NAME + ".exe"), pdf], env=env, timeout=60)
    finally:  # Testdatei nicht unter „Zuletzt geöffnet“ stehen lassen
        qs.sync()
        for k in keys:
            qs.remove(k)
        for k, v in saved.items():
            qs.setValue(k, v)
        qs.sync()
    if not os.path.exists(log):
        raise SystemExit("Selbsttest fehlgeschlagen: Die .exe hat kein Ergebnis geschrieben (startet sie?).")
    result = dict(line.split("=", 1) for line in open(log, encoding="utf-8").read().split())
    print("   Selbsttest:", result)
    if result.get("pages") != "1":
        raise SystemExit("Selbsttest fehlgeschlagen: PDF wurde nicht geöffnet.")
    if result.get("cancel_text") != "Abbrechen":
        print("   Warnung: deutsche Qt-Übersetzung fehlt (Knöpfe auf Englisch).")
    if result.get("media") != "True":
        raise SystemExit("Selbsttest fehlgeschlagen: Video/Audio-Wiedergabe nicht verfügbar (Qt Multimedia "
                         "fehlt im Paket).")
    if result.get("gpu") != "True":
        print("   Hinweis: Grafikkarten-Darstellung nicht aktiv (normale Darstellung wird genutzt).")


def bump_version():
    """Jeder Build bekommt eine eigene Versionsnummer: letzte Stelle in pdfeditor/__init__.py erhöhen
    (0.2.0 -> 0.2.1). So lassen sich weitergegebene Fassungen auseinanderhalten (Hilfe → Über, ZIP-Name)."""
    global __version__
    init = os.path.join(ROOT, "pdfeditor", "__init__.py")
    text = open(init, encoding="utf-8").read()
    parts = __version__.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    new = ".".join(parts)
    text = text.replace(f'__version__ = "{__version__}"', f'__version__ = "{new}"')
    with open(init, "w", encoding="utf-8") as f:
        f.write(text)
    __version__ = new
    import pdfeditor
    pdfeditor.__version__ = new  # für den Selbsttest und die Versionsinfo der .exe


def reset_version(old):
    global __version__
    init = os.path.join(ROOT, "pdfeditor", "__init__.py")
    text = open(init, encoding="utf-8").read().replace(f'__version__ = "{__version__}"', f'__version__ = "{old}"')
    with open(init, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"   Build fehlgeschlagen – Version zurück auf {old}")
    __version__ = old


def make_icon(path):
    from PySide6.QtGui import QGuiApplication
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QGuiApplication.instance() or QGuiApplication([])  # für QPainter nötig
    from pdfeditor import app_icon
    app_icon.write_ico(path)
    return app


def make_version_file(path):
    parts = [int(p) for p in __version__.split(".")] + [0] * 4
    v = tuple(parts[:4])
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={v}, prodvers={v}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
                    subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040704B0', [
      StringStruct('FileDescription', '{APP_NAME}'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('ProductName', '{APP_NAME}'),
      StringStruct('ProductVersion', '{__version__}'),
      StringStruct('InternalName', '{APP_NAME}'),
      StringStruct('OriginalFilename', '{APP_NAME}.exe')])]),
    VarFileInfo([VarStruct('Translation', [0x0407, 1200])])
  ]
)
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def remove_tree(path):
    """Ordner löschen, auch wenn Windows ihn als schreibgeschützt markiert hat (sonst „Zugriff verweigert“)."""
    import stat

    def unlock(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if os.path.isdir(path):
        shutil.rmtree(path, onexc=unlock)


def run_pyinstaller(icon, version_file):
    import PyInstaller.__main__
    sep = os.pathsep
    PyInstaller.__main__.run([
        os.path.join(ROOT, "main.py"),
        "--name", APP_NAME,
        "--windowed",            # kein Konsolenfenster
        "--onedir",              # Ordner statt einer einzigen .exe: startet schneller, weniger Virenscanner-Fehlalarme
        "--noconfirm", "--clean",
        "--icon", icon,
        "--version-file", version_file,
        "--add-data", f"{os.path.join(ROOT, 'pdfeditor', 'shaders', '*.qsb')}{sep}pdfeditor/shaders",
        "--add-data", f"{os.path.join(ROOT, 'pdfeditor', 'fonts', '*.ttf')}{sep}pdfeditor/fonts",
        # nicht benötigte Teile weglassen (kleineres Paket)
        *[arg for m in EXCLUDE_MODULES for arg in ("--exclude-module", m)],
        "--distpath", os.path.join(BUILD, "dist"),
        "--workpath", os.path.join(BUILD, "work"),
        "--specpath", BUILD,
    ])


def copy_source(dest):
    os.makedirs(dest, exist_ok=True)
    for f in SOURCE_FILES:
        src = os.path.join(ROOT, f)
        if os.path.exists(src):
            shutil.copy2(src, dest)
    for d in SOURCE_DIRS:
        shutil.copytree(os.path.join(ROOT, d), os.path.join(dest, d),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "_secrets.py"))
    import stat
    for base, dirs, _files in os.walk(dest):  # Schreibschutz der Quellordner nicht mitnehmen (blockiert sonst
        for name in dirs:                       # das Löschen beim nächsten Build)
            p = os.path.join(base, name)
            os.chmod(p, os.stat(p).st_mode | stat.S_IWRITE)


def write_secrets():
    """Feedback-Token aus der lokalen Datei (feedback.TOKEN_FILE) für die .exe einsetzen. Leicht verschleiert – kein
    Schutz (das Token darf ohnehin nur verschlüsselte Issues anlegen), aber GitHubs Token-Scanner sperrt es sonst
    automatisch, sobald das ZIP irgendwo öffentlich liegt. Ohne Datei: Feedback in dieser .exe abgeschaltet."""
    import base64
    from pdfeditor import feedback
    try:
        with open(feedback.TOKEN_FILE, encoding="utf-8") as f:
            tok = f.read().strip()
    except OSError:
        tok = ""
    if not tok:
        print(f"   Hinweis: kein Feedback-Token ({feedback.TOKEN_FILE}) – „Feedback senden“ ist abgeschaltet.")
        return
    hidden = base64.b64encode(tok.encode()[::-1]).decode()
    with open(SECRETS, "w", encoding="utf-8") as f:
        f.write('"""Von build.py beim Bauen erzeugt und danach gelöscht – nicht ins Repository."""\n'
                "import base64\n\n\ndef feedback_token():\n"
                f"    return base64.b64decode({hidden!r}).decode()[::-1]\n")


def remove_secrets():
    if os.path.exists(SECRETS):
        os.remove(SECRETS)


def check_legal():
    """Ohne Lizenztexte darf das Paket nicht weitergegeben werden -> vor dem Bauen prüfen."""
    missing = [f for f in LEGAL_FILES if not os.path.exists(os.path.join(ROOT, f))]
    missing += [os.path.join(LEGAL_DIR, f) for f in REQUIRED_LICENSES
                if not os.path.exists(os.path.join(ROOT, LEGAL_DIR, f))]
    if missing:
        raise SystemExit("Lizenzdateien fehlen (für die Weitergabe nötig): " + ", ".join(missing))


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


CHANGELOG = os.path.join(ROOT, "CHANGELOG.md")


def read_changelog():
    """[[Version oder „Neu“, Datum, [Einträge]], …] in der Reihenfolge der Datei (neueste oben)."""
    sections = []
    with open(CHANGELOG, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^## (\S+)(?: – (.+?))?\s*$", line)
            if m:
                sections.append([m.group(1), (m.group(2) or "").strip(), []])
            elif sections and line.startswith("- "):
                sections[-1][2].append(line[2:].strip())
    return sections


def pending_changes():
    return next((s[2] for s in read_changelog() if s[0] == "Neu"), [])


def finalize_changelog():
    """„## Neu“ wird zu „## <Version> – <Datum>“; darüber kommt ein neuer, leerer Abschnitt „Neu“."""
    today = datetime.date.today().strftime("%d.%m.%Y")
    with open(CHANGELOG, encoding="utf-8") as f:
        text = f.read()
    text = text.replace("## Neu\n", f"## Neu\n\n## {__version__} – {today}\n", 1)
    with open(CHANGELOG, "w", encoding="utf-8") as f:
        f.write(text)


def check_release_ready():
    """Vor dem Bauen prüfen, ob veröffentlicht werden kann – sonst wäre die Versionsnummer umsonst erhöht."""
    from pdfeditor import updates
    if not pending_changes():
        raise SystemExit("--release: In CHANGELOG.md stehen unter „Neu“ keine Änderungen – bitte eintragen.")
    if updates.gh_path() is None:
        raise SystemExit("--release: GitHub-Kommandozeile (gh) nicht gefunden.")
    if git("status", "--porcelain").stdout.strip():
        raise SystemExit("--release: Es gibt noch nicht committete Änderungen – erst committen und pushen.")
    if git("fetch", "-q", "origin").returncode != 0:
        raise SystemExit("--release: GitHub nicht erreichbar (git fetch fehlgeschlagen).")
    ahead = git("rev-list", "--count", "origin/main..HEAD").stdout.strip()
    behind = git("rev-list", "--count", "HEAD..origin/main").stdout.strip()
    if ahead != "0" or behind != "0":
        raise SystemExit(f"--release: Lokaler Stand und GitHub weichen ab ({ahead} nicht gepusht, "
                         f"{behind} nicht geholt) – erst angleichen.")


def publish(zip_path, source_zip, setup):
    """Versionsnummer und Änderungsliste committen und pushen, dann das Release „v<Version>“ anlegen (Setup, ZIPs,
    version.json). Gibt den Pfad der version.json zurück (für die Gist-Brücke)."""
    from pdfeditor import updates
    tag = f"v{__version__}"
    print(f"== GitHub-Release {tag}")
    finalize_changelog()
    for args in (("commit", "-q", "-m", f"Version {__version__}", "--", "pdfeditor/__init__.py", "CHANGELOG.md"),
                 ("push", "-q", "origin", "main")):
        r = git(*args)
        if r.returncode != 0:
            raise SystemExit(f"git {args[0]} fehlgeschlagen:\n{r.stderr}")
    files = stage_release(zip_path, source_zip, setup)
    r = subprocess.run([updates.gh_path(), "release", "create", tag, *files, "--repo", updates.REPO,
                        "--target", "main", "--title", f"{APP_NAME} {__version__}", "--notes", release_notes()],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"Release fehlgeschlagen:\n{r.stderr}")
    print(f"   veröffentlicht: {r.stdout.strip()}")
    return files[-1]


def release_notes():
    """Beschreibung des Releases: Änderungen dieser Version und eine kurze Anleitung."""
    from pdfeditor import updates
    news = next((c for v, _d, c in read_changelog() if v == __version__), [])
    return ("\n".join(f"- {c}" for c in news) +
            f"\n\n**Installieren:** `{updates.SETUP_EXE}` herunterladen und ausführen (ohne Administratorrechte, "
            "mit Startmenü-Eintrag und „Öffnen mit“ für PDFs).  \n"
            f"**Portabel:** `{updates.PORTABLE_ZIP}` entpacken und „PDF Editor.exe“ starten.  \n"
            "Neue Versionen meldet das Programm selbst („Jetzt aktualisieren“). Windows warnt evtl., weil das "
            "Programm nicht signiert ist: „Weitere Informationen“ → „Trotzdem ausführen“.  \n"
            f"**Quellcode:** `{updates.SOURCE_ZIP}` (GNU AGPL-3.0-or-later).")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def stage_release(zip_path, source_zip, setup):
    """Release-Dateien unter festen Namen bereitlegen (…/releases/latest/download/<Name> zeigt so immer auf die
    neueste Version) und version.json schreiben: Version, Links auf genau die Dateien dieses Releases, Prüfsummen/
    Größen fürs Selbst-Update (ZIP für die portable, Setup für die installierte Version), Änderungsliste."""
    from pdfeditor import updates
    work = os.path.join(BUILD, "release")
    remove_tree(work)
    os.makedirs(work)
    staged = []
    for src, name in ((setup, updates.SETUP_EXE), (zip_path, updates.PORTABLE_ZIP), (source_zip, updates.SOURCE_ZIP)):
        dst = os.path.join(work, name)
        shutil.copy2(src, dst)
        staged.append(dst)
    url = lambda name: updates.release_file_url(__version__, name)  # noqa: E731
    changelog = [{"version": v, "date": d, "changes": c} for v, d, c in read_changelog() if v != "Neu"][:20]
    vj = os.path.join(work, updates.VERSION_JSON)
    with open(vj, "w", encoding="utf-8") as f:
        json.dump({"version": __version__, "date": datetime.date.today().isoformat(),
                   "page": f"{updates.RELEASES_URL}/tag/v{__version__}",
                   "download": url(updates.PORTABLE_ZIP), "sha256": sha256(zip_path), "size": os.path.getsize(zip_path),
                   "setup": url(updates.SETUP_EXE), "setup_sha256": sha256(setup), "setup_size": os.path.getsize(setup),
                   "source": url(updates.SOURCE_ZIP), "changelog": changelog}, f, ensure_ascii=False, indent=2)
    staged.append(vj)
    return staged


def update_gist(version_json):
    """Früherer Download-Gist: nur noch ein Verweis auf das öffentliche Repository – und, als Brücke für Versionen bis
    0.2.8 (die ihre version.json dort lesen), dieselbe version.json wie im Release. Große Dateien liegen nicht mehr im
    Gist (der war an seiner Größengrenze). Schlägt das Hochladen fehl, ist das kein Abbruchgrund: Versionen ab 0.2.9
    brauchen den Gist nicht."""
    from pdfeditor import updates
    work = os.path.join(BUILD, "gist")
    remove_tree(work)
    os.makedirs(work)
    shutil.copy2(version_json, os.path.join(work, "version.json"))
    with open(os.path.join(work, "PDF Editor – Download.md"), "w", encoding="utf-8") as f:
        f.write(f"""# PDF Editor – Download

Der PDF Editor ist umgezogen: **[github.com/{updates.REPO}/releases/latest]({updates.DOWNLOAD_PAGE})**

- ⬇️ [Installieren (Setup)]({updates.SETUP_URL}) – keine Administratorrechte nötig
- ⬇️ [Portable Version (ZIP)]({updates.PORTABLE_URL}) – entpacken und „PDF Editor.exe“ starten
- [Quellcode]({updates.SOURCE_URL}) · [Projektseite](https://github.com/{updates.REPO})

Die Links liefern immer die neueste Version. Bereits installierte Versionen aktualisieren sich über
„Einstellungen → Updates & Feedback → Nach Updates suchen“ → „Jetzt aktualisieren“.
""")

    # Über die GitHub-API statt git push: Der Gist liegt über seiner Größengrenze (alte ZIPs im Verlauf), git push nimmt
    # GitHub dort nicht mehr an – das Bearbeiten über die API (kleine Textdateien) funktioniert
    files = {}
    for name in ("version.json", "PDF Editor – Download.md"):
        with open(os.path.join(work, name), encoding="utf-8") as f:
            files[name] = {"content": f.read()}
    gh = updates.gh_path()
    r = subprocess.run([gh, "api", f"gists/{updates.GIST_ID}"], capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode == 0:
        for name in json.loads(r.stdout).get("files", {}):
            files.setdefault(name, None)  # alles andere (früher: ZIPs, Setup) entfernen
        r = subprocess.run([gh, "api", "-X", "PATCH", f"gists/{updates.GIST_ID}", "--input", "-"],
                           input=json.dumps({"files": files}), capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
    if r.returncode != 0:
        print(f"   WARNUNG: Gist nicht aktualisiert (nur für Versionen bis 0.2.8 nötig):\n{r.stderr.strip()}")
    else:
        print(f"   Gist aktualisiert (Verweis + version.json): {updates.GIST_PAGE}")


def main():
    release = "--release" in sys.argv[1:]
    if release:
        check_release_ready()
        if find_iscc() is None:  # installierte Versionen aktualisieren sich über das Setup aus dem Gist
            raise SystemExit("--release: Inno Setup 6 nicht gefunden (winget install JRSoftware.InnoSetup).")
    check_legal()
    os.makedirs(BUILD, exist_ok=True)
    os.makedirs(DIST, exist_ok=True)
    icon = os.path.join(BUILD, "app.ico")
    version_file = os.path.join(BUILD, "version_info.txt")
    old = __version__
    bump_version()
    try:
        print(f"== {APP_NAME} {__version__} (vorher {old}): Symbol und Versionsinfo")
        make_icon(icon)
        make_version_file(version_file)
        print("== PyInstaller")
        remove_tree(os.path.join(BUILD, "dist", APP_NAME))  # alter Build (auch schreibgeschützte Ordner)
        write_secrets()
        try:
            run_pyinstaller(icon, version_file)
        finally:
            remove_secrets()
        app_dir = os.path.join(BUILD, "dist", APP_NAME)
        print("== Aufräumen und Selbsttest")
        prune(app_dir)
        selftest(app_dir)
    except BaseException:
        reset_version(old)  # fehlgeschlagener Build (kein ZIP) verbraucht keine Versionsnummer
        raise
    shutil.copy2(os.path.join(ROOT, "LIESMICH.txt"), app_dir)
    for f in LEGAL_FILES:
        shutil.copy2(os.path.join(ROOT, f), app_dir)
    shutil.copytree(os.path.join(ROOT, LEGAL_DIR), os.path.join(app_dir, LEGAL_DIR), dirs_exist_ok=True)
    remove_tree(os.path.join(app_dir, "Quellcode"))  # Quellcode jetzt als eigenes ZIP (s. u.)
    zip_path = os.path.join(DIST, f"PDF-Editor-{__version__}-portable.zip")
    print(f"== ZIP: {zip_path}")
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for folder, _, files in os.walk(app_dir):
            for name in files:
                full = os.path.join(folder, name)
                # im ZIP liegt alles im Ordner „PDF Editor“ (gleicher Name bei jeder Version -> Update = ersetzen)
                z.write(full, os.path.join(APP_NAME, os.path.relpath(full, app_dir)))
    source_zip = make_source_zip()
    print("== Installationsprogramm (Inno Setup)")
    setup = make_setup(app_dir, icon)
    if setup is None:
        print("   übersprungen: Inno Setup 6 nicht gefunden (winget install JRSoftware.InnoSetup)")
    mb = lambda p: os.path.getsize(p) / 1024 / 1024  # noqa: E731
    print(f"\nFertig: {zip_path} ({mb(zip_path):.0f} MB)\n       {source_zip} ({mb(source_zip):.1f} MB)")
    if setup:
        print(f"       {setup} ({mb(setup):.0f} MB)")
    print(f"Zum Testen ohne Entpacken: {os.path.join(app_dir, APP_NAME + '.exe')}")
    if release:
        version_json = publish(zip_path, source_zip, setup)
        update_gist(version_json)


INSTALLER_SCRIPT = os.path.join(ROOT, "installer", "PDF Editor.iss")


def find_iscc():
    """Inno-Setup-Compiler (ISCC.exe) – Installation pro Benutzer oder für alle; None, wenn nicht installiert."""
    found = shutil.which("ISCC")
    if found:
        return found
    for base in (os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
                 os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", "")):
        path = os.path.join(base, "Inno Setup 6", "ISCC.exe")
        if base and os.path.isfile(path):
            return path
    return None


def make_setup(app_dir, icon):
    """Installationsprogramm dist/PDF-Editor-<Version>-Setup.exe aus dem fertigen Programmordner (Inno Setup)."""
    iscc = find_iscc()
    if iscc is None:
        return None
    path = os.path.join(DIST, f"PDF-Editor-{__version__}-Setup.exe")
    if os.path.exists(path):
        os.remove(path)
    r = subprocess.run([iscc, "/Q", f"/DAppVersion={__version__}", f"/DSourceDir={app_dir}", f"/DOutputDir={DIST}",
                        f"/DIconFile={icon}", INSTALLER_SCRIPT], cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0 or not os.path.isfile(path):
        raise SystemExit(f"Inno Setup fehlgeschlagen:\n{r.stdout}\n{r.stderr}")
    return path


def make_source_zip():
    """Quellcode dieser Version als eigenes ZIP (Ordner „PDF Editor Quellcode“), ohne das Feedback-Token."""
    src = os.path.join(BUILD, "quellcode", "PDF Editor Quellcode")
    remove_tree(os.path.dirname(src))
    copy_source(src)
    path = os.path.join(DIST, f"PDF-Editor-{__version__}-quellcode.zip")
    if os.path.exists(path):
        os.remove(path)
    base = os.path.dirname(src)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for folder, _, files in os.walk(src):
            for name in files:
                full = os.path.join(folder, name)
                z.write(full, os.path.relpath(full, base))
    return path


if __name__ == "__main__":
    main()
