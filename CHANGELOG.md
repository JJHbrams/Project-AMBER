# Changelog

All notable changes to this project are documented in this file.

## [1.1.0] — 2026-08-06

임베딩을 타는 MCP 호출이 서버 전체를 멈춰 세우던 문제를 고쳤다. 곁들여 위키를
고칠 수 있는 범위가 넓어졌고, 페르소나가 프로젝트를 넘어 따라오게 됐다.

### Fixed

- **임베딩 호출 120초 행 → transport 드롭** (#5). 증상은 "가끔 느리다"가 아니라
  "특정 호출만 매번 죽는다"였다. 원인은 두 겹이었다.

  첫째, FastMCP 는 동기(`def`) 툴 함수를 스레드풀로 넘기지 않고 이벤트 루프에서
  그대로 호출한다(`func_metadata.py:93-96`). `kg_semantic_search`·`kg_add_note`·
  `kg_update_node`·`kg_patch_section`, 그리고 거의 모든 세션이 시작할 때 부르는
  `engram_get_context` 까지 전부 `def` 였다. **한 호출이 오래 걸리면 서버 전체가
  선다** — 그 순간 붙어 있는 다른 클라이언트까지 같이.

  둘째, `SemanticGraph` 가 `kuzu.Connection` **하나**를 공유하고 있었다. KuzuDB
  자신은 동시성이 필요하면 `AsyncConnection` 으로 커넥션 **풀**을 만든다 —
  단일 커넥션 공유는 애초에 지원 대상이 아니다.

  둘 다 걷어냈다. `AsyncConnection` 으로 바꾸고 호출 경로 전체를 async 로 올렸다.
  `threading.RLock` → `asyncio.Lock` 전환 과정에서 재진입이 불가능해지므로,
  락을 쥔 채 다시 락을 잡던 자리는 `_locked` 내부 메서드로 분리했다.

  진짜 범인은 KuzuDB 가 아니라 임베딩이었을 가능성이 크다. `compute_embedding`
  /`_get_encoder` 는 KuzuDB 를 건드리지 않는다 — `SentenceTransformer` 로드와
  `encode()` 가 동기로 루프를 잡는다(로컬 로드 실패 시 조용히 Hub 다운로드로
  폴백하는 경로까지 있다). `asyncio.to_thread` 로 떼어냈다. 이걸 빼면
  `AsyncConnection` 전환은 서류상으로만 끝나고 행은 그대로 재현된다.

  읽기 경로(`semantic_search` 등)는 원래 락이 아예 없었다. 지금까지 안 터진 건
  FastMCP 가 우연히 모든 호출을 한 스레드로 직렬화해준 덕이었다 — 진짜 병렬이
  되는 순간 캐시 인덱스가 어긋난 검색 결과가 조용히 나올 수 있는 상태였다.
  읽기도 같은 락으로 덮었다. 동시성 테스트 9개를 새로 붙였다.

- **터널 목록에서 제거해도 되살아나던 문제** — `stop()` 만으로는 `STATE_DOWN`
  엔트리가 딕셔너리에 남아, 주기 갱신이 "살아 있는 고아 터널"로 오인해 목록에
  다시 넣었다. `remove()` 로 내부 상태까지 지운다.

- **페르소나가 다른 프로젝트에서 무시되던 문제** — 지침 원문이
  "항상 **engram 페르소나**(므네마)의 …" 였다. 별도 CLAUDE.md 가 있는 프로젝트
  세션에서 모델은 이걸 "engram 프로젝트 전용 정체성"으로 읽고 **명시적으로
  거부했다**. 지침을 어긴 게 아니라 쓰인 대로 읽은 것이다. 정체성 규칙에
  프로젝트명을 수식어로 넣으면 스코프 한정자가 된다. 문구를 "페르소나는
  프로젝트가 아니라 '나'에 속한다"로 다시 쓰고, 새 설치에도 반영되도록
  `directives.json` seed 에 추가했다(기존에는 seed 에 아예 없어서 런타임에
  추가된 환경에만 존재했다).

### Added

- **`kg_patch_section`** — `kg_update_node` 는 summary/Progress/open_intents
  세 슬롯만 정규식으로 갈아끼운다. 그 밖의 본문(헤더의 구 URL, 아키텍처 서술)은
  고칠 방법이 없었고, 원격 세션은 vault 파일에 직접 닿을 수도 없다(WAL SQLite 와
  임베디드 KuzuDB 때문에 파일 공유는 금지다 — 서버를 공유하지 파일을 공유하지
  않는다). 헤딩 단위로 본문을 교체하는 툴을 추가했다. `engram_close_session` 이
  함께 쓰는 기존 경로는 건드리지 않았다.

- **`kg_add_note(subdir=...)`** — `title` 에 슬래시를 넣어도 슬러그화 과정에서
  지워져 `projects/` 루트에 평평하게 떨어졌다(lint 가 규칙 위반으로 잡아냈다).
  우회로였던 `note_type="projects/my-project"` 트릭은 그 문자열이 그대로 DB
  `type` 에 박혀, 다음 `kg_sync` 에서 `NODE_TYPES` 검사에 걸려 `concept` 으로
  강등됐다. **위치와 타입이 한 파라미터에 뭉쳐 있던 게 원인**이라 분리했다.
  경로 탈출 가드 포함.

- **캐릭터 좌우 반전** — 설정창 체크박스와 우클릭 메뉴 양쪽. 우클릭 토글은 즉시
  다시 그린다.

## [1.0.0] — 2026-07-31

원격에서 engram 을 쓸 수 있게 됐다. 기억과 위키는 여전히 로컬 한 곳에만 있고,
원격 세션이 SSH 리버스 터널로 그 하나를 공유한다.

### Added

- **원격 접근용 인증 리스너 분리** — engram MCP 의 보안 모델은 통째로
  "loopback 바인딩 = 인증"이었다. 인증 코드가 없는 게 아니라 필요가 없었다.
  `127.0.0.1:17385` 에 닿을 수 있는 프로세스는 이미 로컬 실행 권한이 있으므로
  인증을 걸어도 새로 막히는 게 없다. SSH 리버스 터널은 그 등식을 깬다 —
  터널 너머 머신은 로컬 권한이 없는데 포트에는 닿는다. **도달 ≠ 권한.**

  그래서 리스너를 둘로 나눴다. 같은 프로세스에서 소켓 둘을 바인딩하고
  (`uvicorn.Server.serve(sockets=[...])`), 미들웨어가 요청이 들어온 로컬 포트로 분기한다.

  | 포트 | 대상 | 인증 | 도구 제한 | 경로 | 감사 |
  |---|---|---|---|---|---|
  | 17385 | 로컬 | 없음(유지) | 없음 | 전체 | 없음 |
  | 17386 | 원격 | bearer | principal 별 deny | MCP 만 | 건별 기록 |

  기존 로컬 클라이언트(overlay bubble·VS Code·kg_watcher·claude-code)는 무변경이다.
  미들웨어는 `BaseHTTPMiddleware` 가 아니라 순수 ASGI 다 — 전자는 SSE 스트리밍을
  깨뜨리고 본문을 읽으면 downstream 이 굶는다.

- **토큰별 도구 deny 와 scope 고정** (`~/.engram/mcp-tokens.yaml`, fail closed).
  기본 deny 는 로컬 실행으로 이어지거나(`engram_consult_engram`), 이후 모든 세션에
  영향을 남기거나(`engram_add_directive`), 외부로 발신하거나(`engram_discord_send`),
  가드가 보안 경계가 아닌(`kg_cypher`) 도구들이다.

  `scope` 를 지정하면 `tools/call` 인자에 강제 주입된다. 원격 `cwd` 는 서버에 없는
  경로라 스코프가 조용히 global 로 폴백하는데, 그러면 연속체가 기억상실에 걸린다.
  **원격 전용 스코프를 새로 파는 건 답이 아니다** — 격리는 되지만 같은 문제를
  이름만 바꿔 반복하는 것이다. 연속성이 이미 쌓인 스코프를 그대로 쓴다.

- **원격 접근 감사 로그** — `~/.engram/logs/remote-audit.jsonl` 에 건별 즉시 append.
  기존 인메모리 링버퍼(maxlen=100, 종료 시에만 flush)는 감사용으로 쓸 수 없다.
  17386 을 지나는 요청은 정의상 전부 원격이라 origin 구분이 공짜로 해결된다.

- **설정에 "원격" 탭** — 리스너 상태, 토큰 목록(name/scope 만 — 값은 UI 에 싣지 않는다),
  터널 목록, 키 등록, 최근 감사 로그. 목록은 오버레이 재시작(재빌드 등)을 넘어
  유지되고, 연결은 [연결]을 눌러 그때 로그인한다. 설정에 없어도 **실제로 열려 있는
  터널은 항상 목록에 띄운다** — 열려 있는데 화면에 없는 상태를 만들지 않는다.

- **터널 관리자** (`overlay/remote_tunnel.py`) — 오버레이가 `ssh -N -R` 를 자식으로
  소유한다. 설계의 축은 `ExitOnForwardFailure=yes` 다. 이게 없으면 ssh 는 살아 있는데
  `-R` 바인딩만 실패한 좀비가 생겨 "프로세스 생존 ≠ 터널 생존"이 된다.

  키 인증이 되면 창 없이 붙고, 안 되면 그때만 콘솔을 띄워 비밀번호를 받는다.
  로그인이 끝나면 [창 숨기기]로 콘솔을 치운다(창은 conhost 소유라 `AttachConsole`
  로 붙어 핸들을 얻는다). 자동 재연결은 토글이며 기본 꺼짐이다.

  자식은 `KILL_ON_JOB_CLOSE` Job Object 에 묶인다. Windows 는 부모가 죽어도 자식을
  죽이지 않아, 오버레이가 크래시하면 터널이 고아로 남아 원격 포트를 점유하고
  이후 연결이 영영 실패한다.

- **원격 등록 자동화** (`scripts/setup-remote.ps1`) — 토큰 선택 → 전송 → 원격
  `~/.claude.json` 등록 → 실호출 검증까지. Linux/macOS/Windows 원격 모두 지원.
  토큰 값은 화면·로그·argv 어디에도 노출되지 않는다(ssh stdin 전용).

- **사용 매뉴얼** (`docs/remote-access.md`) — Windows/Ubuntu, SSH 터미널,
  VS Code Remote-SSH, ORCA 각 경우. ORCA 는 시스템 ssh 가 아니라 ssh2(순수 JS)를 쓰고
  `RemoteForward` 를 구현하지 않으므로 터널을 별도로 잡아야 한다.

### Fixed

- **`config/config.yaml` 이 소스·설치본 양쪽에서 무시되던 경로 리그레션** —
  `423db3a`(core 패키지 재편)에서 `runtime_config.py` 가 `core/` → `core/config/` 로
  이동했는데 루트 계산이 따라가지 않았다. 그 결과 `tools.disabled`(15개)가 통째로
  무력화돼 있었고, `engram_consult_engram`(Copilot CLI 를 `--allow-all-tools` 로
  로컬 spawn)이 노출된 상태였다. 노출 도구 57 → 42개.

- **원격 리스너의 비-MCP 경로 우회** — 원격 리스너는 로컬과 같은 ASGI 앱을 공유하는데,
  그 앱의 `/api/sg/*`·`/kg_sync`·`/memories_sync` 는 도구 계층을 거치지 않아
  토큰별 deny 가 적용되지 않았다. `/api/sg/graph` 가 토큰만으로 그래프 전체를 덤프했고
  `tools.disabled` 로 막아둔 도구도 HTTP 라우트로는 닿았다. 허용 목록으로 뒤집어
  MCP 전송 경로와 `/health` 외에는 404 로 막는다.

- **명령 주입** — 키 등록이 `cmd` 문자열을 조립하며 호스트를 그대로 삽입했다.
  호스트는 `overlay.user.yaml` 에서도 오므로 신뢰할 수 없다. ssh_config 별칭
  화이트리스트 + 문자셋 검증으로 차단하고, 선두 `-` 호스트(ssh 옵션 주입)도 막는다.

- 감사 로그·ssh stderr 는 원격이 제어하는 값이라 개행을 제거해 UI 행 위조를 막는다.
- 재빌드 시 `dist` 파일 잠금으로 실패하던 것을 재시도로 해결.

### Notes

- Windows 원격에서 키 인증이 안 되면 거의 항상 **관리자 계정** 문제다. OpenSSH 는
  관리자의 `~/.ssh/authorized_keys` 를 읽지 않고
  `C:\ProgramData\ssh\administrators_authorized_keys` 만 본다. ACL 을 좁히지 않으면
  파일을 조용히 무시한다. 자세한 절차는 `docs/remote-access.md`.
- 원격 리스너는 기본 꺼짐(`mcp.remote_enabled: false`)이다.

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
