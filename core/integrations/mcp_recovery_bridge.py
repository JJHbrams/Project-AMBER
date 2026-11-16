"""Persistent provider-facing stdio; event-driven upstream MCP replacement.

Only successful protocol initialization is replayed, with one exception:
requests the upstream explicitly rejects with "Session terminated" never reach
a handler (the dead Mcp-Session-Id is refused before dispatch), so each proven
request is re-sent once after the transport is re-initialized. Every other
transport loss keeps the no-replay rule: requests in flight are reported as
outcome_unknown and notifications are never queued or replayed.
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage

MAX_LINE = 1_000_000
MAX_INFLIGHT = 128
UNKNOWN = -32071
UNAVAILABLE = -32070
REPLAY_WINDOW = 30.0  # seconds a session-terminated request waits for the transport to recover
EXPIRY_DRAIN_WINDOW = 0.25  # let concurrently rejected requests prove pre-dispatch expiry


@dataclass(frozen=True)
class BridgeConfig:
    upstream_url: str
    state_url: str
    token: str
    root_id: str | None = None
    connect_timeout: float = 2.0
    first_ready_timeout: float = 120.0


def checked_url(value: str, path: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', '::1')
            or parsed.username is not None or parsed.password is not None
            or parsed.path.rstrip('/') != path or parsed.query or parsed.fragment
            or parsed.port is None or not 0 < parsed.port < 65536):
        raise ValueError('invalid loopback endpoint')
    return value


def error(request_id, code, message):
    return {'jsonrpc': '2.0', 'id': request_id,
            'error': {'code': code, 'message': message}}


def session_terminated(message):
    err = message.get('error') if isinstance(message, dict) else None
    return (isinstance(err, dict) and err.get('code') in (32600, -32600)
            and err.get('message') == 'Session terminated')


class StdioRecoveryBridge:
    def __init__(self, config: BridgeConfig, *, stdin=None, stdout=None, discovery_file=None):
        checked_url(config.upstream_url, '/mcp')
        checked_url(config.state_url, '/state/mcp/events')
        self.config = config
        # MCP stdio is UTF-8. Text-mode stdin on Windows decodes with the locale
        # code page (cp949), which rejects raw UTF-8 Korean; read bytes instead.
        self.stdin, self.stdout = stdin or sys.stdin.buffer, stdout or sys.stdout
        self.discovery = Path(discovery_file) if discovery_file else None
        self.output = asyncio.Queue(MAX_INFLIGHT)
        self.changed = asyncio.Event()
        self.resubscribe = asyncio.Event()
        self.finished = asyncio.Event()
        # The configured loopback MCP endpoint is the data-plane authority.
        # Overlay state is optional control metadata; it must never withdraw a
        # working configured connection merely because the overlay is stale.
        self.snapshot = {
            'instance_id': 'configured-endpoint', 'generation': 0,
            'ready': True, 'endpoint': config.upstream_url,
        }
        self.control_snapshot = None
        self.transport = None
        self.transport_key = None
        self.writer = None
        self.available = False
        self.epoch = 0
        self.serial = 0
        self.pending = {}
        self.callbacks = {}
        self.first_initialize = None
        self.initialize_params = None
        self.initialize_result = None
        self.frontend_initialized = False
        self.init_deadline_task = None
        # Only explicit Session terminated replies prove a request never reached
        # a handler.  Keep those requests separate from ordinary in-flight work.
        self.replays = {}
        self.expiry_recovering = False
        self.expiry_epoch = None
        self.expiry_timer = None
        self.deferred = []  # frontend requests not yet sent during explicit expiry recovery
        self.retry_delay = .1
        self.retry_handle = None

    def next_id(self, prefix):
        self.serial += 1
        return f'engram-{prefix}-{self.epoch}-{self.serial}'

    @staticmethod
    def state_key(state):
        if not isinstance(state, dict):
            return None
        return (state.get('instance_id'), state.get('generation'), state.get('ready'), state.get('endpoint'))

    def request_confirmed_expiry_rebind(self, failed_epoch, expected_key):
        """Reopen only the same verified ready endpoint after a pre-dispatch rejection."""
        if failed_epoch != self.epoch:
            return
        if expected_key is None or expected_key != self.state_key(self.snapshot) or not expected_key[2]:
            return
        # This callback runs after the failing connection task has returned.
        # Never overlap an old live transport if event-loop scheduling changes.
        if self.transport is not None and not self.transport.done():
            return
        self.changed.set()

    def schedule_transport_retry(self, failed_epoch, expected_key):
        """Retry a failed configured transport without polling its health."""
        if self.retry_handle is not None and not self.retry_handle.cancelled():
            return
        delay = self.retry_delay
        self.retry_delay = min(self.retry_delay * 2, 2.0)

        def retry():
            self.retry_handle = None
            if (failed_epoch == self.epoch and expected_key == self.state_key(self.snapshot)
                    and expected_key is not None and expected_key[2]
                    and (self.transport is None or self.transport.done())):
                self.changed.set()

        self.retry_handle = asyncio.get_running_loop().call_later(delay, retry)

    def note_control_state(self, state):
        """Accept compatible control hints without granting endpoint authority."""
        self.control_snapshot = state
        if state.get('ready') and state.get('endpoint') == self.config.upstream_url:
            self.changed.set()

    async def emit(self, message):
        await self.output.put(message)

    async def output_loop(self):
        while True:
            message = await self.output.get()
            wire = json.dumps(message, separators=(',', ':'), ensure_ascii=True) + '\n'
            await asyncio.to_thread(self._write, wire)

    def _write(self, wire):
        self.stdout.write(wire)
        self.stdout.flush()

    async def flush_expiry_recovery(self):
        """Expire only pre-dispatch-proven replays; unsent requests were never executed."""
        replays, self.replays = self.replays, {}
        deferred, self.deferred = self.deferred, []
        self.expiry_recovering = False
        self.expiry_epoch = None
        timer, self.expiry_timer = self.expiry_timer, None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        for stash in replays.values():
            await self.emit(error(stash['id'], UNKNOWN, 'outcome_unknown: backend transport lost'))
        for message in deferred:
            await self.emit(error(message['id'], UNAVAILABLE, 'backend_unavailable'))

    async def replay_expired(self):
        await asyncio.sleep(REPLAY_WINDOW)
        if self.expiry_recovering:
            await self.flush_expiry_recovery()

    def stash_for_replay(self, upstream_id):
        """Keep the request that got 'Session terminated' (pre-dispatch rejection) for one re-send."""
        item = self.pending.get(upstream_id)
        if (item is None or item['kind'] != 'request' or item.get('replayed')
                or item.get('message') is None):
            return False
        del self.pending[upstream_id]
        self.replays[item['id']] = {'id': item['id'], 'message': item['message'], 'epoch': self.epoch}
        if not self.expiry_recovering:
            self.expiry_recovering = True
            self.expiry_epoch = self.epoch
            self.expiry_timer = asyncio.create_task(self.replay_expired())
        return True

    async def replay_stashed(self):
        if not self.expiry_recovering or not self.available:
            return
        # Pop only immediately before registering each attempt.  If a later
        # send fails, untouched stashes remain for the connection finalizer to
        # report explicitly rather than being silently lost.
        while self.replays:
            frontend_id, stash = next(iter(self.replays.items()))
            self.replays.pop(frontend_id)
            upstream_id = self.next_id('request')
            # Registered before sending so a second loss reports it instead of replaying again.
            self.pending[upstream_id] = {'kind': 'request', 'id': stash['id'],
                                         'message': stash['message'], 'replayed': True}
            await self.send({**stash['message'], 'id': upstream_id})
        # Keep the expiry gate while releasing deferred requests.  Calling
        # frontend_message here would requeue them, so send each unsent request
        # directly and retain the remaining collection if a write fails.
        while self.deferred:
            message = self.deferred.pop(0)
            upstream_id = self.next_id('request')
            self.pending[upstream_id] = {'kind': 'request', 'id': message['id'], 'message': message}
            await self.send({**message, 'id': upstream_id})
        self.expiry_recovering = False
        self.expiry_epoch = None
        if self.expiry_timer is not None:
            self.expiry_timer.cancel()
            self.expiry_timer = None

    async def send(self, message):
        writer = self.writer
        if writer is None:
            raise ConnectionError('backend unavailable')
        await writer.send(SessionMessage(JSONRPCMessage.model_validate(message)))

    async def fail_transport(self):
        self.writer = None
        self.available = False
        pending, self.pending = self.pending, {}
        for item in pending.values():
            if item['kind'] == 'request':
                await self.emit(error(item['id'], UNKNOWN, 'outcome_unknown: backend transport lost'))
            elif item['kind'] == 'reinitialize' and not item['future'].done():
                item['future'].set_exception(ConnectionError('backend unavailable'))
        # Initial protocol setup may be retried; no application request is replayed.
        callbacks, self.callbacks = self.callbacks, {}
        for frontend_id in callbacks:
            await self.emit({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                             'params': {'requestId': frontend_id, 'reason': 'backend replaced'}})

    async def send_first_initialize(self):
        if (self.first_initialize is None or self.writer is None
                or any(p['kind'] == 'initialize' for p in self.pending.values())):
            return
        upstream_id = self.next_id('init')
        self.pending[upstream_id] = {'kind': 'initialize', 'id': self.first_initialize['id']}
        await self.send({**self.first_initialize, 'id': upstream_id})

    async def invalidate(self):
        await self.fail_transport()
        if self.transport is not None:
            self.transport.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.transport
            self.transport = None
        self.resubscribe.set()

    async def initialize_deadline(self, attempt):
        await asyncio.sleep(self.config.first_ready_timeout)
        if self.first_initialize is attempt:
            await self.emit(error(self.first_initialize['id'], UNAVAILABLE, 'backend_unavailable: initialization timed out'))
            self.first_initialize = None
            for key in [k for k, p in self.pending.items() if p['kind'] == 'initialize']:
                del self.pending[key]
            await self.invalidate()

    async def frontend_message(self, value):
        # Validate JSON-RPC before it reaches the SDK; do not log payloads.
        try:
            JSONRPCMessage.model_validate(value)
        except Exception:
            await self.emit(error(None, -32600, 'invalid request'))
            return
        method = value.get('method')
        if method == 'initialize':
            if self.initialize_result is not None:
                await self.emit({'jsonrpc': '2.0', 'id': value['id'], 'result': self.initialize_result})
                return
            if self.first_initialize is not None:
                await self.emit(error(value['id'], -32600, 'initialization already pending'))
                return
            self.first_initialize = value
            self.initialize_params = value.get('params', {})
            self.init_deadline_task = asyncio.create_task(self.initialize_deadline(value))
            if self.writer is not None:
                await self.send_first_initialize()
            return
        if method == 'notifications/initialized':
            if self.initialize_result is None:
                return  # Protocol requires the successful initialize response first.
            self.frontend_initialized = True
            if self.writer is not None and self.initialize_result is not None:
                await self.send(value)
                self.available = True
            return
        if method is None:  # Response to a backend-originated callback.
            backend_id = self.callbacks.pop(value.get('id'), None)
            if backend_id is not None and self.writer is not None:
                await self.send({**value, 'id': backend_id})
            return
        if method == 'notifications/cancelled':
            original_id = value.get('params', {}).get('requestId')
            if original_id in self.replays:
                self.replays.pop(original_id, None)  # the client no longer wants it; nothing to re-send
                return
            deferred = next((item for item in self.deferred if item.get('id') == original_id), None)
            if deferred is not None:
                self.deferred.remove(deferred)
                return
            match = next((key for key, item in self.pending.items()
                          if item.get('id') == original_id and item['kind'] == 'request'), None)
            if match is not None and self.writer is not None:
                self.pending.pop(match, None)
                await self.send({**value, 'params': {**value['params'], 'requestId': match}})
            return
        request = 'id' in value
        if request and self.expiry_recovering:
            if len(self.pending) + len(self.replays) + len(self.deferred) >= MAX_INFLIGHT:
                await self.emit(error(value['id'], UNAVAILABLE, 'bridge_backpressure'))
            else:
                self.deferred.append(value)
            return
        if not self.available:
            if request:
                await self.emit(error(value['id'], UNAVAILABLE, 'backend_unavailable'))
            return
        if not request:
            await self.send(value)
            return
        if len(self.pending) >= MAX_INFLIGHT:
            await self.emit(error(value['id'], UNAVAILABLE, 'bridge_backpressure'))
            return
        upstream_id = self.next_id('request')
        self.pending[upstream_id] = {'kind': 'request', 'id': value['id'], 'message': value}
        await self.send({**value, 'id': upstream_id})

    async def frontend_loop(self):
        try:
            while True:
                line = await asyncio.to_thread(self.stdin.readline, MAX_LINE + 1)
                if not line:
                    return
                if isinstance(line, str):
                    line = line.encode('utf-8')
                if len(line) > MAX_LINE:
                    await self.emit(error(None, -32600, 'line_too_large'))
                    return  # Fail closed; do not parse fragments of an oversized line.
                try:
                    value = json.loads(line.decode('utf-8'))
                except ValueError:  # includes UnicodeDecodeError: reject the line, keep the pipe
                    await self.emit(error(None, -32700, 'parse error'))
                    continue
                if not isinstance(value, dict):
                    await self.emit(error(None, -32600, 'invalid request'))
                    continue
                try:
                    await self.frontend_message(value)
                except Exception:
                    await self.invalidate()
        finally:
            self.finished.set()

    async def backend_message(self, message, epoch):
        if epoch != self.epoch:
            return
        method = message.get('method')
        if method is not None:
            if 'id' in message:
                if len(self.callbacks) >= MAX_INFLIGHT:
                    await self.send(error(message['id'], UNAVAILABLE, 'bridge_backpressure'))
                    return
                frontend_id = self.next_id('callback')
                self.callbacks[frontend_id] = message['id']
                await self.emit({**message, 'id': frontend_id})
            elif method == 'notifications/cancelled':
                backend_id = message.get('params', {}).get('requestId')
                frontend_id = next((k for k, v in self.callbacks.items() if v == backend_id), None)
                if frontend_id is not None:
                    self.callbacks.pop(frontend_id, None)
                    await self.emit({**message, 'params': {**message['params'], 'requestId': frontend_id}})
            else:
                await self.emit(message)
            return
        pending = self.pending.pop(message.get('id'), None)
        if pending is None:
            return  # Old/cancelled request or protocol response, never deliver twice.
        if pending['kind'] == 'reinitialize':
            if not pending['future'].done():
                pending['future'].set_result(message)
            return
        if pending['kind'] == 'initialize':
            if 'result' in message:
                self.initialize_result = message['result']
                # A successful first handshake proves the configured endpoint
                # is back.  Do not carry a prior outage's capped delay into
                # the next, unrelated transport failure.
                self.retry_delay = .1
            self.first_initialize = None
            if self.init_deadline_task:
                self.init_deadline_task.cancel()
        await self.emit({**message, 'id': pending['id']})

    async def connection(self, endpoint, epoch):
        read_task = None
        failed = False
        confirmed_expiry_key = None
        try:
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                    timeout=httpx.Timeout(self.config.connect_timeout, read=None)) as http:
                async with streamable_http_client(endpoint, http_client=http) as (reader, writer, _):
                    self.writer = writer

                    def fail_reinitialize():
                        # initialize 응답을 기다리는 쪽이 20초를 다 채우지 않게 바로 깨운다.
                        for pending in self.pending.values():
                            if pending['kind'] == 'reinitialize' and not pending['future'].done():
                                pending['future'].set_exception(ConnectionError('backend unavailable'))

                    async def read_upstream():
                        nonlocal confirmed_expiry_key
                        async for item in reader:
                            if isinstance(item, Exception):
                                fail_reinitialize()
                                raise item
                            message = item.message.model_dump(mode='json', by_alias=True, exclude_none=True)
                            if session_terminated(message):
                                # SDK는 404(세션 만료)를 예외가 아닌 JSON-RPC 에러로 전달하고 죽은
                                # Mcp-Session-Id를 계속 쓴다. 세션 검증에서 거절돼 핸들러에 닿지 않은
                                # 요청이므로 그 한 건만 재연결 후 한 번 다시 보낸다(이미 한 번 재전송했으면
                                # pending 에 남겨 outcome_unknown 처리).
                                if epoch == self.epoch and self.stash_for_replay(message.get('id')):
                                    key = self.state_key(self.snapshot)
                                    if key is not None and key[2]:
                                        confirmed_expiry_key = key
                                # Other already-sent requests may independently carry the
                                # same pre-dispatch proof.  Drain only briefly; all others
                                # retain the normal UNKNOWN outcome when fail_transport runs.
                                deadline = asyncio.get_running_loop().time() + EXPIRY_DRAIN_WINDOW
                                while any(p['kind'] == 'request' for p in self.pending.values()):
                                    remaining = deadline - asyncio.get_running_loop().time()
                                    if remaining <= 0:
                                        break
                                    try:
                                        extra = await asyncio.wait_for(reader.__anext__(), remaining)
                                    except (asyncio.TimeoutError, StopAsyncIteration):
                                        break
                                    if isinstance(extra, Exception):
                                        break
                                    extra_message = extra.message.model_dump(mode='json', by_alias=True, exclude_none=True)
                                    if session_terminated(extra_message) and epoch == self.epoch:
                                        self.stash_for_replay(extra_message.get('id'))
                                    else:
                                        await self.backend_message(extra_message, epoch)
                                fail_reinitialize()
                                raise ConnectionError('upstream session terminated')
                            await self.backend_message(message, epoch)
                        fail_reinitialize()
                        raise ConnectionError('backend closed')

                    read_task = asyncio.create_task(read_upstream())
                    try:
                        if self.initialize_result is not None:
                            ident = self.next_id('reinitialize')
                            future = asyncio.get_running_loop().create_future()
                            self.pending[ident] = {'kind': 'reinitialize', 'future': future}
                            await self.send({'jsonrpc': '2.0', 'id': ident, 'method': 'initialize',
                                             'params': self.initialize_params})
                            response = await asyncio.wait_for(future, 20)
                            if 'result' not in response:
                                raise ConnectionError('backend initialization failed')
                            # Capabilities already advertised to the client cannot be silently changed.
                            for key in ('protocolVersion', 'capabilities'):
                                if response['result'].get(key) != self.initialize_result.get(key):
                                    raise ConnectionError('backend capabilities changed')
                            await self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
                            self.available = self.frontend_initialized
                            self.retry_delay = .1
                            await self.replay_stashed()
                        else:
                            await self.send_first_initialize()
                        await read_task
                    finally:
                        if read_task is not None:
                            read_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError, Exception):
                                await read_task
        except asyncio.CancelledError:
            raise
        except Exception:
            # Error details can contain response payloads; never print them.
            failed = True
        finally:
            if epoch == self.epoch:
                await self.fail_transport()
            if (failed and self.expiry_recovering and (self.replays or self.deferred)
                    and epoch != self.expiry_epoch):
                await self.flush_expiry_recovery()  # the recovery attempt itself failed
            if failed:
                # Reconcile only after this task becomes done; otherwise a fresh
                # snapshot could race with manage() and be mistaken for a live pipe.
                asyncio.get_running_loop().call_soon(self.resubscribe.set)
                if confirmed_expiry_key is not None:
                    asyncio.get_running_loop().call_soon(
                        self.request_confirmed_expiry_rebind, epoch, confirmed_expiry_key
                    )
                else:
                    key = self.state_key(self.snapshot)
                    asyncio.get_running_loop().call_soon(
                        self.schedule_transport_retry, epoch, key
                    )

    async def manage(self):
        while True:
            await self.changed.wait()
            self.changed.clear()
            state = self.snapshot
            if state is None:
                continue
            key = self.state_key(state)
            if self.transport is not None and not self.transport.done() and self.transport_key == key:
                continue
            await self.fail_transport()
            if self.transport is not None:
                self.transport.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self.transport
                self.transport = None
            # A control hint may have requested this replacement before an
            # older failed transport's timer fires.  That timer is keyed to
            # the old epoch and cannot recover a failure of the new one.
            if self.retry_handle is not None:
                self.retry_handle.cancel()
                self.retry_handle = None
            self.epoch += 1
            self.transport_key = key
            if state['ready']:
                self.transport = asyncio.create_task(self.connection(state['endpoint'], self.epoch))

    def discovery_credentials(self):
        if self.discovery is None:
            return self.config.state_url, self.config.token
        if self.discovery.stat().st_size > 4096:
            raise ValueError('invalid discovery size')
        doc = json.loads(self.discovery.read_text(encoding='utf-8'))
        port, token = doc.get('port'), doc.get('token')
        if (doc.get('schema_version') != 1 or doc.get('host') != '127.0.0.1'
                or type(port) is not int or not 0 < port < 65536
                or not isinstance(token, str) or not 16 <= len(token) <= 512
                or '\r' in token or '\n' in token):
            raise ValueError('invalid discovery')
        return f'http://127.0.0.1:{port}/state/mcp/events', token

    async def events_once(self):
        url, token = self.discovery_credentials()
        checked_url(url, '/state/mcp/events')
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                timeout=httpx.Timeout(self.config.connect_timeout, read=30)) as http:
            async with http.stream('GET', url, headers={'Authorization': 'Bearer ' + token,
                                                       'Accept': 'text/event-stream'}) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if len(line) > 4096:
                        raise ValueError('invalid readiness size')
                    if not line.startswith('data:'):
                        continue
                    state = json.loads(line[5:].strip())
                    if (not isinstance(state, dict) or type(state.get('ready')) is not bool
                            or type(state.get('generation')) is not int
                            or not isinstance(state.get('instance_id'), str)):
                        raise ValueError('invalid readiness metadata')
                    # Metadata can prompt an already-dead configured transport
                    # to retry, but never supplies an endpoint or revokes one.
                    self.note_control_state(state)

    async def events_loop(self):
        delay = .1
        while True:
            self.resubscribe.clear()
            stream = asyncio.create_task(self.events_once())
            restart = asyncio.create_task(self.resubscribe.wait())
            try:
                done, _ = await asyncio.wait((stream, restart), return_when=asyncio.FIRST_COMPLETED)
                # A broken control channel does not kill a healthy upstream session.
                for task in done:
                    with contextlib.suppress(Exception):
                        task.result()
            finally:
                for task in (stream, restart):
                    task.cancel()
                for task in (stream, restart):
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
            # Reconnect only after channel/transport loss, never a health poll.
            await asyncio.sleep(delay)
            delay = .1 if self.available else min(delay * 2, 2.0)

    async def run(self):
        tasks = [asyncio.create_task(method()) for method in
                 (self.output_loop, self.frontend_loop, self.events_loop, self.manage)]
        # Bootstrap the configured MCP endpoint without waiting for overlay
        # discovery or the control-plane SSE stream.
        self.changed.set()
        try:
            await self.finished.wait()
        finally:
            if self.transport:
                self.transport.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self.transport
            if self.init_deadline_task:
                self.init_deadline_task.cancel()
            if self.expiry_timer:
                self.expiry_timer.cancel()
            if self.retry_handle:
                self.retry_handle.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return 0


async def run_sdk_stdio_bridge(config: BridgeConfig, *, stdin=None, stdout=None, discovery_file=None):
    return await StdioRecoveryBridge(config, stdin=stdin, stdout=stdout, discovery_file=discovery_file).run()


def run_stdio_bridge(config: BridgeConfig, **kwargs):
    return asyncio.run(run_sdk_stdio_bridge(config, **kwargs))
