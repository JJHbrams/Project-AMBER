"""engram (Copilot CLI) subprocess 백엔드."""
import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from core.memory.bus import MemorySession

ENGRAM_CMD = Path.home() / ".engram" / "engram-copilot.cmd"

logger = logging.getLogger(__name__)

try:
    from core.memory.bus import MemorySession, memory_bus as _memory_bus
    _STM_AVAILABLE = True
except Exception as _import_err:
    _memory_bus = None  # type: ignore[assignment]
    _STM_AVAILABLE = False
    logger.warning("STM 비활성: core.memory import 실패 (%s)", _import_err)


class EngramBackend:
    def __init__(self):
        self._lock = threading.Lock()
        self._session: Optional[MemorySession] = None
        if _STM_AVAILABLE:
            try:
                self._session = _memory_bus.start_session(scope_key="overlay")
                logger.info("STM 세션 시작: id=%d scope=overlay", self._session.session_id)
            except Exception as e:
                logger.warning("STM 세션 초기화 실패 (비활성화): %s", e)

    def ask(self, text: str, callback: Callable[[str], None]):
        """비동기로 engram에 질문하고 응답을 callback으로 전달."""
        threading.Thread(target=self._run, args=(text, callback), daemon=True).start()

    def _run(self, text: str, callback: Callable[[str], None]):
        if self._session is not None:
            try:
                _memory_bus.record_user_message(self._session, text)
            except Exception as e:
                logger.debug("STM user 기록 실패: %s", e)

        try:
            # ENGRAM_RUNTIME_ROLE 은 overlay 프로세스 전용 — 자식 프로세스에 전파하지 않음
            child_env = {k: v for k, v in os.environ.items() if k != "ENGRAM_RUNTIME_ROLE"}
            result = subprocess.run(
                ["cmd", "/c", str(ENGRAM_CMD), "-p", text],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=120,
                env=child_env,
            )
            response = result.stdout.strip() or result.stderr.strip() or "(응답 없음)"
        except FileNotFoundError:
            response = f"[오류] engram를 찾을 수 없습니다: {ENGRAM_CMD}"
        except subprocess.TimeoutExpired:
            response = "[오류] 응답 시간 초과 (120초)"
        except Exception as e:
            response = f"[오류] {e}"

        if self._session is not None:
            try:
                _memory_bus.record_assistant_message(self._session, response)
            except Exception as e:
                logger.debug("STM assistant 기록 실패: %s", e)

        callback(response)

    def close(self):
        """세션 종료 시 STM → LTM 승격 트리거 (Discord/programmatic 세션용)."""
        if self._session is not None:
            try:
                from core.graph.semantic import maybe_promote
                maybe_promote(scope_key=self._session.scope_key, session_id=self._session.session_id)
            except Exception as e:
                logger.debug("STM promote on close 실패: %s", e)


