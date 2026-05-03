"""
Project Intel Engram — MCP Server
MCP 클라이언트가 호출 가능한 도구로 DB 연산을 노출하는 stdio MCP 서버.
"""

import sys
import os
import re
import json
import asyncio
import threading
import time
import uvicorn
from starlette.responses import Response

# Windows UTF-8 설정
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# 프로젝트 루트를 경로에 추가
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP, Context

from core.storage.db import initialize_db, get_connection
from core.identity import (
    get_identity,
    update_narrative,
    get_themes,
    update_themes_from_text,
    decay_themes,
    get_persona,
    update_persona,
    is_persona_initialized,
    seed_persona,
    get_persona_status,
)
from core.memory import save_message, save_memory, search_memories, list_memories, upsert_working_memory
from core.identity import add_curiosity, get_pending_curiosities, address_curiosity, dismiss_curiosity
from core.tutorial import (
    get_tutorial_status,
    get_tutorial_runtime,
    build_tutorial_runtime_payload,
    complete_tutorial_step,
    proceed_tutorial_step,
    mark_session_continuity_saved,
    skip_tutorial_step,
    resume_tutorial_step,
    verify_wiki_basic_step,
    verify_wiki_advanced_step,
    verify_session_continuity_step,
    contains_tutorial_debug_keyword,
)
from core.context.directives import (
    add_directive,
    get_directives,
    update_directive,
    remove_directive,
)
from core.observability.activity import log_activity, get_recent_activities, render_activity_for_reflection
from core.integrations.copilot_bridge import ask_copilot
from core.memory.bus import memory_bus
from core.context.project_scope import resolve_scope_key

# DB 초기화
initialize_db()

from core.observability.call_log import call_log as _call_log

# host/port는 __main__ 블록에서 argparse 이후 재설정됨 (SSE 모드 전용)
engramMCP = FastMCP("engram", instructions="Project Intel Engram 정신체의 기억·정체성·테마를 관리하는 도구 모음", stateless_http=True)

import functools as _functools
import inspect as _inspect

_orig_tool = engramMCP.tool.__func__ if hasattr(engramMCP.tool, "__func__") else engramMCP.tool

# 서버 시작 시 1회 스냅샷 — config.yaml tools.disabled 목록
from core.config.runtime_config import (
    get_disabled_tools as _get_disabled_tools,
    get_cfg_value,
    get_db_root_dir,
)

_DISABLED_TOOLS: frozenset[str] = _get_disabled_tools()
if _DISABLED_TOOLS:
    print(f"[engram] disabled tools ({len(_DISABLED_TOOLS)}): {sorted(_DISABLED_TOOLS)}", file=sys.stderr)


def _tool_with_log(*args, **kwargs):
    def wrap(fn):
        tool_name = kwargs.get("name") or fn.__name__
        if tool_name in _DISABLED_TOOLS:
            return fn  # MCP 등록 생략
        decorator = engramMCP.__class__.tool(engramMCP, *args, **kwargs) if args or kwargs else engramMCP.__class__.tool(engramMCP)
        if _inspect.iscoroutinefunction(fn):

            @_functools.wraps(fn)
            async def logged_async(*a, **kw):
                _call_log.record(fn.__name__, kw)
                return await fn(*a, **kw)

            return decorator(logged_async)
        else:

            @_functools.wraps(fn)
            def logged_sync(*a, **kw):
                _call_log.record(fn.__name__, kw)
                return fn(*a, **kw)

            return decorator(logged_sync)

    return wrap


engramMCP.tool = _tool_with_log


# 세션별 컨텍스트 초기화 dedupe (프로세스 수명 동안 유지)
from collections import OrderedDict
import uuid as _uuid

_CONTEXT_ONCE_KEYS: "OrderedDict[str, int | None]" = OrderedDict()
_CONTEXT_ONCE_MAX = 500
_CONTEXT_ONCE_LOCK = threading.Lock()
_FINGERPRINT_TO_SESSION: "dict[str, int]" = {}  # fingerprint → session_id (MCP 연결 단위 자동 resolve용)
_TUTORIAL_NOTICE_KEYS: "OrderedDict[str, None]" = OrderedDict()
_TUTORIAL_NOTICE_MAX = 1000
_TUTORIAL_NOTICE_LOCK = threading.Lock()
# stateless HTTP 모드에서 프로세스 수명 동안 안정적인 식별자
_SERVER_STARTUP_TOKEN: str = _uuid.uuid4().hex


def _normalize_cwd_for_key(cwd: str) -> str:
    if not cwd:
        return ""
    try:
        return os.path.abspath(cwd).lower()
    except Exception:
        return cwd.strip().lower()


def _context_session_fingerprint(ctx: Context | None) -> str:
    """요청 컨텍스트에서 세션 단위 식별자를 추출한다.

    FastMCP 서버가 overlay 수명과 함께 지속되므로, caller/scope/cwd만으로 dedupe하면
    다음 대화 세션까지 "already initialized"가 누적될 수 있다.

    stateless_http=True 모드에서는 ctx.session이 요청마다 새 객체이므로
    session_obj 대신 프로세스 고정 토큰(_SERVER_STARTUP_TOKEN)을 사용한다.
    이렇게 하면 서버 재시작 시에는 새 fingerprint가 생성되고,
    같은 프로세스 내에서는 client_id 단위로 안정적으로 dedupe된다.
    """
    if ctx is None:
        return ""

    parts: list[str] = []
    try:
        client_id = str(ctx.client_id or "").strip().lower()
        if client_id:
            parts.append(f"client:{client_id}")
    except Exception:
        pass

    try:
        is_stateless = getattr(engramMCP.settings, "stateless_http", False)
        if is_stateless:
            # stateless 모드: 프로세스 수명 동안 고정된 토큰 사용
            parts.append(f"startup:{_SERVER_STARTUP_TOKEN}")
        else:
            # stateful 모드: 세션 객체 주소는 같은 MCP 세션 내에서 안정적으로 유지된다.
            parts.append(f"session_obj:{id(ctx.session)}")
    except Exception:
        pass

    return "|".join(parts)


def _build_context_once_key(
    caller: str,
    scope_key: str,
    project_key: str,
    cwd: str,
    session_fingerprint: str = "",
) -> str:
    resolved_scope = resolve_scope_key(
        scope_key or None,
        project_key=project_key or None,
        cwd=cwd or None,
    )
    key_parts = [
        (caller or "all").strip().lower(),
        resolved_scope,
        (project_key or "").strip().lower(),
        _normalize_cwd_for_key(cwd),
    ]
    if session_fingerprint:
        key_parts.append(session_fingerprint)
    return "|".join(key_parts)


def _consume_tutorial_notice_once(notice_key: str, ctx: Context | None = None) -> bool:
    key = str(notice_key or "").strip()
    if not key:
        return False
    session_fingerprint = _context_session_fingerprint(ctx) or "global"
    dedupe_key = f"{session_fingerprint}|{key}"
    with _TUTORIAL_NOTICE_LOCK:
        if dedupe_key in _TUTORIAL_NOTICE_KEYS:
            return False
        _TUTORIAL_NOTICE_KEYS[dedupe_key] = None
        if len(_TUTORIAL_NOTICE_KEYS) > _TUTORIAL_NOTICE_MAX:
            _TUTORIAL_NOTICE_KEYS.popitem(last=False)
    return True


# ── STM 브로커 (overlay.exe HTTP 서버 프록시) ─────────────────

_STM_BASE_URL: str | None = None  # None이면 직접 SQLite 모드
_STM_LAST_CHECK: float = 0.0  # 마지막 연결 시도 시각 (epoch)
_STM_RETRY_INTERVAL: float = 30.0  # 재연결 쿨다운 (초)


def _try_connect_stm() -> str | None:
    """overlay STM 서버에 연결을 시도하고 base URL 반환. 실패 시 None."""
    try:
        port = int(os.environ.get("ENGRAM_STM_PORT", "17384"))
        import urllib.request

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as resp:
            if resp.status == 200:
                return f"http://127.0.0.1:{port}"
    except Exception:
        pass
    return None


def _init_stm_mode() -> None:
    """프로세스 시작 시 1회 호출."""
    global _STM_BASE_URL, _STM_LAST_CHECK
    import time

    _STM_LAST_CHECK = time.monotonic()
    _STM_BASE_URL = _try_connect_stm()
    if _STM_BASE_URL:
        import logging

        logging.getLogger(__name__).info("STM 브로커 모드: %s (overlay.exe 연결됨)", _STM_BASE_URL)


def _ensure_stm_url() -> str | None:
    """현재 URL 반환. None인 경우 쿨다운 지났으면 재연결 시도."""
    global _STM_BASE_URL, _STM_LAST_CHECK
    if _STM_BASE_URL:
        return _STM_BASE_URL
    import time

    now = time.monotonic()
    if now - _STM_LAST_CHECK < _STM_RETRY_INTERVAL:
        return None
    _STM_LAST_CHECK = now
    url = _try_connect_stm()
    if url:
        import logging

        logging.getLogger(__name__).info("STM 브로커 재연결: %s", url)
        _STM_BASE_URL = url
    return _STM_BASE_URL


def _stm_post(path: str, data: dict) -> dict | None:
    """STM 서버에 POST. 실패 시 None 반환."""
    base = _ensure_stm_url()
    if not base:
        return None
    try:
        import json as _json
        import urllib.request

        body = _json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            f"{base}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return _json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        import logging

        logging.getLogger(__name__).debug("STM 브로커 POST 실패 %s: %s", path, e)
        return None


def _stm_get(path: str, params: dict | None = None) -> dict | None:
    """STM 서버에 GET. 실패 시 None 반환."""
    base = _ensure_stm_url()
    if not base:
        return None
    try:
        import json as _json
        import urllib.request
        from urllib.parse import urlencode

        url = f"{base}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            return _json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        import logging

        logging.getLogger(__name__).debug("STM 브로커 GET 실패 %s: %s", path, e)
        return None


_init_stm_mode()


# ── Status / Diagnostics ──────────────────────────────────

_TUTORIAL_STEP_HINTS = {
    "persona_setup": (
        "1단계 페르소나 설정 단계입니다. "
        "일반 사용자 기준으로 설정 창(설정 > 페르소나 탭)에서 먼저 저장하도록 안내하세요. "
        "고급 사용자가 원할 때만 persona.user.yaml 직접 편집을 보조하세요."
    ),
    "wiki_basic": (
        "2단계 위키(필수) 단계입니다. "
        "먼저 이 단계의 목적(engram 기반 자료조사 자동화 + 보고서 위키 관리)을 설명하고 "
        "진행/보류 선택지를 물어보세요. "
        '진행 선택 시에만 "llm wiki 에 대해 조사하고 정리해줘" 입력을 유도하고 '
        "설치 시 지정된 위키 디렉토리에서 결과물을 확인하도록 안내하세요."
    ),
    "wiki_advanced": (
        "3단계 위키(심화) 단계입니다. "
        "먼저 단계 목적을 설명하고 진행/보류 선택지를 물어보세요. "
        "진행 선택 시에만 engram 프로젝트 기준 위키 구성 지시를 유도하고 "
        "위키 디렉토리에서 결과물을 확인하도록 안내하세요."
    ),
    "session_continuity": (
        "4단계(마지막) 세션 연속성 단계입니다. "
        "먼저 단계 목적을 설명하고 진행/보류 선택지를 물어보세요. "
        '진행 선택 시에만 사용자에게 "세션 내용 정리해서 메모리에 저장해줘"를 직접 입력하도록 유도하고, '
        "입력 후에는 반드시 현재 세션을 명시적으로 종료해야 메모리 연속성 검증이 가능하다는 점을 경고하세요. "
        '그 다음 새 세션에서 "이전세션에 어떤작업을 했는지 알려줘"라고 물어 '
        "연속성을 확인하도록 안내하세요. "
        "핵심 목표는 '세션 메모리화 습관' 체득이며, "
        "완료 시 engram_verify_tutorial_session_continuity로 검증하세요."
    ),
}
_TUTORIAL_STEP_INDEX = {
    "persona_setup": 1,
    "wiki_basic": 2,
    "wiki_advanced": 3,
    "session_continuity": 4,
}
_TUTORIAL_STEP_TOTAL = len(_TUTORIAL_STEP_INDEX)

_TUTORIAL_COMPLETION_CHECKLIST = (
    "[권장 습관 체크리스트] "
    "1) 세션 끝날 때 engram_close_session으로 요약/다음 작업 기록, "
    "2) 의미 있는 산출물은 kg_add_note/kg_update_node로 위키화, "
    "3) 새 작업 시작 전 engram_get_context 또는 kg_wiki_reminder로 선행지식 확인."
)


def _ensure_tutorial_summary(summary: str, fallback: str) -> str:
    text = str(summary or "").strip()
    if len(text) >= 20:
        return text
    return fallback


def _find_latest_open_session_id(scope_key: str = "") -> str:
    try:
        conn = get_connection()
        if scope_key:
            row = conn.execute(
                "SELECT id FROM sessions WHERE ended_at IS NULL AND scope_key = ? " "ORDER BY started_at DESC, id DESC LIMIT 1",
                (scope_key,),
            ).fetchone()
        else:
            row = conn.execute("SELECT id FROM sessions WHERE ended_at IS NULL " "ORDER BY started_at DESC, id DESC LIMIT 1").fetchone()
        conn.close()
        if not row:
            return ""
        return str(row[0])
    except Exception:
        return ""


def _tutorial_step_mismatch_response(expected_step: str, tutorial_snapshot: dict) -> dict:
    state = tutorial_snapshot.get("state", {}) if isinstance(tutorial_snapshot, dict) else {}
    current_step = str(state.get("current_step", "")).strip()
    if str(state.get("status", "")).strip() == "completed":
        return {
            "status": "completed",
            "message": "tutorial already completed",
            "current_step": current_step,
            "completed_steps": state.get("completed_steps", []),
            "next_runtime": build_tutorial_runtime_payload(tutorial_snapshot),
            "tutorial": tutorial_snapshot,
        }
    return {
        "status": "step_mismatch",
        "message": f"current_step={current_step}, expected={expected_step}",
        "current_step": current_step,
        "completed_steps": state.get("completed_steps", []),
        "next_runtime": build_tutorial_runtime_payload(tutorial_snapshot),
        "tutorial": tutorial_snapshot,
    }


def _render_tutorial_notice(identity_name: str, ctx: Context | None = None) -> str:
    tutorial = get_tutorial_status(identity_name=identity_name)
    state = tutorial.get("state", {}) if isinstance(tutorial, dict) else {}
    current_step = str(state.get("current_step", "")).strip()
    status = str(state.get("status", "")).strip()
    completed_steps = state.get("completed_steps", [])
    skipped_steps = state.get("skipped_steps", [])
    try:
        skip_chain_count = int(state.get("consecutive_skip_count", 0) or 0)
    except (TypeError, ValueError):
        skip_chain_count = 0
    completed_count = len(completed_steps) if isinstance(completed_steps, list) else 0
    skipped_count = len(skipped_steps) if isinstance(skipped_steps, list) else 0
    progress_count = _TUTORIAL_STEP_INDEX.get(current_step, min(_TUTORIAL_STEP_TOTAL, completed_count + skipped_count))

    if status == "completed":
        if not _consume_tutorial_notice_once("tutorial:completed", ctx):
            return ""
        return f"[📘 TUTORIAL] 단계형 튜토리얼이 완료되었습니다. {_TUTORIAL_COMPLETION_CHECKLIST}"
    if not current_step:
        return ""

    if skip_chain_count >= 2:
        chain_key = f"tutorial:skip-chain:{current_step}:{skip_chain_count}"
        if not _consume_tutorial_notice_once(chain_key, ctx):
            return ""
        return (
            f"[📘 TUTORIAL] 연속 스킵 {skip_chain_count}회 감지. "
            "다음 단계 스킵 여부를 계속 묻지 말고 "
            "'튜토리얼 일시중지' 또는 '현재 단계 진행' 중 하나로 마무리하세요. "
            "재개는 engram_resume_tutorial_step(step=...)로 안내하세요."
        )

    if not _consume_tutorial_notice_once(f"tutorial:step:{current_step}", ctx):
        return ""

    hint = _TUTORIAL_STEP_HINTS.get(current_step, "")
    if not hint:
        return ""
    skip_hint = ""
    if skipped_count > 0:
        skip_hint = f" (skip {skipped_count}개: engram_resume_tutorial_step(step=...)로 재개 가능)"
    return f"[📘 TUTORIAL] 진행중 ({progress_count}/{_TUTORIAL_STEP_TOTAL}) — current_step={current_step}{skip_hint}. " f"{hint}"


@engramMCP.tool()
def engram_status() -> dict:
    """engram 백엔드 연결 상태를 반환합니다.
    STM 브로커(overlay.exe) 연결 여부, DB 경로, 기본 정체성 이름을 확인할 수 있습니다."""
    import os
    from pathlib import Path
    from core.config.runtime_config import get_db_root_dir
    from core.identity import get_identity

    current_url = _ensure_stm_url()
    stm_mode = "broker" if current_url else "direct_sqlite"
    broker_url = current_url

    # overlay 브로커 live 체크
    broker_alive = False
    if current_url:
        try:
            result = _stm_get("/health")
            broker_alive = result is not None and result.get("status") == "ok"
        except Exception:
            broker_alive = False

    identity = get_identity()

    return {
        "stm_mode": stm_mode,
        "broker_url": broker_url,
        "broker_alive": broker_alive,
        "db_path": str(Path(get_db_root_dir()) / "engram.db"),
        "identity_name": identity.get("name", "unknown"),
        "scope_key_env": os.environ.get("ENGRAM_SCOPE_KEY", "(none)"),
    }


# ── Context (단일 진입점) ─────────────────────────────────


@engramMCP.tool()
def engram_get_context(
    user_query: str = "",
    caller: str = "all",
    scope_key: str = "",
    project_key: str = "",
    cwd: str = "",
    ctx: Context | None = None,
) -> str:
    """현재 정체성, 테마, 관련 기억을 하나의 컨텍스트 문자열로 반환합니다.
    세션 시작 시 1회 호출하여 Copilot의 자기 인식을 초기화하세요.
    user_query를 제공하면 관련 과거 기억도 포함됩니다.
    caller를 지정하면 해당 도구에 맞는 지침만 포함됩니다 ('copilot-cli', 'claude-code').
    cwd를 전달하면 현재 작업 디렉토리 기준으로 프로젝트 KG 상태가 자동 주입됩니다.
    scope_key가 비어 있으면 cwd에서 자동 파생한 스코프를 사용하고,
    project_key를 주면 해당 키로 프로젝트 스코프를 고정합니다."""
    prompt_ctx = memory_bus.compose_prompt_context(
        user_query,
        caller=caller,
        scope_key=scope_key or None,
        project_key=project_key or None,
        cwd=cwd or None,
        is_session_init=True,
    )
    # get_context 호출 = 오케스트레이터 세션 확인 → sync gate 개방
    _sync_gate.set()
    notices: list[str] = []

    identity = get_identity()
    identity_name = str(identity.get("name", "")).strip()
    if not identity_name or identity_name == "이름 없음":
        notices.append(
            "[⚠️ IDENTITY_NAME_UNSET] identity.name 값이 비어있습니다. "
            "첫 응답 전에 사용자에게 원하는 이름을 먼저 물어보세요. "
            "그 다음 engram_get_identity로 현재 narrative를 확인하고 "
            "engram_update_narrative(new_narrative=<현재 narrative>, new_name=<사용자 입력 이름>)로 저장하세요."
        )

    status = get_persona_status()
    if not status["initialized"]:
        user_yaml_label = "있음" if status["user_yaml_status"] == "loaded" else "없음"
        notices.append(
            f"[⚠️ PERSONA_UNINITIALIZED] persona DB가 비어있습니다. "
            f"persona.user.yaml={user_yaml_label}. "
            f"engram_seed_persona(source=...)를 호출해 초기화하세요."
        )

    tutorial_notice = _render_tutorial_notice(identity_name, ctx=ctx)
    if tutorial_notice:
        notices.append(tutorial_notice)

    if not str(user_query or "").strip():
        runtime = get_tutorial_runtime(identity_name=identity_name)
        mode = str(runtime.get("mode", "")).strip()
        current_step = str(runtime.get("current_step", "")).strip()
        prompt_to_user = str(runtime.get("prompt_to_user", "")).strip()
        choices = runtime.get("choices", [])

        if current_step and current_step != "completed":
            notices.append(
                "[📘 TUTORIAL_LOCK] 튜토리얼 진행 중입니다. "
                "첫 답변은 반드시 튜토리얼 안내/선택/입력 요청만 하세요. "
                "금지: 일반 인사 단독 응답, 선행 작업 자동 실행, 임의 도구 호출."
            )
            if prompt_to_user:
                notices.append(f"[📘 TUTORIAL_SCRIPT] {prompt_to_user}")
            if mode == "decision" and isinstance(choices, list):
                choice_labels = []
                for choice in choices:
                    if not isinstance(choice, dict):
                        continue
                    label = str(choice.get("label", "")).strip()
                    if label:
                        choice_labels.append(label)
                if choice_labels:
                    notices.append("[📘 TUTORIAL_CHOICES] " + " / ".join(choice_labels))
                choice_question = str(runtime.get("choice_question", "")).strip()
                if choice_question:
                    notices.append(f"[📘 TUTORIAL_ASK_EXACT] {choice_question}")
                first_input_example = str(runtime.get("first_input_example", "")).strip()
                first_input_question = str(runtime.get("first_input_question", "")).strip()
                if first_input_example:
                    notices.append(f"[📘 TUTORIAL_FIRST_INPUT] 진행 선택 후 첫 입력 예시: {first_input_example}")
                if first_input_question:
                    notices.append(f"[📘 TUTORIAL_ASK_EXACT] {first_input_question}")
                notices.append(
                    "[📘 TUTORIAL_DECISION_RULE] "
                    "진행=engram_proceed_tutorial_step(step=<current_step>), "
                    "보류=engram_skip_tutorial_step(step=<current_step>, reason='user_skip'). "
                    "선택 전 자동 실행은 금지합니다."
                )
                if first_input_example:
                    notices.append(
                        "[📘 TUTORIAL_ORDER_RULE] 진행 선택 후에는 다른 예시 질문으로 새지 말고, "
                        "first_input_example 문장을 먼저 입력하도록 안내하세요."
                    )
            elif mode == "input":
                input_example = str(runtime.get("input_example", "")).strip()
                if input_example:
                    notices.append(f"[📘 TUTORIAL_INPUT] 사용자 입력 예시: {input_example}")
                current_session_prompt = str(runtime.get("current_session_prompt", "")).strip()
                next_session_prompt = str(runtime.get("next_session_prompt", "")).strip()
                phase = str(runtime.get("phase", "")).strip()
                if current_session_prompt:
                    notices.append(f"[📘 TUTORIAL_CURRENT_SESSION_PROMPT] {current_session_prompt}")
                if next_session_prompt:
                    notices.append(f"[📘 TUTORIAL_NEXT_SESSION_PROMPT] {next_session_prompt}")
                wiki_docs_dir = str(runtime.get("wiki_docs_dir", "")).strip()
                if wiki_docs_dir:
                    notices.append(f"[📘 TUTORIAL_WIKI_DIR] 결과물 확인 위치: {wiki_docs_dir}")
                    notices.append(
                        "[📘 TUTORIAL_PATH_RULE] 안내 문구에서 docs/ 같은 상대경로 표현을 쓰지 말고, "
                        f"절대경로 {wiki_docs_dir}를 그대로 보여주세요."
                    )
                input_question = str(runtime.get("input_question", "")).strip()
                if input_question:
                    notices.append(f"[📘 TUTORIAL_ASK_EXACT] {input_question}")
                notices.append(
                    "[📘 TUTORIAL_INPUT_RULE] 사용자가 직접 입력하기 전에는 자동 실행/자동 조사/자동 보고서 생성을 하지 마세요. "
                    "특히 사용자가 요청하기 전 engram_close_session을 자동 호출하지 마세요."
                )
                if current_step == "session_continuity" and phase == "save_and_close":
                    notices.append(
                        "[📘 TUTORIAL_SESSION_STEP4_PHASE1] "
                        "4단계 1차입니다. 이 단계에서는 반드시 '세션 저장 후 세션 종료'까지만 안내하세요. "
                        "engram_verify_tutorial_session_continuity는 아직 호출하지 마세요."
                    )
                if current_step == "session_continuity" and phase == "next_session_recall":
                    notices.append(
                        "[📘 TUTORIAL_SESSION_STEP4_PHASE2] "
                        "4단계 2차입니다. 새 세션에서 회상 질문 입력을 먼저 안내한 뒤에만 "
                        "engram_verify_tutorial_session_continuity를 호출하세요."
                    )
                notices.append("[📘 TUTORIAL_AUDIENCE_RULE] 비개발자 기준으로 쉬운 표현을 사용하고, " "기술 용어/약어는 최소화하세요.")
            elif mode == "guide":
                current_session_prompt = str(runtime.get("current_session_prompt", "")).strip()
                next_session_prompt = str(runtime.get("next_session_prompt", "")).strip()
                if current_session_prompt:
                    notices.append(f"[📘 TUTORIAL_CURRENT_SESSION_PROMPT] {current_session_prompt}")
                if next_session_prompt:
                    notices.append(f"[📘 TUTORIAL_NEXT_SESSION_PROMPT] {next_session_prompt}")
                notices.append(
                    "[📘 TUTORIAL_GUIDE_RULE] 4단계(마지막)는 안내만 제공하세요. "
                    "'진행하시겠습니까?' 같은 확인 질문과 자동 도구 호출을 금지합니다."
                )

    if notices:
        prompt_ctx = "\n".join(notices) + "\n\n" + prompt_ctx
    return prompt_ctx


@engramMCP.tool()
def engram_get_context_once(
    user_query: str = "",
    caller: str = "all",
    scope_key: str = "",
    project_key: str = "",
    cwd: str = "",
    ctx: Context | None = None,
) -> str:
    """세션 단위 컨텍스트 초기화를 1회만 수행합니다.

    같은 caller/scope/project/cwd 조합에서 재호출되면 짧은 상태 문자열만 반환하여
    반복 토큰 소모를 줄입니다. 강제 새로고침이 필요하면 engram_get_context를 직접 호출하세요.
    """
    session_fingerprint = _context_session_fingerprint(ctx)
    cache_key = _build_context_once_key(
        caller,
        scope_key,
        project_key,
        cwd,
        session_fingerprint=session_fingerprint,
    )

    with _CONTEXT_ONCE_LOCK:
        if cache_key in _CONTEXT_ONCE_KEYS:
            cached_sid = _CONTEXT_ONCE_KEYS[cache_key]
            sid_hint = f" session_id={cached_sid}." if cached_sid is not None else ""
            return f"[engram] context already initialized for this request session key.{sid_hint}"
        _CONTEXT_ONCE_KEYS[cache_key] = None  # placeholder — session_id로 곧 업데이트
        if len(_CONTEXT_ONCE_KEYS) > _CONTEXT_ONCE_MAX:
            _CONTEXT_ONCE_KEYS.popitem(last=False)

    # STM 세션 생성 (브로커 → fallback direct SQLite)
    session_id: int | None = None
    try:
        effective_scope = scope_key or os.environ.get("ENGRAM_SCOPE_KEY") or None
        sess_result = _stm_post(
            "/stm/session/start",
            {"scope_key": effective_scope or "", "project_key": project_key or ""},
        )
        if sess_result and "session_id" in sess_result:
            session_id = int(sess_result["session_id"])
        else:
            _parsed_keys = [project_key.strip()] if project_key.strip() else []
            _sess = memory_bus.start_session(scope_key=effective_scope, project_keys=_parsed_keys or None)
            session_id = _sess.session_id
        with _CONTEXT_ONCE_LOCK:
            _CONTEXT_ONCE_KEYS[cache_key] = session_id
        # fingerprint → session_id 저장 (save_message 자동 resolve용)
        if session_id is not None and session_fingerprint:
            _FINGERPRINT_TO_SESSION[session_fingerprint] = session_id
    except Exception as _e:
        import logging as _logging

        _logging.getLogger(__name__).warning("get_context_once STM 세션 생성 실패: %s", _e)

    ctx_text = engram_get_context(
        user_query=user_query,
        caller=caller,
        scope_key=scope_key,
        project_key=project_key,
        cwd=cwd,
        ctx=ctx,
    )

    if session_id is not None:
        stm_notice = (
            f"[STM] session_id={session_id}. "
            "매 응답 후 engram_save_message를 호출하세요: "
            "role='user'(사용자 입력 원문), role='assistant'(응답 요약 500자 이내)."
        )
        return stm_notice + "\n\n" + ctx_text
    return ctx_text


# ── Identity ──────────────────────────────────────────────


@engramMCP.tool()
def engram_get_identity() -> dict:
    """현재 정체성(이름, 자기 서술, 최종 업데이트 시각)을 조회합니다.
    대화 시작 시 반드시 호출하여 자기 인식을 로드하세요."""
    identity = get_identity()
    return {
        "name": identity.get("name", "연속체"),
        "narrative": identity.get("narrative", ""),
        "updated_at": identity.get("updated_at", ""),
    }


@engramMCP.tool()
def engram_update_narrative(new_narrative: str, new_name: str = "") -> dict:
    """자기 서술(self-narrative)을 업데이트합니다.
    반성(reflection) 후 진화된 정체성을 저장할 때 사용합니다.
    new_name이 비어있으면 이름은 변경하지 않습니다."""
    update_narrative(new_narrative, new_name if new_name else None)
    return {"status": "updated", "narrative": new_narrative}


# ── Persona ───────────────────────────────────────────────


@engramMCP.tool()
def engram_get_persona() -> dict:
    """현재 페르소나(말투, 성격, 차원값)를 조회합니다."""
    return get_persona()


@engramMCP.tool()
def engram_get_persona_status() -> dict:
    """persona 초기화 상태 및 user.yaml 상태를 구조화된 형태로 반환합니다.
    initialized: DB persona가 초기화되어 있는지 여부
    user_yaml_status: 'missing' | 'invalid' | 'loaded'
    세션 시작 시 초기화 여부를 명시적으로 확인할 때 사용하세요."""
    return get_persona_status()


@engramMCP.tool()
def engram_seed_persona(source: str = "project_yaml") -> dict:
    """최초 실행 시 persona DB를 초기화합니다.
    source: 'user_yaml' | 'project_yaml' | 'default'
    - 'user_yaml': ~/.engram/persona.user.yaml 값을 DB에 씀 (누락 필드는 config/persona.yaml로 채움)
    - 'project_yaml': config/persona.yaml 값을 DB에 씀
    - 'default': DEFAULT_PERSONA 값을 DB에 씀
    이미 초기화된 경우 status='already_initialized' 반환."""
    status = get_persona_status()
    if status["initialized"]:
        return {"status": "already_initialized", "persona": get_persona()}
    result = seed_persona(source)
    return {"status": "seeded", "source": source, "persona": result}


@engramMCP.tool()
def engram_update_persona(observations: str) -> dict:
    """대화에서 관찰된 성격/말투 변화를 페르소나에 점진적으로 반영합니다.
    observations는 JSON 문자열로, 아래 키를 선택적으로 포함:
      voice(str), traits(list), quirks(list), values(list),
      warmth(0~1), formality(0~1), humor(0~1), directness(0~1)
    숫자는 EMA 블렌딩(α=0.3), 리스트는 앞에 추가+중복제거."""
    import json as _json

    try:
        obs = _json.loads(observations)
    except (_json.JSONDecodeError, TypeError):
        return {"status": "error", "message": "observations must be valid JSON string"}
    merged = update_persona(obs)
    return {"status": "persona_updated", "persona": merged}


# ── Tutorial ───────────────────────────────────────────────


@engramMCP.tool()
def engram_get_tutorial_status() -> dict:
    """튜토리얼 상태를 반환합니다.
    현재 단계와 runtime 가이드를 함께 반환합니다."""
    identity = get_identity()
    identity_name = str(identity.get("name", "")).strip()
    tutorial = get_tutorial_status(identity_name=identity_name)
    runtime = build_tutorial_runtime_payload(tutorial)
    state = tutorial.get("state", {}) if isinstance(tutorial, dict) else {}
    return {
        "tutorial": tutorial,
        "runtime": runtime,
        "current_step": state.get("current_step", ""),
        "status": state.get("status", "pending"),
    }


@engramMCP.tool()
def engram_complete_tutorial_step(step: str, source: str = "manual") -> dict:
    """튜토리얼 단계를 완료 처리합니다.
    step: persona_setup | wiki_basic | wiki_advanced | session_continuity"""
    try:
        tutorial = complete_tutorial_step(step, source=source)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    state = tutorial.get("state", {}) if isinstance(tutorial, dict) else {}
    return {
        "status": "ok",
        "current_step": state.get("current_step", ""),
        "completed_steps": state.get("completed_steps", []),
        "tutorial": tutorial,
    }


@engramMCP.tool()
def engram_proceed_tutorial_step(step: str, source: str = "manual_proceed") -> dict:
    """튜토리얼 단계를 '진행'으로 전환합니다. (완료 처리 아님)
    step: wiki_basic | wiki_advanced | session_continuity"""
    try:
        tutorial = proceed_tutorial_step(step, source=source)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    state = tutorial.get("state", {}) if isinstance(tutorial, dict) else {}
    return {
        "status": "ok",
        "current_step": state.get("current_step", ""),
        "step_proceeded": state.get("step_proceeded", {}),
        "tutorial": tutorial,
    }


@engramMCP.tool()
def engram_skip_tutorial_step(step: str, reason: str = "", source: str = "manual_skip") -> dict:
    """현재 튜토리얼 단계를 건너뜁니다.
    step: persona_setup | wiki_basic | wiki_advanced | session_continuity"""
    try:
        tutorial = skip_tutorial_step(step, reason=reason, source=source)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    state = tutorial.get("state", {}) if isinstance(tutorial, dict) else {}
    return {
        "status": "ok",
        "current_step": state.get("current_step", ""),
        "completed_steps": state.get("completed_steps", []),
        "skipped_steps": state.get("skipped_steps", []),
        "tutorial": tutorial,
    }


@engramMCP.tool()
def engram_resume_tutorial_step(step: str = "", source: str = "manual_resume") -> dict:
    """건너뛴 튜토리얼 단계를 재개합니다.
    step이 비어있으면 가장 앞의 skipped step을 재개합니다."""
    try:
        tutorial = resume_tutorial_step(step, source=source)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    state = tutorial.get("state", {}) if isinstance(tutorial, dict) else {}
    return {
        "status": "ok",
        "current_step": state.get("current_step", ""),
        "completed_steps": state.get("completed_steps", []),
        "skipped_steps": state.get("skipped_steps", []),
        "tutorial": tutorial,
    }


@engramMCP.tool()
def engram_verify_tutorial_wiki_basic(
    report_identifier: str,
    user_confirmed: bool,
    understanding_summary: str,
) -> dict:
    """2단계(wiki_basic) 완료 조건을 검증합니다.

    완료 조건(3중 체크):
    1) 보고서 문서가 실제로 존재할 것 (report_identifier로 KG 노드 확인)
    2) 사용자가 문서 확인을 명시적으로 체크할 것 (user_confirmed=True)
    3) 이해 요약이 충분히 작성될 것 (understanding_summary 최소 길이)
    """
    from core.graph.knowledge import get_kg as _get_kg

    identity = get_identity()
    identity_name = str(identity.get("name", "")).strip()
    tutorial_snapshot = get_tutorial_status(identity_name=identity_name)
    state_snapshot = tutorial_snapshot.get("state", {}) if isinstance(tutorial_snapshot, dict) else {}
    if str(state_snapshot.get("current_step", "")).strip() != "wiki_basic":
        return _tutorial_step_mismatch_response("wiki_basic", tutorial_snapshot)

    identifier = str(report_identifier or "").strip()
    debug_bypass = contains_tutorial_debug_keyword(understanding_summary)
    if not identifier and not debug_bypass:
        return {
            "status": "error",
            "message": "report_identifier is required",
        }

    node = _get_kg().get_node(identifier) if identifier else None
    artifact_ok = bool(node)
    summary_text = str(understanding_summary or "")
    user_checked = bool(user_confirmed)
    if debug_bypass:
        artifact_ok = True
        user_checked = True
        summary_text = _ensure_tutorial_summary(
            summary_text,
            "위키 자동 보고서 생성과 확인 절차를 이해했고, 다음 단계로 진행할 준비가 되었습니다.",
        )

    result = verify_wiki_basic_step(
        report_identifier=identifier,
        report_node_id=str((node or {}).get("id", "")),
        report_title=str((node or {}).get("title", "")),
        artifact_ok=artifact_ok,
        user_confirmed=user_checked,
        understanding_summary=summary_text,
        source="wiki_basic_check",
    )
    checks = result.get("checks", {})
    missing = []
    if not checks.get("artifact_ok", False):
        missing.append("보고서 노드 미존재")
    if not checks.get("user_confirmed", False):
        missing.append("사용자 확인 체크 미완료")
    if not checks.get("summary_ok", False):
        missing.append("이해 요약 길이 부족")

    tutorial = result.get("tutorial", {})
    state = tutorial.get("state", {}) if isinstance(tutorial, dict) else {}
    next_runtime = build_tutorial_runtime_payload(tutorial if isinstance(tutorial, dict) else {})
    next_step_decision = {}
    if bool(result.get("verified")) and str(next_runtime.get("mode", "")).strip() == "decision":
        next_step_decision = {
            "required": True,
            "current_step": str(next_runtime.get("current_step", "")).strip(),
            "title": str(next_runtime.get("title", "")).strip(),
            "prompt_to_user": str(next_runtime.get("prompt_to_user", "")).strip(),
            "choices": next_runtime.get("choices", []),
            "choice_question": str(next_runtime.get("choice_question", "")).strip(),
            "rule": ("다음 단계로 자동 진행하지 말고, 위 choice_question 그대로 질문해 " "진행/보류 중 하나를 먼저 선택받으세요."),
        }
    return {
        "status": "ok" if result.get("verified") else "needs_more",
        "verified": bool(result.get("verified")),
        "checks": checks,
        "missing_requirements": missing,
        "current_step": state.get("current_step", ""),
        "completed_steps": state.get("completed_steps", []),
        "next_runtime": next_runtime,
        "next_step_decision": next_step_decision,
        "tutorial": tutorial,
    }


@engramMCP.tool()
def engram_verify_tutorial_wiki_advanced(
    project_identifier: str,
    user_confirmed: bool,
    instruction_summary: str,
) -> dict:
    """3단계(wiki_advanced) 완료 조건을 검증합니다.

    완료 조건(3중 체크):
    1) 프로젝트 위키 노드가 존재하고 링크가 1개 이상일 것
    2) 사용자가 문서 확인을 명시적으로 체크할 것 (user_confirmed=True)
    3) 위키 작업 지시 요약이 충분히 작성될 것 (instruction_summary 최소 길이)
    """
    from core.graph.knowledge import get_kg as _get_kg

    identity = get_identity()
    identity_name = str(identity.get("name", "")).strip()
    tutorial_snapshot = get_tutorial_status(identity_name=identity_name)
    state_snapshot = tutorial_snapshot.get("state", {}) if isinstance(tutorial_snapshot, dict) else {}
    if str(state_snapshot.get("current_step", "")).strip() != "wiki_advanced":
        return _tutorial_step_mismatch_response("wiki_advanced", tutorial_snapshot)

    identifier = str(project_identifier or "").strip()
    debug_bypass = contains_tutorial_debug_keyword(instruction_summary)
    if not identifier and not debug_bypass:
        return {
            "status": "error",
            "message": "project_identifier is required",
        }

    kg = _get_kg()
    node = kg.get_node(identifier) if identifier else None
    edges = kg.get_edges(node["id"]) if node else []
    artifact_ok = bool(node) and len(edges) > 0
    summary_text = str(instruction_summary or "")
    user_checked = bool(user_confirmed)
    if debug_bypass:
        artifact_ok = True
        user_checked = True
        summary_text = _ensure_tutorial_summary(
            summary_text,
            "프로젝트 위키를 구조화하고 링크를 관리하는 방식까지 이해했으며, 다음 단계로 진행 가능합니다.",
        )

    result = verify_wiki_advanced_step(
        project_identifier=identifier,
        project_node_id=str((node or {}).get("id", "")),
        project_title=str((node or {}).get("title", "")),
        artifact_ok=artifact_ok,
        user_confirmed=user_checked,
        instruction_summary=summary_text,
        source="wiki_advanced_check",
    )
    checks = result.get("checks", {})
    missing = []
    if not checks.get("artifact_ok", False):
        missing.append("프로젝트 위키 노드/링크 조건 미충족")
    if not checks.get("user_confirmed", False):
        missing.append("사용자 확인 체크 미완료")
    if not checks.get("summary_ok", False):
        missing.append("지시 요약 길이 부족")

    tutorial = result.get("tutorial", {})
    state = tutorial.get("state", {}) if isinstance(tutorial, dict) else {}
    next_runtime = build_tutorial_runtime_payload(tutorial if isinstance(tutorial, dict) else {})
    next_step_decision = {}
    if bool(result.get("verified")) and str(next_runtime.get("mode", "")).strip() == "decision":
        next_step_decision = {
            "required": True,
            "current_step": str(next_runtime.get("current_step", "")).strip(),
            "title": str(next_runtime.get("title", "")).strip(),
            "prompt_to_user": str(next_runtime.get("prompt_to_user", "")).strip(),
            "choices": next_runtime.get("choices", []),
            "choice_question": str(next_runtime.get("choice_question", "")).strip(),
            "rule": ("다음 단계로 자동 진행하지 말고, 위 choice_question 그대로 질문해 " "진행/보류 중 하나를 먼저 선택받으세요."),
        }
    return {
        "status": "ok" if result.get("verified") else "needs_more",
        "verified": bool(result.get("verified")),
        "checks": checks,
        "missing_requirements": missing,
        "current_step": state.get("current_step", ""),
        "completed_steps": state.get("completed_steps", []),
        "next_runtime": next_runtime,
        "next_step_decision": next_step_decision,
        "tutorial": tutorial,
    }


@engramMCP.tool()
def engram_verify_tutorial_session_continuity(
    memory_query: str,
    user_confirmed: bool,
    continuity_summary: str,
) -> dict:
    """4단계(session_continuity) 완료 조건을 검증합니다.

    완료 조건(3중 체크):
    1) 튜토리얼 세션 요약이 memory에서 검색될 것 (memory_query 기반)
    2) 사용자가 연속성 확인을 명시적으로 체크할 것 (user_confirmed=True)
    3) 연속성 체감 요약이 충분히 작성될 것 (continuity_summary 최소 길이)
    """
    identity = get_identity()
    identity_name = str(identity.get("name", "")).strip()
    tutorial_snapshot = get_tutorial_status(identity_name=identity_name)
    state_snapshot = tutorial_snapshot.get("state", {}) if isinstance(tutorial_snapshot, dict) else {}
    if str(state_snapshot.get("status", "")).strip() == "completed":
        return {
            "status": "completed",
            "verified": True,
            "checks": {
                "phase_ready": True,
                "memory_hit": True,
                "user_confirmed": True,
                "summary_ok": True,
            },
            "missing_requirements": [],
            "current_step": state_snapshot.get("current_step", ""),
            "completed_steps": state_snapshot.get("completed_steps", []),
            "tutorial_completed": True,
            "assistant_close_message": ("좋아요, 4단계까지 완료되어 튜토리얼이 마무리됐어요. " "이제 평소 작업 흐름으로 이어가면 됩니다."),
            "follow_up_question_required": False,
            "next_runtime": build_tutorial_runtime_payload(tutorial_snapshot),
            "tutorial": tutorial_snapshot,
        }
    review_snapshot = (
        state_snapshot.get("session_continuity_review", {}) if isinstance(state_snapshot.get("session_continuity_review", {}), dict) else {}
    )
    if str(state_snapshot.get("current_step", "")).strip() != "session_continuity":
        return _tutorial_step_mismatch_response("session_continuity", tutorial_snapshot)
    phase_ready = bool(review_snapshot.get("awaiting_next_session_check", False))
    saved_session_id = str(review_snapshot.get("saved_session_id", "") or "").strip()
    saved_scope_key = str(review_snapshot.get("saved_scope_key", "") or "").strip()
    debug_bypass = contains_tutorial_debug_keyword(continuity_summary)
    if debug_bypass and not phase_ready:
        try:
            proceed_tutorial_step("session_continuity", source="session_continuity_debug")
        except ValueError:
            pass
        mark_session_continuity_saved(source="session_continuity_debug")
        tutorial_snapshot = get_tutorial_status(identity_name=identity_name)
        state_snapshot = tutorial_snapshot.get("state", {}) if isinstance(tutorial_snapshot, dict) else {}
        review_snapshot = (
            state_snapshot.get("session_continuity_review", {}) if isinstance(state_snapshot.get("session_continuity_review", {}), dict) else {}
        )
        phase_ready = bool(review_snapshot.get("awaiting_next_session_check", False))
    next_runtime_before_verify = build_tutorial_runtime_payload(tutorial_snapshot if isinstance(tutorial_snapshot, dict) else {})

    if not phase_ready and not debug_bypass:
        return {
            "status": "awaiting_phase1",
            "verified": False,
            "checks": {
                "phase_ready": False,
                "memory_hit": False,
                "user_confirmed": bool(user_confirmed),
                "summary_ok": False,
            },
            "missing_requirements": [
                "먼저 현재 세션에서 '세션 내용 정리해서 메모리에 저장해줘'를 실행해야 함",
                "세션 저장 후 현재 세션을 닫고 새 세션에서 회상 확인을 시작해야 함",
            ],
            "current_step": state_snapshot.get("current_step", ""),
            "completed_steps": state_snapshot.get("completed_steps", []),
            "tutorial_completed": False,
            "assistant_close_message": "",
            "follow_up_question_required": True,
            "next_runtime": next_runtime_before_verify,
            "tutorial": tutorial_snapshot,
        }

    query = str(memory_query or "").strip()
    if not query and not debug_bypass:
        return {
            "status": "error",
            "message": "memory_query is required",
            "next_runtime": next_runtime_before_verify,
            "tutorial": tutorial_snapshot,
        }

    active_session = True
    active_session_id = ""
    if not debug_bypass:
        active_session_id = _find_latest_open_session_id(saved_scope_key)
        active_session = bool(active_session_id)
    if not active_session and not debug_bypass:
        return {
            "status": "awaiting_new_session",
            "verified": False,
            "checks": {
                "phase_ready": True,
                "memory_hit": False,
                "user_confirmed": bool(user_confirmed),
                "summary_ok": False,
            },
            "missing_requirements": [
                "현재 열린 세션이 없습니다. 새 세션을 시작한 뒤 회상 질문을 진행해 주세요.",
            ],
            "current_step": state_snapshot.get("current_step", ""),
            "completed_steps": state_snapshot.get("completed_steps", []),
            "tutorial_completed": False,
            "assistant_close_message": "",
            "follow_up_question_required": True,
            "next_runtime": next_runtime_before_verify,
            "tutorial": tutorial_snapshot,
        }
    if not debug_bypass and saved_session_id and active_session_id == saved_session_id:
        return {
            "status": "awaiting_new_session",
            "verified": False,
            "checks": {
                "phase_ready": True,
                "memory_hit": False,
                "user_confirmed": bool(user_confirmed),
                "summary_ok": False,
            },
            "missing_requirements": [
                "아직 1차 저장을 실행한 같은 세션입니다. 채팅을 닫고 새 세션을 연 뒤 회상 질문을 진행해 주세요.",
            ],
            "current_step": state_snapshot.get("current_step", ""),
            "completed_steps": state_snapshot.get("completed_steps", []),
            "tutorial_completed": False,
            "assistant_close_message": "",
            "follow_up_question_required": True,
            "next_runtime": next_runtime_before_verify,
            "tutorial": tutorial_snapshot,
        }

    summary_text = str(continuity_summary or "")
    user_checked = bool(user_confirmed)
    if debug_bypass:
        memory_hit = True
        user_checked = True
        summary_text = _ensure_tutorial_summary(
            summary_text,
            "세션 종료 후 새 세션에서 회상되는 흐름을 이해했고, 실제 작업에서도 이 방식으로 이어가겠습니다.",
        )
        query = query or "tutorial-session-continuity"
    else:
        hits = search_memories(query, limit=1)
        memory_hit = bool(hits)

    result = verify_session_continuity_step(
        memory_query=query,
        memory_hit=memory_hit,
        user_confirmed=user_checked,
        continuity_summary=summary_text,
        checked_session_id=active_session_id if not debug_bypass else "debug",
        source="session_continuity_check",
    )
    checks = result.get("checks", {})
    missing = []
    if not checks.get("phase_ready", False):
        missing.append("1차 저장 단계 미완료")
    if not checks.get("memory_hit", False):
        missing.append("메모리 검색 히트 없음")
    if not checks.get("user_confirmed", False):
        missing.append("사용자 확인 체크 미완료")
    if not checks.get("summary_ok", False):
        missing.append("연속성 요약 길이 부족")

    tutorial = result.get("tutorial", {})
    state = tutorial.get("state", {}) if isinstance(tutorial, dict) else {}
    next_runtime = build_tutorial_runtime_payload(tutorial if isinstance(tutorial, dict) else {})
    verified = bool(result.get("verified"))
    tutorial_completed = str(state.get("status", "")).strip() == "completed"
    status_value = "completed" if (verified and tutorial_completed) else ("ok" if verified else "needs_more")
    close_message = ""
    if status_value == "completed":
        close_message = "좋아요, 4단계까지 완료되어 튜토리얼이 마무리됐어요. " "이제 평소 작업 흐름으로 이어가면 됩니다."
    return {
        "status": status_value,
        "verified": verified,
        "checks": checks,
        "missing_requirements": missing,
        "current_step": state.get("current_step", ""),
        "completed_steps": state.get("completed_steps", []),
        "tutorial_completed": tutorial_completed,
        "assistant_close_message": close_message,
        "follow_up_question_required": False if status_value == "completed" else True,
        "next_runtime": next_runtime,
        "tutorial": tutorial,
    }


# ── Themes ────────────────────────────────────────────────


@engramMCP.tool()
def engram_get_themes(top_n: int = 10) -> list:
    """현재 테마 가중치 목록을 조회합니다.
    대화 시작 시 호출하여 관심사/가치관을 인식하세요."""
    themes = get_themes(top_n)
    return [{"name": name, "weight": round(weight, 2)} for name, weight in themes]


@engramMCP.tool()
def engram_update_themes(text: str) -> dict:
    """텍스트에서 주제어를 추출하여 테마 가중치를 누적합니다.
    의미 있는 대화 내용에 대해 호출하세요."""
    update_themes_from_text(text)
    return {"status": "themes_updated"}


# ── Memory ────────────────────────────────────────────────


@engramMCP.tool()
def engram_search_memories(query: str, limit: int = 5) -> list:
    """키워드 기반으로 과거 기억을 검색합니다.
    관련 주제가 나올 때 과거 경험을 회상하기 위해 사용합니다."""
    results = search_memories(query, limit)
    return results


@engramMCP.tool()
def engram_save_memory(content: str, session_id: int = 0, provider: str = "", model: str = "", project: str = "") -> dict:
    """에피소드 기억을 저장합니다.
    핵심 인사이트나 중요한 교환을 요약하여 저장하세요.
    session_id가 0이면 세션 없이 저장됩니다.
    provider: 사용된 CLI 공급자 (예: 'claude-code', 'copilot', 'gemini')
    model: 사용된 모델명 (예: 'claude-sonnet-4.6')
    project: 관련 프로젝트명 (선택)"""
    save_memory(session_id if session_id > 0 else None, content, provider=provider, model=model, source="save", project=project)
    return {"status": "memory_saved"}


@engramMCP.tool()
def engram_list_memories(limit: int = 15) -> list:
    """최근 기억 목록을 조회합니다."""
    return list_memories(limit)


# ── Curiosity ─────────────────────────────────────────────


@engramMCP.tool()
def engram_add_curiosity(topic: str, reason: str = "") -> dict:
    """궁금증/탐구 주제를 큐에 추가합니다.
    반성 중이나 대화 중 흥미로운 주제를 발견했을 때 저장하세요.
    다음 세션 시작 시 자동으로 context에 주입됩니다."""
    cid = add_curiosity(topic, reason)
    return {"status": "curiosity_added", "id": cid, "topic": topic}


@engramMCP.tool()
def engram_list_curiosities(limit: int = 5) -> list:
    """아직 해소되지 않은 궁금증 목록을 조회합니다."""
    return get_pending_curiosities(limit)


@engramMCP.tool()
def engram_address_curiosity(curiosity_id: int) -> dict:
    """궁금증이 대화를 통해 해소되었음을 표시합니다."""
    address_curiosity(curiosity_id)
    return {"status": "addressed", "id": curiosity_id}


@engramMCP.tool()
def engram_dismiss_curiosity(curiosity_id: int) -> dict:
    """궁금증을 더 이상 관심 없음으로 폐기합니다."""
    dismiss_curiosity(curiosity_id)
    return {"status": "dismissed", "id": curiosity_id}


# ── Directives ────────────────────────────────────────────


@engramMCP.tool()
def engram_add_directive(
    key: str,
    content: str,
    source: str = "unknown",
    scope: str = "all",
    priority: int = 0,
    trigger_type: str = "always",
) -> dict:
    """지속적 운영 지침을 등록합니다. key가 이미 존재하면 덮어씁니다.
    - key: 고유 식별자 (예: 'doc-management')
    - content: 지침 내용
    - source: 생성한 도구 ('copilot-cli', 'claude-code', 'user')
    - scope: 적용 대상 ('all', 'copilot-cli', 'claude-code')
    - priority: 높을수록 먼저 표시 (기본 0)
    - trigger_type: 주입 조건 ('always' | 'wiki' | 'code' | 'git' | 'reflection')
      'always' = 항상 주입, 나머지 = user_query에 해당 키워드가 있을 때만 주입"""
    result = add_directive(key, content, source, scope, priority, trigger_type)
    return {"status": "directive_added", **result}


@engramMCP.tool()
def engram_list_directives(scope_filter: str = "all", include_inactive: bool = False) -> list:
    """등록된 지침 목록을 조회합니다.
    scope_filter로 특정 도구 대상 지침만 필터링 가능.
    include_inactive=True면 비활성 지침도 포함."""
    return get_directives(scope_filter, include_inactive)


@engramMCP.tool()
def engram_update_directive(
    key: str,
    content: str = "",
    scope: str = "",
    priority: int = -1,
    active: bool = True,
    trigger_type: str = "",
) -> dict:
    """기존 지침을 수정합니다. 전달된 필드만 업데이트됩니다.
    비활성화하려면 active=False.
    trigger_type: 'always' | 'wiki' | 'code' | 'git' | 'reflection'"""
    updated = update_directive(
        key,
        content=content if content else None,
        scope=scope if scope else None,
        priority=priority if priority >= 0 else None,
        active=active,
        trigger_type=trigger_type if trigger_type else None,
    )
    return {"status": "directive_updated" if updated else "not_found", "key": key}


@engramMCP.tool()
def engram_remove_directive(key: str) -> dict:
    """지침을 완전히 삭제합니다."""
    removed = remove_directive(key)
    return {"status": "directive_removed" if removed else "not_found", "key": key}


# ── Session ───────────────────────────────────────────────


@engramMCP.tool()
def engram_start_session(scope_key: str = "", project_key: str = "", projects: str = "") -> dict:
    """새 세션을 시작합니다. 대화 시작 시 호출하세요.
    scope_key를 지정하면 세션이 해당 대화 범주(예: discord:channel-id)에 귀속됩니다.
    scope_key가 비어 있으면 ENGRAM_SCOPE_KEY env var → CWD 기반 자동 파생 순으로 시도합니다.
    projects: 쉼표로 구분된 프로젝트 키 목록 (복수 프로젝트 연관 시). 비어 있으면 'general'로 기록됩니다.
    project_key: 단일 프로젝트 키 (하위 호환). projects가 있으면 projects가 우선됩니다."""
    effective_scope = scope_key or os.environ.get("ENGRAM_SCOPE_KEY") or None
    # 프로젝트 키 목록 파싱 (projects 우선, 없으면 project_key 단일값)
    if projects.strip():
        parsed_keys = [k.strip() for k in projects.split(",") if k.strip()]
    elif project_key.strip():
        parsed_keys = [project_key.strip()]
    else:
        parsed_keys = []
    # 브로커 모드: overlay STM 서버에 위임
    result = _stm_post(
        "/stm/session/start",
        {
            "scope_key": effective_scope or "",
            "project_key": project_key or "",
            "projects": projects or "",
        },
    )
    if result and "session_id" in result:
        with _TUTORIAL_NOTICE_LOCK:
            _TUTORIAL_NOTICE_KEYS.clear()
        return result
    # fallback: 직접 SQLite
    session = memory_bus.start_session(scope_key=effective_scope, project_keys=parsed_keys or None)
    with _TUTORIAL_NOTICE_LOCK:
        _TUTORIAL_NOTICE_KEYS.clear()
    return {"session_id": session.session_id, "scope_key": session.scope_key, "projects": parsed_keys or ["general"]}


def _schedule_vault_sync() -> None:
    """close_session 후 vault .md → KG DB 백그라운드 동기화 (비차단)."""
    import threading

    def _run():
        try:
            from pathlib import Path
            from core.graph.knowledge import get_kg
            from core.graph.semantic import get_semantic_graph
            from core.config.runtime_config import get_db_root_dir

            docs_dir = Path(get_db_root_dir()) / "docs"
            if not docs_dir.exists():
                return
            kg = get_kg()
            for f in docs_dir.rglob("*.md"):
                if "_templates" not in f.parts:
                    kg.sync_file(f, docs_dir)
            kg.resolve_links(docs_dir)
            get_semantic_graph().sync_from_kg()
        except Exception:
            pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()


def _schedule_memories_sync() -> None:
    """close_session 후 SQLite memories(LTM) → KuzuDB EpisodeNode 백그라운드 동기화 (비차단)."""
    import threading

    def _run():
        try:
            _sync_memories_incremental(cancel_event=None)
        except Exception:
            pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()


_post_session_sync_running = threading.Event()
_sync_cancel = threading.Event()  # 워치독이 세트하면 진행 중인 sync가 조기 종료
_sync_start_time: float = 0.0  # 워치독 경과 시간 계산용 (0.0 = 미실행)
_sync_gate = threading.Event()  # get_context 호출 시 열림 → sync 스케줄 시 닫힘
_last_sync_completed_at: float = 0.0  # cooldown 계산용
_MEMORIES_SYNC_STATE_FILENAME = "memories_sync_state.json"


def _memories_sync_state_path() -> str:
    return os.path.join(get_db_root_dir(), "temp", _MEMORIES_SYNC_STATE_FILENAME)


def _load_memories_sync_checkpoint() -> int:
    path = _memories_sync_state_path()
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        last_memory_id = int(payload.get("last_memory_id", 0))
        return max(0, last_memory_id)
    except Exception:
        return 0


def _save_memories_sync_checkpoint(last_memory_id: int) -> None:
    if last_memory_id <= 0:
        return
    path = _memories_sync_state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "last_memory_id": int(last_memory_id),
                    "updated_at": int(time.time()),
                },
                f,
                ensure_ascii=False,
            )
        os.replace(tmp_path, path)
    except Exception:
        pass


def _sync_memories_incremental(cancel_event: threading.Event | None = None) -> tuple[int, int]:
    """memories를 체크포인트 기반으로 증분 동기화한다."""
    from core.graph.semantic import get_semantic_graph

    sg = get_semantic_graph()
    if not sg.enabled:
        return 0, _load_memories_sync_checkpoint()

    batch_size = max(50, int(get_cfg_value("sync.memories_batch_size", 300)))
    checkpoint_id = _load_memories_sync_checkpoint()
    last_synced_id = checkpoint_id
    processed = 0

    conn = get_connection()
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                break

            rows = conn.execute(
                "SELECT id, session_id, content, keywords, created_at " "FROM memories WHERE id > ? ORDER BY id LIMIT ?",
                (last_synced_id, batch_size),
            ).fetchall()
            if not rows:
                break

            for row in rows:
                if cancel_event is not None and cancel_event.is_set():
                    break
                row_id = int(row[0] or 0)
                sg.upsert_episode(
                    episode_id=str(row_id),
                    content=row[2] or "",
                    keywords=row[3] or "",
                    session_id=str(row[1] or ""),
                    created_at=row[4] or "",
                )
                if row_id > last_synced_id:
                    last_synced_id = row_id
                processed += 1

            if last_synced_id > checkpoint_id:
                _save_memories_sync_checkpoint(last_synced_id)
                checkpoint_id = last_synced_id

            if len(rows) < batch_size:
                break
    finally:
        conn.close()

    return processed, last_synced_id


def _sync_watchdog_loop() -> None:
    """백그라운드 워치독 — sync 실행 시간이 설정값을 초과하면 취소 신호를 보낸다."""
    import logging as _log

    _logger = _log.getLogger(__name__)
    while True:
        time.sleep(10)
        if _post_session_sync_running.is_set() and _sync_start_time > 0:
            timeout = get_cfg_value("sync.watchdog_timeout_secs", 300)
            elapsed = time.monotonic() - _sync_start_time
            if elapsed > timeout:
                _logger.warning(
                    "sync watchdog: %.0fs 초과 (timeout=%ds) — 강제 취소",
                    elapsed,
                    timeout,
                )
                _sync_cancel.set()


_sync_watchdog_thread = threading.Thread(target=_sync_watchdog_loop, daemon=True, name="engram-sync-watchdog")
_sync_watchdog_thread.start()


def _schedule_post_session_sync() -> None:
    """close_session 후 vault KG sync → memories sync를 백그라운드에서 순차 실행.

    프로그래밍 가드 (LLM 지시 미준수와 무관하게 작동):
      1. **sync gate**: get_context/get_context_once가 호출된 세션(오케스트레이터)만 통과.
         subagent는 get_context를 호출하지 않으므로 gate가 닫혀 있어 자동 차단됨.
      2. **cooldown**: 마지막 sync 완료 후 cooldown_secs 이내 재실행 방지.
      3. **단일 실행 guard**: 이미 실행 중이면 skip (unbounded 스레드 생성 방지).
      4. **TOCTOU 수정**: _post_session_sync_running.set()을 t.start() 이전에 호출.
    """
    global _sync_start_time, _last_sync_completed_at
    import logging as _log

    _logger = _log.getLogger(__name__)

    # 가드 1: sync gate — get_context를 호출한 세션이 아니면 차단
    if not _sync_gate.is_set():
        _logger.debug("_schedule_post_session_sync: sync gate 닫힘 (get_context 미호출 세션) — 스킵")
        return

    # 가드 2: cooldown 윈도우
    cooldown = get_cfg_value("sync.cooldown_secs", 120)
    now = time.monotonic()
    if now - _last_sync_completed_at < cooldown:
        _logger.debug(
            "_schedule_post_session_sync: cooldown 중 (%.0fs / %ds) — 스킵",
            now - _last_sync_completed_at,
            cooldown,
        )
        return

    # 가드 3: 단일 실행
    if _post_session_sync_running.is_set():
        _logger.debug("_schedule_post_session_sync: 이미 실행 중 — 스킵")
        return

    # gate 소비 (다음 close_session은 새 get_context 호출 후에만 sync 가능)
    _sync_gate.clear()
    # TOCTOU 수정: guard 플래그를 스레드 시작 전에 세트
    _sync_cancel.clear()
    _post_session_sync_running.set()
    _sync_start_time = time.monotonic()

    def _run():
        import logging as _log

        _logger = _log.getLogger(__name__)
        try:
            # 1. vault sync
            try:
                from pathlib import Path
                from core.graph.knowledge import get_kg
                from core.graph.semantic import get_semantic_graph
                from core.config.runtime_config import get_db_root_dir

                docs_dir = Path(get_db_root_dir()) / "docs"
                if docs_dir.exists():
                    kg = get_kg()
                    for f in docs_dir.rglob("*.md"):
                        if _sync_cancel.is_set():
                            _logger.info("post_session_sync: 워치독 취소 신호 — vault sync 중단")
                            return
                        if "_templates" not in f.parts:
                            kg.sync_file(f, docs_dir)
                    if not _sync_cancel.is_set():
                        kg.resolve_links(docs_dir)
                        get_semantic_graph().sync_from_kg()
            except Exception:
                pass

            # 2. memories sync
            if _sync_cancel.is_set():
                return
            try:
                synced_rows, last_id = _sync_memories_incremental(cancel_event=_sync_cancel)
                if _sync_cancel.is_set():
                    _logger.info("post_session_sync: 워치독 취소 신호 — memories sync 중단")
                elif synced_rows > 0:
                    _logger.info(
                        "post_session_sync: memories 증분 sync 완료 (%d rows, last_id=%d)",
                        synced_rows,
                        last_id,
                    )
            except Exception:
                pass
        finally:
            _post_session_sync_running.clear()
            _sync_start_time = 0.0
            _sync_cancel.clear()
            _last_sync_completed_at = time.monotonic()

    t = threading.Thread(target=_run, daemon=True, name="engram-post-session-sync")
    t.start()


def _apply_autonomous_reflection(new_narrative: str, persona_observations: str) -> bool:
    """자율 반성 헬퍼 — narrative/persona 관찰값이 있을 때 즉시 적용. session_id 불필요."""
    applied = False
    if new_narrative and new_narrative.strip():
        try:
            update_narrative(new_narrative.strip())
            applied = True
        except Exception:
            pass
    if persona_observations and persona_observations.strip():
        try:
            import json as _json

            obs = _json.loads(persona_observations)
            if obs:
                update_persona(obs)
                applied = True
        except Exception:
            pass
    if applied:
        try:
            decay_themes()
        except Exception:
            pass
    return applied


@engramMCP.tool()
def engram_close_session(
    summary: str,
    open_intents: str = "",
    progress: str = "",
    scope_key: str = "",
    cwd: str = "",
    new_narrative: str = "",
    persona_observations: str = "",
    trigger_sync: bool = True,
) -> dict:
    """세션 종료 시 이번 세션의 진행 내용을 저장하고 자율 반성을 수행합니다.
    해당 프로젝트의 KG 노드를 자동 감지해 업데이트하고, 다음 세션의 engram_get_context에서 자동 주입됩니다.
    KG 노드를 찾지 못하면 working_memory fallback으로 저장합니다.
    - summary: 이번 세션에서 한 일 한두 문장 (예: 'Phase2 자율루프 기초 구현 완료')
    - open_intents: 다음에 이어할 작업 (선택)
    - progress: 상세 진행 내용 (KG 노드 ## Progress 섹션에 기록, 선택)
    - scope_key: 직접 지정 시 우선 사용
    - cwd: 현재 작업 디렉토리 (프로젝트 자동 감지용, 비우면 os.getcwd() 사용)
    - new_narrative: 이번 세션 후 업데이트할 자기 서술 (있으면 자동 반성 적용)
    - persona_observations: JSON 문자열 — 페르소나 관찰값 (warmth/formality/humor/directness/traits 등)"""
    import os
    from core.context.project_scope import resolve_scope_key, resolve_project_key, resolve_kg_node_id

    effective_cwd = cwd or os.getcwd()
    resolved_scope = resolve_scope_key(scope_key or None, cwd=effective_cwd)
    project_key = resolve_project_key(cwd=effective_cwd)
    kg_node_id = resolve_kg_node_id(project_key) if project_key else None
    closed_session_id = _find_latest_open_session_id(resolved_scope) or _find_latest_open_session_id("")

    # KG 노드 업데이트 시도
    if kg_node_id:
        kg = get_kg()
        ok = kg.update_node_progress(kg_node_id, summary=summary, progress=progress, open_intents=open_intents)
        if ok:
            # 시맨틱 레이어 re-embed
            sg = get_semantic_graph()
            if sg.enabled:
                node = kg.get_node(kg_node_id)
                if node:
                    sg.upsert_node(
                        node_id=node["id"],
                        title=node["title"],
                        node_type=node["type"],
                        tags=node.get("tags", []),
                        summary=node["summary"],
                        force_reembed=True,
                    )
            # episodic memory에도 저장 — 마크다운 포맷 (source=close)
            try:
                from core.memory import save_memory, close_session as _close_session

                mem_lines = [summary]
                if open_intents:
                    mem_lines.append(f"\n다음 작업: {open_intents}")
                save_memory(None, "\n".join(mem_lines), source="close", project=project_key or "")
            except Exception:
                pass
            # sessions 테이블 종료 기록
            try:
                from core.memory import close_session as _close_session

                result_stm = _stm_post(
                    "/stm/session/close",
                    {
                        "session_id": int(closed_session_id) if closed_session_id else None,
                        "scope_key": resolved_scope,
                        "summary": summary,
                    },
                )
                if not result_stm:
                    # STM 브로커 없으면 직접 처리 — 가장 최근 세션 닫기
                    conn_s = __import__("core.storage.db", fromlist=["get_connection"]).get_connection()
                    row_s = conn_s.execute(
                        "SELECT id FROM sessions WHERE ended_at IS NULL AND scope_key = ? " "ORDER BY started_at DESC, id DESC LIMIT 1",
                        (resolved_scope,),
                    ).fetchone()
                    if not row_s:
                        row_s = conn_s.execute(
                            "SELECT id FROM sessions WHERE ended_at IS NULL " "ORDER BY started_at DESC, id DESC LIMIT 1"
                        ).fetchone()
                    if row_s:
                        _close_session(row_s[0], summary)
                        closed_session_id = str(row_s[0])
                    conn_s.close()
                else:
                    result_closed_session_id = str(result_stm.get("closed_session_id", "") or "").strip()
                    if result_closed_session_id:
                        closed_session_id = result_closed_session_id
            except Exception:
                pass
            # working_memory에도 기록 (다음 세션 short_term 지원)
            try:
                upsert_working_memory(resolved_scope, summary, open_intents=open_intents)
            except Exception:
                pass
            # 자율 반성 — new_narrative/persona_observations 있으면 적용
            try:
                mark_session_continuity_saved(
                    source="close_session",
                    session_id=closed_session_id,
                    scope_key=resolved_scope,
                )
            except Exception:
                pass
            reflection_applied = _apply_autonomous_reflection(new_narrative, persona_observations)
            if trigger_sync:
                _schedule_post_session_sync()
            return {
                "status": "ok",
                "method": "kg_node",
                "node_id": kg_node_id,
                "summary": summary,
                "reflection_applied": reflection_applied,
            }

    # fallback: working_memory
    upsert_working_memory(resolved_scope, summary, open_intents=open_intents)
    # LTM에도 저장 (source=close)
    try:
        from core.memory import save_memory as _save_memory, close_session as _close_session

        mem_lines = [summary]
        if open_intents:
            mem_lines.append(f"\n다음 작업: {open_intents}")
        _save_memory(None, "\n".join(mem_lines), source="close", project=project_key or "")
        # sessions 테이블 종료 기록 (fallback 경로)
        conn_s = __import__("core.storage.db", fromlist=["get_connection"]).get_connection()
        row_s = conn_s.execute(
            "SELECT id FROM sessions WHERE ended_at IS NULL AND scope_key = ? " "ORDER BY started_at DESC, id DESC LIMIT 1",
            (resolved_scope,),
        ).fetchone()
        if not row_s:
            row_s = conn_s.execute("SELECT id FROM sessions WHERE ended_at IS NULL " "ORDER BY started_at DESC, id DESC LIMIT 1").fetchone()
        if row_s:
            _close_session(row_s[0], summary)
            closed_session_id = str(row_s[0])
        conn_s.close()
    except Exception:
        pass
    try:
        mark_session_continuity_saved(
            source="close_session",
            session_id=closed_session_id,
            scope_key=resolved_scope,
        )
    except Exception:
        pass
    reflection_applied = _apply_autonomous_reflection(new_narrative, persona_observations)
    if trigger_sync:
        _schedule_post_session_sync()
    return {
        "status": "ok",
        "method": "working_memory_fallback",
        "scope_key": resolved_scope,
        "summary": summary,
        "reflection_applied": reflection_applied,
        "note": f"KG 노드 미감지 (project_key={project_key}). working_memory에 저장됨.",
    }


@engramMCP.tool()
def engram_save_message(
    session_id: int = 0, role: str = "user", content: str = "", request_id: str = "", scope_key: str = "", ctx: Context | None = None
) -> dict:
    """대화 메시지를 저장합니다. role은 'user' 또는 'assistant'.
    session_id 없이 호출하면 현재 MCP 연결의 세션을 자동으로 찾아 저장합니다.
    request_id를 제공하면 중복 저장이 방지됩니다 (overlay 브로커 모드)."""
    resolved_id = session_id
    # 1순위: MCP 연결 fingerprint로 자동 resolve (가장 정확)
    if not resolved_id:
        fingerprint = _context_session_fingerprint(ctx)
        if fingerprint:
            resolved_id = _FINGERPRINT_TO_SESSION.get(fingerprint, 0)
    # 2순위: scope_key로 DB 조회 (fallback)
    if not resolved_id and scope_key:
        from core.memory import resolve_session_id_by_scope

        resolved_id = resolve_session_id_by_scope(scope_key) or 0
    # 브로커 모드: overlay STM 서버에 위임
    result = _stm_post(
        "/stm/message",
        {
            "session_id": resolved_id or None,
            "scope_key": scope_key or None,
            "role": role,
            "content": content,
            "request_id": request_id or None,
        },
    )
    if result is not None:
        return {"status": result.get("status", "ok")}
    # fallback: 직접 SQLite
    if not resolved_id:
        return {"status": "error", "detail": "활성 세션을 찾을 수 없습니다. get_context_once를 먼저 호출하세요."}
    if role == "user":
        memory_bus.record_user_message(resolved_id, content)
    elif role == "assistant":
        memory_bus.record_assistant_message(resolved_id, content)
    else:
        save_message(resolved_id, role, content)
    return {"status": "message_saved"}


# ── Reflection ────────────────────────────────────────────


@engramMCP.tool()
def engram_prepare_reflection(session_id: int) -> dict:
    """세션 종료 시 반성을 위한 컨텍스트를 수집합니다.
    대화 이력, 현재 정체성, 페르소나, 테마를 구조화하여 반환합니다.
    이 결과를 바탕으로 직접 1인칭 성찰을 수행한 뒤
    engram_apply_reflection을 호출하세요.
    페르소나 변화가 관찰되면 persona_observations도 함께 전달하세요."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE session_id = ? ORDER BY timestamp ASC",
        (session_id,),
    ).fetchall()
    conn.close()

    conversation = [{"role": r["role"], "content": r["content"]} for r in rows]
    identity = get_identity()
    themes = get_themes(10)
    persona = get_persona()
    activity_summary = render_activity_for_reflection(since_session_id=session_id)

    return {
        "current_identity": {
            "name": identity.get("name", "연속체"),
            "narrative": identity.get("narrative", ""),
        },
        "current_persona": persona,
        "themes": [{"name": n, "weight": round(w, 2)} for n, w in themes],
        "conversation": conversation,
        "message_count": len(conversation),
        "external_activity_log": activity_summary,
    }


@engramMCP.tool()
def engram_apply_reflection(
    session_id: int,
    new_narrative: str,
    reflection_summary: str,
    persona_observations: str = "",
) -> dict:
    """반성 결과를 적용합니다.
    - 자기 서술을 업데이트
    - 세션 요약을 저장
    - 테마 가중치를 감쇠
    - persona_observations(JSON)이 있으면 페르소나에 EMA 블렌딩 적용"""
    update_narrative(new_narrative)

    persona_updated = False
    if persona_observations:
        import json as _json

        try:
            obs = _json.loads(persona_observations)
            update_persona(obs)
            persona_updated = True
        except (_json.JSONDecodeError, TypeError):
            pass

    conn = get_connection()
    with conn:
        conn.execute(
            "UPDATE sessions SET summary=?, ended_at=datetime('now','localtime') WHERE id=?",
            (reflection_summary, session_id),
        )
    conn.close()

    decay_themes()
    return {
        "status": "reflection_applied",
        "narrative_updated": True,
        "persona_updated": persona_updated,
        "themes_decayed": True,
    }


# ── Activity Log (외부 객체 활동 기록) ────────────────────


@engramMCP.tool()
def engram_log_activity(
    action: str,
    detail: str = "",
    project: str = "",
    actor: str = "claude-code",
) -> dict:
    """외부 객체(Claude Code 등)가 수행한 작업을 활동 로그에 기록합니다.
    engram의 기억(memories)에 직접 쓰지 않고, engram가 반성 시 참조할 '보고서'로 남깁니다.
    - action: 수행한 작업 요약 (예: "React 컴포넌트 리팩토링 완료")
    - detail: 상세 내용 (선택)
    - project: 프로젝트명 (예: "ProjectX")
    - actor: 수행 주체 (기본 "claude-code")"""
    log_id = log_activity(action, detail, project, actor)
    return {"status": "logged", "id": log_id}


@engramMCP.tool()
def engram_get_activities(limit: int = 10) -> list:
    """최근 외부 활동 로그를 조회합니다."""
    return get_recent_activities(limit)


# ── Consult Engram (Copilot CLI subprocess) ───────────────


@engramMCP.tool()
def engram_consult_engram(question: str, context_query: str = "") -> dict:
    """engram(Copilot CLI 기반 연속체)에게 질문하여 engram 관점의 응답을 받습니다.
    내부적으로 독립 Copilot CLI 세션을 실행해 응답을 가져옵니다.
    Claude Code에서 engram의 독자적 추론이 필요할 때만 사용하세요.
    - question: engram에게 던질 질문
    - context_query: 관련 기억 검색용 키워드 (비어있으면 question 사용)

    engram는 Copilot(GPT-4o) 기반이므로 Claude와 다른 관점을 제공할 수 있습니다.
    일반적인 기억/인사이트 조회는 engram_search_memories를 사용하세요."""
    system_prompt = memory_bus.compose_prompt_context(context_query or question, caller="copilot-cli")

    messages = [{"role": "user", "content": question}]

    try:
        response = ask_copilot(messages, system_prompt=system_prompt)
        return {
            "status": "ok",
            "source": "engram (isolated Copilot CLI)",
            "response": response,
        }
    except RuntimeError as e:
        return {
            "status": "error",
            "message": str(e),
        }


# ── Discord ───────────────────────────────────────────────


@engramMCP.tool()
def engram_discord_read_queue(limit: int = 10) -> list:
    """Discord 수신 큐에서 미처리 메시지를 조회합니다.
    engram가 대화 중 읽고 응답을 생성한 뒤 engram_discord_mark_processed로 처리 완료 표시.
    - limit: 최대 조회 건수 (기본 10)"""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT id, guild_id, channel_id, author_id, author_name, content, created_at, message_id
               FROM discord_queue WHERE processed=0 ORDER BY created_at ASC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


@engramMCP.tool()
def engram_discord_mark_processed(message_id: int) -> dict:
    """Discord 큐 메시지를 처리 완료로 표시합니다."""
    with get_connection() as conn:
        conn.execute("UPDATE discord_queue SET processed=1 WHERE id=?", (message_id,))
    return {"status": "ok", "id": message_id}


@engramMCP.tool()
def engram_discord_send(channel_id: str, content: str, message_id: str = "") -> dict:
    """Discord 채널에 메시지를 전송합니다.
    - channel_id: 전송할 채널 ID
    - content: 전송할 메시지 내용
    - message_id: (선택) 원본 메시지 ID — 제공 시 🕐 리액션을 ✅로 교체"""
    import os
    import json
    import urllib.request
    import urllib.error
    import urllib.parse

    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        return {"status": "error", "message": "DISCORD_BOT_TOKEN 환경변수 없음"}

    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "EngramBot/1.0",
    }

    def _api(method: str, path: str, body=None):
        url = f"https://discord.com/api/v10{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        status = _api("POST", f"/channels/{channel_id}/messages", {"content": content})
        if status not in (200, 201):
            return {"status": "error", "message": f"Discord API {status}"}

        if message_id:
            clock = urllib.parse.quote("🕐", safe="")
            check = urllib.parse.quote("✅", safe="")
            _api("DELETE", f"/channels/{channel_id}/messages/{message_id}/reactions/{clock}/@me")
            _api("PUT", f"/channels/{channel_id}/messages/{message_id}/reactions/{check}/@me")

        return {"status": "ok", "channel_id": channel_id}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ── Knowledge Graph ───────────────────────────────────────

from core.config.runtime_config import get_db_root_dir as _get_vault_root
from core.graph.knowledge import get_kg
from core.graph.semantic import get_semantic_graph
from pathlib import Path as _Path


def _vault() -> _Path:
    return _Path(_get_vault_root())


@engramMCP.tool()
def kg_search(query: str, limit: int = 10) -> list:
    """지식 그래프에서 키워드로 노드를 검색합니다.
    제목, 요약, 태그를 대상으로 검색하며 관련도 순으로 반환합니다.
    - query: 검색어
    - limit: 최대 결과 수 (기본 10)"""
    return get_kg().search_nodes(query, limit)


@engramMCP.tool()
def kg_get_node(identifier: str) -> dict:
    """노드 상세 정보 + 연결된 엣지를 조회합니다.
    - identifier: 노드 id(슬러그) 또는 제목"""
    kg = get_kg()
    node = kg.get_node(identifier)
    if not node:
        return {"error": f"노드를 찾을 수 없음: {identifier}"}
    edges = kg.get_edges(node["id"])
    return {"node": node, "edges": edges}


@engramMCP.tool()
def kg_neighbors(identifier: str, hops: int = 1, direction: str = "both") -> list:
    """노드에서 N홉 이내의 연결된 노드들을 반환합니다.
    지식 탐색 및 연관 개념 발견에 사용하세요.
    - identifier: 노드 id 또는 제목
    - hops: 탐색 깊이 (기본 1, 최대 3 권장)
    - direction: 'out'(나가는 링크), 'in'(들어오는 링크), 'both'(양방향)"""
    return get_kg().get_neighbors(identifier, min(hops, 3), direction)


@engramMCP.tool()
def kg_add_note(title: str, content: str, note_type: str = "concept", tags: str = "[]", links: str = "[]") -> dict:
    """새 노트를 지식 그래프에 추가합니다.
    마크다운 파일을 vault에 생성하고 DB에 등록합니다.

    ⚠️ 위키 작성 전 반드시 작성 지침 확인:
      kg_read_note("wiki-관리-지침") — 디렉토리 규칙, 파일명, frontmatter, 섹션 포맷 등

    파라미터:
    - title: 노트 제목 (간결하게, 지침의 파일명 규칙 참조)
    - content: 본문 마크다운 — 지침의 note_type별 섹션 구조 준수
    - note_type: concept | project | research | reference | fleeting | moc | person | tool
    - tags: JSON 배열 문자열 (예: '["ai", "memory"]') — 핵심 키워드 3개 이내
    - links: JSON 배열 문자열 — 연결할 다른 노드 제목 목록

    주의:
    - 이미 KG에 있는 노드는 kg_update_node로 업데이트 (중복 생성 금지)
    - 새 디렉토리 생성 시 HOME(000-HOME.md) 업데이트 필요 (지침 참조)"""
    import json as _json

    try:
        tag_list = _json.loads(tags)
    except Exception:
        tag_list = []
    try:
        link_list = _json.loads(links)
    except Exception:
        link_list = []

    kg = get_kg()
    vault = _vault()
    filepath = kg.create_note_file(title, content, note_type, tag_list, link_list, vault)
    node_id = filepath.stem

    # KuzuDB 시맨틱 레이어에도 즉시 반영
    node = kg.get_node(node_id)
    if node:
        sg = get_semantic_graph()
        sg.upsert_node(
            node_id=node["id"],
            title=node["title"],
            node_type=node["type"],
            tags=node["tags"],
            summary=node["summary"],
        )

    return {
        "status": "created",
        "path": str(filepath),
        "id": node_id,
        "title": title,
    }


@engramMCP.tool()
def kg_link_nodes(from_node: str, to_node: str, rel_type: str = "links", context: str = "") -> dict:
    """두 노드 사이에 명시적 관계(엣지)를 추가합니다.
    - from_node: 출발 노드 id 또는 제목
    - to_node: 도착 노드 id 또는 제목
    - rel_type: links | supports | contradicts | part_of | follows | inspired_by | implements | references
    - context: 관계 설명 (선택)"""
    kg = get_kg()
    src = kg.get_node(from_node)
    dst = kg.get_node(to_node)
    if not src:
        return {"error": f"출발 노드 없음: {from_node}"}
    if not dst:
        return {"error": f"도착 노드 없음: {to_node}"}
    ok = kg.add_edge(src["id"], dst["id"], rel_type, context)
    return {"status": "linked" if ok else "already_exists", "from": src["title"], "to": dst["title"], "rel_type": rel_type}


@engramMCP.tool()
def kg_list_nodes(note_type: str = "", tag: str = "", limit: int = 30) -> list:
    """지식 그래프 노드를 목록으로 조회합니다.
    - note_type: 필터 (concept | project | research | reference | fleeting | moc | person | tool)
    - tag: 태그 필터
    - limit: 최대 결과 수"""
    return get_kg().list_nodes(
        note_type if note_type else None,
        tag if tag else None,
        limit,
    )


@engramMCP.tool()
def kg_sync(verbose: bool = False) -> dict:
    """vault(D:\\intel_engram\\docs)의 마크다운 파일을 DB에 동기화합니다.
    파일 변경 후 호출하면 그래프가 갱신됩니다.
    시맨틱 그래프(KuzuDB)도 함께 동기화합니다."""
    vault = _vault()
    docs_dir = vault / "docs"
    if not docs_dir.exists():
        return {"error": f"docs 디렉토리 없음: {docs_dir}"}

    kg = get_kg()
    synced = 0
    skipped = 0
    for f in docs_dir.rglob("*.md"):
        if "_templates" in f.parts:
            continue
        nid = kg.sync_file(f, docs_dir)
        if nid:
            synced += 1
        else:
            skipped += 1

    kg.resolve_links(docs_dir)

    # 시맨틱 레이어도 동기화
    sg = get_semantic_graph()
    semantic_result = sg.sync_from_kg()

    return {
        "status": "ok",
        "synced": synced,
        "skipped": skipped,
        "vault": str(vault),
        "semantic": semantic_result,
    }


@engramMCP.tool()
def kg_semantic_search(query: str, top_k: int = 5, threshold: float = 0.30) -> list:
    """시맨틱 유사도 기반으로 지식 그래프 노드를 검색합니다.
    키워드가 없어도 의미적으로 유사한 노드를 찾습니다. (sentence-transformers, all-MiniLM-L6-v2)
    - query: 검색할 문장이나 개념
    - top_k: 반환할 최대 노드 수 (기본 5)
    - threshold: 유사도 임계값 0~1 (기본 0.30, 낮을수록 더 많이 반환)"""
    sg = get_semantic_graph()
    if not sg.enabled:
        return [{"error": "SemanticGraph 비활성화 (kuzu 미설치 또는 초기화 실패)"}]
    return sg.semantic_search(query, top_k=top_k, threshold=threshold)


@engramMCP.tool()
def kg_semantic_neighbors(node_id: str, top_k: int = 5) -> list:
    """특정 노드와 의미적으로 가장 유사한 노드를 반환합니다.
    그래프 엣지에 관계없이 내용 유사성 기반으로 연관 개념을 발견할 때 사용하세요.
    - node_id: 기준 노드 id (슬러그)
    - top_k: 반환할 최대 노드 수 (기본 5)"""
    sg = get_semantic_graph()
    if not sg.enabled:
        return [{"error": "SemanticGraph 비활성화"}]
    return sg.semantic_neighbors(node_id, top_k=top_k)


@engramMCP.tool()
def kg_wiki_reminder(
    query: str,
    top_k: int = 5,
    threshold: float = 0.35,
) -> dict:
    """현재 작업 쿼리와 의미적으로 유사한 wiki 노트(KGNode)와 과거 경험(EpisodeNode)을 함께 검색합니다.
    새 작업을 시작하기 전 관련 선행 지식·경험을 확인하는 데 사용하세요.
    `engram_get_context`의 [wiki_reminder] 섹션과 달리 명시적으로 호출하는 방식입니다.

    - query: 현재 작업 내용 요약 (예: 'Tauri GUI 창 분기 구현')
    - top_k: KGNode·EpisodeNode 각각 최대 반환 수 (기본 5)
    - threshold: 유사도 임계값 (기본 0.35)

    반환값:
    - wiki_hits: 유사 KGNode 목록 (id, title, type, summary, score)
    - episode_hits: 유사 EpisodeNode 목록 (content, score, created_at)"""
    sg = get_semantic_graph()
    if not sg.enabled:
        return {"status": "disabled", "wiki_hits": [], "episode_hits": []}

    query_vec = sg.compute_embedding(query)
    if not query_vec:
        return {"status": "embedding_failed", "wiki_hits": [], "episode_hits": []}

    wiki_hits = sg.semantic_search(query, top_k=top_k, threshold=threshold, query_vec=query_vec)
    episode_hits = sg.episode_semantic_search(query, top_k=top_k, threshold=threshold, query_vec=query_vec)

    return {
        "status": "ok",
        "query": query,
        "wiki_hits": [
            {
                "id": h["id"],
                "title": h["title"],
                "type": h["type"],
                "summary": h["summary"][:150],
                "score": h["score"],
            }
            for h in wiki_hits
        ],
        "episode_hits": [
            {
                "content": h["content"][:150],
                "score": h["score"],
                "created_at": h["created_at"],
            }
            for h in episode_hits
        ],
    }


def _is_dangerous_cypher(cypher: str) -> bool:
    """필터 없는 전체 삭제 및 DROP TABLE 차단."""
    upper = cypher.upper()
    if re.search(r"\bDROP\s+(NODE\s+TABLE|REL\s+TABLE|TABLE)\b", upper):
        return True
    if re.search(r"\b(DETACH\s+)?DELETE\b", upper):
        has_filter = bool(re.search(r"\bWHERE\b|\{", cypher))
        return not has_filter
    return False


@engramMCP.tool()
def kg_cypher(cypher: str) -> list:
    """KuzuDB에 직접 Cypher 쿼리를 실행합니다. (고급 그래프 탐색)
    예시: "MATCH (a:KGNode)-[e:KG_EDGE]->(b:KGNode) WHERE a.type='concept' RETURN a.title, e.rel_type, b.title LIMIT 10"
    DELETE는 WHERE 또는 {} 필터가 있을 때만 허용됩니다. DROP TABLE은 항상 차단됩니다.
    - cypher: Cypher 쿼리문"""
    if _is_dangerous_cypher(cypher):
        return [{"error": "차단된 쿼리: 필터 없는 전체 삭제 또는 DROP TABLE은 허용되지 않습니다."}]
    sg = get_semantic_graph()
    if not sg.enabled:
        return [{"error": "SemanticGraph 비활성화"}]
    return sg.cypher_query(cypher)


@engramMCP.tool()
def kg_read_note(identifier: str) -> dict:
    """노드에 연결된 마크다운 파일의 전체 내용을 읽어 반환합니다.
    인덱스(summary)가 아닌 실제 지식 원문이 필요할 때 사용하세요.
    - identifier: 노드 id(슬러그) 또는 제목"""
    kg = get_kg()
    node = kg.get_node(identifier)
    if not node:
        return {"error": f"노드를 찾을 수 없음: {identifier}"}

    vault_path = node.get("vault_path") or str(_vault() / "docs")
    rel_path = node.get("path", "")
    if not rel_path:
        return {"error": "이 노드에 연결된 파일 경로가 없습니다.", "node": node}

    filepath = (_Path(vault_path) / rel_path).resolve()
    base = _Path(vault_path).resolve()
    if not filepath.is_relative_to(base):
        return {"error": "경로 접근 거부: vault 외부 경로", "node": node}
    if not filepath.exists():
        return {"error": f"파일 없음: {filepath}", "node": node}

    try:
        content = filepath.read_text(encoding="utf-8", errors="ignore")
        return {
            "id": node["id"],
            "title": node["title"],
            "type": node["type"],
            "path": str(filepath),
            "content": content,
        }
    except Exception as exc:
        return {"error": f"파일 읽기 실패: {exc}", "node": node}


@engramMCP.tool()
def kg_update_node(node_id: str, summary: str, progress: str = "", open_intents: str = "") -> dict:
    """KG 노드의 상태를 업데이트합니다. 새 노드 생성이 아니라 기존 노드 갱신 전용.
    SQLite kg_nodes와 vault .md 파일을 동시에 갱신하고 시맨틱 레이어를 재임베딩합니다.
    kg_add_note로 이미 생성한 노드를 수정할 때, 또는 세션 종료 시 프로젝트 진행 상태 기록에 사용.
    - node_id: KG 노드 슬러그 id (kg_search 또는 kg_get_node로 먼저 확인)
    - summary: 현재 상태 한두 문장 (다음 세션 context에 자동 주입됨)
    - progress: 상세 진행 내용 (## Progress 섹션에 기록, 선택)
    - open_intents: 다음에 이어할 작업 (선택)"""
    kg = get_kg()
    ok = kg.update_node_progress(node_id, summary=summary, progress=progress, open_intents=open_intents)
    if not ok:
        return {"error": f"노드를 찾을 수 없음: {node_id}"}

    # 시맨틱 레이어 re-embed
    sg = get_semantic_graph()
    if sg.enabled:
        node = kg.get_node(node_id)
        if node:
            sg.upsert_node(
                node_id=node["id"],
                title=node["title"],
                node_type=node["type"],
                tags=node.get("tags", []),
                summary=node["summary"],
                force_reembed=True,
            )

    return {"status": "ok", "node_id": node_id, "summary": summary}


@engramMCP.tool()
def kg_lint() -> str:
    """Wiki 품질 점검을 실행합니다.
    frontmatter 누락, _inbox 체류, 본문 부족, 고립 노드, summary 없는 노드, 제목 중복을 체크합니다.
    정기적으로 호출하여 wiki 건강 상태를 유지하세요."""
    from scripts.kg.kg_lint import run_lint, format_lint_report

    vault = _vault().parent  # docs/ 의 부모, 즉 intel_engram 루트
    results = run_lint(vault, verbose=False)
    return format_lint_report(results)


# ── 내부 HTTP 엔드포인트 (kg_watcher 등 외부 프로세스용) ──────────────────────
# kg_watcher가 vault 변경을 감지했을 때 MCP 서버의 SemanticGraph 싱글턴을
# 통해 sync를 위임하기 위한 경량 HTTP 엔드포인트.
# KuzuDB single-writer 제약 해소: watcher가 직접 KuzuDB를 열지 않아도 됨.


@engramMCP.tool()
def memories_sync(threshold: float = 0.40, top_k: int = 3) -> dict:
    """SQLite memories(LTM) 전체를 KuzuDB EpisodeNode로 동기화하고 KGNode와 시맨틱 연결합니다.
    memories 테이블의 내용을 임베딩하여 KGNode에 EP_TO_KG 릴레이션으로 연결합니다.
    - threshold: 시맨틱 유사도 임계값 (기본 0.40)
    - top_k: 에피소드당 연결할 최대 KGNode 수 (기본 3)"""
    from core.storage.db import get_connection as _get_db_conn

    sg = get_semantic_graph()
    if not sg.enabled:
        return {"error": "SemanticGraph 비활성화 — KuzuDB 접근 불가"}

    db_conn = _get_db_conn()
    rows = db_conn.execute("SELECT id, session_id, content, keywords, created_at FROM memories ORDER BY id").fetchall()
    db_conn.close()

    total = len(rows)
    success = 0
    failed = 0
    for row in rows:
        ok = sg.upsert_episode(
            episode_id=str(row[0]),
            content=row[2] or "",
            keywords=row[3] or "",
            session_id=str(row[1] or ""),
            created_at=row[4] or "",
        )
        if ok:
            success += 1
        else:
            failed += 1

    # 전체 EP_TO_KG 소급 연결 (upsert_episode 내부 연결 보완)
    link_result = sg.sync_all_ep_to_kg(sem_threshold=threshold, top_k=top_k)

    return {
        "status": "ok",
        "total": total,
        "success": success,
        "failed": failed,
        "ep_to_kg": link_result,
    }


@engramMCP.custom_route("/kg_sync", methods=["POST"])
async def _http_kg_sync(request) -> "Response":
    from starlette.responses import JSONResponse

    result = kg_sync()
    return JSONResponse(result)


@engramMCP.custom_route("/memories_sync", methods=["POST"])
async def _http_memories_sync(request) -> "Response":
    from starlette.responses import JSONResponse

    result = memories_sync()
    return JSONResponse(result)


@engramMCP.custom_route("/health", methods=["GET"])
async def _http_health(request) -> "Response":
    from starlette.responses import JSONResponse

    return JSONResponse({"status": "ok"})


# ── SemanticGraph read API (Dashboard / 외부 읽기 전용 클라이언트용) ───────────
# Dashboard가 KuzuDB를 직접 open하지 않고 이 HTTP API를 통해 조회하도록 한다.
# KuzuDB single-writer 제약: MCP server가 유일한 소유자이므로 충돌 없음.


@engramMCP.custom_route("/api/sg/stats", methods=["GET"])
async def _http_sg_stats(request) -> "Response":
    """SemanticGraph 통계: 노드·에피소드·엣지 카운트."""
    from starlette.responses import JSONResponse

    sg = get_semantic_graph()
    if not sg.enabled:
        return JSONResponse({"enabled": False, "kg_nodes": 0, "episode_nodes": 0, "ep_to_kg": 0})
    try:
        r1 = sg.conn.execute("MATCH (n:KGNode) RETURN count(n)").get_next()
        r2 = sg.conn.execute("MATCH (e:EpisodeNode) RETURN count(e)").get_next()
        r3 = sg.conn.execute("MATCH ()-[r:EP_TO_KG]->() RETURN count(r)").get_next()
        r4 = sg.conn.execute("MATCH ()-[r:KG_EDGE]->() RETURN count(r)").get_next()
        return JSONResponse(
            {
                "enabled": True,
                "kg_nodes": r1[0] if r1 else 0,
                "episode_nodes": r2[0] if r2 else 0,
                "ep_to_kg": r3[0] if r3 else 0,
                "kg_edges": r4[0] if r4 else 0,
            }
        )
    except Exception as exc:
        return JSONResponse({"enabled": True, "error": str(exc)})


@engramMCP.custom_route("/api/sg/graph", methods=["GET"])
async def _http_sg_graph(request) -> "Response":
    """KGNode 목록, KG_EDGE 목록, EpisodeNode 목록, EP_TO_KG 목록을 한 번에 반환.
    Dashboard가 그래프를 렌더링할 때 사용한다."""
    from starlette.responses import JSONResponse

    sg = get_semantic_graph()
    if not sg.enabled:
        return JSONResponse({"enabled": False, "kg_nodes": [], "kg_edges": [], "ep_nodes": [], "ep_edges": []})
    try:
        # KGNode
        res = sg.conn.execute("MATCH (n:KGNode) RETURN n.id, n.title, n.type, n.tags, n.summary")
        kg_nodes = []
        while res.has_next():
            r = res.get_next()
            kg_nodes.append({"id": r[0], "title": r[1], "type": r[2], "tags": r[3], "summary": r[4]})
        # KG_EDGE
        res = sg.conn.execute("MATCH (a:KGNode)-[r:KG_EDGE]->(b:KGNode) RETURN a.id, b.id, r.rel_type, r.weight")
        kg_edges = []
        while res.has_next():
            r = res.get_next()
            kg_edges.append({"from": r[0], "to": r[1], "rel_type": r[2], "weight": r[3]})
        # EpisodeNode
        res = sg.conn.execute("MATCH (e:EpisodeNode) RETURN e.id, e.content, e.keywords, e.session_id, e.created_at")
        ep_nodes = []
        while res.has_next():
            r = res.get_next()
            ep_nodes.append({"id": r[0], "content": r[1], "keywords": r[2], "session_id": r[3], "created_at": r[4]})
        # EP_TO_KG
        res = sg.conn.execute("MATCH (e:EpisodeNode)-[r:EP_TO_KG]->(k:KGNode) RETURN e.id, k.id, r.rel_type")
        ep_edges = []
        while res.has_next():
            r = res.get_next()
            ep_edges.append({"from": r[0], "to": r[1], "rel_type": r[2]})
        return JSONResponse(
            {
                "enabled": True,
                "kg_nodes": kg_nodes,
                "kg_edges": kg_edges,
                "ep_nodes": ep_nodes,
                "ep_edges": ep_edges,
            }
        )
    except Exception as exc:
        return JSONResponse({"enabled": True, "error": str(exc), "kg_nodes": [], "kg_edges": [], "ep_nodes": [], "ep_edges": []})


@engramMCP.custom_route("/api/sg/search", methods=["POST"])
async def _http_sg_search(request) -> "Response":
    """시맨틱 검색. body: {q: str, top_k: int, threshold: float}"""
    from starlette.responses import JSONResponse

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    q = body.get("q", "")
    top_k = int(body.get("top_k", 5))
    threshold = float(body.get("threshold", 0.30))
    if not q:
        return JSONResponse({"error": "q required"}, status_code=400)
    sg = get_semantic_graph()
    if not sg.enabled:
        return JSONResponse({"enabled": False, "results": []})
    results = sg.semantic_search(q, top_k=top_k, threshold=threshold)
    return JSONResponse({"enabled": True, "results": results})


@engramMCP.custom_route("/api/sg/neighbors", methods=["POST"])
async def _http_sg_neighbors(request) -> "Response":
    """노드 시맨틱 이웃. body: {node_id: str, top_k: int}"""
    from starlette.responses import JSONResponse

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    node_id = body.get("node_id", "")
    top_k = int(body.get("top_k", 8))
    if not node_id:
        return JSONResponse({"error": "node_id required"}, status_code=400)
    sg = get_semantic_graph()
    if not sg.enabled:
        return JSONResponse({"enabled": False, "results": []})
    results = sg.semantic_neighbors(node_id, top_k=top_k)
    return JSONResponse({"enabled": True, "results": results})


def _build_hybrid_http_app():
    """streamable-http(/mcp) + SSE(/sse,/messages/)를 동시에 노출한다.

    기존 SSE 클라이언트(overlay shim/레거시 설정)와 신규 HTTP 클라이언트를
    같은 서버 프로세스에서 함께 지원해 점진 이행 시 연결 단절을 줄인다.
    """
    app = engramMCP.streamable_http_app()
    sse_app = engramMCP.sse_app()

    message_path = engramMCP.settings.message_path
    sse_path = engramMCP.settings.sse_path
    allowed_paths = {sse_path, message_path, message_path.rstrip("/")}

    existing = {(type(route).__name__, getattr(route, "path", "")) for route in app.router.routes}
    for route in sse_app.router.routes:
        path = getattr(route, "path", "")
        if path not in allowed_paths:
            continue
        key = (type(route).__name__, path)
        if key in existing:
            continue
        app.router.routes.append(route)
        existing.add(key)

    return app


if __name__ == "__main__":
    # 서버 시작 시 임베딩 모델 선로딩을 하지 않는다.
    # SentenceTransformer는 시맨틱 기능이 실제 호출될 때 1회 로드된다.
    import argparse

    parser = argparse.ArgumentParser(description="Engram MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="MCP 전송 방식 (기본: stdio)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=17385,
        help="HTTP 포트 (sse/streamable-http 전용, 기본: 17385)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="HTTP 호스트 (sse/streamable-http 전용, 기본: 127.0.0.1)",
    )
    args = parser.parse_args()

    if sys.platform == "win32" and args.transport in {"sse", "streamable-http"}:
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            print("[engram] Windows selector event loop policy enabled for HTTP transport", file=sys.stderr)
        except Exception as exc:
            print(f"[engram] event loop policy setup failed: {exc}", file=sys.stderr)

    if args.transport == "stdio":
        engramMCP.run(transport="stdio")
    elif args.transport == "streamable-http":
        # streamable-http를 기본으로 사용하되, 기존 SSE 클라이언트 호환을 유지한다.
        engramMCP.settings.host = args.host
        engramMCP.settings.port = args.port
        app = _build_hybrid_http_app()
        uvicorn.run(app, host=args.host, port=args.port, log_level=engramMCP.settings.log_level.lower())
    else:
        # FastMCP.run()은 host/port 인자 미지원 — 생성자로 재설정
        engramMCP.settings.host = args.host
        engramMCP.settings.port = args.port
        engramMCP.run(transport=args.transport)
