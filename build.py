"""Baut die portable Version: dist/PDF-Editor-<Version>-portable.zip

Aufruf (im Projektordner):  python build.py
Voraussetzung (einmalig):    python -m pip install -r requirements-dev.txt

Jeder Aufruf erhöht die Versionsnummer (letzte Stelle in pdfeditor/__init__.py).

Ergebnis: ein ZIP mit dem Ordner „PDF Editor“ (darin „PDF Editor.exe“). Zum Weitergeben einfach das ZIP
verschicken – auf dem Zielrechner wird weder Python noch eine Installation benötigt.
"""
import os
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(ROOT, "build")
DIST = os.path.join(ROOT, "dist")
sys.path.insert(0, ROOT)

from pdfeditor import APP_NAME, __version__  # noqa: E402

# Quellcode, der dem ZIP beiliegt (PyMuPDF steht unter der AGPL: Weitergabe nur mit Quellcode)
SOURCE_FILES = ["main.py", "build.py", "requirements.txt", "requirements-dev.txt", "README.md",
                "PDF Editor starten.bat", "LIESMICH.txt", "LICENSE", "LIZENZEN.txt"]
SOURCE_DIRS = ["pdfeditor", "tools", "licenses"]
# Liegen neben der .exe (Weitergabe: Lizenz der AGPL/LGPL-Teile muss beiliegen)
LEGAL_FILES = ["LICENSE", "LIZENZEN.txt"]
LEGAL_DIR = "licenses"
REQUIRED_LICENSES = ["AGPL-3.0.txt", "GPL-3.0.txt", "LGPL-3.0.txt", "LGPL-2.1.txt", "Python-PSF-LICENSE.txt",
                     "PyInstaller-COPYING.txt"]


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
         "plugins/platforminputcontexts", "plugins/networkinformation", "plugins/tls",
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
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    import stat
    for base, dirs, _files in os.walk(dest):  # Schreibschutz der Quellordner nicht mitnehmen (blockiert sonst
        for name in dirs:                       # das Löschen beim nächsten Build)
            p = os.path.join(base, name)
            os.chmod(p, os.stat(p).st_mode | stat.S_IWRITE)


def check_legal():
    """Ohne Lizenztexte darf das Paket nicht weitergegeben werden -> vor dem Bauen prüfen."""
    missing = [f for f in LEGAL_FILES if not os.path.exists(os.path.join(ROOT, f))]
    missing += [os.path.join(LEGAL_DIR, f) for f in REQUIRED_LICENSES
                if not os.path.exists(os.path.join(ROOT, LEGAL_DIR, f))]
    if missing:
        raise SystemExit("Lizenzdateien fehlen (für die Weitergabe nötig): " + ", ".join(missing))


def main():
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
        run_pyinstaller(icon, version_file)
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
    copy_source(os.path.join(app_dir, "Quellcode"))
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
    size = os.path.getsize(zip_path) / 1024 / 1024
    print(f"\nFertig: {zip_path} ({size:.0f} MB)")
    print(f"Zum Testen ohne Entpacken: {os.path.join(app_dir, APP_NAME + '.exe')}")


if __name__ == "__main__":
    main()
