#define AppName "Forge"
#define AppVersion "5.1.6"
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
DefaultDirName={localappdata}\Programs\Forge5
UsePreviousAppDir=no
DisableDirPage=yes
DefaultGroupName=Forge
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=release
OutputBaseFilename=Forge-5.1.6-Setup
SetupIconFile=assets\forge.ico
UninstallDisplayIcon={app}\Forge.exe
LicenseFile=LICENSE
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=no
RestartApplications=no
SetupMutex=Forge.InstallerBootstrap
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

[Code]
// Setup never deletes the previous tree or closes unrelated processes. A clean
// destination is required by the publisher proof, including the first upgrade
// from previews which did not record a trusted file inventory.
const
  ReparsePoint = $400;
  DirectoryAttribute = $10;
  InvalidAttributes = $FFFFFFFF;
  DeleteAccess = $10000;
  ReadAttributesAccess = $80;
  ShareRead = $1;
  ShareDelete = $4;
  OpenExisting = 3;
  OpenReparsePoint = $200000;
  BackupSemantics = $2000000;
  MaxGuardEntries = 20000;

var
  AppRoot, BackupRoot, PreviousRoot: String;
  PreviousMoved, InstallCompleted, BootstrapPrepared, KeepPreviousGuards: Boolean;
  GuardEntries: Integer;
  GuardHandles: array of THandle;
  DestinationHandle: THandle;

function FileAttributes(const Filename: String): DWORD;
  external 'GetFileAttributesW@kernel32.dll stdcall';
function OpenGuard(const Filename: String; Access, Share, Security, Creation,
  Flags: DWORD; Template: THandle): THandle;
  external 'CreateFileW@kernel32.dll stdcall';
function CloseGuard(Handle: THandle): BOOL;
  external 'CloseHandle@kernel32.dll stdcall';
function CurrentProcessId: DWORD;
  external 'GetCurrentProcessId@kernel32.dll stdcall';
function LongPathName(const Filename, Buffer: String; Capacity: DWORD): DWORD;
  external 'GetLongPathNameW@kernel32.dll stdcall';

function BootstrapBase: String;
begin
  Result := ExpandConstant('{localappdata}');
end;

function CanonicalPath(const Value: String): String;
begin
  Result := RemoveBackslashUnlessRoot(ExpandFileName(Value));
end;

function ProcessImagePath(const Value: String; var FullyResolved: Boolean): String;
var
  Buffer, Current, Parent, Suffix: String;
  Count: DWORD;
  Depth: Integer;
begin
  Result := CanonicalPath(Value);
  FullyResolved := False;
  if (Copy(Result, 1, 4) = '\\?\') and (Length(Result) >= 7) and
    (Result[6] = ':') then Result := Copy(Result, 5, Length(Result) - 4);
  // WMI can retain a loaded spelling after the original app folder is moved.
  // Resolve the nearest existing ancestor too; never guess an 8.3 alias.
  Current := Result;
  Suffix := '';
  SetLength(Buffer, 32768);
  for Depth := 0 to 32 do begin
    Count := LongPathName(Current, Buffer, Length(Buffer));
    if (Count > 0) and (Count < DWORD(Length(Buffer))) then begin
      Result := CanonicalPath(Copy(Buffer, 1, Count) + Suffix);
      FullyResolved := Depth = 0;
      Exit;
    end;
    Parent := ExtractFileDir(Current);
    if (Parent = '') or (CompareText(Parent, Current) = 0) then Exit;
    Suffix := '\' + ExtractFileName(Current) + Suffix;
    Current := Parent;
  end;
end;

function HostAliasNames(const Folder: String): String;
var
  FirstName, SecondName: String;
begin
  // Ask Windows for the actual alias, never infer its numbering or spelling.
  FirstName := ExtractFileName(GetShortName(AddBackslash(Folder) + 'Forge.exe'));
  SecondName := ExtractFileName(GetShortName(AddBackslash(Folder) + 'ForgeBrowserHost.exe'));
  Result := Lowercase(FirstName + '|' + SecondName + '|');
end;

function PlainAncestors(const Value: String): Boolean;
var
  Current, Parent: String;
  Attributes: DWORD;
begin
  Result := False;
  Current := CanonicalPath(Value);
  // Only absolute ordinary drive paths can be installation/backup targets.
  if (Length(Current) < 3) or (Current[2] <> ':') or (Current[3] <> '\') then
    Exit;
  repeat
    Attributes := FileAttributes(Current);
    if Attributes = InvalidAttributes then begin
      if (DLLGetLastError <> 2) and (DLLGetLastError <> 3) then Exit;
    end else if (Attributes and ReparsePoint) <> 0 then Exit;
    Parent := ExtractFileDir(Current);
    if CompareText(Parent, Current) = 0 then Break;
    Current := Parent;
  until Current = '';
  Result := True;
end;

procedure ReleaseTreeGuards;
var
  Index: Integer;
begin
  for Index := GetArrayLength(GuardHandles) - 1 downto 0 do
    CloseGuard(GuardHandles[Index]);
  SetArrayLength(GuardHandles, 0);
end;

function HoldTree(const Folder: String; Depth: Integer): Boolean;
var
  Found: TFindRec;
  Filename: String;
  Handle: THandle;
  Count: Integer;
begin
  Result := False;
  if (Depth > 32) or not PlainAncestors(Folder) then Exit;
  // Keep each directory ordinary and stationary while its children are checked.
  // These handles are released only for the folder move, then acquired again.
  Handle := OpenGuard(Folder, ReadAttributesAccess, ShareRead, 0,
    OpenExisting, BackupSemantics or OpenReparsePoint, 0);
  if Handle = THandle(-1) then Exit;
  Count := GetArrayLength(GuardHandles);
  SetArrayLength(GuardHandles, Count + 1);
  GuardHandles[Count] := Handle;
  if not PlainAncestors(Folder) then Exit;
  if not FindFirst(AddBackslash(Folder) + '*', Found) then Exit;
  try
    repeat
      if (Found.Name <> '.') and (Found.Name <> '..') then begin
        GuardEntries := GuardEntries + 1;
        if (GuardEntries > MaxGuardEntries) or
          ((Found.Attributes and ReparsePoint) <> 0) then Exit;
        Filename := AddBackslash(Folder) + Found.Name;
        if (Found.Attributes and DirectoryAttribute) <> 0 then begin
          if not HoldTree(Filename, Depth + 1) then Exit;
        end else begin
          // Metadata-only DELETE access detects incompatible open-file locks.
          // Deny new readers/writers while held; process images are also checked
          // separately. No file is read, changed or deleted by this guard.
          if not PlainAncestors(Filename) then Exit;
          Handle := OpenGuard(Filename, DeleteAccess, ShareDelete, 0,
            OpenExisting, OpenReparsePoint, 0);
          if Handle = THandle(-1) then Exit;
          Count := GetArrayLength(GuardHandles);
          SetArrayLength(GuardHandles, Count + 1);
          GuardHandles[Count] := Handle;
          if (FileAttributes(Filename) and ReparsePoint) <> 0 then Exit;
        end;
      end;
    until not FindNext(Found);
    Result := True;
  finally
    FindClose(Found);
  end;
end;

function UnusedBackup(const Kind: String): String;
var
  Prefix, Candidate: String;
  Index: Integer;
begin
  Result := '';
  Prefix := AddBackslash(BackupRoot) + 'Forge5-' + Kind + '-' +
    GetDateTimeString('yyyymmdd-hhnnss', '-', ':') + '-' + IntToStr(Integer(CurrentProcessId));
  for Index := 0 to 255 do begin
    Candidate := Prefix + '-' + IntToStr(Index);
    if FileAttributes(Candidate) = InvalidAttributes then begin
      if (DLLGetLastError = 2) or (DLLGetLastError = 3) then begin
        Result := Candidate;
        Exit;
      end;
    end;
  end;
end;

function RootsAreSafe: Boolean;
begin
  Result := (CompareText(AppRoot, CanonicalPath(BootstrapBase + '\Programs\Forge5')) = 0) and
    (CompareText(BackupRoot, CanonicalPath(BootstrapBase + '\ForgeBackups')) = 0) and
    PlainAncestors(AppRoot) and PlainAncestors(BackupRoot);
end;

function PreviousHostsStopped: Boolean;
var
  Locator, Services, Processes, Item, Image, Name: Variant;
  Index: Integer;
  Prefix, PreviousPrefix, KnownNames, ResolvedImage: String;
  FullyResolved, KnownHost: Boolean;
begin
  Result := False;
  try
    // Query names/image paths only, never command lines, accounts or content.
    // Include renamed workers and packaged tool runtimes, not just Forge hosts.
    // Exact folder prefixes exclude unrelated apps and Forge installations.
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Services := Locator.ConnectServer('', 'root\CIMV2');
    KnownNames := '|forge.exe|forgebrowserhost.exe|' + HostAliasNames(AppRoot);
    if PreviousRoot <> '' then KnownNames := KnownNames + HostAliasNames(PreviousRoot);
    Processes := Services.ExecQuery('SELECT Name, ExecutablePath FROM Win32_Process', 'WQL', 0);
    if Processes.Count > 4096 then Exit;
    Prefix := AddBackslash(AppRoot);
    PreviousPrefix := AddBackslash(PreviousRoot);
    for Index := 0 to Processes.Count - 1 do begin
      Item := Processes.ItemIndex(Index);
      Name := Item.Name;
      KnownHost := Pos('|' + Lowercase(String(Name)) + '|', KnownNames) > 0;
      Image := Item.ExecutablePath;
      // A protected/elevated matching image may hide its path. Its readiness
      // cannot be established; never treat an unreadable image as stopped.
      if VarIsNull(Image) or VarIsEmpty(Image) then begin
        if KnownHost then Exit;
        Continue;
      end;
      if String(Image) = '' then begin
        if KnownHost then Exit;
        Continue;
      end;
      ResolvedImage := ProcessImagePath(String(Image), FullyResolved);
      if KnownHost and not FullyResolved then Exit;
      if CompareText(Copy(ResolvedImage, 1, Length(Prefix)), Prefix) = 0 then Exit;
      if (PreviousRoot <> '') and
        (CompareText(Copy(ResolvedImage, 1, Length(PreviousPrefix)), PreviousPrefix) = 0) then Exit;
    end;
    Result := True;
  except
    // Unknown readiness is a safe refusal, never permission to move a live app.
    Result := False;
  end;
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo,
  MemoTypeInfo, MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
begin
  Result := MemoDirInfo + NewLine + NewLine + MemoGroupInfo;
  if MemoTasksInfo <> '' then Result := Result + NewLine + NewLine + MemoTasksInfo;
  Result := Result + NewLine + NewLine +
    'Close Forge before continuing. Setup preserves the complete previous app folder in ForgeBackups under Local AppData and installs into a clean folder.' +
    NewLine + 'Saved state and model folders outside the app folder stay in place. Every file inside the previous app folder is preserved in its backup. A cancelled or failed installation restores that folder when it is safe to do so.';
end;

procedure ManualRecovery(const Message: String);
begin
  Log(Message);
  SuppressibleMsgBox(Message + #13#10#13#10 +
    'Your previous app remains in ForgeBackups under Local AppData. Close Forge and any app-folder files before restoring that complete folder to Programs\Forge5. Keep the retained folders until recovery is complete.',
    mbError, MB_OK, IDOK);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Attributes: DWORD;
begin
  Result := '';
  AppRoot := CanonicalPath(ExpandConstant('{app}'));
  BackupRoot := CanonicalPath(BootstrapBase + '\ForgeBackups');
  if not RootsAreSafe then begin
    Result := 'Choose the default Forge installation location without linked folders. Use the portable download for a custom location.';
    Exit;
  end;
  if BootstrapPrepared then Exit;
  if PreviousMoved then begin
    Result := 'The previous folder is preserved. Close this setup to restore it before trying again.';
    Exit;
  end;
  Attributes := FileAttributes(AppRoot);
  if Attributes <> InvalidAttributes then begin
    if (Attributes and DirectoryAttribute) = 0 then begin
      Result := 'The Forge installation location is not a folder. Nothing was changed.';
      Exit;
    end;
    GuardEntries := 0;
    try
      if not HoldTree(AppRoot, 0) then begin
        Result := 'Close Forge and any files open in its app folder, then try again. Linked or unusually large app folders require a manual clean installation. Nothing was changed.';
        Exit;
      end;
      if not PreviousHostsStopped then begin
        Result := 'Close this Forge installation before updating. Setup could not confirm that its app and browser host have stopped; nothing was changed.';
        Exit;
      end;
      if not RootsAreSafe then begin
        Result := 'A safe private backup folder could not be prepared. Nothing was changed.';
        Exit;
      end;
      if not ForceDirectories(BackupRoot) then begin
        Result := 'A safe private backup folder could not be prepared. Nothing was changed.';
        Exit;
      end;
      if not RootsAreSafe then begin
        Result := 'A safe private backup folder could not be prepared. Nothing was changed.';
        Exit;
      end;
      PreviousRoot := UnusedBackup('previous');
      // Windows requires child metadata handles to be closed for this rename.
      ReleaseTreeGuards;
      if (PreviousRoot = '') or not PlainAncestors(PreviousRoot) or
        not RootsAreSafe then begin
        Result := 'The previous Forge app folder could not be preserved. Close Forge and try again; nothing was overwritten.';
        Exit;
      end;
      if not RenameFile(AppRoot, PreviousRoot) then begin
        Result := 'The previous Forge app folder could not be preserved. Close Forge and try again; nothing was overwritten.';
        Exit;
      end;
      PreviousMoved := True;
      // A file/process can open between closing child handles and NTFS rename.
      // Re-admit the moved tree before extracting, and retain all guards until
      // setup exits. A late opener refuses installation and restores the old
      // folder; a later launch cannot read the guarded backup files.
      GuardEntries := 0;
      if not HoldTree(PreviousRoot, 0) or not PreviousHostsStopped then begin
        Result := 'Forge or an app-folder file opened during the update. Close it, then close this setup to restore the previous folder before trying again.';
        Exit;
      end;
      KeepPreviousGuards := True;
      Log('The previous Forge app folder was preserved in ForgeBackups.');
    finally
      if not KeepPreviousGuards then ReleaseTreeGuards;
    end;
  end;
  // Create the fresh root ourselves and pin it against rename/reparse changes
  // while Inno extracts. Children remain writable; the root is metadata-only.
  if not RootsAreSafe then begin
    Result := 'The clean Forge app folder could not be created. The previous folder remains preserved.';
    Exit;
  end;
  if not ForceDirectories(ExtractFileDir(AppRoot)) then begin
    Result := 'The clean Forge app folder could not be created. The previous folder remains preserved.';
    Exit;
  end;
  if not RootsAreSafe then begin
    Result := 'The clean Forge app folder could not be created. The previous folder remains preserved.';
    Exit;
  end;
  if not CreateDir(AppRoot) then begin
    Result := 'The clean Forge app folder could not be created. The previous folder remains preserved.';
    Exit;
  end;
  if not RootsAreSafe then begin
    Result := 'The clean Forge app folder could not be created. The previous folder remains preserved.';
    Exit;
  end;
  DestinationHandle := OpenGuard(AppRoot, ReadAttributesAccess, ShareRead, 0,
    OpenExisting, BackupSemantics or OpenReparsePoint, 0);
  if (DestinationHandle = THandle(-1)) or not RootsAreSafe then begin
    Result := 'The clean Forge app folder could not be protected. The previous folder remains preserved.';
    Exit;
  end;
  BootstrapPrepared := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssDone then InstallCompleted := True;
end;

procedure DeinitializeSetup;
var
  FailedRoot: String;
begin
  if (DestinationHandle <> 0) and (DestinationHandle <> THandle(-1)) then
    CloseGuard(DestinationHandle);
  ReleaseTreeGuards;
  if not PreviousMoved or InstallCompleted then Exit;
  // Inno's own failed-install rollback runs before this termination callback.
  // Preserve any partial new tree rather than deleting it, then restore the
  // whole old folder. A changed/locked path aborts recovery with both preserved.
  if not RootsAreSafe or not PlainAncestors(PreviousRoot) then begin
    ManualRecovery('Forge recovery requires manual inspection of the retained backup.');
    Exit;
  end;
  if not PreviousHostsStopped then begin
    ManualRecovery('Forge recovery is waiting for an app or browser host to stop; existing folders remain preserved.');
    Exit;
  end;
  try
    if DirExists(AppRoot) then begin
      GuardEntries := 0;
      if not HoldTree(AppRoot, 0) or not PreviousHostsStopped then begin
        ManualRecovery('The partial installation is locked, active or redirected; the previous backup remains preserved.');
        Exit;
      end;
      FailedRoot := UnusedBackup('failed');
      ReleaseTreeGuards;
      if (FailedRoot = '') or not PlainAncestors(FailedRoot) or
        not RootsAreSafe then begin
        ManualRecovery('The partial installation could not be retained; the previous backup remains preserved.');
        Exit;
      end;
      if not RenameFile(AppRoot, FailedRoot) then begin
        ManualRecovery('The partial installation could not be retained; the previous backup remains preserved.');
        Exit;
      end;
    end;
  finally
    ReleaseTreeGuards;
  end;
  if not RootsAreSafe or not PlainAncestors(PreviousRoot) or
    not PreviousHostsStopped or
    DirExists(AppRoot) or FileExists(AppRoot) then begin
    ManualRecovery('Forge recovery requires manual restoration from the retained backup.');
    Exit;
  end;
  if RenameFile(PreviousRoot, AppRoot) then
    Log('The complete previous Forge app folder was restored after installation failed.')
  else
    ManualRecovery('Forge recovery requires manual restoration from the retained backup.');
end;
