; Inno Setup script for Prospectors Plus
; Compile with:  ISCC installer.iss   (or via build.bat)
; Produces:      Output\Prospectors Plus Setup.exe

#define MyAppName "Prospectors Plus"
#define MyAppVersion "2.2.3"
#define MyAppPublisher "Prospectors Plus"
#define MyAppExeName "Prospectors Plus.exe"

[Setup]
AppId={{8E4D2C1A-7F3B-4E9A-9C21-PROSPECTORS01}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=ProspectorsPlusSetup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; install per-user so no admin prompt is needed (works without SmartScreen admin)
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible
; for the in-app auto-updater: close the running app and relaunch after install
CloseApplications=force
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
; the entire PyInstaller one-folder output
Source: "dist\Prospectors Plus\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall runasoriginaluser
