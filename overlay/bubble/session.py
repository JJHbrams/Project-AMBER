"""말풍선 모드의 상주 Claude Agent SDK 세션 매니저.

reflection_client.py가 증명한 패턴(claude_cli_transport의 Windows CLI 경로 탐색 +
ProactorEventLoop)을 1회성 호출에서 "세션 내내 도는 상주 루프"로 확장한다.

스레딩 모델:
- 전용 스레드 하나에서 ProactorEventLoop를 돌리고, 그 안에서 query()를 세션 수명 동안
  단 한 번만 호출해 계속 소비한다(reflection_client의 1회성 호출과 다른 점).
- send()는 어느 스레드에서 불러도 안전 — loop.call_soon_threadsafe로 asyncio.Queue에 넣는다.
- on_event/on_approval_request 콜백은 이 클래스의 이벤트루프 스레드에서 직접 호출된다.
  tkinter 쪽으로 넘길 때는 호출부가 root.after(0, ...)로 감싸야 한다(이 클래스는 tkinter를
  모른다).
"""

import asyncio
import json
import logging
import os
import threading
from typing import Any, Callable, Optional

from claude_code_sdk import query as _sdk_query
from claude_code_sdk.types import (
    AssistantMessage,
    ClaudeCodeOptions,
    ResultMessage,
    StreamEvent,
    UserMessage,
)

from core.integrations.claude_cli_transport import make_transport, run_in_proactor
from overlay.bubble.approval import ApprovalRequest, ToolApprovalBroker
from overlay.bubble.events import (
    assistant_message_to_bubble_events,
    error_event,
    extract_final_text,
    result_message_to_bubble_event,
    stream_event_to_bubble_events,
    user_message_to_bubble_events,
)
from overlay.config import normalize_permission_level

logger = logging.getLogger(__name__)

_PASSTHROUGH_TYPES = frozenset({"rate_limit_event"})
_MAX_ATTEMPTS = 3  # 최초 시도 + 동일 세션 재시도 1회 + 새 세션 재시도 1회
_RETRY_BACKOFF_SECS = 2.0

# 말풍선 모드 전용 출력 스타일 가이드 — append_system_prompt로만 붙이므로 전역
# CLAUDE.md/기본 시스템 프롬프트는 그대로 두고 "이 세션에서의 표현 방식"만 덧댄다.
# 렌더러(markdown_parser + tk.Text)가 아래 문법을 실제로 스타일링하므로, 모델이
# 이 문법을 써 줄수록 말풍선이 보기 좋아진다(특히 인용은 > 를 안 쓰면 그냥 평문으로
# 나와 구분이 안 됨 — 사용자 피드백).
_BUBBLE_STYLE_PROMPT = (
    "다음 응답들은 데스크톱 캐릭터의 '말풍선' UI에 렌더링된다. 작은 말풍선에 담기므로:\n"
    "- 간결하게. 한 번에 핵심만, 불필요한 서론/반복은 생략.\n"
    "- 구조가 있으면 마크다운으로 표현: 제목은 #/##, 목록은 -/1., 강조는 **굵게**/*기울임*,\n"
    "  코드·식별자·경로는 `백틱`, 여러 줄 코드는 ``` 펜스, 표는 | 파이프 표, 구분은 ---.\n"
    "- 인용문·발췌·대사를 보여줄 땐 반드시 각 줄 앞에 '> '(마크다운 인용) 을 붙여라 —\n"
    "  안 그러면 본문과 구분되지 않는다.\n"
    "- 링크는 [텍스트](url) 형식."
)


class BubbleSessionManager:
    def __init__(
        self,
        cwd: str,
        env_overrides: Optional[dict[str, str]] = None,
        permission_level: str = "auto",
        on_event: Optional[Callable[[dict], None]] = None,
        on_approval_request: Optional[Callable[[ApprovalRequest], None]] = None,
        approval_timeout: float = 60.0,
        resume_session_id: Optional[str] = None,
        on_session_id: Optional[Callable[[str], None]] = None,
        stm_bridge: Any = None,
        thinking_tokens: int = 0,
        bootstrap_prompt: Optional[str] = None,
    ):
        self._cwd = cwd
        self._env_overrides = dict(env_overrides or {})
        self._thinking_tokens = int(thinking_tokens or 0)
        self._bootstrap_prompt = (bootstrap_prompt or "").strip() or None
        self._on_event = on_event or (lambda ev: None)
        self._resume_session_id = resume_session_id
        self._on_session_id = on_session_id
        self._stm_bridge = stm_bridge

        self._permission_level = normalize_permission_level(permission_level)
        if self._permission_level == "auto":
            self._permission_mode = "bypassPermissions"
            self._can_use_tool = None
        else:
            self._permission_mode = "default"
            broker = ToolApprovalBroker(self._permission_level, on_request=on_approval_request, timeout=approval_timeout)
            self._can_use_tool = broker.can_use_tool

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._prompt_queue: Optional[asyncio.Queue] = None
        self._consume_task: Optional[asyncio.Task] = None
        self._thread: Optional[threading.Thread] = None
        self._alive = threading.Event()
        self._ready = threading.Event()
        self._turn_seq = 0
        self._current_turn_seq = 0
        self._assistant_text_buf: list[str] = []

    # ── 공개 API ──────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if self._stm_bridge is not None:
            try:
                self._stm_bridge.open()
            except Exception:
                logger.exception("[bubble] STM open 실패")
        self._ready.clear()
        self._alive.clear()
        self._thread = threading.Thread(target=self._thread_main, daemon=True, name="bubble-sdk-loop")
        self._thread.start()
        self._ready.wait(timeout=10.0)

    def is_alive(self) -> bool:
        return self._alive.is_set()

    def send(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        if self._loop is None or self._prompt_queue is None:
            logger.warning("[bubble] send() 호출됐지만 세션이 준비되지 않음 — 무시: %r", text)
            return
        self._turn_seq += 1
        turn_seq = self._turn_seq
        payload = {"type": "user", "message": {"role": "user", "content": text}}
        self._loop.call_soon_threadsafe(self._prompt_queue.put_nowait, (turn_seq, payload))

    def stop(self, timeout: float = 5.0) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._cancel_consume_task)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("[bubble] 세션 스레드가 %.1fs 안에 종료되지 않음", timeout)
        self._thread = None
        self._loop = None
        self._prompt_queue = None
        if self._stm_bridge is not None:
            try:
                self._stm_bridge.close()
            except Exception:
                logger.exception("[bubble] STM close 실패")

    # ── 내부 ──────────────────────────────────────────────────────────

    def _cancel_consume_task(self) -> None:
        if self._consume_task is not None and not self._consume_task.done():
            self._consume_task.cancel()

    def _thread_main(self) -> None:
        run_in_proactor(self._run_forever())

    async def _run_forever(self) -> None:
        self._loop = asyncio.get_event_loop()
        self._prompt_queue = asyncio.Queue()
        self._alive.set()
        self._ready.set()

        attempts = [self._resume_session_id, self._resume_session_id, None][:_MAX_ATTEMPTS]
        for attempt_idx, resume_id in enumerate(attempts):
            self._resume_session_id = resume_id
            try:
                self._consume_task = asyncio.ensure_future(self._consume())
                await self._consume_task
                break
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("[bubble] 세션 시도 %d/%d 실패(resume=%s): %s", attempt_idx + 1, len(attempts), resume_id, exc)
                self._emit(error_event(f"Claude 세션 오류, 재시도 중... ({exc})", self._current_turn_seq))
                await asyncio.sleep(_RETRY_BACKOFF_SECS)
        else:
            logger.error("[bubble] 모든 재시도 실패 — 세션을 시작할 수 없습니다.")
            self._emit(error_event("Claude 세션을 시작할 수 없습니다.", self._current_turn_seq))

        self._alive.clear()

    async def _consume(self) -> None:
        options = self._build_options()
        prompt_gen = self._prompt_generator()
        transport = make_transport(prompt_gen, options, _PASSTHROUGH_TYPES)
        async for msg in _sdk_query(prompt=prompt_gen, options=options, transport=transport):
            self._handle_message(msg)

    async def _prompt_generator(self):
        assert self._prompt_queue is not None
        while True:
            turn_seq, payload = await self._prompt_queue.get()
            self._current_turn_seq = turn_seq
            self._assistant_text_buf = []
            if self._stm_bridge is not None:
                try:
                    self._stm_bridge.record_user(payload["message"]["content"])
                except Exception:
                    logger.exception("[bubble] STM 사용자 메시지 기록 실패")
            yield payload

    def _build_options(self) -> ClaudeCodeOptions:
        env = {**os.environ, **self._env_overrides}
        # 확장 사고(extended thinking) 예산을 켜서 실제 추론 텍스트가 thinking_delta로
        # 스트리밍되게 한다 — 예산이 0이면 CLI가 추론을 거의 안 하고 estimated_tokens
        # 자리표시자만 흘려 생각풍선이 "생각 중…"에서 멈춘다. 사용자가 env로 이미
        # 지정했으면 그 값을 존중한다. 0으로 명시하면 끈다(폴백 표시로 되돌아감).
        if self._thinking_tokens and "MAX_THINKING_TOKENS" not in env:
            env["MAX_THINKING_TOKENS"] = str(self._thinking_tokens)
        extra_args: dict[str, Optional[str]] = {}
        if self._can_use_tool is not None:
            # 사용자 전역 ~/.claude/settings.json에 skipDangerousModePermissionPrompt/
            # skipAutoPermissionPrompt가 켜져 있으면(headless 판단용 reflection_client
            # 경로를 위해 설정된 값) can_use_tool을 거치지 않고 CLI가 먼저 거부해버린다.
            # confirm_risky/confirm_always에서는 실제로 승인 풍선을 띄워야 하므로
            # 세션 단위로 두 플래그를 명시적으로 꺼서 우리 브로커까지 오게 만든다.
            extra_args["settings"] = json.dumps(
                {"skipDangerousModePermissionPrompt": False, "skipAutoPermissionPrompt": False}
            )
        # auto_inject(session.auto_inject) 가 켜지면 스타일 프롬프트 뒤에 engram 부트스트랩
        # 지시문을 덧댄다 — 첫 응답 전 get_context_once 를 1회 부르도록 유도(중복은 무해).
        append_prompt = _BUBBLE_STYLE_PROMPT
        if self._bootstrap_prompt:
            append_prompt = f"{_BUBBLE_STYLE_PROMPT}\n\n{self._bootstrap_prompt}"
        return ClaudeCodeOptions(
            resume=self._resume_session_id,
            cwd=self._cwd,
            env=env,
            permission_mode=self._permission_mode,
            can_use_tool=self._can_use_tool,
            include_partial_messages=True,
            append_system_prompt=append_prompt,
            extra_args=extra_args,
        )

    def _handle_message(self, msg: Any) -> None:
        turn_seq = self._current_turn_seq
        if isinstance(msg, StreamEvent):
            for ev in stream_event_to_bubble_events(msg, turn_seq):
                self._emit(ev)
        elif isinstance(msg, AssistantMessage):
            text = extract_final_text(msg)
            if text:
                self._assistant_text_buf.append(text)
            for ev in assistant_message_to_bubble_events(msg, turn_seq):
                self._emit(ev)
        elif isinstance(msg, UserMessage):
            for ev in user_message_to_bubble_events(msg, turn_seq):
                self._emit(ev)
        elif isinstance(msg, ResultMessage):
            if msg.session_id:
                self._persist_session_id(msg.session_id)
            final_text = "".join(self._assistant_text_buf)
            if self._stm_bridge is not None and final_text:
                try:
                    self._stm_bridge.record_assistant(final_text)
                except Exception:
                    logger.exception("[bubble] STM 어시스턴트 메시지 기록 실패")
            self._emit(result_message_to_bubble_event(msg, turn_seq))
        # SystemMessage 등은 무시

    def _persist_session_id(self, session_id: str) -> None:
        self._resume_session_id = session_id
        if self._on_session_id is not None:
            try:
                self._on_session_id(session_id)
            except Exception:
                logger.exception("[bubble] session_id 콜백 실패")

    def _emit(self, event: dict) -> None:
        try:
            self._on_event(event)
        except Exception:
            logger.exception("[bubble] on_event 콜백 실패: %r", event)
