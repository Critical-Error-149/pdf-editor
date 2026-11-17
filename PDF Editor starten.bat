@echo off
setlocal
cd /d "%~dp0"
rem Ein Python suchen, in dem PyMuPDF und PySide6 installiert sind. Einfach "pythonw" aufzurufen reicht nicht:
rem Steht z. B. Anaconda zuerst im Suchpfad, fehlen dort die Pakete und das Programm startet lautlos nicht.
for /f "delims=" %%P in ('where pythonw 2^>nul') do (
    if exist "%%~dpPpython.exe" (
        "%%~dpPpython.exe" -c "import pymupdf, PySide6" >nul 2>&1 && (
            start "" "%%P" main.py %*
            exit /b 0
        )
    )
)
rem Python-Starter (py) als zweiter Versuch
where py >nul 2>&1 && (
    py -3 -c "import pymupdf, PySide6" >nul 2>&1 && (
        start "" pyw -3 main.py %*
        exit /b 0
    )
)
echo.
echo Der PDF Editor konnte nicht gestartet werden:
echo Es wurde kein Python gefunden, in dem PyMuPDF und PySide6 installiert sind.
echo.
echo Abhilfe (im Projektordner):   python -m pip install -r requirements.txt
echo Oder die fertige Version aus dem Ordner "dist" (PDF Editor.exe) verwenden.
echo.
pause
exit /b 1
