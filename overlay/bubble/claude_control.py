"""Private controllable Claude stream used by rich bubble requests.

It deliberately exposes no raw diagnostics.  A request key travels with every
parsed SDK message so a host can reject old-attempt events before rendering.
"""
from __future__ import annotations
import asyncio, base64, json
from dataclasses import replace
from typing import Any, AsyncIterator, Callable
from claude_code_sdk._internal.query import Query
from claude_code_sdk._internal.message_parser import parse_message
from core.integrations.claude_cli_transport import make_transport
from overlay.bubble.rich_input import Attachment
from overlay.bubble.turn_queue import TurnKey as RequestKey

def rich_user_payload(text: str, attachments: tuple[Attachment, ...], key: RequestKey) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if text: content.append({"type":"text", "text":text})
    for attachment in attachments:
        content.append({"type":"image", "source":{"type":"base64", "media_type":attachment.mime_type, "data":base64.b64encode(attachment.data).decode("ascii")}})
    if not content: raise ValueError("empty rich request")
    return {"type":"user","message":{"role":"user","content":content if len(content)>1 or attachments else text}}

class ClaudeControl:
    def __init__(self, options: Any, prompt, *, transport_factory=make_transport, on_message: Callable[[Any, RequestKey], None] | None=None):
        if options.can_use_tool and options.permission_prompt_tool_name:
            raise ValueError("conflicting permission callback options")
        self.options=replace(options, permission_prompt_tool_name="stdio") if getattr(options, "can_use_tool", None) else options
        self._prompt=prompt
        self._factory, self._on_message=transport_factory, on_message or (lambda _m,_k:None)
        self._query: Query | None=None; self._transport=None; self._active: RequestKey|None=None
    async def connect(self) -> None:
        stream=self._input_stream()
        self._transport=self._factory(stream, self.options, frozenset({"rate_limit_event"}))
        await self._transport.connect()
        hooks={event:[{"matcher":getattr(m,"matcher",None),"hooks":m.hooks} for m in matchers] for event,matchers in (self.options.hooks or {}).items()}
        servers={name:config["instance"] for name,config in self.options.mcp_servers.items() if isinstance(config,dict) and config.get("type")=="sdk"} if isinstance(self.options.mcp_servers,dict) else {}
        self._query=Query(self._transport, is_streaming_mode=True, can_use_tool=self.options.can_use_tool, hooks=hooks or None, sdk_mcp_servers=servers)
        await self._query.start(); await self._query.initialize()
        assert self._query._tg is not None
        self._query._tg.start_soon(self._query.stream_input, stream)
    async def _input_stream(self):
        async for envelope in self._prompt:
            envelope=dict(envelope)
            key = envelope.pop("_private_request_key", None)
            if not isinstance(key, RequestKey): continue
            self._active=key
            yield envelope
    async def interrupt(self, key: RequestKey) -> bool:
        if not self._query or key != self._active: return False
        await self._query.interrupt() # control acknowledgement only; terminal comes from ResultMessage.
        return True
    async def messages(self) -> AsyncIterator[tuple[Any, RequestKey]]:
        if not self._query: raise RuntimeError("not connected")
        async for raw in self._query.receive_messages():
            key=self._active
            message=parse_message(raw)
            if key is None and message.__class__.__name__ != "SystemMessage": continue
            self._on_message(message,key)
            # Result terminal is the only point at which a new request is allowed.
            if message.__class__.__name__ == "ResultMessage": self._active=None
            yield message,key
    async def close(self) -> None:
        if self._query: await self._query.close()
        elif self._transport: await self._transport.close()
        self._query=None; self._transport=None; self._active=None
