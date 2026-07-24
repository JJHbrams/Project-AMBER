@echo off
chcp 65001 >nul
cd /d "%~dp0..\.."
echo [debug] 빌드 중...

REM engram-overlay.exe가 dist\engram-overlay\ 파일들을 잠그고 있으면 빌드가 깨지니 먼저 종료.
taskkill /IM engram-overlay.exe /F >nul 2>&1

REM install.ps1(installer\modules\09_overlay.ps1)이 쓰는 것과 동일한 engram-overlay.spec을
REM 재사용한다 — 예전에 여기서 --onefile로 CLI 플래그를 줘서 스펙을 매번 새로 만들었더니
REM 그 onedir 스펙(COLLECT 사용)이 onefile 스펙으로 덮어써져서 install.ps1 쪽 빌드가
REM (dist\engram-overlay\ 폴더가 아예 안 나와서) 조용히 깨지는 사고가 있었다. 절대 스펙을
REM CLI 플래그로 재생성하지 말 것 — 항상 이 .spec 파일을 그대로 써야 install.ps1과 결과물이
REM 어긋나지 않는다.
where conda >nul 2>&1
if %ERRORLEVEL%==0 (
  conda env list | findstr /R /C:"^[ ]*intel_engram[ ]" >nul 2>&1
)
if %ERRORLEVEL%==0 (
  call conda run -n intel_engram python -m PyInstaller --noconfirm --distpath dist engram-overlay.spec
) else (
  python -m PyInstaller --noconfirm --distpath dist engram-overlay.spec
)

set "DIST_EXE=%~dp0..\..\dist\engram-overlay\engram-overlay.exe"

if not exist "%DIST_EXE%" (
    echo [debug] 빌드 실패
    pause
    exit /b 1
)

REM dist 폴더를 사용자 PATH에 등록 (최초 1회) — engram-overlay 폴더 자체가 아니라
REM dist\ 를 등록해두면 engram-overlay.cmd 등 다른 shim과도 충돌하지 않는다.
set "DIST=%~dp0..\..\dist"
powershell -Command "$p=[Environment]::GetEnvironmentVariable('PATH','User'); if($p -notlike '*%DIST%*'){[Environment]::SetEnvironmentVariable('PATH',$p+';%DIST%','User'); Write-Host '[debug] PATH 등록됨'} else {Write-Host '[debug] PATH 이미 등록됨'}"

echo [debug] 실행 중...
start "" "%DIST_EXE%"
