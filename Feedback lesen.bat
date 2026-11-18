@echo off
setlocal
cd /d "%~dp0"
rem Neues Feedback holen, entschluesseln (privater Schluessel in %USERPROFILE%\PDF Editor Schluessel)
rem und den Ordner oeffnen. Mit Parameter --alle werden auch schon gelesene Nachrichten erneut geholt.
rem Nur ASCII und CRLF: cmd liest Batch-Dateien sonst stellenweise falsch.
set "PY="
for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined PY (
        "%%P" -c "import cryptography, pymupdf" >nul 2>&1 && set "PY="%%P""
    )
)
if not defined PY (
    where py >nul 2>&1 && py -3 -c "import cryptography, pymupdf" >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
    echo Kein Python mit den noetigen Paketen gefunden.
    echo Abhilfe im Projektordner:   python -m pip install -r requirements.txt
    pause
    exit /b 1
)
%PY% tools\feedback_lesen.py %*
if exist "%USERPROFILE%\Documents\PDF Editor Feedback" start "" "%USERPROFILE%\Documents\PDF Editor Feedback"
echo.
pause
