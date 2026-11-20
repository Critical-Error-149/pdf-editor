# PDF Editor

Windows-PDF-Editor für Stift, Touch und Maus (z. B. Surface) auf Basis von **PySide6 (Qt)** und **PyMuPDF**:
schreiben und markieren, vorhandenen Text bearbeiten, Seiten einfügen, drehen und ordnen, Passwörter entfernen,
Lineal, Scans flüssig über die Grafikkarte – kostenlos, ohne Werbung, ohne Konto. Freie Software (GNU AGPL-3.0).

## ⬇️ [Neueste Version herunterladen](https://github.com/Critical-Error-149/pdf-editor/releases/latest)

Dort unter „Assets“ entweder `PDF-Editor-Setup.exe` (Installation ohne Administratorrechte, mit
Startmenü-Eintrag und „Öffnen mit“ für PDFs) oder `PDF-Editor-portable.zip` (entpacken und `PDF Editor.exe`
starten) laden – kein Python nötig. Ältere Versionen: [alle Releases](https://github.com/Critical-Error-149/pdf-editor/releases).

Direkte Links, die immer die neueste Version laden (zum Weitergeben):
[Setup](https://github.com/Critical-Error-149/pdf-editor/releases/latest/download/PDF-Editor-Setup.exe) ·
[portable ZIP](https://github.com/Critical-Error-149/pdf-editor/releases/latest/download/PDF-Editor-portable.zip) ·
[Quellcode](https://github.com/Critical-Error-149/pdf-editor/releases/latest/download/PDF-Editor-Quellcode.zip).
Der frühere Download-Gist verweist nur noch hierher (und hält für Versionen bis 0.2.8 die Update-Information bereit).

## Starten

```
pip install -r requirements.txt
python main.py [datei.pdf]
```

oder Doppelklick auf `PDF Editor starten.bat`.

## Als .exe bauen und weitergeben

```
pip install -r requirements-dev.txt
python build.py
```

Ergebnis in `dist/`:
- `PDF-Editor-<Version>-portable.zip` mit dem Ordner `PDF Editor` (darin `PDF Editor.exe` und `LIESMICH.txt`) –
  keine Installation nötig, entpacken und starten
- `PDF-Editor-<Version>-Setup.exe` – Installationsprogramm (braucht beim Bauen [Inno Setup 6](https://jrsoftware.org/isinfo.php):
  `winget install JRSoftware.InnoSetup`; fehlt es, wird das Setup übersprungen)
- `PDF-Editor-<Version>-quellcode.zip`

Das Setup (`installer/PDF Editor.iss`) installiert pro Benutzer nach `%LOCALAPPDATA%\Programs\PDF Editor` (keine
Administratorrechte; wahlweise auch für alle Benutzer). Es legt einen Startmenü-Eintrag an, auf Wunsch ein
Desktop-Symbol, und meldet den PDF Editor für „Öffnen mit“/Standard-Apps an (ProgID `PDFEditor.Dokument`).
Beim Deinstallieren fragt es, ob alles restlos entfernt werden soll: Einstellungen (`HKCU\Software\PDFEditor`)
und `%LOCALAPPDATA%\PDF Editor`. PDF-Dateien bleiben immer erhalten. Still: `unins000.exe /VERYSILENT [/RESTLOS]`.
Läuft das Programm noch, bricht die Deinstallation mit einem Hinweis ab. Das Programm meldet sich dafür über das
Mutex `PDFEditorLaeuft`.

Neue Version weitergeben: `python build.py` ausführen und das neue ZIP verschicken. Jeder Build erhöht
die Versionsnummer automatisch (letzte Stelle von `__version__` in `pdfeditor/__init__.py`), so hat jedes
weitergegebene ZIP eine eigene Nummer. Empfänger ersetzen den alten Ordner durch den neuen; Einstellungen
bleiben erhalten (Registrierung).

Auf GitHub veröffentlichen: Änderungen unter „Neu“ in `CHANGELOG.md` eintragen (Pflicht), dann
`python build.py --release`. Das baut wie oben, macht aus „Neu“ den Abschnitt der neuen Version, committet und pusht
Versionsnummer und Änderungsliste und legt das Release `v<Version>` an: Setup, Programm- und Quellcode-ZIP (feste
Dateinamen, damit `…/releases/latest/download/<Datei>` immer die neueste Version liefert) und `version.json`
(Version, Links auf genau diese Dateien, Prüfsummen, Größen, Änderungsliste). Der frühere Download-Gist bekommt nur
noch einen Verweis und – als Brücke für Versionen bis 0.2.8 – dieselbe `version.json` (`gh` muss angemeldet sein,
Inno Setup installiert).

Updates im Programm: Kurz nach dem Start prüft das Programm im Hintergrund `version.json` des neuesten Releases
(abschaltbar unter
Einstellungen → Updates & Feedback; dort auch „Nach Updates suchen“) – ohne Anmeldung, also auch bei anderen. Bei
einer neuen Version zeigt ein Fenster die Änderungen („Diese Version überspringen“: beim Start erst wieder bei der
nächsten Version melden – Einstellung `updates/skipped`; „Nach Updates suchen“ zeigt sie weiter, mit „Wieder
melden“); „Jetzt aktualisieren“ lädt das Programm-ZIP, prüft die
Prüfsumme (SHA-256), entpackt es und schließt das Programm (ungespeicherte Änderungen werden abgefragt). Ein kleines
PowerShell-Skript (`pdfeditor/self_update.py`) ersetzt dann die Programmdateien – die alten werden vorher zur Seite
gelegt und bei einem Fehler wiederhergestellt – und startet die neue Version. Das funktioniert ohne Installation,
solange der Programmordner beschreibbar ist (sonst: „Download-Seite öffnen“). Die installierte Version (erkennbar an
`unins000.exe` neben der .exe) lädt stattdessen das Setup und führt es nach dem Schließen still aus. So stimmen
auch Versionsangabe und Deinstallation. Bietet `version.json` noch kein Setup an, ersetzt sie die Dateien wie die
portable Version. Für HTTPS behält der Build nur die
Windows-eigene TLS-Erweiterung von Qt (Schannel); OpenSSL ist nicht enthalten.

Quellcode: liegt nicht im Programm-ZIP, sondern als eigenes ZIP am selben Ort (GNU AGPL Abschnitt 6 d) – in jedem
Release („PDF-Editor-Quellcode.zip“); dazu ist das Repository öffentlich. LIZENZEN.txt, LIESMICH.txt und „Über“
nennen den Ort.

Fehler der .exe landen in `%LOCALAPPDATA%\PDF Editor\fehler.log` bzw. `absturz.log`; reagiert das Programm
20 s lang nicht, stehen die Aufrufstapel aller Threads in `haenger.log`. Bei harten Abstürzen schreibt das Programm
ein Speicherabbild `absturz-….dmp` (die letzten 5), Meldungen von Qt landen in `qt.log`.
Startet der Grafiktreiber neu (Gerät verloren), schaltet die Ansicht sofort auf die normale Darstellung und versucht
die Grafikkarte nach einigen Sekunden erneut (höchstens zweimal, jeweils mit Prüfung, ob wirklich gezeichnet wird).

## Funktionen

- PDF (und XPS, EPUB, Bilder …) in jedem Seitenformat öffnen, speichern, drucken
- Eigener Druckdialog mit Seitenvorschau (der Windows-11-Dialog zeigt für klassische Programme nur „Diese App
  unterstützt keine Seitenansicht“): Drucker, Exemplare, Seiten (alle, aktuelle, in der Miniaturleiste ausgewählte
  oder „1-3, 5, 8-“), Papier, Ausrichtung automatisch je Seite (Querformat-Seiten kommen quer aufs Papier),
  Anpassen/Nur verkleinern/Tatsächliche Größe, Graustufen, beidseitig, mit oder ohne Zeichnungen und Notizen.
  Die Vorschau zeigt Papier und nicht bedruckbaren Rand des Druckers; „Druckereinstellungen …“ öffnet den
  Windows-Dialog für Treiberoptionen. Die Einstellungen werden gemerkt
- Seiten an beliebiger Stelle einfügen: leer, liniert, kariert, Punktraster; jedes Format (A3–A6, Letter, Folie, benutzerdefiniert in mm)
- Vorlage selbst eingefügter Seiten nachträglich ändern (Rechtsklick/langes Drücken/Stift-Seitentaste auf die Seite
  oder in der Miniaturleiste → „Vorlage ändern …“): Muster (leer, liniert, kariert, Punktraster) und Format
  (z. B. A4 → A3, Ausrichtung, eigene Maße); der Hintergrund wird neu gezeichnet. Sind Zeichnungen auf der Seite,
  fragt das Programm, ob sie (und ihre Strichdicke) mitskaliert werden. Neue Vorlagenseiten sind im PDF
  gekennzeichnet, ältere werden am Inhalt erkannt (nur Linien/Punkte in den Vorlagenfarben)
- Seiten drehen mit „Breite angleichen“ (Schalter neben den Dreh-Knöpfen und in den Kontextmenüs, Standard an):
  die gedrehte Seite wird auf die Breite der übrigen Seiten skaliert – Inhalt verlustfrei per Transformationsmatrix,
  Anmerkungen mit; sind Zeichnungen auf der Seite, fragt das Programm, ob Zeichnungen und Strichdicke mitskaliert
  werden sollen
- Andere PDFs oder Bilder als Seiten einfügen, Seiten duplizieren, drehen, löschen, exportieren
- Bildseiten: Format wählbar (Standard: Breite der vorigen Seite), Einpassen mit weißem/schwarzem/Vorlagen-Rand,
  Strecken oder Füllen, Zuschneiden – auch nachträglich (Rechtsklick → Bildseite bearbeiten), ohne Qualitätsverlust
- Einstellungen (Datei → Einstellungen, Strg+,): Grafikkarte, Scanseiten am Stück rendern, Bildseiten, Autosave
- Seiten per Drag & Drop in der Miniaturleiste umsortieren
- Eigene Stifte und Marker (Farbe + Dicke) als Schnellauswahl, wie in OneNote
- Stift schreibt, Finger scrollt/zoomt – ohne Umschalten; Radierer-Ende wird erkannt,
  Stift-Seitentaste öffnet das Kontextmenü; die Windows-Animation beim Antippen/Halten mit dem Stift ist
  im Programmfenster abgeschaltet (stört beim Schreiben von Punkten und kurzen Strichen)
- Kontextmenüs (langes Antippen) lassen sich auch mit dem Finger bedienen
- Ausgewählte Objekte (Lassoauswahl): Kopieren (Strg+C), Ausschneiden (Strg+X), Duplizieren (Strg+D), Farbe,
  Strichstärke, Schriftgröße und Deckkraft in der Optionsleiste; Einfügen mit Strg+V oder lange auf die Seite
  drücken → „Einfügen“ (an dieser Stelle) – auch Bilder und Text aus anderen Programmen
- Maus-Werkzeug mit der Maus: Ziehen markiert Text (Textcursor über Text) – Strg+C kopiert, Rechtsklick auf die
  Markierung: Textmarker, Unterstreichen, Durchstreichen; Umlaute aus LaTeX-PDFs („k¨orper“) werden beim Kopieren
  zusammengesetzt. Die Seite verschiebt man mit Mausrad, mittlerer Maustaste oder Finger
- Maus-Werkzeug mit dem Stift: Über die Seite ziehen wechselt automatisch zum zuletzt benutzten Stift bzw. Marker
  und zeichnet ab dem Aufsetzpunkt; Tippen bleibt ein Klick (Link, Video, Notiz), Halten ein Rechtsklick
  (abschaltbar unter Einstellungen → Bedienung – dann verschiebt der Stift die Seite)
- Fingermodus (Zeichnen → Auswahl → „Finger“): Ein Finger zeichnet sofort mit dem gewählten Stift, Marker,
  Radierer bzw. der Form; mit zwei Fingern wird weiter gezoomt/verschoben – kommt der zweite Finger etwas später,
  wird die gerade angefangene Zeichnung verworfen (Wegradiertes kommt zurück). Im Maus-/Lasso-Werkzeug scrollt
  ein Finger wie gewohnt
- Rückgängig/Wiederholen behält die sichtbare Stelle (kein Sprung an den Seitenanfang)
- Rechtsklick (Maus oder Stift-Seitentaste): Ziehen = Lassoauswahl in jedem Werkzeug, ohne Bewegung = Kontextmenü;
  mit dem Stift läuft das Lasso bis zum Abheben (Seitentaste muss nicht gehalten werden)
- Verschieben ausgewählter Objekte (auch vieler auf einmal) ist live sichtbar: Die Auswahl wird als Bild mitgeführt,
  die Originale sind solange ausgeblendet; beim Loslassen werden Striche, Linien, Formen, Markierungen und Bilder
  direkt im PDF versetzt (Erscheinungsbild bleibt) – 30 Striche in ~60 ms statt einzeln neu erzeugt
- Radierer: Formen (Rechteck, Ellipse, Vieleck) werden nur an ihrem Rand wegradiert – darin kann man radieren.
  Die eingestellte Größe ist der Durchmesser des Kreises (wie die Strichstärke beim Stift), auch unter 1 pt;
  das Radierende des Stifts zeigt ebenfalls den Radierer-Kreis
- Tonaufnahme im Hintergrund (Einfügen → Mikrofon): Das Audio-Symbol erscheint sofort auf der Seite, die
  Aufnahmeleiste oben zeigt die Zeit und hat Pause/Fortsetzen, „Stopp & einfügen“ und „Verwerfen“; währenddessen
  kann man normal weiterarbeiten
- Notizen lesen ohne zu bearbeiten: im Maus-Werkzeug antippen/klicken (oder in der Auswahl antippen) zeigt den Text
  als Sprechblase mit „Bearbeiten“; mit der Maus genügt Darüberfahren. Die Sprechblase bleibt beim Scrollen und
  Zoomen offen und wandert mit; Tippen/Klicken daneben schließt sie (abschaltbar unter Einstellungen → Bedienung)
- Ausgewählte Objekte (z. B. per Rechtsklick/langem Drücken) lassen sich in jedem Werkzeug direkt mit Maus oder
  Stift verschieben und an den Ecken vergrößern
- Beim Start öffnet sich die zuletzt geöffnete PDF (abschaltbar unter Einstellungen → Darstellung → Beim
  Programmstart). Das Fenster ist dabei sofort bedienbar: ob die Datei noch da ist, prüft ein Hintergrund-Thread;
  wer vorher selbst eine Datei öffnet, behält sie; geschützte PDFs nur mit gemerktem Passwort (keine Abfrage)
- Beim Start geöffneter Menütab und Seitenleiste wählbar (Einstellungen → Beim Programmstart; Standard: wie zuletzt)
- Zoomen (Knöpfe, Seitenbreite, Strg+Mausrad) behält die sichtbare Stelle; „Ganze Seite“ zeigt die Seite in der
  Bildmitte vollständig
- Einstellung „Nach dem Einfügen automatisch zum Maus-Werkzeug wechseln“ (Datei → Einstellungen → Bedienung)
- Haltezeit für langes Drücken und Einrastwinkel für gerade Linien einstellbar (Datei → Einstellungen → Bedienung)
- Lineal (Zeichnen → Messen → „Lineal“, Strg+R; auch in der Vollbild-Leiste): liegt fest auf dem Bildschirm, Skala
  in echten Zentimetern der Seite (passt sich dem Zoom an). Stift oder Marker an einer Längskante zieht eine gerade
  Linie in jedem Winkel, die Länge steht dabei auf dem Lineal und danach in der Statusleiste („Linie: 6,4 cm“).
  Verschieben mit einem Finger, Stift oder Maus auf dem Lineal; Drehen mit zwei Fingern (rastet bei 0°, 45°, 90° …
  ein) oder dem Mausrad (1° je Raste); der Winkel wird angezeigt. Im Fingermodus zeichnet ein Finger an der Kante
  ebenfalls gerade Linien. Die Grafikkarte legt das Lineal gedreht auf – Drehen und Verschieben kosten nichts
- Glättung der Stiftstriche einstellbar (Einstellungen → Bedienung, Stufe 0 = genau wie geschrieben bis 10 = stark;
  Standard 3): Gauß-Mittel entlang der Strichlänge, gemessen in Bildschirmpixeln – wirkt bei jedem Zoom und
  Schreibtempo gleich; Anfang und Ende des Strichs bleiben an ihrem Platz
- Rückgängig nach Gesten: Eine erkannte Form wird wieder zum Freihandstrich; nach dem Durchkritzeln kommen
  Gekritzel und Durchgestrichenes zurück (ein weiteres Rückgängig entfernt das Gekritzel)
- Linie, Pfeil, Rechteck, Ellipse, Textfelder, Notizzettel, Text hervorheben
  (Textfelder: blinkender Cursor, beim Schreiben dieselbe Schrift wie im fertigen Textfeld,
  Tab wird zu vier Leerzeichen; Textfelder mit kaputtem Schriftverweis werden beim Öffnen repariert;
  Textmarkierungen auf einem Textfeld wandern beim Verschieben mit)
- Bilder einfügen (Datei oder Strg+V), danach verschieben, skalieren, zuschneiden, drehen, löschen;
  Bild aus der Zwischenablage auch als eigene Seite (Strg+Umschalt+V, z. B. eine kopierte Folie)
- Radierer, Lassoauswahl (mehrere Objekte umfahren und gemeinsam verschieben/löschen; einzelnes Objekt
  antippen: verschieben, Größe ändern, Text per Doppelklick bearbeiten; umfahrenen Text kopieren)
- Rückgängig/Wiederholen (MuPDF-Journal: merkt sich nur die geänderten Objekte), Volltextsuche,
  Zoom (Strg+Mausrad, Pinch)
- Maus-Werkzeug: Links anklicken (Maus, Stift oder Finger; Handzeiger über Links), Ziehen verschiebt die
  Ansicht; „Zurück“ (Alt+←, Maus-Zurücktaste, Knopf unter Ansicht → Navigation) springt zur vorherigen Stelle.
  Webseiten öffnen nach Rückfrage, Programme werden nie gestartet
- Große PDFs (ab 64 MB, z. B. ein 700-MB-Fachbuch) werden direkt von der Platte gelesen und beim Speichern
  nur um die Änderungen ergänzt; mit 8 GB Arbeitsspeicher halbiert das Programm seine Zwischenspeicher
- Video und Audio einfügen (Einfügen → Video/Audio) oder direkt aufnehmen (Einfügen → Mikrofon; Einfügen →
  Kamera öffnet die Windows-Kamera-App: neue Fotos/Videos in „Bilder\Eigene Aufnahmen“ werden erkannt, sobald
  sie fertig gespeichert sind, eingefügt und danach auf Wunsch in den Papierkorb gelöscht – nur Dateien, die
  nach dem Öffnen der Kamera neu entstanden sind und Byte für Byte dem Eingebetteten entsprechen); alles wird
  in die PDF eingebettet (keine Zusatzdateien),
  als PDF-Standard-Bildschirmobjekt mit Vorschaubild – Adobe Acrobat/Reader spielt es ebenfalls ab, andere
  Betrachter zeigen das Vorschaubild und bieten die Datei als Anhang an. Abspielen: im Maus-Werkzeug antippen,
  Doppelklick (Auswahl), Kontextmenü oder „Abspielen“ in der Optionsleiste. Audio erscheint als kleines
  Lautsprecher-Symbol (wie eine Notiz); Antippen spielt ab bzw. pausiert, der Schieberegler erscheint darunter.
  Videowiedergabe mit Grafikkarten-Darstellung: Die Bildebenen (NV12/YUV420) werden im Hintergrund kopiert und
  im Shader (`shaders/video.frag`) nach RGB umgerechnet; auch die Bedienleiste zeichnet die Grafikkarte – so läuft
  1080p mit vollen 30 Bildern/s ohne Ruckler (vorher ~22). Ohne Grafikkarten-Darstellung ~20 Bilder/s.
  Antippen des Videos hält an bzw. spielt weiter.
  Notiz-Symbole haben leicht abgerundete Ecken (auch in anderen Betrachtern). Auch Videos aus anderen PDFs
- Animierte GIFs (über das Bild-Werkzeug): PDF zeigt das erste Bild, im Editor läuft die Animation
  (im Maus- und Auswahl-Werkzeug)
- Getaggte PDFs (z. B. aus Word/Acrobat PDFMaker) werden beim Öffnen vorbereitet, Lesezugriffe laufen in
  impliziten Journal-Schritten (sonst stürzt MuPDF bei aktivem Rückgängig-Journal ab); lange Windows-Pfade
  (über 260 Zeichen) werden unterstützt
- Verweigert Windows den Zugriff (Datei in anderem Programm geöffnet, OneDrive, „Überwachter Ordnerzugriff“),
  erklärt eine Meldung die möglichen Ursachen
- Menüband wie in OneNote (Start, Zeichnen, Einfügen, Seiten, Ansicht) mit benannten Gruppen; bei schmalem
  Fenster nur Symbole, Beschriftung als Tooltip
- Mehrere Dokumente in Tabs; Tabs lassen sich nebeneinander und untereinander teilen (Ansicht → Tabs,
  Rechtsklick auf einen Tab, Strg+\); Strg+Tab wechselt, Strg+W schließt; Tabs per Ziehen anordnen,
  dasselbe Dokument mehrfach öffnen (Duplizieren / Erneut öffnen), Dateien per Rechtsklick umbenennen
- Feedback senden (Datei → Feedback senden … oder Einstellungen → Updates & Feedback): Nachricht, freiwillig Name,
  Systeminformationen, Fehlerprotokoll (standardmäßig an, Benutzername in Pfaden unkenntlich), Bild des Programmfensters und eigene
  Bilder; „Vorschau“ zeigt genau, was gesendet wird. Alles wird mit dem öffentlichen Schlüssel des Entwicklers
  verschlüsselt (X25519 + AES-256-GCM, Bibliothek `cryptography`) und als Issue im privaten Repository
  `pdf-editor-feedback` abgelegt – kein GitHub-Konto nötig, niemand sonst kann es lesen. Das eingebaute Token darf nur
  Issues in diesem Repository anlegen; es steht nicht im Git-Repository (build.py setzt es beim Bauen aus
  `%LOCALAPPDATA%\PDF Editor\feedback\token.txt` ein). Lesen: Doppelklick auf `Feedback lesen.bat` bzw.
  `python tools/feedback_lesen.py` (entschlüsselt mit `%USERPROFILE%\PDF Editor Schlüssel\privater_schluessel.pem`,
  legt jede Nachricht unter Dokumente\PDF Editor Feedback ab – mit `feedback.pdf`, die Nachricht, Systemdaten, Bilder
  und Fehlerprotokoll zusammenfasst); Schlüsselpaar anlegen: `python tools/feedback_schluessel.py`
- Nur ein laufendes Programm: Wird eine PDF im Explorer geöffnet, während der Editor schon läuft, öffnet sie dort
  als Tab und das Fenster kommt nach vorn (auch mehrere gleichzeitig markierte Dateien); der zusätzliche Start
  beendet sich nach etwa 1 s, ohne Hauptfenster und PyMuPDF zu laden. Übergabe per lokaler Verbindung, die nur das
  eigene Windows-Konto benutzen kann (`pdfeditor/single_instance.py`)
- Vollbildmodus (F11): nur Dokument plus Rückgängig, Auswahl, Maus, Stifte und „Vollbild beenden“ (Esc);
  im schwebenden Fenster behält das Fenster seine Größe
- Dunkles Design im Stil „Modern Dark“
- Seitenansicht über die Grafikkarte (Direct3D 11): flüssiges Scrollen mit bis zu 120 Hz; fällt bei Problemen
  automatisch auf die normale Darstellung zurück (erzwingen: Umgebungsvariable `PDFEDITOR_RENDERER=raster`).
  Aufgebaut aus Ebenen, die die Grafikkarte jedes Bild neu zusammensetzt; Unverändertes wird wiederverwendet:
  - Seitenebene: Kacheln von MuPDF in wenigen Atlas-Texturen; nur bei Änderungen neu aufgebaut, Scrollen und
    Zoomen verschieben/skalieren sie über ihre Matrix
  - Scanseiten (nur ein JPEG, evtl. unsichtbarer OCR-Text): das Bild selbst als Textur mit Mipmaps, im Hintergrund
    dekodiert – sofort scharf in jeder Zoomstufe (`scan_pages.py`; abschalten: Einstellung `scan_textures=false`)
  - Striche (Freihand, Marker): als Dreiecke aus dem Erscheinungsbild, das MuPDF schreibt – bei jedem Zoom scharf,
    nach neuen/gelöschten Strichen wird keine Kachel neu gerastert (`ink_layer.py`, `shaders/ink.*`; abschalten:
    Einstellung `gpu_ink=false`)
  - darüber Überlagerung, Strich in Arbeit, Lineal, Bildlaufbalken
- Stiftgesten: gerade Linie bzw. Kreis/Rechteck durch Zeichnen und kurzes Halten,
  Löschen durch Durchkritzeln; Formen übernehmen die Eigenschaften des gewählten Stifts
- Dateileiste (F3) zum Durchsuchen von Ordnern und Öffnen von Dateien, Liste „Zuletzt“
- Automatisch speichern – global und pro Datei einstellbar
- Passwortgeschützte PDFs: Bei falschem Passwort wird gleich erneut gefragt; „Passwort auf diesem PC merken“
  speichert es mit Windows-DPAPI verschlüsselt (nur für das eigene Windows-Konto, auch nach Umbenennen/Verschieben
  der Datei gültig) und verwendet es beim nächsten Öffnen automatisch. Beim Speichern bleiben Schutz, Passwort,
  Verschlüsselung und Rechte erhalten; Datei → „Passwortschutz entfernen …“ speichert ohne Passwort,
  „Gespeichertes Passwort vergessen“ bzw. Einstellungen → „Alle löschen“ entfernt gemerkte Passwörter

Alle Zeichnungen werden als Standard-PDF-Annotationen gespeichert und sind in jedem
PDF-Reader sichtbar und dort weiterhin bearbeitbar.

## Aufbau

| Datei | Inhalt |
|---|---|
| `pdfeditor/document.py` | Dokumentmodell, Rückgängig über MuPDF-Journal, große PDFs, Seiten- und Annotationsoperationen |
| `pdfeditor/page_view.py` | Seitenansicht, Werkzeuge, Stifteingabe |
| `pdfeditor/thumbnails.py` | Miniatur-Seitenleiste |
| `pdfeditor/main_window.py` | Hauptfenster, Aktionen, Dialoge |
| `pdfeditor/gpu_view.py`, `pdfeditor/shaders/` | Seitenfläche über Direct3D (Seitenebene mit Kachel-Atlas, Scan-Texturen, Striche, Strich-Ebene, Bildlaufbalken) |
| `pdfeditor/renderer.py` | Kachel-Rendern im Leerlauf; Seiteninhalt und Anmerkungen getrennt (nach einem Strich nur die betroffenen Kacheln neu) |
| `pdfeditor/scan_pages.py` | Scanseiten erkennen und ihr Bild im Hintergrund für die Grafikkarte dekodieren |
| `pdfeditor/ink_layer.py` | Striche als Dreiecke für die Grafikkarte (aus MuPDFs Erscheinungsbild) |
| `pdfeditor/image_page.py` | Bildseiten: Aufbau, Ausschnitt, Dialog mit Vorschau |
| `pdfeditor/settings_dialog.py` | Einstellungen |
| `pdfeditor/tabs.py` | Tabs, Tab-Gruppen und Teilung (nebeneinander/untereinander) |
| `pdfeditor/ribbon.py` | Menüband mit Registerkarten, Gruppen und Kompaktmodus |
| `pdfeditor/theme.py`, `pdfeditor/icons.py` | Design „Modern Dark“ und Strich-Symbole |
| `pdfeditor/insert_dialog.py` | Dialog „Seiten einfügen“ |
| `pdfeditor/pen_feedback.py` | schaltet das visuelle Stift-Feedback von Windows für die eigenen Fenster ab |
| `pdfeditor/touch_menu.py` | Fingerbedienung für Kontextmenüs |
| `pdfeditor/media.py` | Video/Audio abspielen (Qt Multimedia), Vorschaubilder, GIF-Animation |
| `pdfeditor/capture.py` | Aufnehmen: Windows-Kamera-App (Foto/Video, sicheres Löschen), Mikrofon |
| `pdfeditor/text_editor.py`, `pdfeditor/fonts/` | Textfeld-Editor mit eigenem Cursor; MuPDFs Helvetica als TrueType |
| `tools/make_pdf_font.py` | erzeugt `pdfeditor/fonts/PDFSans.ttf` neu (nur bei neuer PyMuPDF-Version, braucht fonttools) |

## Lizenz

Der PDF Editor steht unter der **GNU AGPL-3.0-or-later** (`LICENSE`), Copyright © 2026 Critical-Error-149. Grund: PyMuPDF/MuPDF
ist AGPL-lizenziert, darum muss das ganze Programm bei der Weitergabe unter einer AGPL-kompatiblen Lizenz stehen und
mit Quellcode weitergegeben werden. Kostenlos an Freunde weitergeben ist damit erlaubt (Verkaufen übrigens auch –
aber immer mit Quellcode und unter der AGPL; eine geschlossene/kommerzielle Fassung bräuchte eine Lizenz von Artifex).

Was die Weitergabe verlangt und wie das Paket es erfüllt (`build.py` prüft die Dateien vor dem Bauen):
- Lizenztexte: `LICENSE` (AGPL-3.0), `licenses/` (GPL-3.0, LGPL-3.0, LGPL-2.1, PSF, PyInstaller) liegen neben der .exe
- Hinweise zu allen Bibliotheken samt Pflichtvermerken (FreeType, IJG) und Fundorten ihres Quellcodes: `LIZENZEN.txt`
- Quellcode des Programms (inkl. `build.py`) im Ordner `Quellcode` des Pakets
- Rechtshinweise in der Oberfläche: „Hilfe → Über“ (Urheber, Lizenz, keine Gewährleistung, Quellcode)
- LGPL: Qt/PySide6/FFmpeg liegen als einzelne, austauschbare DLLs im Ordner `_internal`
- FFmpeg ist eine reine LGPL-Fassung; nur LGPL-lizenzierte Qt-Module sind enthalten
