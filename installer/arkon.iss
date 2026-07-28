; Arkon Launcher installer.
;
; One artifact, two outcomes. The first wizard page asks whether to install
; properly (Program Files, Start Menu, uninstaller) or unpack a portable copy
; that keeps its settings beside the executable.
;
; PrivilegesRequired=lowest matters: Inno decides its privilege level before any
; custom page runs, so requiring admin up front would throw a UAC prompt at
; someone who only wanted the portable copy. With "lowest" plus the dialog
; override, portable users are never prompted, and choosing Install triggers
; elevation only when the chosen directory needs it.

#define AppName "Arkon Launcher"
#define AppExe "Arkon Launcher.exe"
#define AppPublisher "Arkon"
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{8F3C1A54-6E2B-4D77-9B0E-2A7C5D9E4B11}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=no
OutputDir=..\dist
OutputBaseFilename=ArkonLauncherSetup
SetupIconFile=app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[InstallDelete]
; Clear the PyInstaller payload before laying down the new one. Upgrading in
; place would otherwise leave orphaned DLLs and modules from the previous build
; whenever Python or PySide6 changes shape, and a stale .pyd that still loads is
; a genuinely confusing failure to debug.
;
; Scoped to _internal deliberately: it is pure app payload. A portable copy's
; settings live in {app}\data and its marker is {app}\portable.txt - both are
; siblings of _internal, so neither is touched.
Type: filesandordirs; Name: "{app}\_internal"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked; Check: not IsPortable

[Files]
Source: "..\dist\Arkon Launcher\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; The marker that switches the app to keeping its settings beside the exe.
Source: "portable.txt"; DestDir: "{app}"; Flags: ignoreversion; Check: IsPortable

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; Check: not IsPortable
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"; Check: not IsPortable
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; runasoriginaluser: never hand the app an elevated token, or it would write
; files the user cannot later modify.
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent runasoriginaluser

[Code]
var
  ModePage: TInputOptionWizardPage;
  PreviousVersion: String;

{ Reads the version recorded by a previous install, if there is one. HKA follows
  whichever hive this run is using, so a per-user install is found by a per-user
  run and a per-machine one by an elevated run. A portable copy deliberately
  writes no registry key, so it is never detected here - that is the point of it. }
function GetPreviousVersion(): String;
var
  Value: String;
begin
  Result := '';
  if RegQueryStringValue(HKA,
      'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{#SetupSetting("AppId")}_is1',
      'DisplayVersion', Value) then
    Result := Value;
end;

function IsUpgrade(): Boolean;
begin
  Result := PreviousVersion <> '';
end;

{ Refuse to quietly replace a newer build with an older one. Silent runs are
  left alone so scripted deployments still behave predictably. }
function InitializeSetup(): Boolean;
var
  Installed, Incoming: Int64;
begin
  Result := True;
  PreviousVersion := GetPreviousVersion();

  if (PreviousVersion <> '') and not WizardSilent() then
  begin
    if StrToVersion(PreviousVersion, Installed) and
       StrToVersion('{#AppVersion}', Incoming) then
    begin
      if ComparePackedVersion(Installed, Incoming) > 0 then
        Result := SuppressibleMsgBox(
          'Version ' + PreviousVersion + ' is already installed, which is newer than ' +
          'the {#AppVersion} you are about to install.' + #13#10#13#10 +
          'Install the older version anyway?',
          mbConfirmation, MB_YESNO, IDYES) = IDYES;
    end;
  end;
end;

procedure InitializeWizard;
var
  ModeIntro: String;
begin
  if IsUpgrade() then
    ModeIntro := 'Arkon Launcher ' + PreviousVersion + ' is already installed and will ' +
                 'be updated in place. Your settings and world backups are kept.'
  else
    ModeIntro := 'You can change your mind later by running this installer again.';

  ModePage := CreateInputOptionPage(
    wpWelcome,
    'How would you like to use Arkon Launcher?',
    'Choose an installation type.',
    ModeIntro,
    True,   { exclusive - radio buttons }
    False);

  ModePage.Add(
    'Install on this PC' + #13#10 +
    '     Adds a Start Menu entry and an uninstaller. Settings are stored in your' + #13#10 +
    '     user profile. Recommended.');
  ModePage.Add(
    'Portable' + #13#10 +
    '     Unpacks to a folder you choose and keeps everything inside it, so it can' + #13#10 +
    '     live on a USB stick. No Start Menu entry and no uninstaller.');

  ModePage.SelectedValueIndex := 0;
end;

function IsPortable: Boolean;
begin
  Result := Assigned(ModePage) and (ModePage.SelectedValueIndex = 1);
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  { On an upgrade the location is already settled and Inno reuses it, so asking
    again just invites someone to install a second copy somewhere else. }
  Result := (PageID = wpSelectDir) and IsUpgrade() and not IsPortable();
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpSelectDir then
  begin
    if IsPortable then
    begin
      WizardForm.DirEdit.Text := ExpandConstant('{%USERPROFILE}\Arkon Launcher');
      WizardForm.SelectDirLabel.Caption :=
        'Choose where to unpack the portable copy. Pick somewhere you can write to, ' +
        'such as your Desktop, Documents, or a USB drive.';
    end
    else
      WizardForm.SelectDirLabel.Caption :=
        'Choose where to install Arkon Launcher.';
  end;
end;

{ Portable copies leave no uninstaller and no registry footprint. }
function NeedsUninstallRegKey: Boolean;
begin
  Result := not IsPortable;
end;

[UninstallDelete]
; Deliberately narrow. The uninstaller removes only what it installed.
; It must never touch <instance>\.arkonlauncher, which holds world backups,
; and it leaves %LOCALAPPDATA%\Arkon Launcher alone so settings survive an
; upgrade performed as uninstall-then-install.
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\portable.txt"
