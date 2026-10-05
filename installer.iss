#define AppName "Forge"
#define AppVersion "4.2.1"
#ifndef StageDir
#define StageDir "dist\Forge"
#endif
[Setup]
AppId={{0DC7BBA4-F3C0-4E07-918A-7BCA8B387400}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Forge contributors
AppPublisherURL=https://github.com/Bh0ps/forge-local-ai
AppSupportURL=https://github.com/Bh0ps/forge-local-ai/issues
DefaultDirName={localappdata}\Programs\Forge4
DefaultGroupName=Forge
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=release
OutputBaseFilename=Forge-4.2.1-Setup
SetupIconFile=assets\forge.ico
UninstallDisplayIcon={app}\Forge.exe
LicenseFile=LICENSE
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
DisableProgramGroupPage=yes
[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\Forge"; Filename: "{app}\Forge.exe"
Name: "{autodesktop}\Forge"; Filename: "{app}\Forge.exe"; Tasks: desktopicon
[Run]
Filename: "{app}\Forge.exe"; Description: "Open Forge"; Flags: nowait postinstall skipifsilent
; Saved .forge state and the previous Sidekick installation are never removed.
