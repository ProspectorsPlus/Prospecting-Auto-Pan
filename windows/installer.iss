; Inno Setup script for Prospector Lite
; Compile with:  ISCC installer.iss   (or via build.bat)
; Produces:      Output\ProspectorLite-<version>-Windows-x64-Setup.exe

#define MyAppName "Prospector Lite"
#define MyAppVersion "5.0.0"
#define MyAppPublisher "Prospector Lite project"
#define MyAppExeName "Prospector Lite.exe"

[Setup]
AppId={{9A3D71E4-2F60-47B9-8C55-PROSPLITE001}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=ProspectorLite-{#MyAppVersion}-Windows-x64-Setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; per-user install by default: no administrator rights needed (the dialog
; still lets someone deliberately pick an all-users install)
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible
; close a running copy cleanly before replacing files on upgrade
CloseApplications=force
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
; the entire PyInstaller one-folder output
Source: "dist\Prospector Lite\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall runasoriginaluser

; Uninstall removes the program files only. User data (settings, builds, run
; history) lives in %APPDATA%\Prospector Lite and is deliberately kept so an
; uninstall/reinstall never loses calibration; delete that folder by hand to
; remove every trace.
