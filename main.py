# PDF Editor
# Copyright (C) 2026 Critical-Error-149
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero
# General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. This program is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# Affero General Public License (file LICENSE) for more details.
import ctypes
import datetime
import faulthandler
import os
import sys
import traceback

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from pdfeditor import APP_NAME, __version__


def log_dir():
    """Ordner für das Fehlerprotokoll: %LOCALAPPDATA%\\PDF Editor (beim Löschen des Programms gern mitlöschen)."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def install_crash_log():
    """Fehler in eine Datei schreiben – die .exe hat kein Konsolenfenster, sonst gingen sie verloren."""
    log_path = os.path.join(log_dir(), "fehler.log")
    # Harte Abstürze: install_crash_dump schreibt Speicherabbild + Python-Aufrufstapel nach absturz.log.
    # NICHT faulthandler.enable(): Unter Windows fängt es auch harmlose, behandelte Ausnahmen ab, die z. B. der
    # Datei-/Ordner-Dialog in seinen eigenen Threads auslöst, und gibt dann aus diesem fremden Thread heraus Stapel
    # aus – dabei stürzte es selbst ab (PyCode_Addr2Line, „+“ in der Seitenleiste → Ordner wählen).
    shown = [False]

    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n=== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} · Version {__version__} ===\n{text}")
        except OSError:
            pass
        if sys.stderr is not None:
            sys.stderr.write(text)
        if not shown[0] and QApplication.instance() is not None:
            shown[0] = True  # nur einmal pro Sitzung nachfragen
            QMessageBox.warning(None, APP_NAME,
                                f"Es ist ein Fehler aufgetreten:\n{exc}\n\n"
                                f"Details stehen in:\n{log_path}\n\nDas Programm läuft weiter.")

    sys.excepthook = hook


def install_diag_log():
    """Meldungen von Qt (z. B. „Device loss detected“) und Notizen der Grafikkarten-Ansicht nach qt.log schreiben.
    Nur ohne Konsole (.exe bzw. pythonw) – dort gingen sie sonst verloren."""
    if sys.stderr is not None and not getattr(sys, "frozen", False):
        return
    path = os.path.join(log_dir(), "qt.log")
    try:
        if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
            os.remove(path)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        os.write(fd, f"\n=== Sitzung {datetime.datetime.now():%Y-%m-%d %H:%M:%S} · Version {__version__} ===\n"
                 .encode("utf-8"))
        os.dup2(fd, 2)  # C-Standardfehlerausgabe (Qt schreibt dorthin)
        os.close(fd)
        os.environ["QT_FORCE_STDERR_LOGGING"] = "1"
    except OSError:
        pass


_dump_filter = None


def install_crash_dump():
    """Bei einem harten Absturz ein Speicherabbild (absturz-….dmp) schreiben – zeigt, wo es passiert ist.
    Windows legt für die .exe selbst keines an; die letzten 5 werden aufbewahrt."""
    global _dump_filter
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes as W
    folder = log_dir()
    try:
        dumps = sorted(f for f in os.listdir(folder) if f.startswith("absturz-") and f.endswith(".dmp"))
        for old in dumps[:-4]:
            os.remove(os.path.join(folder, old))
    except OSError:
        pass

    class MINIDUMP_EXCEPTION_INFORMATION(ctypes.Structure):
        _pack_ = 4  # in dbghelp.h mit pshpack4 gepackt – sonst liegt der Zeiger an der falschen Stelle
        _fields_ = [("ThreadId", W.DWORD), ("ExceptionPointers", ctypes.c_void_p), ("ClientPointers", W.BOOL)]

    k32 = ctypes.windll.kernel32
    dbghelp = ctypes.windll.dbghelp
    k32.CreateFileW.restype = W.HANDLE
    k32.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, ctypes.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
    k32.GetCurrentProcess.restype = W.HANDLE
    dbghelp.MiniDumpWriteDump.argtypes = [W.HANDLE, W.DWORD, W.HANDLE, ctypes.c_int,
                                          ctypes.POINTER(MINIDUMP_EXCEPTION_INFORMATION), ctypes.c_void_p,
                                          ctypes.c_void_p]
    dbghelp.MiniDumpWriteDump.restype = W.BOOL
    FILTER = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)

    def on_crash(pointers):
        try:
            path = os.path.join(folder, f"absturz-{datetime.datetime.now():%Y%m%d-%H%M%S}.dmp")
            h = k32.CreateFileW(path, 0x40000000, 0, None, 2, 0x80, None)
            info = MINIDUMP_EXCEPTION_INFORMATION(k32.GetCurrentThreadId(), pointers, False)
            # Threads, Stapel, Module und indirekt referenzierter Speicher (zum Auswerten der Aufrufkette)
            ok = dbghelp.MiniDumpWriteDump(k32.GetCurrentProcess(), k32.GetCurrentProcessId(), h, 0x1000 | 0x40,
                                           ctypes.byref(info), None, None)
            err = k32.GetLastError()
            k32.CloseHandle(h)
            os.write(2, f"Absturz – Speicherabbild: {'geschrieben' if ok else f'Fehler {err:#x}'}\n".encode())
        except Exception:
            pass
        try:  # Python-Aufrufstapel des abgestürzten Threads (nur dieser – andere Threads laufen evtl. noch)
            with open(os.path.join(folder, "absturz.log"), "a", encoding="utf-8") as f:
                f.write(f"\n=== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} · Version {__version__} · "
                        f"Absturz (Speicherabbild {os.path.basename(path)}) ===\n")
                f.flush()
                faulthandler.dump_traceback(f, all_threads=False)
        except Exception:
            pass
        return 0  # EXCEPTION_CONTINUE_SEARCH: Windows meldet den Absturz wie gewohnt

    _dump_filter = FILTER(on_crash)
    k32.SetUnhandledExceptionFilter.argtypes = [ctypes.c_void_p]
    k32.SetUnhandledExceptionFilter(ctypes.cast(_dump_filter, ctypes.c_void_p))


HANG_SECONDS = 20


def install_hang_watch(app):
    """Hänger aufzeichnen: Reagiert das Programm 20 s lang nicht (Haupt-Thread blockiert), schreibt faulthandler
    die Aufrufstapel aller Threads nach haenger.log – so lässt sich später sehen, wo es hing. Solange alles läuft,
    stellt ein Zeitgeber die Frist alle 2 s neu (kostet nichts)."""
    path = os.path.join(log_dir(), "haenger.log")
    try:
        if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
            os.remove(path)  # nicht endlos wachsen lassen
        f = open(path, "a", encoding="utf-8")
        f.write(f"\n=== Sitzung {datetime.datetime.now():%Y-%m-%d %H:%M:%S} · Version {__version__} ===\n")
        f.flush()
    except OSError:
        return
    app._hang_log = f  # offen halten (faulthandler schreibt direkt hinein)

    def rearm():
        faulthandler.dump_traceback_later(HANG_SECONDS, repeat=False, file=f, exit=False)

    timer = QTimer(app)
    timer.setInterval(2000)
    timer.timeout.connect(rearm)
    timer.start()
    app._hang_timer = timer
    rearm()


def main():
    if sys.platform == "win32":
        # eigenes Taskleisten-Symbol statt dem von Python
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"PDFEditor.{APP_NAME}")
    install_diag_log()
    install_crash_log()
    install_crash_dump()
    app = QApplication(sys.argv)
    paths = [os.path.abspath(a) for a in sys.argv[1:]]
    # Läuft das Programm schon (z. B. weitere PDF im Explorer doppelt angeklickt)? Dann dort öffnen und gleich
    # wieder beenden – vor dem Laden von Hauptfenster und PyMuPDF, damit das nur Bruchteile einer Sekunde dauert.
    from pdfeditor import single_instance
    primary = bool(os.environ.get("PDFEDITOR_SELFTEST") or os.environ.get("PDFEDITOR_NEW_INSTANCE")) \
        or single_instance.is_primary()
    if not primary and single_instance.send_to_running(paths):
        return
    server = single_instance.Server(app) if primary else None

    from pdfeditor import app_icon, pen_feedback, theme, touch_menu
    from pdfeditor.main_window import MainWindow
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setWindowIcon(app_icon.icon())
    install_hang_watch(app)
    theme.apply(app)  # dunkles Design („Modern Dark“)
    pen_feedback.install(app)  # keine Windows-Animation beim Antippen/Halten mit dem Stift
    touch_menu.install(app)  # Kontextmenüs auch mit dem Finger bedienbar
    win = MainWindow()
    win.show()
    for path in paths:
        win.open_file(path, ask=False)
    if server is not None:
        server.files_received.connect(lambda ps: open_from_other_start(win, ps))
    if os.environ.get("PDFEDITOR_SELFTEST"):
        QTimer.singleShot(4000, lambda: selftest(app, win))
    sys.exit(app.exec())


def open_from_other_start(win, paths):
    """Dateien eines weiteren Starts (Explorer) in diesem Fenster öffnen und es nach vorn holen."""
    from pdfeditor import single_instance
    single_instance.bring_to_front(win)
    for path in paths:
        win.open_file(path, ask=False)


def selftest(app, win):
    """Prüft die gebaute .exe (build.py): läuft die Grafikkarten-Darstellung, sind Übersetzungen da?"""
    from PySide6.QtWidgets import QDialogButtonBox
    box = QDialogButtonBox(QDialogButtonBox.Cancel)
    from pdfeditor import media
    lines = [f"version={__version__}", f"gpu={win.view.gpu}", f"media={media.available()}",
             f"frames={getattr(win.view.viewport(), 'frame_count', 0)}",
             f"pages={win.document.page_count}", f"cancel_text={box.button(QDialogButtonBox.Cancel).text()}"]
    with open(os.path.join(log_dir(), "selbsttest.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    win.document.set_modified(False)
    app.quit()


if __name__ == "__main__":
    main()
