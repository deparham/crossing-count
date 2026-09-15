; Inno Setup script: wraps the PyInstaller folder (dist\CrossingCount) into setup.exe.
; Build on Windows after PyInstaller:
;     ISCC.exe /DAppVersion=0.1.0 packaging\installer.iss
; With a code-signing certificate set up, build with
;     ISCC.exe /DAppVersion=0.1.0 /DSign packaging\installer.iss
; and a sign tool named "mysign" (ISCC /Smysign=... or Inno's Tools > Configure Sign Tools).
; Unsigned, Windows SmartScreen warns that the publisher is unknown: docs/INSTALL_WINDOWS.md.
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{41744A5A-201C-4C45-9B79-DF3C1B32DAAA}
AppName=Crossing Count
AppVersion={#AppVersion}
AppPublisher=iTOi Solutions
AppCopyright=Copyright (C) 2026 Parham Forozan. All rights reserved.
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
; what Windows shows about setup.exe itself (properties, and the "unknown publisher" prompt)
VersionInfoVersion={#AppVersion}
VersionInfoCompany=iTOi Solutions
VersionInfoProductName=Crossing Count
VersionInfoDescription=Crossing Count setup
VersionInfoCopyright=Copyright (C) 2026 Parham Forozan. All rights reserved.
#ifdef Sign
SignTool=mysign
SignedUninstaller=yes
#endif

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
; An update from the app's own window installs silently: then open the app again.
Filename: "{app}\CrossingCount.exe"; Flags: nowait runasoriginaluser; Check: WizardSilent
