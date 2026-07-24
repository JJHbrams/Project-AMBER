"""can_use_tool 콜백 ↔ tkinter 승인 UI 브릿지.

권한 수준(overlay/config.py의 permission_level)에 따라 동작이 다르다:
- auto: session.py가 이 브로커를 아예 안 붙인다(permission_mode="bypassPermissions").
- confirm_risky: 조회성 도구(_SAFE_TOOL_PREFIXES)는 자동 승인, 나머지는 승인 요청.
- confirm_always: 모든 도구 호출마다 승인 요청.

can_use_tool은 asyncio 이벤트루프 스레드에서 await되는 코루틴이다. 그 안에서
threading.Event().wait()로 블로킹하면 이벤트루프 전체(다른 SDK I/O 포함)가 멈추므로,
반드시 concurrent.futures.Future + asyncio.wrap_future 조합만 쓴다 — tkinter 메인스레드는
future.set_result()만 호출하면 되고(스레드 세이프), 이벤트루프 스레드는 그 future를
non-blocking하게 기다린다.
"""

import asyncio
import logging
import uuid
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Callable, Optional

from claude_code_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

logger = logging.getLogger(__name__)

# confirm_risky 수준에서 자동 승인하는 조회/읽기 전용 도구.
_SAFE_TOOL_PREFIXES = (
    "Read",
    "Glob",
    "Grep",
    "NotebookRead",
    "TodoRead",
    "mcp__engram__kg_",
    "mcp__engram__engram_get_",
    "mcp__engram__engram_status",
    "mcp__engram__engram_list_",
    "mcp__engram__engram_search_memories",
    "mcp__engram__engram_peek_stm",
)


def _is_safe_tool(tool_name: str) -> bool:
    return isinstance(tool_name, str) and tool_name.startswith(_SAFE_TOOL_PREFIXES)


@dataclass
class ApprovalRequest:
    id: str
    tool_name: str
    tool_input: dict[str, Any]
    future: "Future[PermissionResultAllow | PermissionResultDeny]"

    def allow(self) -> None:
        """tkinter 메인스레드에서 호출 — 승인 버튼 핸들러용."""
        if not self.future.done():
            self.future.set_result(PermissionResultAllow(behavior="allow"))

    def deny(self, message: str = "사용자 거부") -> None:
        """tkinter 메인스레드에서 호출 — 거부 버튼 핸들러용."""
        if not self.future.done():
            self.future.set_result(PermissionResultDeny(behavior="deny", message=message, interrupt=False))


class ToolApprovalBroker:
    """can_use_tool 콜백 구현체."""

    def __init__(
        self,
        permission_level: str,
        on_request: Optional[Callable[[ApprovalRequest], None]] = None,
        timeout: float = 60.0,
    ):
        self._permission_level = permission_level
        self._on_request = on_request
        self._timeout = timeout

    def _should_auto_allow(self, tool_name: str) -> bool:
        if self._permission_level == "confirm_always":
            return False
        if self._permission_level == "confirm_risky":
            return _is_safe_tool(tool_name)
        return True  # auto (보통은 session.py가 아예 안 붙이지만, 방어적으로)

    async def can_use_tool(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ):
        if self._on_request is None or self._should_auto_allow(tool_name):
            return PermissionResultAllow(behavior="allow")

        future: "Future[Any]" = Future()
        request = ApprovalRequest(id=str(uuid.uuid4()), tool_name=tool_name, tool_input=tool_input, future=future)
        try:
            self._on_request(request)
        except Exception:
            logger.exception("[bubble] 승인 요청 콜백 실패 — 기본 거부: %s", tool_name)
            return PermissionResultDeny(behavior="deny", message="승인 UI 오류로 거부됨", interrupt=False)

        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout=self._timeout)
        except asyncio.TimeoutError:
            future.cancel()
            logger.warning("[bubble] 도구 승인 시간 초과(%.0fs) — 기본 거부: %s", self._timeout, tool_name)
            return PermissionResultDeny(behavior="deny", message="사용자 응답 시간 초과 — 기본 거부", interrupt=False)
