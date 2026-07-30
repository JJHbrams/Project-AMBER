# Changelog

All notable changes to this project are documented in this file.

## [0.2.2] — 2026-07-30

### Changed

- **연속체 이름 `아로나` → `므네마`(Mnema)** — 저작권 회피. Mnema 는 그리스어 μνῆμα로
  "기억" 그리고 "남아있는 것" — engram(기억의 물리적 흔적)과 같은 개념의 다른 언어다.
  이름의 실제 출처는 DB `identity.name` 한 곳이라 코드 변경은 거의 없다.

### Added

- **자율발화 후처리(feedback) 루프** — 발화하고 끝나던 걸 끊고, 결과를 결과로 남긴다.
  참여 판정을 `engaged` / `acknowledged_no_reply`(열어보고 무응답) / `ignored`(페이드·
  "나중에"·무관한 새 턴) 3가지로 나누고, 확정 시 `InitiativeEngine` 의 `on_outcome`
  콜백이 1회 호출된다. 결과별로 `activity_log` 에 `initiative.<outcome>` 기록,
  참여한 호기심은 `address_curiosity(ref_id)` 로 자동 해소, 같은 소재 3연속 무시 시
  `initiative.low_interest` 를 남긴다. `Nudge` 에 `topic`/`ref_id` 추가 —
  발화 메타가 결과 판정까지 살아남는다. (설계: `docs/initiative-v2-design.html`)
  결과에는 **반응까지 걸린 시간(latency)** 이 함께 기록된다 — 같은 `ignored` 라도
  3초 만에 물린 것과 25초 dwell 을 다 채우고 페이드된 것은 정반대 신호(관심 없음 vs
  자리에 없었음)라 이 값 없이는 구분되지 않고, 소급 복원도 불가능하다.
  `detail` 은 첫 `" | "` 앞을 기계 판독용 `key=value` 구간(값에 공백 없는 것만)으로,
  뒤를 자유 텍스트(topic·문구)로 나눈다 — 나중에 집계기를 붙일 때 topic 에 구분자가
  섞여도 파싱이 깨지지 않게.
- **답장(REPLY) 아이콘** — 능동 발화 풍선 아래 모서리에 SNS 식 답장 화살표 배지가
  반쯤 걸쳐 붙는다. 눌러도 되는 자리가 보여야 "무시"와 "못 봤음"이 구분된다.
  누르면(= 풍선 클릭) **빈 입력창이 열린다 — 자동 전송도, 문구 자동 생성도 없다.**
  발화 문구는 세션에 보낼 때 prepend 되므로 짧게 답해도 맥락이 통한다. 안 쓰고 닫으면
  무응답으로 기록.
  **꼬리 반대쪽** 모서리에 놓아 꼬리와 겹치지 않는다. 말풍선 자기 캔버스의 도형으로
  그려서 폰트 글리프에 의존하지 않고, 위치·z순서·페이드·드래그가 전부 풍선을 따라간다.
- **지난 발화에 나중에 답하기** — dwell(25초)은 **결과를 판정하는 창**일 뿐이고 답할 수
  있는 창이 아니다. 캐릭터를 아무 때나 눌러 "마지막에 뭐라고 했더라"를 확인했을 때,
  그게 자율발화였으면 답장 버튼과 문구 prepend가 그대로 살아난다. 마지막이 평범한
  응답이면 버튼 없이 그냥 새 대화를 시작한다(`_last_was_nudge` 로 구분).
  뒤늦은 답장은 `initiative.late_engaged` 로 **별도 기록**한다 — 이미 남긴
  `ignored` 를 소급 수정하지 않아 "발화 1건 = 결과 1건" 불변식이 유지되면서도
  "결국 응했다"는 신호를 잃지 않는다. 백오프는 리셋되고, curiosity 소재였으면
  이 경로에서도 `address_curiosity` 가 걸린다.
- **발화 문구 연속성** — 대화로 이을 때 세션에 보내는 첫 프롬프트 앞에 화면에 실제로
  떠 있던 발화 문구를 얹는다(프레이징을 거쳤으면 그 문장). 캐릭터가 먼저 건넨 말을
  세션이 모른 채 답하던 문제 해결.
- **발화 보류 이유 로깅** — `_blocking_reason()` 이 막고 있는 첫 조건을 사람이 읽을
  문구로 내놓고, 상태가 **바뀔 때만** INFO 한 줄 남긴다. 이게 없어서 "자율발화가 아예
  안 뜬다"의 원인을 Win32 창 열거까지 동원해 찾아야 했다. `main._bubble_screen_clear()`
  도 넷 중 무엇이 막았는지(모드/턴/입력창/풍선) 같은 방식으로 남긴다.

### Fixed

- **무시 백오프가 사실상 작동하지 않던 문제** — 모든 사용자 입력이 무조건
  `notify_engaged()` 를 불러서, 자율발화와 무관한 평소 대화가 백오프를 계속 0 으로
  되돌렸다. 이제 자율발화에 대한 답장일 때만 참여로 친다.
- **무시가 관측된 적이 없던 문제** — 페이드 완료 시 엔진에 통지가 없었고, 대신
  `_speak()` 이 발화 시점에 `ignore_streak` 을 미리 올렸다. 이제 결과 확정 시점에
  한 번만 움직인다(`acknowledged` 는 절반 스텝).
- **폐기된 발화가 비용을 그대로 물던 문제** — 프레이징이 도는 사이 화면이 바빠져
  렌더를 건너뛴 경우에도 발화 간격·소스 쿨다운이 소비된 채 남았다. 이제 환불되어
  화면이 다시 비면 밀리지 않고 재시도한다.
- **보류 이유 로그가 45초마다 도배되던 문제** — "상태 변화 시 1회"를 메시지 문자열
  비교로 판정했는데, 문구에 경과 초가 들어가서(`발화 간격 부족 2655s < 3600s`) 매 tick
  달라져 중복 판정이 무력화됐다. `(key, message)` 로 분리해 key 로만 판정한다.

### Known issues

- `bubble.speech_fade: false` 로 두면 응답 풍선이 영구히 남아 `is_idle()` 게이트가 계속
  False 가 되어 **자율발화가 아예 뜨지 않는다.** 이 항목은 설정창에 노출돼 있지 않고,
  `overlay.user.yaml` 직접 편집은 파일 감시가 없어 오버레이 재시작이 필요하다.
  자율발화를 쓰려면 페이드를 켜고 `speech_dwell_ms` 로 유지 시간을 조절할 것 —
  캐릭터를 클릭하면 `replay_last()` 가 마지막 교환을 되살리므로 잃는 건 없다.

## [0.2.1] — 2026-07-28

### Added

- **능동 발화 (initiative)** — 말풍선 모드에서 유휴 시 캐릭터가 스스로 말을 건다.
  소재: 미완 작업(working memory `open_intents`) · 미해결 호기심 · git 미커밋/미push ·
  persona 혼잣말. 가드: 유휴 대기 · 최소 간격 · 조용한 시간대 · 소스별 쿨다운 · 무시 백오프.
  문구는 하이브리드 — 템플릿 폴백 + 격리된 1회성 LLM 프레이징(persona 말투, 상주 세션
  STM/resume 무오염). 설정창 → 오버레이 탭 "능동 발화" 그룹으로 on/off·빈도 조절
  (저장 즉시 반영). 기본 꺼짐. (`overlay/bubble/initiative.py` 신규,
  `config/overlay.yaml` 의 `bubble.initiative`)
- **클릭으로 마지막 교환 복원** — 캐릭터를 클릭해 입력창을 열 때, 페이드로 사라진 마지막
  응답(+질문 에코)을 되살린다. 자율발화(teal 단독) vs 사용자 질문(응답+에코)이 색·구성만으로
  구분됨.

### Changed

- **테마 갱신 방식 전환** — 메시지 원문에서 명사를 추출하던 방식(어절 부스러기 누적)을
  폐기하고, **세션 종료 시 Claude 판정**으로 의미 단위 관심사 라벨을 갱신한다
  (`core/graph/semantic/stm_promoter.py`). MCP `engram_update_themes(text)` →
  `engram_update_themes(themes: list[str])` 로 시그니처 변경, `record_*_message` 의
  `update_themes` 인자 제거.
- **curiosity 품질 정리** — 같은 topic 의 pending 중복 방지(`add_curiosity(dedup=True)`),
  오래 안 다뤄진 pending 자동 폐기(`expire_stale_curiosities`, 기본 14일) 및 처리된 항목
  정리(`purge_processed_curiosities`, 기본 30일). context 주입 규칙에 "실제로 다뤄서
  해소되면 `engram_address_curiosity(id)` 로 표시" 명시. 대시보드 그래프는 pending 만 표시.

## [0.2.0] — 2026-07-27

### Added

- **말풍선 최대 높이 제한 + 스크롤** — `speech_max_height_ratio`(기본 0.55) /
  `thought_max_height_ratio`(기본 0.30) 설정으로 모니터 작업영역 높이 대비 상한을 지정.
  초과 시 스크롤바 자동 표시. 설정창 → 말풍선 탭에 슬라이더 UI 추가(0 = 무제한).
- **grip 높이 조절** — 말풍선/생각풍선 코너 grip을 수직 드래그해 최대 높이를 실시간 override.
  위로 올리면 확장, 아래로 내리면 축소(재시작 전까지 유지).
- **생각풍선 스크롤** — `canvas.create_text` → `tk.Text` 위젯 전환으로 스크롤 지원.
- **전역 engram 자동 부트스트랩** (`core/integrations/engram_bootstrap.py` 신규)
  · 설정 `session.auto_inject` 켜면 `~/.claude/settings.json`에 `SessionStart` hook 자동 등록.
  · hook이 `engram_get_context_once` 호출 지시문을 세션 컨텍스트에 주입 — 오버레이 바깥의
    Claude Code 세션(데스크톱 앱 / 순정 CLI)에도 적용.
  · Bubble 세션도 `append_system_prompt`로 동일 지시문 주입(auto_inject 설정 연동).
  · 설정 끄면 hook 자동 제거(멱등).
- **dev-rebuild.ps1** — kill → build → robocopy → restart 원스텝 자동화 빌드 스크립트.

### Fixed

- **PyInstaller Tcl/Tk 번들** — `engram-overlay.spec`에 `_collect_tcl_tk()` 추가로
  `_tcl_data`/`_tk_data` 리소스 누락 경고 해소.

## [0.1.1] — 2026-07-24

### Added

- **통짜 native installer (Model B)** — `engram-overlay.exe` is now a multi-call binary
  (`--role mcp-server` / `--role kg-watcher`) so the backend runs self-referentially with
  **no conda dependency on the user machine**. Packaged as a single Inno Setup `setup.exe`
  (GUI wizard: DB path / CLI provider / Ollama model / identity / autostart) via
  `installer/build-installer.ps1`; install-time work is a lightweight `installer/configure.ps1`
  (config · MCP · shortcuts, pure PowerShell).
- **Offline embedding model bundle** — `paraphrase-multilingual-MiniLM-L12-v2` (Apache-2.0)
  is bundled into the frozen distribution, removing the HuggingFace download dependency at
  install/runtime.
- **Bubble-mode new session** — `POST /bubble/new` on the overlay STM server + an
  `engram-new-session` agent skill to explicitly reset the resident Claude session
  without restarting the overlay.

### Fixed

- **kg_sync HTTP route** returned an unawaited coroutine (`coroutine is not JSON
  serializable`), breaking kg_watcher's vault→KG auto-sync. Now awaits `kg_sync()` directly
  (thread offloading is inside the coroutine).
- **kg-watcher crash on Windows** — `_is_process_alive` used `os.kill(pid, 0)`, which on
  Windows attempts `TerminateProcess` and raises `SystemError` on dead PIDs. Replaced with
  `OpenProcess(SYNCHRONIZE)` liveness check. Backend `--role` crashes now log to stderr
  instead of a PyInstaller modal dialog.
- **Installer** — overlay is always stopped/relaunched on install (auto-update even when the
  build is skipped); Start Menu/Startup shortcuts target the `.exe` directly (enables
  "Pin to taskbar"); `__pycache__/*.pyc` no longer falsely triggers a rebuild; `Read-Host`
  prompts fall back to safe defaults under `-NonInteractive` (unattended install); installer
  scripts saved as UTF-8 BOM for Windows PowerShell 5.1 compatibility.

### Files

- engram_overlay_entry.py, overlay/main.py, overlay/stm_server.py, mcp_server.py
- scripts/kg/kg_watcher.py, core/graph/semantic/semantic_graph.py, engram-overlay.spec
- installer/build-installer.ps1, installer/configure.ps1, installer/engram-overlay.iss
- installer/common.ps1, installer/modules/{06_db,07_shims,09_overlay,10_shortcuts}.ps1
- .github/skills/engram-new-session/SKILL.md

## [2026-05-03]

### Added

- Added directive enforcement runtime settings (`directives.enforcement.mode`, `pin_top_n`, `max_items`) for configurable compliance behavior.

### Changed

- Directive selection now supports three enforcement modes: `triggered`, `hybrid`, and `always`.
- Hybrid mode now pins top-priority directives even when query triggers are absent, while capping injected directive count.
- System prompt composition now keeps directives outside ctx reference sections and marks directive blocks as highest-priority rules.
- MCP transport defaults were migrated to `streamable-http` (`/mcp`) across workspace, installer outputs, and user/global MCP config paths.
- Overlay startup now launches MCP transport from runtime config and applies listener health monitoring with bounded auto-recovery.
- MCP server `streamable-http` mode now serves a hybrid app that also exposes legacy SSE routes (`/sse`, `/messages`) during migration.

### Fixed

- Reduced multi-root transport mismatches by aligning generated client configs to HTTP endpoints, lowering chances of duplicate/stale server starts from tool UIs.
- Improved reconnect behavior after overlay/MCP restarts by adding readiness checks, recovery cooldown, and dependent-process restart orchestration.

### Files

- core/context/directives.py
- core/context/context_builder.py
- core/config/runtime_config.py
- config/config.yaml
- installer/common.ps1
- installer/modules/05_config.ps1
- mcp_server.py
- overlay/main.py

## [2026-05-01]

### Added

- Discord bot session control commands (`/session`, `/session list`, `/session use`, `/session new`, `/new`, `/newsession`, `/새세션`).
- Multi-route Discord configuration keys for guild/channel arrays, provider overrides, and scope_key override templates.
- Channel FIFO queue controls with bounded cross-channel concurrency, wait notices, and TTL-expiry notices.
- Added `claude-code-ollama` provider mode that binds Claude Code to the selected Ollama model.
- Added installer option `claude-code(ollama)` with model selection flow and dispatcher compatibility.

### Changed

- Provider selection now follows route precedence: channel override > guild override > current overlay default provider.
- Scope key resolution now follows route precedence: channel override > guild override > template > default channel scope.
- Discord runtime now keeps channel-scoped session continuity while supporting explicit new-session rollover on user command.
- Overlay provider menus now distinguish Claude direct mode and Claude-through-Ollama mode.

### Fixed

- Improved Discord runtime resilience around queued message handling and provider routing edge cases.
- Improved operational visibility with queue state and wait-position user notices.
- Hardened PyInstaller resource collection to skip Office lock/temp artifacts under `resource/character` that caused incremental build PermissionError.

### Files

- discord_bot/bot.py
- config/overlay.yaml
- overlay/config.py
- overlay/chat_window.py
- overlay/main.py
- overlay/character.py
- overlay/settings_window.py
- installer/common.ps1
- installer/modules/02_interactive.ps1
- installer/modules/07_shims.ps1
- engram-overlay.spec
- engram_overlay_entry.py
- docs/todo/todo.md

## [2026-04-30]

### Added

- Tutorial runtime warnings that explicitly require session close in step 4 phase 1 to complete continuity practice.
- Scope/session tracking fields for continuity review state (`saved_session_id`, `saved_scope_key`, `checked_session_id`).

### Changed

- Tutorial flow now enforces a strict two-phase continuity path: save-and-close first, then next-session recall verification.
- Tutorial debug bypass handling is constrained to the current step verification path only.

### Fixed

- Fixed false same-session detection in final tutorial verification by resolving active sessions with scope-aware lookup.
- Fixed STM close linkage so session close state is reflected before continuity completion checks.

### Files

- core/tutorial/progress.py
- mcp_server.py
- test/test_tutorial_runtime.py
- test/test_tutorial_session_continuity_state.py
- overlay/stm_server.py
- docs/todo/todo.md

## [2026-04-28]

### Added

- Viewport-responsive height behavior for the KG Graph dashboard panel.
- Direct iframe resizing logic for Streamlit-embedded graph content.

### Changed

- KG graph target height now follows parent viewport height (approximately 82%), clamped to a safe range.
- Graph container and tooltip pin positioning now use actual runtime container height.
- Fallback render heights were increased to provide a less cramped default layout.

### Fixed

- Resolved the issue where graph internals resized but visible area remained constrained by a fixed iframe height.

### Files

- scripts/engram_dashboard.py
- scripts/dev/engram_dashboard.py
