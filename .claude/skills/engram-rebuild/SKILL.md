---
name: engram-rebuild
description: 이미 설치된 engram 환경에 개발 변경을 적용한다. dev-rebuild.ps1 로 충분한지 INSTALL.ps1 전체를 돌려야 하는지 판단하고 실행한다. 트리거 — 재빌드, 리빌드, dev 변경 적용, 설치본에 반영, overlay 재시작, exe 갱신, "고친 거 확인하려면 뭐 돌려야 해", rebuild, redeploy.
---

# engram 재빌드 · 배포

이미 설치가 끝난 환경에 코드 변경을 반영할 때 쓴다. 상세 근거는
`docs/dev/system-technical-reference.md` §10.

## 1. 먼저 판단

**Python 소스(`overlay/`, `core/`, `discord_bot/`)를 개발 환경에서 확인할 때는
`dev-rebuild.ps1` 로 끝난다.** 이 경로는 frozen exe를 만들지 않는다.

frozen 빌드는 멀티콜 바이너리라 같은 exe 가 `--role` 로 MCP 서버·kg-watcher 까지 겸한다
(`engram_overlay_entry.py::_dispatch_backend_role`). 모든 Python 소스가 exe 하나에
번들되므로 exe 교체 = 전 컴포넌트 갱신이다.

전체 `INSTALL.ps1` 이 필요한 경우 — 이때만:

| 변경 | 이유 |
|---|---|
| `requirements.txt` / `environment.yml` | PyInstaller 가 conda env 에서 패키지를 수집한다. env 에 없으면 번들에도 없음 |
| shim(`~/.engram/*.cmd`) · PATH · 바로가기 | 설치 모듈 07/08/10 |
| MCP 클라이언트 등록 · `config/clients/*.md` | 설치 모듈 05 |

DB 스키마 추가는 보통 예외 — `core/storage/db.py` 가 연결 시
`CREATE TABLE IF NOT EXISTS` 마이그레이션을 돌린다.

## 2. 실행

```powershell
.\dev-rebuild.ps1              # source contract + source 재기동 + readiness
.\dev-rebuild.ps1 -NoStart     # source contract만 확인
```

기존 source installer 호환 경로가 필요할 때:

```powershell
.\INSTALL.ps1                              # auto — mtime 비교로 필요할 때만 빌드
.\INSTALL.ps1 -OverlayBuildMode clean      # 빌드 캐시 꼬임 정리
```

`-OverlayBuildMode`: `auto`(기본, mtime 비교) | `rebuild`(항상 증분) | `clean`(항상 clean) | `skip`(빌드 생략)

배포용 frozen bundle/installer는 다음 경로만 사용한다:

```powershell
.\installer\build-installer.ps1
```

## 3. 실행 전 확인할 것

- **현재 STM 포트의 overlay 가 종료된다.** STM 브로커·MCP 서버·kg-watcher 가 함께 내려가므로,
  다른 CLI 세션이 engram MCP 를 쓰는 중이면 그 세션의 MCP 호출이 실패한다.
  사용자에게 먼저 알리고 진행할 것.
- `dev-rebuild.ps1`은 PyInstaller, embedding model packaging, `dist/` 변경을 수행하지 않는다.
- `-Deploy`, `-FreshBuild`는 더 이상 지원하지 않으며 installer builder 사용 안내와 함께 실패한다.
- 사용자 설정(`~/.engram/overlay.user.yaml`)은 두 경로 모두 보존한다(없을 때만 생성).

## 4. 반영됐는지 확인

새 프로세스가 source entrypoint인지:

```powershell
Get-CimInstance Win32_Process |
  Where-Object CommandLine -like '*engram_overlay_entry.py*' |
  Select-Object ProcessId, CommandLine
```

동작 확인은 로그가 가장 확실하다 — 새 코드의 로그 라인을 찾는다:

```powershell
Get-ChildItem "$env:USERPROFILE\.engram\logs" -Filter "overlay-*.log" |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1
```

## 5. 자율발화(initiative) 변경을 확인할 때

기본 가드가 길어서(유휴 10분 · 발화 간격 30분 · 무시 시 배수 증가) 그냥 기다리면 안 뜬다.
`~/.engram/overlay.user.yaml` 에 임시로 낮춰두면 저장 즉시 반영된다(`update_cfg`):

```yaml
bubble:
  initiative:
    enabled: true
    idle_min_sec: 30
    min_gap_sec: 60
```

확인 후 되돌릴 것. `quiet_start_hour`~`quiet_end_hour` 구간이면 아예 발화하지 않는다.
