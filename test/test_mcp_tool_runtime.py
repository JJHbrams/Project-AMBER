"""MCP 동기 도구 오프로딩 런타임 회귀.

배경: 동기 도구가 이벤트 루프에서 직접 돌아 kg_lint(66 s) 하나가 /health 까지 얼렸다.
mcp_server 를 import 하면 실 DB 를 초기화하므로, 같은 래퍼(wrap_sync_tool)를 쓰는
합성 FastMCP 앱으로 검증한다.
"""

import asyncio
import contextvars
import logging
import socket
import threading
import time
from unittest.mock import patch

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.fastmcp import FastMCP

from core.integrations import mcp_tool_runtime as rt

SENTINEL = "SENTINEL-ARG-9f3a7c"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _cfg(**values):
    def get(key, default=None):
        return values.get(key.rsplit(".", 1)[-1], default)

    return get


def _build_app(slow_secs: float):
    mcp = FastMCP("synthetic")

    def slow_sync() -> str:
        time.sleep(slow_secs)
        return "slow-done"

    def fast_sync() -> str:
        return "fast-done"

    mcp.tool()(rt.wrap_sync_tool(slow_sync))
    mcp.tool()(rt.wrap_sync_tool(fast_sync))

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request):
        from starlette.responses import JSONResponse

        return JSONResponse({"status": "ok"})

    return mcp.streamable_http_app()


# [AC1] 긴 동기 도구가 도는 동안 /health 와 다른 도구가 응답한다.
def test_long_sync_tool_does_not_freeze_health_or_other_tools():
    port = _free_port()
    slow_secs = 6.0
    # slow_sync 는 kg_lint 처럼 병렬 레인, fast_sync 는 직렬 레인(놀고 있음).
    lanes = rt.CONCURRENT_TOOLS | {"slow_sync"}

    async def scenario():
        server = uvicorn.Server(uvicorn.Config(
            _build_app(slow_secs), host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
        serve = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.05)
        gaps: list[float] = []
        stop = asyncio.Event()

        async def ticker():
            last = time.monotonic()
            while not stop.is_set():
                await asyncio.sleep(0.05)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        tick = asyncio.create_task(ticker())
        statuses: list[int] = []
        fast_ms: list[float] = []
        try:
            async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp") as (read, write, _sid):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    slow = asyncio.create_task(session.call_tool("slow_sync", {}))
                    await asyncio.sleep(0.3)  # 도구가 워커에서 시작되게 한다
                    async with httpx.AsyncClient() as http:
                        t_end = time.monotonic() + slow_secs - 1.0
                        while time.monotonic() < t_end:
                            resp = await http.get(f"http://127.0.0.1:{port}/health", timeout=1.5)
                            statuses.append(resp.status_code)
                            started = time.monotonic()
                            result = await session.call_tool("fast_sync", {})
                            fast_ms.append((time.monotonic() - started) * 1000)
                            assert result.content[0].text == "fast-done"
                            await asyncio.sleep(0.5)
                    assert not slow.done(), "slow tool should still be running while we polled"
                    slow_result = await asyncio.wait_for(slow, timeout=slow_secs + 5)
                    assert slow_result.content[0].text == "slow-done"
        finally:
            stop.set()
            await tick
            server.should_exit = True
            await serve
        return statuses, fast_ms, gaps

    with patch.object(rt, "CONCURRENT_TOOLS", lanes):
        statuses, fast_ms, gaps = asyncio.run(scenario())

    assert len(statuses) >= 8
    assert set(statuses) == {200}
    assert max(fast_ms) < 1000, fast_ms
    assert max(gaps) < 0.5, max(gaps)


# [AC3] 레인 의미론
def test_serial_lane_never_overlaps_and_concurrent_lane_overlaps():
    active = {"n": 0, "max": 0}
    lock = threading.Lock()

    def tool():
        with lock:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        time.sleep(0.2)
        with lock:
            active["n"] -= 1

    async def run(name, count):
        await asyncio.gather(*(rt.run_sync_tool(name, tool, (), {}) for _ in range(count)))

    asyncio.run(run("some_mutating_tool", 3))
    assert active["max"] == 1

    active["max"] = 0
    asyncio.run(run("kg_search", 3))
    assert active["max"] >= 2


def test_exception_propagates_unchanged_and_registry_is_cleaned():
    class Boom(Exception):
        pass

    def tool():
        raise Boom("x")

    async def run():
        with pytest.raises(Boom):
            await rt.run_sync_tool("some_tool", tool, (), {})

    asyncio.run(run())
    assert rt.in_flight_snapshot() == []


def test_contextvars_are_visible_in_worker():
    var = contextvars.ContextVar("probe", default="unset")

    async def run():
        var.set("from-loop")
        return await rt.run_sync_tool("some_tool", lambda: var.get(), (), {})

    assert asyncio.run(run()) == "from-loop"


def test_asyncio_run_inside_offloaded_function_works():
    # run_sg_coro 는 워커 스레드에서 asyncio.run 을 쓴다.
    async def inner():
        await asyncio.sleep(0.01)
        return 7

    async def run():
        return await rt.run_sync_tool("some_tool", lambda: asyncio.run(inner()), (), {})

    assert asyncio.run(run()) == 7


def test_cancelled_await_keeps_running_thread_in_flight_until_it_returns():
    release = threading.Event()

    def tool():
        release.wait(5)

    async def run():
        task = asyncio.create_task(rt.run_sync_tool("kg_lint", tool, (), {}))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        names_while_running = [name for name, _lane, _age in rt.in_flight_snapshot()]
        release.set()
        await asyncio.sleep(0.3)
        return names_while_running, rt.in_flight_snapshot()

    during, after = asyncio.run(run())
    assert during == ["kg_lint"]
    assert after == []


# [AC5] 호출 로그
def test_one_call_line_per_call_with_levels_and_no_argument_leak(caplog):
    caplog.set_level(logging.INFO, logger="engram.mcp.calls")

    def slow_tool(secret: str):
        time.sleep(0.3)
        return "ok"

    def fast_tool(secret: str):
        return "ok"

    def failing_tool(secret: str):
        raise ValueError(secret)

    wrapped = [rt.wrap_sync_tool(f) for f in (slow_tool, fast_tool, failing_tool)]

    async def run():
        await wrapped[0](SENTINEL)
        await wrapped[1](SENTINEL)
        with pytest.raises(ValueError):
            await wrapped[2](SENTINEL)

    with patch.object(rt, "get_cfg_value", _cfg(slow_call_warn_secs=0.1)):
        asyncio.run(run())

    records = [r for r in caplog.records if r.name == "engram.mcp.calls"]
    assert len(records) == 3
    messages = [r.getMessage() for r in records]
    assert messages[0].startswith("[engram-call] tool=slow_tool lane=serial ms=")
    assert messages[0].endswith("outcome=ok") and records[0].levelno == logging.WARNING
    assert "tool=fast_tool" in messages[1] and records[1].levelno == logging.INFO
    assert messages[2].endswith("outcome=error:ValueError")
    for record in caplog.records:
        assert SENTINEL not in record.getMessage()
        assert SENTINEL not in str(record.args) and SENTINEL not in str(record.exc_info)


def test_loop_lane_async_call_timer_reports_cancelled(caplog):
    caplog.set_level(logging.INFO, logger="engram.mcp.calls")

    async def run():
        async def tool():
            async with rt.CallTimer("async_tool", "loop"):
                await asyncio.sleep(5)

        task = asyncio.create_task(tool())
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    lines = [r.getMessage() for r in caplog.records if r.name == "engram.mcp.calls"]
    assert len(lines) == 1 and "lane=loop" in lines[0] and lines[0].endswith("outcome=cancelled")


# [AC6] 루프 정지 워치독
def test_watchdog_warns_once_names_inflight_tool_and_reports_recovery(caplog):
    caplog.set_level(logging.INFO, logger="engram.mcp.loop")

    def blocking_tool_body(secret):
        time.sleep(1.5)

    async def run():
        watchdog = rt.LoopStallWatchdog(threshold_secs=0.5, tick_secs=0.1, poll_secs=0.1)
        await watchdog.start()
        await asyncio.sleep(0.5)
        async with rt.CallTimer("blocking_async_tool", "loop"):
            blocking_tool_body(SENTINEL)
        await asyncio.sleep(0.6)
        await watchdog.stop()

    asyncio.run(run())
    loop_records = [r for r in caplog.records if r.name == "engram.mcp.loop"]
    blocked = [r for r in loop_records if "blocked for" in r.getMessage()]
    recovered = [r for r in loop_records if "recovered after" in r.getMessage()]
    assert len(blocked) == 1 and blocked[0].levelno == logging.WARNING
    assert "blocking_async_tool:" in blocked[0].getMessage()
    assert "blocking_tool_body" in blocked[0].getMessage()  # 프레임 위치(함수명)
    assert len(recovered) == 1
    for record in loop_records:
        assert SENTINEL not in record.getMessage()


def test_watchdog_logs_other_threads_stacks_without_locals(caplog):
    caplog.set_level(logging.INFO, logger="engram.mcp.loop")
    release = threading.Event()
    started = threading.Event()

    def helper_parked_here(secret):
        started.set()
        release.wait(10)

    def leaked_loop_here():
        release.wait(10)

    # 유휴 대기 스레드 45개와, 앱 코드에서 같은 자리에 멈춘 스레드 60개(다른 테스트가 남긴
    # 발송 스레드나 풀 워커 같은 것)를 원인 스레드보다 먼저 만든다. 같은 스택이 상한
    # (40개, 4,000자)을 채워 원인 스레드를 밀어내면 안 된다.
    extras = [threading.Thread(target=release.wait, args=(10,), name=f"extra-{i}", daemon=True) for i in range(45)]
    extras += [threading.Thread(target=leaked_loop_here, name=f"leaked-{i}", daemon=True) for i in range(60)]
    for t in extras:
        t.start()
    worker = threading.Thread(
        target=helper_parked_here, args=(SENTINEL,), name="weird name!@#" + "x" * 80, daemon=True,
    )
    worker.start()
    assert started.wait(5)

    async def run():
        watchdog = rt.LoopStallWatchdog(threshold_secs=0.5, tick_secs=0.1, poll_secs=0.1)
        await watchdog.start()
        await asyncio.sleep(0.5)
        time.sleep(1.2)  # 루프 정지
        await asyncio.sleep(0.5)
        await watchdog.stop()

    try:
        asyncio.run(run())
    finally:
        release.set()
    msgs = [r.getMessage() for r in caplog.records if r.name == "engram.mcp.loop"]
    lines = [m for m in msgs if m.startswith("[engram-loop] threads=")]
    assert len(lines) == 1
    line = lines[0]
    assert "helper_parked_here" in line
    body = line.split("threads=", 1)[1]
    assert "extra-0*45=" in body and "leaked-0*60=" in body  # 같은 스택은 한 줄 + 개수
    assert body.index("helper_parked_here") < body.index("leaked-0*60=") < body.index("extra-0*45=")
    assert "weirdname" in line and "!" not in line.split("threads=", 1)[1].split("=", 1)[0]
    assert "x" * 41 not in line
    assert "engram-loop-watchdog" not in line
    assert len(line) - len("[engram-loop] threads=") <= 4000
    assert line.count("|") + 1 <= 40
    for m in msgs:
        assert SENTINEL not in m


def test_thread_summary_caps_distinct_stacks_and_marks_overflow():
    release = threading.Event()
    threads = []
    for i in range(50):
        # 서로 다른 함수 이름 = 서로 다른 스택(합쳐지지 않음)
        namespace: dict = {}
        exec(f"def distinct_park_{i}(ev):\n    ev.wait(10)\n", namespace)
        threads.append(threading.Thread(target=namespace[f"distinct_park_{i}"], args=(release,),
                                        name=f"distinct-{i}", daemon=True))
    for t in threads:
        t.start()
    try:
        body = rt._all_threads_summary({threading.get_ident()}, max_threads=40, max_chars=4000)
    finally:
        release.set()
    parts = body.split("|")
    assert len(parts) <= 40
    assert len(body) <= 4000
    assert parts[-1].startswith("+") and parts[-1].endswith(" more")


def test_step_timer_emits_names_and_ms_only(caplog):
    caplog.set_level(logging.INFO, logger="engram.mcp.steps")
    timer = rt.StepTimer()
    with timer.step("policy"):
        pass
    with timer.step("compose"):
        pass
    timer.tag("stm_outcome", "none")
    timer.emit(logging.getLogger("engram.mcp.steps"), "unit")
    message = caplog.records[-1].getMessage()
    assert message.startswith("[engram-steps] unit total_ms=")
    assert "policy=" in message and "compose=" in message and "stm_outcome=none" in message
    assert caplog.records[-1].levelno == logging.INFO
