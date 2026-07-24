"""말풍선 세션의 턴을 STM에 기록한다.

overlay/backend.py(레거시 Copilot 백엔드)가 이미 증명한 패턴 그대로 — 같은 프로세스
안에서 core.memory.bus.memory_bus를 HTTP 없이 직접 호출한다. bubble 세션은 SDK 이벤트를
직접 받는 유일한 소유자라 /stm/message의 request_id dedup이 필요 없고, 매 턴마다
localhost HTTP 왕복을 거칠 이유가 없다.

thinking/tool_use/tool_result는 저장하지 않는다 — STM 히스토리는 최종 답변 중심으로
유지한다(_stm_transcript_capture_loop의 "최종 텍스트만 추출" 원칙과 동일).
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from core.memory.bus import MemorySession, memory_bus as _memory_bus

    _STM_AVAILABLE = True
except Exception as _import_err:  # pragma: no cover - STM 모듈 자체가 없는 극단적 상황 대비
    _memory_bus = None  # type: ignore[assignment]
    _STM_AVAILABLE = False
    logger.warning("[bubble] STM 비활성: core.memory import 실패 (%s)", _import_err)


class StmBridge:
    def __init__(self, scope_key: str = "overlay"):
        self._scope_key = scope_key
        self._session: "Optional[MemorySession]" = None

    def open(self) -> None:
        if not _STM_AVAILABLE or self._session is not None:
            return
        try:
            self._session = _memory_bus.start_session(scope_key=self._scope_key)
            logger.info("[bubble] STM 세션 시작: id=%d scope=%s", self._session.session_id, self._scope_key)
        except Exception:
            logger.exception("[bubble] STM 세션 시작 실패")

    def record_user(self, text: str) -> None:
        if self._session is None:
            return
        try:
            _memory_bus.record_user_message(self._session, text)
        except Exception:
            logger.exception("[bubble] STM user 메시지 기록 실패")

    def record_assistant(self, text: str) -> None:
        if self._session is None:
            return
        try:
            _memory_bus.record_assistant_message(self._session, text)
        except Exception:
            logger.exception("[bubble] STM assistant 메시지 기록 실패")

    def close(self, summary: str = "") -> None:
        """overlay/main.py의 _claude_code_watchdog_loop._trigger_close_session과 동일한 4단계."""
        if self._session is None:
            return
        session_id = self._session.session_id
        try:
            from core.memory import close_session

            close_session(session_id, summary)
        except Exception:
            logger.exception("[bubble] STM close_session 실패")

        try:
            from core.graph.semantic import update_working_memory_from_recent_session

            update_working_memory_from_recent_session(scope_key=self._scope_key)
        except Exception:
            logger.exception("[bubble] working_memory 갱신 실패")

        try:
            from core.graph.semantic import flag_reflection_event_from_recent_session

            flag_reflection_event_from_recent_session(scope_key=self._scope_key)
        except Exception:
            logger.exception("[bubble] reflection event 감지 실패")

        try:
            from core.graph.semantic import maybe_promote

            maybe_promote(scope_key=self._scope_key)
        except Exception:
            logger.exception("[bubble] STM promote 실패")

        self._session = None
