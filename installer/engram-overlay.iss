; ============================================================
;  Engram Overlay — Inno Setup 통짜 installer (Model B / frozen)
;
;  frozen 번들(dist\engram-overlay\*)을 설치하고, GUI 로 수집한 옵션을
;  configure.ps1 에 넘겨 config/MCP/환경변수/바로가기를 구성한다.
;  사용자 머신에 conda/python 불필요.
;
;  빌드: build-installer.ps1 (ISCC 컴파일)
; ============================================================

#define AppName "AMBER (ENGRAM)"
#ifndef AppVersion
  #define AppVersion "0.0.0.0"
#endif
; 검증된 외부 renderer 버전. build-installer.ps1 이 installer/external-overlay.pin
; 에서 읽어 넘긴다 — 이 파일에 버전을 적어두면 릴리스마다 썩는다.
#ifndef ExternalOverlayVersion
  #define ExternalOverlayVersion "unpinned"
#endif
#define AppPublisher "DRTECH"
#define AppExeName "engram-overlay.exe"
#ifndef BuildCompression
  #define BuildCompression "zip"
#endif
#ifndef BuildSolidCompression
  #define BuildSolidCompression "no"
#endif
#ifndef BuildOutputSuffix
  #define BuildOutputSuffix "-dev"
#endif

[Setup]
AppId={{A7E3C1D2-9B4F-4E6A-8C11-5D2F1A0B3E64}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\EngramOverlay
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
; drt-notebookLM 처럼 setup.exe 를 프로젝트 루트에 생성 (.iss 기준 상위 = repo 루트)
OutputDir=..
OutputBaseFilename=AMBER_{#AppVersion}{#BuildOutputSuffix}_x64-setup
Compression={#BuildCompression}
SolidCompression={#BuildSolidCompression}
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\dist\engram-overlay\{#AppExeName}

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "autostart"; Description: "Windows 시작 시 자동 실행 (Startup 등록)"; Flags: unchecked

[Types]
Name: "custom"; Description: "내장 볼따구 + 선택 구성요소"; Flags: iscustom

[Components]
Name: "native"; Description: "Engram 및 내장 볼따구 (기본)"; Types: custom; Flags: fixed
Name: "external"; Description: "외부 오버레이 v{#ExternalOverlayVersion} — 설치만 하며 현재 캐릭터를 변경하지 않음"
#include "external-components\tree.iss"
Name: "sdk"; Description: "개발자 SDK — 안내와 예제 (오버레이 선택과 독립)"

[Files]
#include "external-components\files.iss"
Source: "component-status.ps1"; Flags: dontcopy
Source: "external-components.ps1"; Flags: dontcopy
; install 전 실행 중인 동일 설치본을 종료하기 위한 공통 helper (PrepareToInstall에서 추출)
Source: "stop-engram-processes.ps1"; Flags: dontcopy
; frozen 번들 전체 (dist\engram-overlay\*)
Source: "..\dist\engram-overlay\*"; DestDir: "{app}\dist\engram-overlay"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\config\overlay.yaml"; DestDir: "{app}\dist\engram-overlay\config"; Flags: ignoreversion
Source: "..\config\config.yaml"; DestDir: "{app}\dist\engram-overlay\config"; Flags: ignoreversion
Source: "..\config\clients\copilot.md"; DestDir: "{app}\config\clients"; Flags: ignoreversion
; 공급자별 subagent 정의. 형식과 경로가 공급자마다 달라 한 파일을 세 곳에 복사하면
; 안 된다. 이전에는 소스 설치 경로(07_shims)만 이걸 배치해서, 설치본 사용자는
; planner/coder/servant 를 아예 받지 못했다.
Source: "..\config\agents\*"; DestDir: "{app}\config\agents"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "deploy_agent_definitions.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "templates\*"; DestDir: "{app}\installer\templates"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\.github\skills\engram\SKILL.md"; DestDir: "{app}\.github\skills\engram"; Flags: ignoreversion
Source: "..\.github\skills\orchestrate\SKILL.md"; DestDir: "{app}\.github\skills\orchestrate"; Flags: ignoreversion
Source: "..\.github\skills\engram-new-session\SKILL.md"; DestDir: "{app}\.github\skills\engram-new-session"; Flags: ignoreversion
Source: "..\.github\skills\engram-task-workflow\SKILL.md"; DestDir: "{app}\.github\skills\engram-task-workflow"; Flags: ignoreversion
Source: "..\.github\skills\engram-wiki-workflow\SKILL.md"; DestDir: "{app}\.github\skills\engram-wiki-workflow"; Flags: ignoreversion
Source: "..\.github\skills\engram-close-session\SKILL.md"; DestDir: "{app}\.github\skills\engram-close-session"; Flags: ignoreversion
; 설치타임 구성기
Source: "configure.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "joint-startup.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "external-components.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "external-bundle.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "external-components.pin"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "external-wheel.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "stop-engram-processes.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
; configure.ps1 이 검증된 외부 renderer 버전을 읽는다. 같이 배포되지 않으면 unpinned 로 떨어진다.
Source: "external-overlay.pin"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "external-overlay.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion

[UninstallRun]
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\installer\configure.ps1"" -Uninstall -InstallDir ""{app}"""; \
  Flags: runhidden; RunOnceId: "EngramConfigureUninstall"

[Code]
var
  DirPage: TInputDirWizardPage;
  ProviderPage: TInputOptionWizardPage;
  MakeYourOwnCheckBox: TNewCheckBox;
  QueryPage: TInputQueryWizardPage;
  ExistingExternalComponents: TArrayOfString;
  ExternalInventoryAvailable: Boolean;
  SelectAllExternalButton: TNewButton;
  SelectNoneExternalButton: TNewButton;
  ExternalInventoryLabel: TNewStaticText;

function ExistingExternalComponent(Id: String): Boolean;
var I: Integer;
begin
  Result := False;
  for I := 0 to GetArrayLength(ExistingExternalComponents) - 1 do
    if ExistingExternalComponents[I] = Id then Result := True;
end;

procedure ExtractComponentArtifact(Name: String; RelativePath: String);
begin
  ExtractTemporaryFile(Name);
  if not FileCopy(ExpandConstant('{tmp}\') + Name,
    ExpandConstant('{tmp}\component-bundle\') + RelativePath, False) then
    RaiseException('Unable to stage selected external component.');
end;

#include "external-components\code.iss"

procedure SelectAllExternalClick(Sender: TObject);
var Selection: String;
begin
  Selection := 'native,' + AllExternalComponentNames();
  if WizardIsComponentSelected('sdk') then Selection := Selection + ',sdk';
  WizardSelectComponents(Selection);
end;

procedure SelectNoneExternalClick(Sender: TObject);
var Selection: String;
begin
  Selection := 'native';
  if WizardIsComponentSelected('sdk') then Selection := Selection + ',sdk';
  WizardSelectComponents(Selection);
end;

function DefaultDbDir(): String;
begin
  if DirExists('D:\') then
    Result := 'D:\intel_engram'
  else
    Result := ExpandConstant('{sd}') + '\intel_engram';
end;

function DecodeYamlScalar(Value: String): String;
var
  L: Integer;
begin
  Result := Trim(Value);
  L := Length(Result);
  if L >= 2 then
  begin
    if ((Result[1] = '"') and (Result[L] = '"')) or
       ((Result[1] = '''') and (Result[L] = '''')) then
      Result := Copy(Result, 2, L - 2);
  end;
end;

procedure LoadExistingUserPaths(var DbDir: String; var WorkDir: String);
var
  ConfigPath: String;
  Lines: TArrayOfString;
  I: Integer;
  Line: String;
  Clean: String;
  InDb: Boolean;
begin
  ConfigPath := ExpandConstant('{%USERPROFILE}\.engram\user.config.yaml');
  if not LoadStringsFromFile(ConfigPath, Lines) then
    Exit;

  InDb := False;
  for I := 0 to GetArrayLength(Lines) - 1 do
  begin
    Line := Lines[I];
    Clean := Trim(Line);
    if (Clean = '') or (Copy(Clean, 1, 1) = '#') then
      Continue;

    if Line = Clean then
    begin
      InDb := Clean = 'db:';
      if Pos('workdir:', Clean) = 1 then
      begin
        Line := DecodeYamlScalar(Copy(Clean, Length('workdir:') + 1, MaxInt));
        if Line <> '' then
          WorkDir := Line;
      end;
    end
    else if InDb and (Pos('root_dir:', Clean) = 1) then
    begin
      Line := DecodeYamlScalar(Copy(Clean, Length('root_dir:') + 1, MaxInt));
      if Line <> '' then
        DbDir := Line;
    end;
  end;
end;

procedure InitializeWizard();
var
  InitialDbDir: String;
  InitialWorkDir: String;
  StatusCode: Integer;
  I: Integer;
  InventoryText: String;
begin
  ExtractTemporaryFile('external-components.ps1');
  ExtractTemporaryFile('component-status.ps1');
  if Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + ExpandConstant('{tmp}\component-status.ps1') +
    '" -OutputPath "' + ExpandConstant('{tmp}\component-status.txt') + '"', '', SW_HIDE, ewWaitUntilTerminated, StatusCode) then begin
    ExternalInventoryAvailable := StatusCode = 0;
    if ExternalInventoryAvailable then LoadStringsFromFile(ExpandConstant('{tmp}\component-status.txt'), ExistingExternalComponents);
  end;
  if not ExternalInventoryAvailable then
    WizardForm.ComponentsList.Hint := '외부 설치 정보를 확인할 수 없습니다. 내장 볼따구만 설치하거나 외부 설치를 먼저 복구하세요.';
  WizardForm.ComponentsList.Height := WizardForm.ComponentsList.Height - ScaleY(55);
  SelectAllExternalButton := TNewButton.Create(WizardForm);
  SelectAllExternalButton.Parent := WizardForm.SelectComponentsPage;
  SelectAllExternalButton.Left := WizardForm.ComponentsList.Left;
  SelectAllExternalButton.Top := WizardForm.ComponentsList.Top + WizardForm.ComponentsList.Height + ScaleY(4);
  SelectAllExternalButton.Width := ScaleX(100);
  SelectAllExternalButton.Height := ScaleY(23);
  SelectAllExternalButton.Caption := '외부 전체 선택';
  SelectAllExternalButton.OnClick := @SelectAllExternalClick;
  SelectNoneExternalButton := TNewButton.Create(WizardForm);
  SelectNoneExternalButton.Parent := WizardForm.SelectComponentsPage;
  SelectNoneExternalButton.Left := SelectAllExternalButton.Left + SelectAllExternalButton.Width + ScaleX(8);
  SelectNoneExternalButton.Top := SelectAllExternalButton.Top;
  SelectNoneExternalButton.Width := ScaleX(100);
  SelectNoneExternalButton.Height := ScaleY(23);
  SelectNoneExternalButton.Caption := '외부 선택 해제';
  SelectNoneExternalButton.OnClick := @SelectNoneExternalClick;
  InventoryText := '';
  for I := 0 to GetArrayLength(ExistingExternalComponents) - 1 do
    if Pos('ownership=', ExistingExternalComponents[I]) <> 1 then begin
      if InventoryText <> '' then InventoryText := InventoryText + ', ';
      InventoryText := InventoryText + ExistingExternalComponents[I];
    end;
  if InventoryText = '' then InventoryText := '없음';
  if ExistingExternalComponent('ownership=user-owned') then
    InventoryText := '사용자 소유 런타임 (자동 변경 안 함) · ' + InventoryText;
  if not ExternalInventoryAvailable then InventoryText := '확인 불가 — 외부 설치 복구 필요 (내장 설치 가능)';
  ExternalInventoryLabel := TNewStaticText.Create(WizardForm);
  ExternalInventoryLabel.Parent := WizardForm.SelectComponentsPage;
  ExternalInventoryLabel.Left := WizardForm.ComponentsList.Left;
  ExternalInventoryLabel.Top := SelectAllExternalButton.Top + SelectAllExternalButton.Height + ScaleY(3);
  ExternalInventoryLabel.Width := WizardForm.ComponentsList.Width;
  ExternalInventoryLabel.Height := ScaleY(25);
  ExternalInventoryLabel.WordWrap := True;
  ExternalInventoryLabel.Caption := '기존: ' + InventoryText + ' · 체크 해제로 기존 항목을 삭제하지 않습니다.';
  InitialDbDir := DefaultDbDir();
  InitialWorkDir := InitialDbDir;
  LoadExistingUserPaths(InitialDbDir, InitialWorkDir);

  { 1) DB / 작업 디렉토리 }
  DirPage := CreateInputDirPage(wpSelectDir,
    'AMBER (ENGRAM) 데이터 위치', 'DB 와 작업 디렉토리를 선택하세요',
    'engram 의 지식 그래프 DB 와 위키가 저장될 위치입니다.', False, '');
  DirPage.Add('DB / 위키 디렉토리');
  DirPage.Add('작업 디렉토리 (engram 실행 시 기준 경로)');
  DirPage.Values[0] := InitialDbDir;
  DirPage.Values[1] := InitialWorkDir;

  { 2) CLI provider (라디오) }
  ProviderPage := CreateInputOptionPage(DirPage.ID,
    '기본 CLI 공급자', '오버레이가 기본으로 사용할 AI 공급자를 선택하세요',
    '나중에 트레이 메뉴에서 변경할 수 있습니다.', True, False);
  ProviderPage.Add('Copilot CLI');
  ProviderPage.Add('Antigravity (agy)');
  ProviderPage.Add('Codex CLI');
  ProviderPage.Add('Claude Code (직접)');
  ProviderPage.Add('Claude Code (Ollama 라우팅)');
  ProviderPage.Add('Ollama (로컬)');
  ProviderPage.SelectedValueIndex := 3;

  { 4) Ollama 모델 / Identity 이름 (선택) }
  QueryPage := CreateInputQueryPage(ProviderPage.ID,
    '추가 설정 (선택)', '비워두면 기본값을 사용합니다',
    'Ollama 를 쓰는 경우 모델명을, engram 이름을 미리 정하려면 입력하세요.');
  QueryPage.Add('Ollama 모델명 (예: qwen3.5:4b) — 선택', False);
  QueryPage.Add('Engram Identity 이름 — 선택', False);
end;

function ExternalOverlaySdkCode(): String;
begin
  if WizardIsComponentSelected('sdk') then
    Result := 'yes'
  else
    Result := 'no';
end;

function ProviderCode(): String;
begin
  case ProviderPage.SelectedValueIndex of
    0: Result := 'copilot';
    1: Result := 'antigravity';
    2: Result := 'codex';
    3: Result := 'claude-code';
    4: Result := 'claude-code-ollama';
    5: Result := 'ollama';
  else
    Result := 'claude-code';
  end;
end;

function GetConfigureParams(Param: String): String;
var
  Q: String;
  R: String;
begin
  Q := '"';
  R := '-InstallDir ' + Q + ExpandConstant('{app}') + Q;
  R := R + ' -DbDir ' + Q + DirPage.Values[0] + Q;
  R := R + ' -WorkDir ' + Q + DirPage.Values[1] + Q;
  R := R + ' -CliProvider ' + ProviderCode();
  R := R + ' -ExternalOverlayMode none';
  if ExternalOverlayComponentsCode() <> '' then
    R := R + ' -ExternalOverlayComponents ' + Q + ExternalOverlayComponentsCode() + Q;
  R := R + ' -ExternalOverlaySdk ' + ExternalOverlaySdkCode();
  R := R + ' -ExternalComponentManifestPath ' + Q + ExpandConstant('{tmp}\component-bundle\engram-overlay-components.json') + Q;
  if Trim(QueryPage.Values[0]) <> '' then
    R := R + ' -OllamaModel ' + Q + Trim(QueryPage.Values[0]) + Q;
  if Trim(QueryPage.Values[1]) <> '' then
    R := R + ' -IdentityName ' + Q + Trim(QueryPage.Values[1]) + Q;
  if WizardIsTaskSelected('autostart') then
    R := R + ' -AutoStart on'
  else
    R := R + ' -AutoStart off';
  R := R + ' -LaunchNow';
  Result := R;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  { 설치 결과는 configure.ps1 이 판정한다(Python 적격성, 기존 런타임 소유권).
    위저드는 결과를 단정하지 않고 어디서 확인·변경하는지만 알린다. }
  if CurPageID <> wpFinished then
    Exit;
  if ExternalOverlayComponentsCode() <> '' then
    WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10 + #13#10 +
      '캐릭터: 설정 > 오버레이에서 확인하고 언제든 바꿀 수 있습니다.';
  if WizardIsComponentSelected('sdk') then
    WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10 +
      '개발자 SDK: %LOCALAPPDATA%\engram-overlay\sdk 에 안내와 예제를 준비했습니다.';

  { 네 번째 인지 표면. SDK 를 체크하지 않은 사람도 여기서 "직접 만들 수 있다"를
    알게 된다. [Run] 섹션으로 넣으면 configure 를 [Code] 에서만 실행하고 exit code
    를 전파한다는 이 installer 의 계약을 깬다 — 그래서 코드로 만든다. }
  if MakeYourOwnCheckBox = nil then begin
    MakeYourOwnCheckBox := TNewCheckBox.Create(WizardForm);
    MakeYourOwnCheckBox.Parent := WizardForm.FinishedPage;
    MakeYourOwnCheckBox.Left := WizardForm.FinishedLabel.Left;
    MakeYourOwnCheckBox.Top := WizardForm.FinishedLabel.Top + WizardForm.FinishedLabel.Height + ScaleY(16);
    MakeYourOwnCheckBox.Width := WizardForm.FinishedLabel.Width;
    MakeYourOwnCheckBox.Height := ScaleY(17);
    MakeYourOwnCheckBox.Caption := '나만의 캐릭터 만드는 법 보기';
    MakeYourOwnCheckBox.Checked := False;
  end;
end;

procedure DeinitializeSetup();
var
  ErrorCode: Integer;
begin
  if (MakeYourOwnCheckBox <> nil) and MakeYourOwnCheckBox.Checked then
    ShellExec('open', 'https://github.com/JJHbrams/engram-overlay#readme',
      '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
end;

procedure RunConfigure;
var
  ResultCode: Integer;
  Params: String;
begin
  if (ExternalOverlayComponentsCode() <> '') and not ExternalInventoryAvailable then
    RaiseException('외부 설치 정보를 확인할 수 없습니다. 기존 파일은 보존되었습니다. 외부 항목 없이 다시 설치하거나 기존 설치를 복구하세요.');
  PrepareExternalComponentBundle();
  WizardForm.StatusLabel.Caption := 'AMBER (ENGRAM) 구성 중 (config · MCP · 바로가기)...';
  Params := '-NoProfile -ExecutionPolicy Bypass -File "' +
    ExpandConstant('{app}\installer\configure.ps1') + '" ' + GetConfigureParams('');
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Params,
    ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    RaiseException('AMBER (ENGRAM) 구성기를 실행하지 못했습니다.');
  if ResultCode <> 0 then
    RaiseException('AMBER (ENGRAM) 구성에 실패했습니다. 로그: ' +
      ExpandConstant('{%USERPROFILE}\.engram\logs') +
      ' (exit=' + IntToStr(ResultCode) + ')');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    RunConfigure;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  HelperPath: String;
  Params: String;
begin
  ExtractTemporaryFile('stop-engram-processes.ps1');
  HelperPath := ExpandConstant('{tmp}\stop-engram-processes.ps1');
  Params := '-NoProfile -ExecutionPolicy Bypass -File "' + HelperPath +
    '" -ArtifactDir "' + ExpandConstant('{app}\dist\engram-overlay') + '"';
  if (not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Params,
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode)) then
  begin
    Result := '실행 중인 AMBER (ENGRAM) 프로세스를 종료하지 못했습니다.';
    Exit;
  end;
  if ResultCode <> 0 then
    Result := '실행 중인 AMBER (ENGRAM) 프로세스를 종료하지 못했습니다. (exit=' + IntToStr(ResultCode) + ')';
end;
