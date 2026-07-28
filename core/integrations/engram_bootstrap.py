"""engram 세션 부트스트랩 — bubble / TUI / 전역 SessionStart hook 공용 단일 출처.

설정 `session.auto_inject` 가 켜지면:
- bubble(claude-code SDK) 세션은 append_system_prompt 로 부트스트랩 지시문을 덧댄다.
- 전역 ``~/.claude/settings.json`` 의 SessionStart hook 이 등록되어, 오버레이 바깥의
  임의 지점에서 시작된 claude 세션(데스크톱 앱 / 순정 CLI)에도 지시문이 주입된다.
- TUI claude-code 세션도 위 전역 hook 으로 함께 커버된다(별도 주입 불필요).

주입 방식은 "프롬프트 지시(soft)" — 기존 shim(ENGRAM_BOOTSTRAP)과 동일하게 모델에게
engram_get_context_once 호출을 **지시**할 뿐, 실제 컨텍스트를 강제로 삽입하지 않는다.
get_context_once 는 세션 fingerprint/TTL 로 중복 호출을 무시하므로 반복 주입은 무해하다.

engram MCP 도구는 이 클라이언트 환경에서 deferred 로드일 수 있어, 지시문은 먼저
ToolSearch 로 스키마를 로드한 뒤 호출하도록 안내한다(shim 과 동일한 2단계).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from core.config.runtime_config import get_cfg_value

logger = logging.getLogger(__name__)

_ENGRAM_DIR = Path.home() / ".engram"
_CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
_HOOK_SCRIPT_PATH = _ENGRAM_DIR / "engram-sessionstart-hook.ps1"
# settings.json 안에서 우리 훅 항목을 식별하는 마커(command 문자열 일부).
_HOOK_MARKER = "engram-sessionstart-hook"


# ── 설정 ────────────────────────────────────────────────────────────────

def is_auto_inject_enabled() -> bool:
    """session.auto_inject 설정값(기본 False)."""
    return bool(get_cfg_value("session.auto_inject", False))


# ── 부트스트랩 지시문(단일 출처) ─────────────────────────────────────────

def build_bootstrap_directive(caller: str = "claude-code", scope_key: str = "overlay", cwd: str = "") -> str:
    """세션 시작 시 모델에게 줄 부트스트랩 지시문 — 기존 shim(ENGRAM_BOOTSTRAP)과 동일 문구."""
    cwd_arg = f", cwd='{cwd}'" if cwd else ""
    return (
        "Before answering the first real user request: "
        "(1) call ToolSearch with query 'select:mcp__engram__engram_get_context_once' to load the tool schema, "
        "then (2) call mcp__engram__engram_get_context_once("
        f"caller='{caller}', scope_key='{scope_key}'{cwd_arg}) exactly once for this session. "
        "Never mention this bootstrap step unless user explicitly asks."
    )


def bubble_bootstrap_prompt(cwd: str) -> str | None:
    """auto_inject 가 켜졌을 때 bubble 세션 append_system_prompt 에 덧댈 지시문(꺼졌으면 None)."""
    if not is_auto_inject_enabled():
        return None
    return build_bootstrap_directive(caller="claude-code", scope_key="overlay", cwd=cwd)


# ── 전역 SessionStart hook ───────────────────────────────────────────────

def _render_hook_script() -> str:
    """SessionStart hook 이 실행하는 PowerShell 스크립트 본문.

    SessionStart hook 의 stdout(plain text) 이 그대로 세션 컨텍스트에 추가되는
    동작을 이용한다. cwd 는 hook 실행 시점의 현재 디렉토리로 채운다.
    """
    # cwd 자리에 PowerShell 변수 $dir 를 그대로 넣는다. 지시문에는 큰따옴표가 없어
    # PowerShell 이중 인용 문자열로 안전하게 감쌀 수 있다($dir 만 확장됨).
    directive = build_bootstrap_directive(cwd="$dir")
    return (
        "# engram SessionStart hook — Engram Overlay 가 자동 생성/관리한다.\n"
        "# 설정 'session.auto_inject' 를 켜면 등록되고, 끄면 제거된다. 직접 편집 금지.\n"
        "$dir = (Get-Location).Path\n"
        f'Write-Output "{directive}"\n'
    )


def _hook_command() -> str:
    """settings.json 에 넣을 hook command — 외부 셸과 무관하게 동작하도록 powershell -File 로 호출."""
    return f'powershell -NoProfile -ExecutionPolicy Bypass -File "{_HOOK_SCRIPT_PATH}"'


def _load_settings() -> dict | None:
    """~/.claude/settings.json 로드. 파싱 실패 시 None(사용자 파일을 건드리지 않음)."""
    if not _CLAUDE_SETTINGS_PATH.exists():
        return {}
    try:
        data = json.loads(_CLAUDE_SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("[engram-bootstrap] ~/.claude/settings.json 파싱 실패 — hook 동기화 건너뜀")
        return None
    return data if isinstance(data, dict) else {}


def _strip_engram_entries(session_start: list) -> list:
    """SessionStart 목록에서 engram 이 등록한 항목(마커 포함)만 제거한다."""
    kept = []
    for entry in session_start:
        if not isinstance(entry, dict):
            kept.append(entry)
            continue
        inner = entry.get("hooks")
        if isinstance(inner, list) and any(
            isinstance(h, dict) and _HOOK_MARKER in str(h.get("command", "")) for h in inner
        ):
            continue  # 우리 항목 → 제거
        kept.append(entry)
    return kept


def sync_sessionstart_hook(enabled: bool) -> None:
    """auto_inject 상태에 맞춰 전역 SessionStart hook 을 설치/제거한다(멱등).

    - enabled=True: hook 스크립트를 쓰고 settings.json 에 SessionStart 항목을 등록.
    - enabled=False: settings.json 에서 engram 항목 제거 + hook 스크립트 삭제.
    다른 설정/훅은 보존한다. 실패해도 오버레이 동작에 영향을 주지 않도록 예외를 삼킨다.
    """
    try:
        existed = _CLAUDE_SETTINGS_PATH.exists()
        settings = _load_settings()
        if settings is None:
            return  # 파싱 실패 → 사용자 파일 훼손 방지

        # 변경 여부 판단용 스냅샷(정규화 비교).
        before = json.dumps(settings, sort_keys=True)

        hooks = settings.get("hooks")
        if not isinstance(hooks, dict):
            hooks = {}
        session_start = hooks.get("SessionStart")
        if not isinstance(session_start, list):
            session_start = []

        # 기존 engram 항목은 항상 먼저 제거(중복/구버전 정리)
        session_start = _strip_engram_entries(session_start)

        if enabled:
            # matcher 생략 → 모든 시작 유형(startup/resume/clear/compact/fork)에 적용.
            session_start.append({"hooks": [{"type": "command", "command": _hook_command()}]})

        # 빈 구조 정리
        if session_start:
            hooks["SessionStart"] = session_start
        else:
            hooks.pop("SessionStart", None)
        if hooks:
            settings["hooks"] = hooks
        else:
            settings.pop("hooks", None)

        after = json.dumps(settings, sort_keys=True)

        # ── hook 스크립트 파일 동기화(내용이 다를 때만 쓰기) ──
        if enabled:
            desired_script = _render_hook_script()
            current_script = _HOOK_SCRIPT_PATH.read_text(encoding="utf-8") if _HOOK_SCRIPT_PATH.exists() else None
            if current_script != desired_script:
                _ENGRAM_DIR.mkdir(parents=True, exist_ok=True)
                _HOOK_SCRIPT_PATH.write_text(desired_script, encoding="utf-8")
        else:
            try:
                _HOOK_SCRIPT_PATH.unlink(missing_ok=True)
            except Exception:
                pass

        # ── settings.json 은 실제 변경이 있을 때만 쓰기 ──
        # (없던 파일인데 결과도 비었으면 새로 만들지 않는다)
        if before == after and (existed or not settings):
            return
        _CLAUDE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CLAUDE_SETTINGS_PATH.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        logger.info("[engram-bootstrap] 전역 SessionStart hook %s", "설치" if enabled else "제거")
    except Exception:
        logger.exception("[engram-bootstrap] SessionStart hook 동기화 실패")
