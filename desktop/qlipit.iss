; The QlipIt Studio installer: one file, no administrator, and it leaves your videos alone unless you say otherwise.
; Built by desktop\build.ps1 with Inno Setup (winget install JRSoftware.InnoSetup).

#define MyAppName "QlipIt"
#define MyAppFullName "QlipIt Studio"
#define MyAppVersion "1.0.0"
#define MyAppExeName "QlipIt.exe"

[Setup]
AppId={{6A1C3D2E-7F84-4B0E-9C51-0B2D9E7A5C31}
AppName={#MyAppFullName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppName}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
DisableReadyPage=yes
UsePreviousAppDir=yes
UsePreviousGroup=yes
UsePreviousTasks=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=dist\installer
OutputBaseFilename=QlipIt-Setup-{#MyAppVersion}
SetupIconFile=qlipit.ico
UninstallDisplayIcon={app}\_internal\desktop\qlipit.ico
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
Name: "{userdesktop}\{#MyAppFullName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\_internal\desktop\qlipit.ico"; Tasks: desktopicon; AppUserModelID: "QlipIt.Studio"
Name: "{userprograms}\{#MyAppFullName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\_internal\desktop\qlipit.ico"; AppUserModelID: "QlipIt.Studio"
Name: "{userprograms}\Uninstall {#MyAppFullName}"; Filename: "{uninstallexe}"

[Registry]
Root: HKCU; Subkey: "Software\QlipIt"; ValueType: dword; ValueName: "DeleteData"; ValueData: "0"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\QlipIt"; ValueType: dword; ValueName: "DeleteData"; ValueData: "1"; Tasks: deletedata

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppFullName}}"; Flags: nowait postinstall skipifsilent

[InstallDelete]
Type: files; Name: "{userdesktop}\PeakCut.lnk"
Type: files; Name: "{userprograms}\PeakCut.lnk"
Type: files; Name: "{userdesktop}\QlipIt.lnk"

[Code]
procedure SHChangeNotify(wEventId: Integer; uFlags: Cardinal; dwItem1, dwItem2: Cardinal);
  external 'SHChangeNotify@shell32.dll stdcall';

function IsUpgrade: Boolean;
begin
  Result := RegKeyExists(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{6A1C3D2E-7F84-4B0E-9C51-0B2D9E7A5C31}_is1')
    or FileExists(ExpandConstant('{localappdata}\Programs\QlipIt\QlipIt.exe'));
end;

procedure InitializeWizard;
begin
  if IsUpgrade then begin
    WizardForm.Caption := 'Update QlipIt Studio';
    WizardForm.WelcomeLabel1.Caption := 'Update QlipIt Studio';
    WizardForm.WelcomeLabel2.Caption := 'An existing installation of QlipIt Studio was detected.' + #13#10#13#10 +
      'Setup will now update QlipIt Studio to version {#MyAppVersion}.' + #13#10#13#10 +
      'Your projects, settings, clips, and database are safely preserved.' + #13#10#13#10 +
      'Click Update to proceed.';
    WizardForm.NextButton.Caption := 'Update';
  end;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if IsUpgrade then begin
    // For a seamless 1-click update experience, skip folder selection, tasks, and ready pages
    if (PageID = wpSelectDir) or (PageID = wpSelectTasks) or (PageID = wpReady) then
      Result := True;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then begin
    DeleteFile(ExpandConstant('{userdesktop}\PeakCut.lnk'));
    DeleteFile(ExpandConstant('{userprograms}\PeakCut.lnk'));
  end;
  if CurStep = ssPostInstall then begin
    // Force Windows Explorer shell to reload icon cache immediately
    SHChangeNotify($08000000, 0, 0, 0); // SHCNE_ASSOCCHANGED
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Delete: Cardinal;
begin
  if CurUninstallStep = usPostUninstall then begin
    if RegQueryDWordValue(HKCU, 'Software\QlipIt', 'DeleteData', Delete) and (Delete = 1) then begin
      DelTree(ExpandConstant('{localappdata}\QlipIt'), True, True, True);
    end;
    SHChangeNotify($08000000, 0, 0, 0);
  end;
end;
