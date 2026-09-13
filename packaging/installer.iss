; Inno Setup script: wraps the PyInstaller folder (dist\CrossingCount) into setup.exe.
; Build on Windows after PyInstaller:
;     ISCC.exe /DAppVersion=0.1.0 packaging\installer.iss
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{41744A5A-201C-4C45-9B79-DF3C1B32DAAA}
AppName=Crossing Count
AppVersion={#AppVersion}
AppPublisher=iTOi Solutions
DefaultDirName={autopf}\Crossing Count
DefaultGroupName=Crossing Count
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=CrossingCount-Setup-{#AppVersion}
Compression=lzma2/normal
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequiredOverridesAllowed=dialog
WizardStyle=modern
UninstallDisplayIcon={app}\CrossingCount.exe

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\CrossingCount\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Crossing Count"; Filename: "{app}\CrossingCount.exe"
Name: "{group}\Uninstall Crossing Count"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Crossing Count"; Filename: "{app}\CrossingCount.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\CrossingCount.exe"; Description: "Start Crossing Count now"; Flags: nowait postinstall skipifsilent
