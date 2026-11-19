; The QlipIt installer: one file, no administrator, and it leaves your videos alone unless you say otherwise.
; Built by desktop\build.ps1 with Inno Setup (winget install JRSoftware.InnoSetup).

#define MyAppName "QlipIt"
#define MyAppVersion "1.0.0"
#define MyAppExeName "QlipIt.exe"

[Setup]
AppId={{6A1C3D2E-7F84-4B0E-9C51-0B2D9E7A5C31}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppName}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=dist\installer
OutputBaseFilename=QlipIt-Setup-{#MyAppVersion}
SetupIconFile=qlipit.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
AppMutex=QlipItAppMutex
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "deletedata"; Description: "Also delete my videos, clips and settings"; GroupDescription: "When QlipIt is removed:"; Flags: unchecked

[Files]
Source: "dist\QlipIt\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon; AppUserModelID: "QlipIt.Studio"
Name: "{userprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; AppUserModelID: "QlipIt.Studio"
Name: "{userprograms}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"

[Registry]
; Remembered now, because at uninstall time there is nobody left to ask.
Root: HKCU; Subkey: "Software\QlipIt"; ValueType: dword; ValueName: "DeleteData"; ValueData: "0"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\QlipIt"; ValueType: dword; ValueName: "DeleteData"; ValueData: "1"; Tasks: deletedata

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Delete: Cardinal;
begin
  if CurUninstallStep = usPostUninstall then
    if RegQueryDWordValue(HKCU, 'Software\QlipIt', 'DeleteData', Delete) and (Delete = 1) then begin
      DelTree(ExpandConstant('{localappdata}\QlipIt'), True, True, True);
    end;
end;
