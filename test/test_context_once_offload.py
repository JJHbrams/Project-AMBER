"""engram_get_context_once / summarize / close 의 블로킹 구간이 이벤트 루프 밖에서 도는지.

mcp_server 는 다른 컨텍스트 테스트와 같은 방식으로 import 한다(conftest 가 HOME/DB 를 임시로 돌린다).
git 서브프로세스, STM 브로커 POST(최대 70 s), 컨텍스트 조합을 1~2 초씩 자는 가짜로 바꿔
루프가 그동안 계속 도는지, 결과 문자열이 지연 유무와 무관하게 같은지, 로그에 입력이 새지 않는지 본다.
"""

import asyncio
import logging
import threading
import time
from unittest.mock import AsyncMock, patch

import pytest

import mcp_server

CWD = "C:/SENTINEL-CWD-5d2e1a"
QUERY = "SENTINEL-QUERY-5d2e1a"


class _Probe:
    def __init__(self, delay: float, stm_delay: float | None = None):
        self.delay = delay
        self.stm_delay = delay if stm_delay is None else stm_delay
        self.stm_calls = 0
        self.threads: dict[str, str] = {}
        self.policy_calls = 0

    def _enter(self, name):
        self.threads[name] = threading.current_thread().name
        if self.delay:
            time.sleep(self.delay)

    def policy(self, _cwd):
        self.policy_calls += 1
        self._enter("policy")
        return {}

    def stm_post(self, _path, _data):
        self.stm_calls += 1
        self.threads["stm"] = threading.current_thread().name
        if self.stm_delay:
            time.sleep(self.stm_delay)
        return {"session_id": 4242}

    async def compose(self, *_args, **_kwargs):
        self._enter("compose")
        return "[연속체] persona"

    def identity(self):
        self._enter("notices")
        return {"name": "name"}


def _patches(probe: _Probe):
    return [
        patch.object(mcp_server, "ensure_repo_policy", probe.policy),
        patch.object(mcp_server, "_stm_post", probe.stm_post),
        patch.object(mcp_server.memory_bus, "compose_prompt_context", probe.compose),
        patch.object(mcp_server, "get_identity", probe.identity),
        patch.object(mcp_server, "get_persona_status", lambda: {"initialized": True}),
        patch.object(mcp_server, "_render_tutorial_notice", lambda *_a, **_k: ""),
        patch.object(mcp_server, "_report_bootstrap_project", AsyncMock()),
        patch.object(mcp_server, "_mark_trusted_root_bootstrap"),
    ]


async def _ticking(coro):
    """coro 가 도는 동안 루프 tick 간격의 최댓값을 잰다."""
    gaps: list[float] = []
    stop = asyncio.Event()

    async def ticker():
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.05)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    try:
        result = await coro
    finally:
        stop.set()
        await task
    return result, max(gaps)


def _once_call():
    return mcp_server.engram_get_context_once(
        user_query=QUERY, caller="claude-code", scope_key="overlay", cwd=CWD,
    )


def _execute(delay: float, extra_patches=()):
    probe = _Probe(delay)
    mcp_server._CONTEXT_ONCE_KEYS.clear()
    mcp_server._FINGERPRINT_TO_SESSION.clear()
    patches = _patches(probe) + list(extra_patches)
    for p in patches:
        p.start()
    try:
        text, max_gap = asyncio.run(_ticking(_once_call()))
    finally:
        for p in reversed(patches):
            p.stop()
        mcp_server._CONTEXT_ONCE_KEYS.clear()
        mcp_server._FINGERPRINT_TO_SESSION.clear()
    return probe, text, max_gap


# [AC2] 블로킹 구간은 워커에서 돌고 루프는 멈추지 않는다. 결과는 지연과 무관하다.
def test_context_once_blocking_sections_run_off_the_loop_and_result_is_unchanged():
    main_thread = threading.current_thread().name
    _fast_probe, fast_text, _ = _execute(0)
    slow_probe, slow_text, max_gap = _execute(1.0)

    assert slow_text == fast_text
    assert "session_id=4242" in slow_text and "[연속체] persona" in slow_text
    for name in ("policy", "stm", "compose", "notices"):
        assert slow_probe.threads[name] != main_thread, name
        # asyncio 기본 executor(presence/hook 보고용)가 아니라 도구 전용 레인에서 돈다.
        assert slow_probe.threads[name].startswith("engram-tool"), (name, slow_probe.threads[name])
    assert max_gap < 0.5, max_gap
    assert slow_probe.policy_calls == 1  # get_context 가 같은 cwd 로 policy 를 다시 돌리지 않는다


# [AC7] 단계 로그에는 단계 이름과 ms 만 있다.
def test_step_log_has_step_names_and_no_inputs(caplog):
    caplog.set_level(logging.INFO)
    _execute(0)
    lines = [r.getMessage() for r in caplog.records if "[engram-steps]" in r.getMessage()]
    once = [line for line in lines if " get_context_once " in line]
    inner = [line for line in lines if " get_context " in line]
    assert len(once) == 1 and len(inner) == 1
    for step in ("total_ms=", "policy=", "token_check=", "cache_lookup=", "native_lookup=",
                 "stm_session_start=", "stm_outcome=ok", "bootstrap_project=", "get_context="):
        assert step in once[0], step
    for step in ("compose=", "notices="):
        assert step in inner[0], step
    for record in caplog.records:
        assert "SENTINEL" not in record.getMessage(), record.name


@pytest.mark.parametrize("outcome", ["none", "error"])
def test_stm_outcome_tag_for_missing_or_failing_broker(caplog, outcome):
    caplog.set_level(logging.INFO)

    def stm_post(_path, _data):
        if outcome == "error":
            raise RuntimeError("SENTINEL-BROKER-ERR")
        return None

    class _Session:
        session_id = 77

    _execute(0, [
        patch.object(mcp_server, "_stm_post", stm_post),
        patch.object(mcp_server.memory_bus, "start_session", lambda **_kw: _Session()),
    ])
    lines = [r.getMessage() for r in caplog.records if " get_context_once " in r.getMessage()]
    assert lines and f"stm_outcome={outcome}" in lines[0]
    if outcome == "none":
        assert "start_session=" in lines[0]
    assert all("SENTINEL" not in r.getMessage() for r in caplog.records)


def test_summarize_and_close_blocking_work_runs_off_the_loop():
    main_thread = threading.current_thread().name
    seen: dict[str, str] = {}

    def summarize_sync(*_a, **_k):
        seen["summarize"] = threading.current_thread().name
        time.sleep(1.0)
        return {"status": "checkpointed"}, None

    def prepare_sync(*_a, **_k):
        seen["prepare"] = threading.current_thread().name
        state = {"resolved_scope": "overlay", "effective_cwd": CWD, "closed_session_id": "5", "applied": {}}
        return None, state

    def finish_sync(*_a, **_k):
        seen["finish"] = threading.current_thread().name
        time.sleep(1.0)
        return {"status": "ok"}

    async def scenario():
        summary = await mcp_server.engram_summarize_session(summary="s")
        with patch.object(mcp_server, "engram_summarize_session",
                          AsyncMock(return_value={"status": "checkpointed"})):
            closed = await mcp_server.engram_close_session(summary="s")
        return summary, closed

    with patch.object(mcp_server, "_summarize_session_sync", summarize_sync), \
            patch.object(mcp_server, "_close_session_prepare_sync", prepare_sync), \
            patch.object(mcp_server, "_close_session_finish_sync", finish_sync):
        (summary, closed), max_gap = asyncio.run(_ticking(scenario()))

    assert summary == {"status": "checkpointed"} and closed == {"status": "ok"}
    assert all(seen[name] != main_thread for name in ("summarize", "prepare", "finish"))
    assert max_gap < 0.5, max_gap


# [D1] 같은 키의 동시 호출은 세션을 하나만 만든다.
def test_concurrent_once_calls_with_same_key_create_exactly_one_session():
    probe = _Probe(0, stm_delay=0.5)
    mcp_server._CONTEXT_ONCE_KEYS.clear()
    mcp_server._FINGERPRINT_TO_SESSION.clear()
    patches = _patches(probe) + [patch.object(mcp_server, "_session_is_open", lambda *_a, **_k: True)]
    for p in patches:
        p.start()
    try:
        async def both():
            return await asyncio.gather(_once_call(), _once_call())

        first, second = asyncio.run(both())
    finally:
        for p in reversed(patches):
            p.stop()
        mcp_server._CONTEXT_ONCE_KEYS.clear()
        mcp_server._FINGERPRINT_TO_SESSION.clear()
    assert probe.stm_calls == 1
    assert "session_id=4242" in first and "session_id=4242" in second


# [D2] 70 s 짜리 브로커 POST 가 도는 동안에도 presence/hook 보고용 to_thread 는 즉시 끝난다.
def test_blocked_stm_post_does_not_starve_default_executor():
    probe = _Probe(0, stm_delay=2.0)
    elapsed: list[float] = []

    async def report_like():
        await asyncio.sleep(0.5)  # 브로커 POST 가 워커에서 시작된 뒤
        started = time.monotonic()
        await asyncio.to_thread(lambda: None)
        elapsed.append(time.monotonic() - started)

    async def scenario():
        await asyncio.gather(_once_call(), report_like())

    mcp_server._CONTEXT_ONCE_KEYS.clear()
    patches = _patches(probe)
    for p in patches:
        p.start()
    try:
        asyncio.run(scenario())
    finally:
        for p in reversed(patches):
            p.stop()
        mcp_server._CONTEXT_ONCE_KEYS.clear()
    assert probe.stm_calls == 1 and elapsed and elapsed[0] < 0.5, elapsed


# 긴 브로커 POST 가 도는 동안 serial 레인(상태 변경 동기 도구)은 막히지 않는다.
def test_blocked_stm_post_does_not_hold_the_serial_lane():
    from core.integrations import mcp_tool_runtime as rt

    probe = _Probe(0, stm_delay=2.0)
    elapsed: list[float] = []

    async def state_changing_tool():
        await asyncio.sleep(0.5)  # 브로커 POST 가 워커에서 시작된 뒤
        started = time.monotonic()
        await rt.run_sync_tool("fake_state_changing_tool", lambda: "done", (), {})
        elapsed.append(time.monotonic() - started)

    async def scenario():
        await asyncio.gather(_once_call(), state_changing_tool())

    mcp_server._CONTEXT_ONCE_KEYS.clear()
    patches = _patches(probe)
    for p in patches:
        p.start()
    try:
        asyncio.run(scenario())
    finally:
        for p in reversed(patches):
            p.stop()
        mcp_server._CONTEXT_ONCE_KEYS.clear()
    assert probe.stm_calls == 1 and elapsed and elapsed[0] < 0.5, elapsed
    assert rt.lane_for("fake_state_changing_tool") == "serial"


# summarize: 느린 브로커 POST 는 concurrent 레인, 로컬 checkpoint 쓰기는 짧은 serial 작업.
def test_summarize_splits_slow_io_from_serial_local_write():
    from core.integrations import mcp_tool_runtime as rt

    seen: dict[str, str] = {}
    elapsed: list[float] = []

    def slow_io(*_a, **_k):
        seen["io"] = threading.current_thread().name
        time.sleep(2.0)  # fake _stm_post
        return None, ("local-args",)

    def local_write(local):
        seen["local"] = threading.current_thread().name
        assert local == ("local-args",)
        return {"status": "checkpointed"}

    async def state_changing_tool():
        await asyncio.sleep(0.5)  # 느린 I/O 중
        started = time.monotonic()
        await rt.run_sync_tool("fake_state_changing_tool", lambda: "done", (), {})
        elapsed.append(time.monotonic() - started)

    async def scenario():
        result, _ = await asyncio.gather(mcp_server.engram_summarize_session(summary="s"), state_changing_tool())
        return result

    with patch.object(mcp_server, "_summarize_session_sync", slow_io),             patch.object(mcp_server, "_summarize_checkpoint_local_sync", local_write):
        result = asyncio.run(scenario())

    assert result == {"status": "checkpointed"}
    assert seen["io"].startswith("engram-tool-io")
    assert seen["local"].startswith("engram-tool-serial")
    assert elapsed and elapsed[0] < 0.5, elapsed


# [#1] 긴 serial 작업이 돌아도 get_context_once 는 줄 서지 않는다.
def test_get_context_once_does_not_queue_behind_a_long_serial_job():
    from core.integrations import mcp_tool_runtime as rt

    probe = _Probe(0)
    elapsed: list[float] = []

    async def scenario():
        long_job = asyncio.create_task(rt.run_sync_tool("fake_long_state_tool", lambda: time.sleep(3.0), (), {}))
        await asyncio.sleep(0.3)  # serial 워커가 잡고 있는 동안
        started = time.monotonic()
        text = await _once_call()
        elapsed.append(time.monotonic() - started)
        await long_job
        return text

    mcp_server._CONTEXT_ONCE_KEYS.clear()
    patches = _patches(probe)
    for p in patches:
        p.start()
    try:
        text = asyncio.run(scenario())
    finally:
        for p in reversed(patches):
            p.stop()
        mcp_server._CONTEXT_ONCE_KEYS.clear()
    assert "session_id=4242" in text
    assert elapsed and elapsed[0] < 1.0, elapsed


def test_read_only_and_long_tools_are_not_on_the_serial_lane():
    from core.integrations import mcp_tool_runtime as rt

    assert rt.lane_for("engram_peek_stm") == "concurrent"
    assert rt.lane_for("engram_consult_engram") == "io"
    # 병렬 save_message 는 커밋 순서가 뒤바뀌고 close 와 인터리브될 수 있어 serial 에 둔다.
    assert rt.lane_for("engram_save_message") == "serial"


# [#3] bootstrap 과 save_message(create_by_native) 가 같은 native id 로 동시에 와도 세션은 하나만 묶인다.
def test_concurrent_bootstrap_and_save_message_bind_one_session_for_one_native_id():
    from types import SimpleNamespace

    native = "native-5d2e1a-aaaa-bbbb"
    transport = "transport-race-5d2e1a"
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(headers={}, scope={"engram.presence.transport_id": transport})
        )
    )
    conn_key = mcp_server._context_once_connection_key(ctx)
    assert conn_key
    probe = _Probe(0)

    def slow_no_broker(path, _data):
        if path == "/stm/session/start":
            time.sleep(0.6)  # save_message 가 먼저 세션을 만들 시간
        return None

    async def scenario():
        mcp_server._CONN_TO_NATIVE[conn_key] = native
        once = asyncio.create_task(mcp_server.engram_get_context_once(
            user_query=QUERY, caller="claude-code", scope_key="overlay", cwd=CWD, ctx=ctx))
        await asyncio.sleep(0.2)
        save = mcp_server.engram_save_message  # tools.disabled 기본값에 들어 있으면 래핑 없이 동기 함수다
        kwargs = dict(role="user", content="hello", scope_key="overlay", ctx=ctx)
        if asyncio.iscoroutinefunction(save):
            saved = await save(**kwargs)
        else:
            saved = await asyncio.to_thread(save, **kwargs)
        return await once, saved

    mcp_server._CONTEXT_ONCE_KEYS.clear()
    mcp_server._FINGERPRINT_TO_SESSION.clear()
    patches = _patches(probe) + [patch.object(mcp_server, "_stm_post", slow_no_broker)]
    for p in patches:
        p.start()
    try:
        text, saved = asyncio.run(scenario())
        conn = mcp_server.get_connection()
        try:
            rows = conn.execute(
                "SELECT id FROM sessions WHERE native_session_id=? AND ended_at IS NULL", (native,)
            ).fetchall()
            open_rows = conn.execute("SELECT id FROM sessions WHERE ended_at IS NULL").fetchall()
        finally:
            conn.close()
        bound = mcp_server._FINGERPRINT_TO_SESSION.get(conn_key)
    finally:
        for p in reversed(patches):
            p.stop()
        mcp_server._CONN_TO_NATIVE.pop(conn_key, None)
        mcp_server._CONTEXT_ONCE_KEYS.clear()
        mcp_server._FINGERPRINT_TO_SESSION.clear()
    assert saved == {"status": "message_saved"}
    assert len(rows) == 1
    assert bound == rows[0]["id"]
    assert len(open_rows) == 1 and open_rows[0]["id"] == rows[0]["id"], "진 쪽 빈 세션이 열린 채 남았다"
    assert f"session_id={rows[0]['id']}" in text


# [D3] IO 레인 작업 4개가 잡혀 있어도 읽기 전용 도구와 get_context_once 의 policy/compose 는 막히지 않는다.
def test_saturated_io_lane_does_not_block_concurrent_work():
    from core.integrations import mcp_tool_runtime as rt

    probe = _Probe(0)
    elapsed: dict[str, float] = {}

    async def scenario():
        io_jobs = [
            asyncio.create_task(rt.run_sync_tool("engram_consult_engram", lambda: time.sleep(3.0), (), {}))
            for _ in range(4)
        ]
        await asyncio.sleep(0.3)
        started = time.monotonic()
        await rt.run_sync_tool("kg_search", lambda: "rows", (), {})
        elapsed["read_only"] = time.monotonic() - started
        # get_context(compose/policy/notices)는 io 에 묶이지 않는다. (bootstrap 은 io 라 새 키라도 줄을 선다)
        started = time.monotonic()
        text = await mcp_server.engram_get_context(user_query=QUERY, caller="claude-code",
                                                   scope_key="overlay", cwd=CWD)
        elapsed["get_context"] = time.monotonic() - started
        await asyncio.gather(*io_jobs)
        return text

    patches = _patches(probe)
    for p in patches:
        p.start()
    try:
        text = asyncio.run(scenario())
    finally:
        for p in reversed(patches):
            p.stop()
    assert "[연속체] persona" in text
    assert elapsed["read_only"] < 1.0 and elapsed["get_context"] < 1.0, elapsed
    assert rt.lane_for("engram_consult_engram") == "io"
