"""페르소나 자율 반성용 — resume 가능한 Claude Code 세션 호출.

drt-notebookLM(backend/core/claude_client.py)의 검증된 패턴을 최소한으로 가져온다:
Windows에서 claude.cmd를 node cli.js로 우회 실행, ProactorEventLoop로 subprocess 지원.

로컬 Ollama(qwen2.5:1.5b)로는 "반성할 만한 이벤트가 있었나" 같은 판단 작업의
신뢰도가 낮았음(포맷 지시 무시, 장황한 메타분석) — 이 판단은 세션 종료마다
1회뿐이라 빈도가 낮으므로, 새 API 키 없이 이미 인증된 claude CLI 세션을
그대로 재사용해 Claude 품질의 판단을 받는다. 비용 축은 API 키가 아니라
기존 Claude Code 사용량/쿼터.
"""

import json as _json
import logging
from pathlib import Path
from typing import Optional

from claude_code_sdk import query as _sdk_query
from claude_code_sdk.types import (
    ClaudeCodeOptions,
    AssistantMessage,
    ResultMessage,
    TextBlock,
)

from core.integrations.claude_cli_transport import make_transport, run_in_proactor

logger = logging.getLogger(__name__)

# SDK message_parser가 아직 인식하지 못하는 CLI 이벤트 타입.
_PASSTHROUGH_TYPES = frozenset({"rate_limit_event"})


def _make_transport(prompt, options: ClaudeCodeOptions):
    return make_transport(prompt, options, _PASSTHROUGH_TYPES)


def _run_in_proactor(coro):
    return run_in_proactor(coro)


async def _prompt_stream(prompt: str):
    yield {"type": "user", "message": {"role": "user", "content": prompt}}


async def _call_async(
    prompt: str,
    session_id: Optional[str],
    json_schema: Optional[dict],
) -> tuple[str, Optional[str]]:
    # safe-mode: CLAUDE.md/skills/plugins/hooks 등 다 끄고 순수 판단만 받는다.
    # --bare와 달리 기존 OAuth/keychain 인증을 그대로 쓸 수 있음(API 키 불필요).
    extra_args: dict[str, Optional[str]] = {"strict-mcp-config": None, "tools": "", "safe-mode": None}
    if json_schema is not None:
        extra_args["json-schema"] = _json.dumps(json_schema)

    options = ClaudeCodeOptions(
        resume=session_id,
        cwd=str(Path.home()),
        extra_args=extra_args,
    )

    parts: list[str] = []
    new_session_id: Optional[str] = session_id
    pg = _prompt_stream(prompt)

    # SDK의 async generator를 직접 순회한다 — asyncio.wait_for로 __anext__를
    # 감싸면 anyio cancel scope가 다른 task에서 exit되는 오류가 남(구조적 문제,
    # drt-notebookLM의 검증된 패턴처럼 generator를 그대로 소비해야 함).
    # 타임아웃이 필요하면 이 함수를 호출하는 스레드 쪽에서 join(timeout=)으로 건다.
    async for msg in _sdk_query(prompt=pg, options=options, transport=_make_transport(pg, options)):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    parts.append(block.text)
        elif isinstance(msg, ResultMessage):
            if msg.subtype != "success":
                raise RuntimeError(f"claude 실행 실패: subtype={msg.subtype}")
            if msg.session_id:
                new_session_id = msg.session_id
            if msg.result and not parts:
                parts.append(msg.result)

    return "".join(parts), new_session_id


def call_claude_resumable(
    prompt: str,
    session_id: Optional[str] = None,
    json_schema: Optional[dict] = None,
    timeout: float = 120.0,
) -> tuple[Optional[str], Optional[str]]:
    """resume 가능한 단발 Claude 호출. (응답 텍스트, 다음에 이어쓸 session_id) 반환.

    실패/타임아웃 시 (None, session_id) — session_id는 그대로 유지해 다음 호출에서 재시도 가능.
    별도 스레드에서 전용 ProactorEventLoop로 실행하고 join(timeout=)으로 시간 제한을 건다
    (SDK의 async generator 자체에 asyncio.wait_for를 걸면 anyio cancel scope 오류가 남).
    """
    import threading

    result: list = [None, session_id]

    def _run():
        try:
            result[0], result[1] = _run_in_proactor(_call_async(prompt, session_id, json_schema))
        except Exception as e:
            logger.warning("call_claude_resumable 실패: %s", e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        logger.warning("call_claude_resumable 타임아웃 (%.0fs)", timeout)
        return None, session_id
    return result[0], result[1]
