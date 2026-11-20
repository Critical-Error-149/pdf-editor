# Änderungen

Neue Einträge kommen unter „Neu“ (eine Zeile je Änderung, beginnend mit „- “). `python build.py --release` macht
daraus den Abschnitt der neuen Version; das Programm zeigt ihn bei „Nach Updates suchen“ an.

## Neu
- Striche und Marker zeichnet jetzt die Grafikkarte: bei jeder Zoomstufe sofort scharf, und nach neuen oder gelöschten Strichen muss nichts neu gerendert werden
- Gescannte PDFs: Seitenbilder liegen direkt auf der Grafikkarte – Scrollen viel flüssiger (gemessen 17 → 93 Bilder/s), Zoomen sofort scharf
- Beim Verschieben von Strichen zeigte die Vorschau verfälschte Farben (z. B. Blau zu blass)
- Schnellere Darstellung: Seitenkacheln liegen gesammelt auf der Grafikkarte und werden beim Scrollen und Zoomen wiederverwendet statt jedes Bild neu aufgebaut (Rechenzeit pro Bild beim Scrollen etwa halbiert, im Stillstand ein Fünftel, Zoomen schneller scharf)
- Installationsprogramm: Häkchen der zusätzlichen Aufgaben waren bei hoher Bildschirmskalierung halb abgeschnitten

## 0.2.8 – 19.11.2026
- Automatische Suche nach Updates beim Programmstart (abschaltbar unter Einstellungen → Updates & Feedback); bei einer neuen Version werden die Änderungen angezeigt
- „Jetzt aktualisieren“: Das Programm lädt die neue Version selbst herunter, ersetzt sich nach dem Schließen und startet neu
- Download ohne Quellcode: Programm und Quellcode sind jetzt zwei getrennte ZIP-Dateien (der Quellcode bleibt frei erhältlich)
- Beim Start öffnet sich die zuletzt geöffnete PDF (abschaltbar); das Fenster ist dabei sofort bedienbar
- Seiten drehen mit „Breite angleichen“: gedrehte Seiten werden so breit wie die übrigen Seiten
- Vorlage selbst eingefügter Seiten nachträglich ändern (Muster und Format, z. B. A4 → A3) über das Kontextmenü
- Beim Skalieren einer Seite mit Zeichnungen fragt das Programm, ob Zeichnungen und Strichdicke mitskaliert werden
- Feedback senden (Datei → Feedback senden): verschlüsselt, nur der Entwickler kann es lesen; Fehlerprotokoll standardmäßig dabei
- Hilfe und Feedback stehen im Menü „Datei“
- Farbige Symbole (📌 Speicherorte, 📁 Dateien) sind zurück
- „Senden“, „Drucken“ und „OK“ wirkten nach dem Klick auf einen anderen Knopf fälschlich ausgegraut
- Programm blieb nach „Feedback senden“ aus den Einstellungen gesperrt
- Neu: Installationsprogramm (ohne Administratorrechte) mit Startmenü-Eintrag, optionalem Desktop-Symbol und „Öffnen mit“ für PDFs; die portable ZIP-Version gibt es weiterhin
- Die installierte Version aktualisiert sich über „Jetzt aktualisieren“ ebenfalls selbst
- Deinstallieren auf Wunsch restlos – inklusive Einstellungen, gespeicherter Passwörter und Protokolle (PDF-Dateien bleiben immer erhalten)

## 0.2.7 – 18.11.2026
- „Nach Updates suchen“ funktioniert ohne Anmeldung über die Download-Seite (auch bei anderen)

## 0.2.6 – 18.11.2026
- Einstellungen auf Registerkarten verteilt (das Fenster war höher als der Bildschirm)

## 0.2.5 – 18.11.2026
- Einstellungen → Updates: „Nach Updates suchen“
- Eigener Druckdialog mit Seitenvorschau
- Passwörter geschützter PDFs merken, Passwortschutz entfernen; Schutz bleibt beim Speichern erhalten
- Lineal zum Zeichnen und Messen
- Glättung der Stiftstriche einstellbar
- Nur ein laufendes Programm: Dateien aus dem Explorer öffnen im vorhandenen Fenster
- Strg+C kopiert mit der Maus markierten Text
