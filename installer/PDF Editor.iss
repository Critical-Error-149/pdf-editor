; Installationsprogramm für den PDF Editor (Inno Setup 6). build.py ruft ISCC.exe mit diesen Werten auf:
;   /DAppVersion=0.2.8 /DSourceDir=<build\dist\PDF Editor> /DOutputDir=<dist> /DIconFile=<build\app.ico>
; Installation pro Benutzer (%LOCALAPPDATA%\Programs\PDF Editor): keine Administratorrechte nötig, und das
; Selbst-Update kann den Ordner später beschreiben.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "PDF Editor"
#define AppExe "PDF Editor.exe"
#define ProgId "PDFEditor.Dokument"
; eigener Schlüssel für die „Standard-Apps“-Angaben – NICHT Software\PDFEditor (dort liegen die Einstellungen)
#define CapKey "Software\PDF Editor (Installation)\Capabilities"
; Ablageorte des Programms (QSettings „PDFEditor/PDFEditor“, Protokolle in main.py) – für „restlos entfernen“;
; nur zum Testen überschreibbar (/DSettingsKey=… /DDataDir=…), damit echte Einstellungen unberührt bleiben
#ifndef SettingsKey
  #define SettingsKey "Software\PDFEditor"
#endif
#ifndef DataDir
  #define DataDir "{localappdata}\PDF Editor"
#endif

[Setup]
AppId={{8F3B6A52-4C1D-4E7A-9B2F-5D6E7A8C9B10}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Critical-Error-149
AppPublisherURL=https://gist.github.com/Critical-Error-149/79b10e874ee6302909641519633e18c5
VersionInfoVersion={#AppVersion}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
UsePreviousAppDir=yes
OutputDir={#OutputDir}
OutputBaseFilename=PDF-Editor-{#AppVersion}-Setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; laufendes Programm vor dem Ersetzen schließen (Restart Manager) – auch beim stillen Update
CloseApplications=force
RestartApplications=no
ChangesAssociations=yes
SetupLogging=yes

[Languages]
Name: "de"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktopicon"; Description: "Symbol auf dem Desktop anlegen"; GroupDescription: "Zusätzlich:"; Flags: unchecked
Name: "pdfassoc"; Description: "Als Programm für PDF-Dateien anbieten („Öffnen mit“, Standard-Apps)"; GroupDescription: "Zusätzlich:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Reste älterer Versionen (z. B. Quellcode-Ordner früherer ZIPs, Sicherungen eines Selbst-Updates)
Type: filesandordirs; Name: "{app}\Quellcode"
Type: filesandordirs; Name: "{app}\_internal.alt"

[UninstallDelete]
; auch Dateien, die ein späteres Selbst-Update hinzugefügt hat (Inno kennt nur die selbst installierten). Fremde
; Dateien im Programmordner (z. B. dort gespeicherte PDFs) bleiben – dann bleibt auch der Ordner.
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\licenses"
Type: filesandordirs; Name: "{app}\Quellcode"
Type: filesandordirs; Name: "{app}\_internal.alt"
Type: files; Name: "{app}\{#AppExe}.alt"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; Dateityp-Beschreibung (ProgID) und „Öffnen mit“ für .pdf – die Standard-App festlegen kann unter Windows 10/11 nur
; der Nutzer selbst (Einstellungen → Apps → Standard-Apps)
Root: HKA; Subkey: "Software\Classes\{#ProgId}"; ValueType: string; ValueName: ""; ValueData: "PDF-Dokument"; Flags: uninsdeletekey; Tasks: pdfassoc
Root: HKA; Subkey: "Software\Classes\{#ProgId}\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"",0"; Tasks: pdfassoc
Root: HKA; Subkey: "Software\Classes\{#ProgId}\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""; Tasks: pdfassoc
Root: HKA; Subkey: "Software\Classes\.pdf\OpenWithProgids"; ValueType: string; ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue; Tasks: pdfassoc
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "{#AppName}"; Flags: uninsdeletekey; Tasks: pdfassoc
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".pdf"; ValueData: ""; Tasks: pdfassoc
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""; Tasks: pdfassoc
; „Standard-Apps“: Programm mit seinen Fähigkeiten anmelden
Root: HKA; Subkey: "{#CapKey}"; ValueType: string; ValueName: "ApplicationName"; ValueData: "{#AppName}"; Flags: uninsdeletekey; Tasks: pdfassoc
Root: HKA; Subkey: "{#CapKey}"; ValueType: string; ValueName: "ApplicationDescription"; ValueData: "PDFs ansehen, beschriften und bearbeiten – für Stift und Touch"; Tasks: pdfassoc
Root: HKA; Subkey: "{#CapKey}\FileAssociations"; ValueType: string; ValueName: ".pdf"; ValueData: "{#ProgId}"; Tasks: pdfassoc
Root: HKA; Subkey: "Software\RegisteredApplications"; ValueType: string; ValueName: "{#AppName}"; ValueData: "{#CapKey}"; Flags: uninsdeletevalue; Tasks: pdfassoc
; der Schlüssel gehört allein dem Installer (Einträge werden rückwärts entfernt – „ifempty“ griffe hier zu früh)
Root: HKA; Subkey: "Software\PDF Editor (Installation)"; Flags: uninsdeletekey; Tasks: pdfassoc

[Run]
Filename: "{app}\{#AppExe}"; Description: "{#AppName} jetzt starten"; Flags: nowait postinstall skipifsilent

[Code]
// Deinstallation: auf Wunsch alles restlos entfernen – Einstellungen, Favoriten, gespeicherte Passwörter (Registry
// HKCU\Software\PDFEditor), Protokolle/Absturzberichte (%LOCALAPPDATA%\PDF Editor). PDF-Dateien werden nie gelöscht.
// Still (/VERYSILENT) nur mit dem Schalter /RESTLOS, sonst bleiben die Einstellungen.
const
  RunningMutex = 'PDFEditorLaeuft';   // legt das laufende Programm an (pdfeditor/single_instance.py)
  SettingsKey = '{#SettingsKey}';
  DataDir = '{#DataDir}';

var
  RemoveAll: Boolean;

function SetFileAttributes(lpFileName: String; dwFileAttributes: Cardinal): Boolean;
  external 'SetFileAttributesW@kernel32.dll stdcall';

function HasParam(const Name: String): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), Name) = 0 then
      Result := True;
end;

// Ordner samt Inhalt löschen – PDF-Dateien ausgenommen (dann bleibt der Ordner mit ihnen stehen).
// Verknüpfte Ordner (Junctions/Symlinks) werden nur entfernt, nicht durchlaufen.
procedure DelTreeKeepPdf(const Dir: String);
var
  FR: TFindRec;
  Path: String;
begin
  if FindFirst(AddBackslash(Dir) + '*', FR) then
  begin
    try
      repeat
        if (FR.Name <> '.') and (FR.Name <> '..') then
        begin
          Path := AddBackslash(Dir) + FR.Name;
          if (FR.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
          begin
            if (FR.Attributes and FILE_ATTRIBUTE_REPARSE_POINT) <> 0 then
              RemoveDir(Path)
            else
              DelTreeKeepPdf(Path);
          end
          else if CompareText(ExtractFileExt(FR.Name), '.pdf') <> 0 then
          begin
            if (FR.Attributes and FILE_ATTRIBUTE_READONLY) <> 0 then
              SetFileAttributes(Path, FR.Attributes and not FILE_ATTRIBUTE_READONLY);
            DeleteFile(Path);
          end;
        end;
      until not FindNext(FR);
    finally
      FindClose(FR);
    end;
  end;
  RemoveDir(Dir);   // klappt nur, wenn er jetzt leer ist
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  // läuft das Programm noch, würde es beim Beenden Einstellungen neu schreiben und Dateien blockieren
  while Result and CheckForMutexes(RunningMutex) do
    Result := (not UninstallSilent) and
      (MsgBox('Der PDF Editor ist noch geöffnet.' #13#10#13#10 +
              'Bitte das Programm schließen (ungespeicherte Änderungen vorher speichern) und dann „Wiederholen“ wählen.',
              mbError, MB_RETRYCANCEL) = IDRETRY);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    if UninstallSilent then
      RemoveAll := HasParam('/RESTLOS')
    else
      RemoveAll := MsgBox('Sollen auch alle persönlichen Daten des PDF Editors entfernt werden?' #13#10#13#10 +
        '•  Einstellungen, Favoriten und die Liste der zuletzt geöffneten Dateien' #13#10 +
        '•  gespeicherte PDF-Passwörter' #13#10 +
        '•  Fehlerprotokolle und Absturzberichte' #13#10#13#10 +
        'Ihre PDF-Dateien werden in keinem Fall gelöscht.' #13#10#13#10 +
        'Ja: alles restlos entfernen' #13#10 +
        'Nein: Einstellungen behalten (z. B. für eine spätere Neuinstallation)',
        mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
  end
  else if CurUninstallStep = usPostUninstall then
  begin
    DelTreeKeepPdf(AddBackslash(GetTempDir) + 'PDF Editor Update');   // Reste eines Selbst-Updates
    if RemoveAll then
    begin
      RegDeleteKeyIncludingSubkeys(HKCU, SettingsKey);
      DelTreeKeepPdf(ExpandConstant(DataDir));
    end;
  end;
end;
