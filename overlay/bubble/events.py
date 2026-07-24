"""claude_code_sdk 메시지/블록을 UI가 소비하는 BubbleEvent(dict)로 변환한다.

세션(session.py)이 include_partial_messages=True로 여는 것을 전제로,
text/thinking은 StreamEvent의 raw delta에서만 뽑는다(중복 방지). AssistantMessage의
완성된 블록에서는 tool_use만 뽑는다 — ToolUseBlock.input은 delta(input_json_delta)로
조금씩 오는데 v1에서는 JSON diff 누적을 안 하고 완성된 input을 한 번에 쓴다.
tool_result는 AssistantMessage가 아니라 다음 UserMessage.content에 담겨 돌아온다.
"""

from typing import Any

from claude_code_sdk.types import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

_EVENT_DEFAULTS = {
    "id": None,
    "delta": False,
    "text": None,
    "tool_name": None,
    "tool_input": None,
    "tool_output": None,
    "is_error": False,
}


def _event(kind: str, turn_seq: int, **fields) -> dict:
    ev = {"kind": kind, "turn_seq": turn_seq, **_EVENT_DEFAULTS}
    ev.update(fields)
    return ev


def _tool_output_to_text(content: "str | list[dict[str, Any]] | None") -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("text"):
            parts.append(str(item["text"]))
    return "\n".join(parts)


def _thinking_progress_text(tokens: int) -> str:
    """estimated_tokens만 있고 실제 추론 텍스트는 레닥션된 경우의 표시 문구.

    토큰 수 자체는 사용자에게 의미 없는 숫자라 그대로 보여주지 않는다 — 대신 그
    수치가 실제로 반영하는 유일한 신호(지금 얼마나 오래/깊게 생각하고 있는지)만
    "생각의 깊이" 정도로 거칠게 구간화해서 보여준다. 무엇에 대해 생각하는지(주제)는
    이 데이터에 전혀 담겨 있지 않으므로 — 지어내지 않는다."""
    if tokens < 50:
        return "잠깐 생각 중…"
    if tokens < 200:
        return "생각을 정리하는 중…"
    return "깊이 고민하는 중…"


def stream_event_to_bubble_events(msg: StreamEvent, turn_seq: int) -> list[dict]:
    """content_block_delta의 text_delta/thinking_delta만 speech/thought로 변환."""
    event = msg.event or {}
    if event.get("type") != "content_block_delta":
        return []
    delta = event.get("delta") or {}
    dtype = delta.get("type")
    block_index = event.get("index")
    if dtype == "text_delta":
        return [_event("speech", turn_seq, id=f"block-{block_index}", text=delta.get("text", ""), delta=True)]
    if dtype == "thinking_delta":
        thinking_text = delta.get("thinking", "")
        if thinking_text:
            return [_event("thought", turn_seq, id=f"block-{block_index}", text=thinking_text, delta=True)]
        # Claude Code CLI는 실제 추론 텍스트를 비워서 보내고(레닥션/요약 정책)
        # estimated_tokens만 스트리밍하는 경우가 있다 — 이때 생각풍선을 계속 비워두면
        # (숨김 상태로 남음) 모델이 생각하고 있다는 게 전혀 안 보이므로, 토큰 수
        # 진행 표시로 대체한다. delta=False로 보내 매번 최신 수치로 교체한다.
        tokens = delta.get("estimated_tokens")
        if tokens is not None:
            return [
                _event("thought", turn_seq, id=f"block-{block_index}", text=_thinking_progress_text(tokens), delta=False)
            ]
        return []
    return []


def assistant_message_to_bubble_events(msg: AssistantMessage, turn_seq: int) -> list[dict]:
    """tool_use 발행 + thinking 블록 보완 발행.

    text/thinking은 보통 StreamEvent 델타로 이미 흘려보냈다고 가정하지만, 실제로는
    (도구 호출과 섞인 interleaved thinking 등 상황에 따라) thinking_delta 없이
    완성된 ThinkingBlock만 AssistantMessage에 실려 오는 경우가 있다 — 이때 델타를
    안 기다리면 생각풍선에 아예 안 뜬다. delta=False(전체 텍스트로 교체)로 보내면
    이미 델타로 다 흘러온 경우엔 같은 텍스트로 덮어써서 무해하고, 델타가 없었던
    경우엔 이게 유일한 소스가 되어 누락을 막는다."""
    events: list[dict] = []
    for block in msg.content:
        if isinstance(block, ToolUseBlock):
            events.append(_event("tool_use", turn_seq, id=block.id, tool_name=block.name, tool_input=block.input))
        elif isinstance(block, ThinkingBlock) and block.thinking:
            events.append(_event("thought", turn_seq, text=block.thinking, delta=False))
    return events


def user_message_to_bubble_events(msg: UserMessage, turn_seq: int) -> list[dict]:
    """tool_result는 다음 UserMessage.content에 ToolResultBlock으로 돌아온다."""
    events: list[dict] = []
    content = msg.content
    if isinstance(content, str):
        return events
    for block in content:
        if isinstance(block, ToolResultBlock):
            events.append(
                _event(
                    "tool_result",
                    turn_seq,
                    id=block.tool_use_id,
                    tool_output=_tool_output_to_text(block.content),
                    is_error=bool(block.is_error),
                )
            )
    return events


def result_message_to_bubble_event(msg: ResultMessage, turn_seq: int) -> dict:
    if msg.subtype != "success":
        return _event("error", turn_seq, text=f"claude 세션 오류: subtype={msg.subtype}")
    return _event("turn_end", turn_seq, text=msg.result)


def extract_final_text(msg: AssistantMessage) -> str:
    """STM 저장용 — TextBlock만 이어붙인 최종 텍스트(thinking/tool은 제외)."""
    return "".join(block.text for block in msg.content if isinstance(block, TextBlock))


def error_event(text: str, turn_seq: int) -> dict:
    return _event("error", turn_seq, text=text)
