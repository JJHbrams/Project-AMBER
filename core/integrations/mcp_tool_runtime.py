"""MCP 동기 도구 실행 런타임.

배경: 동기 도구(`def`)를 이벤트 루프 위에서 직접 호출하면 긴 도구(kg_lint 66 s 등) 하나가
async `/health` 를 포함한 서버 전체를 얼려, overlay 가 서버를 죽이고 모든 클라이언트가
끊긴다. 동기 도구는 전용 스레드 풀로 보내고(루프는 await 만 한다), 호출 시간과 루프
정지를 관측한다.

프라이버시: 이 모듈의 로그에는 도구 이름, 레인, ms, 예외 클래스명, 프레임 위치(파일명:줄:함수)
만 남긴다. 인자·결과·프롬프트·cwd·쿼리·예외 메시지는 절대 기록하지 않는다.
mcp_server 를 import 하지 않으므로 단독으로 import/테스트할 수 있다.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import itertools
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

try:  # 설정을 못 읽는 환경에서도 기본값으로 동작해야 한다.
    from core.config.runtime_config import get_cfg_value
except Exception:  # pragma: no cover - 방어적 폴백

    def get_cfg_value(path, default=None):
        return default


calls_logger = logging.getLogger("engram.mcp.calls")
loop_logger = logging.getLogger("engram.mcp.loop")
steps_logger = logging.getLogger("engram.mcp.steps")

# asyncio 기본 executor 와 분리한다 - 도구 하나가 to_thread 풀을 굶기지 않게.
SERIAL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="engram-tool-serial")
CONCURRENT = ThreadPoolExecutor(max_workers=8, thread_name_prefix="engram-tool")
# 브로커/LLM/네트워크에 묶인 긴 작업은 읽기 전용 DB/KG 작업(CONCURRENT)을 굶기지 않게 따로 둔다.
IO = ThreadPoolExecutor(max_workers=4, thread_name_prefix="engram-tool-io")
# presence 보고(report_*)는 다른 레인을 기다리지 않는 전용 레인 - 오버레이 전달 확인을 잠깐 기다릴 뿐이다.
PRESENCE = ThreadPoolExecutor(max_workers=4, thread_name_prefix="engram-tool-presence")
IO_TOOLS = frozenset({"engram_consult_engram"})  # 외부 Copilot CLI 호출(분 단위), 로컬 쓰기 없음

# 읽기 전용/장시간 도구만 병렬 레인으로 보낸다. 나머지 동기 도구는 SERIAL 하나로 직렬화해
# 기존의 "상태 변경 동기 도구끼리 상호 배제"를 유지한다.
# engram_get_tutorial_status 는 읽기처럼 보여도 진행 상태를 재계산해 저장할 수 있어 SERIAL 에 둔다.
CONCURRENT_TOOLS = frozenset(
    {
        "kg_lint",
        "kg_search",
        "kg_get_node",
        "kg_neighbors",
        "kg_list_nodes",
        "kg_read_note",
        "engram_search_transcript",
        "engram_read_transcript",
        "engram_list_transcript_projects",
        "engram_list_memories",
        "engram_get_identity",
        "engram_get_persona",
        "engram_get_persona_status",
        "engram_get_themes",
        "engram_status",
        # 읽기 전용이거나 로컬 상태를 바꾸지 않는 긴 도구. SERIAL 에 두면 get_context 류가 그 뒤에 줄 선다.
        "engram_peek_stm",  # 읽기 전용(브로커 GET)
    }
)


_CFG_TTL_SECS = 10.0
_cfg_cache: dict[str, tuple[object, float, float]] = {}


def _cfg_float(key: str, default: float) -> float:
    """임계값은 호출마다 설정 파일을 stat 하지 않도록 잠깐 캐시한다(get_cfg_value 가 바뀌면 무효)."""
    now = time.monotonic()
    cached = _cfg_cache.get(key)
    if cached is not None and cached[0] is get_cfg_value and now - cached[2] < _CFG_TTL_SECS:
        return cached[1]
    try:
        value = float(get_cfg_value(key, default))
    except Exception:
        value = float(default)
    _cfg_cache[key] = (get_cfg_value, value, now)
    return value


def lane_for(name: str) -> str:
    if name in IO_TOOLS:
        return "io"
    return "concurrent" if name in CONCURRENT_TOOLS else "serial"


# -- in-flight 레지스트리 ------------------------------------------------------
_ids = itertools.count(1)
_inflight_lock = threading.Lock()
_inflight: dict[int, tuple[str, str, float]] = {}


def _register(name: str, lane: str) -> int:
    call_id = next(_ids)
    with _inflight_lock:
        _inflight[call_id] = (name, lane, time.monotonic())
    return call_id


def _unregister(call_id: int) -> None:
    with _inflight_lock:
        _inflight.pop(call_id, None)


def in_flight_snapshot() -> list[tuple[str, str, float]]:
    """(이름, 레인, 경과 초) 목록 - 오래된 순."""
    now = time.monotonic()
    with _inflight_lock:
        items = [(name, lane, now - started) for name, lane, started in _inflight.values()]
    return sorted(items, key=lambda item: -item[2])


async def run_sync_tool(name: str, fn, args, kwargs):
    """동기 도구를 전용 executor 에서 실행한다. 예외는 그대로 전파된다."""
    return await _run_in_lane(name, lane_for(name), fn, args, kwargs)


async def run_helper(name: str, fn, *args, lane: str = "serial", **kwargs):
    """async 도구의 블로킹 구간을 도구 전용 레인에서 실행한다(asyncio 기본 executor 는 쓰지 않는다).

    상태를 바꾸는 구간은 lane="serial"(동기 도구와 상호 배제), 읽기 전용 DB/KG 는 lane="concurrent",
    브로커/LLM/네트워크 대기가 주인 구간은 lane="io".
    serial 레인 안에서 다른 serial 작업을 기다리면 교착하므로, 헬퍼는 루프만 await 하고
    다른 헬퍼를 직접 부르지 않는다."""
    return await _run_in_lane(name, lane, fn, args, kwargs)


async def _run_in_lane(name: str, lane: str, fn, args, kwargs):
    """레지스트리 항목은 워커가 함수에서 실제로 돌아올 때 지운다 - await 가 취소돼도
    스레드가 아직 돌고 있으면 in-flight 에 남는다."""
    executor = {"concurrent": CONCURRENT, "io": IO, "presence": PRESENCE}.get(lane, SERIAL)
    call_id = _register(name, lane)
    ctx = contextvars.copy_context()
    call = functools.partial(fn, *args, **kwargs)

    def work():
        try:
            return call()
        finally:
            _unregister(call_id)

    try:
        future = asyncio.get_running_loop().run_in_executor(executor, ctx.run, work)
    except BaseException:
        _unregister(call_id)  # 제출 자체가 실패 - 워커가 없으니 여기서 지운다
        raise
    return await future


# -- 호출 타이머 ---------------------------------------------------------------
_call_depth: contextvars.ContextVar[int] = contextvars.ContextVar("engram_call_depth", default=0)


class CallTimer:
    """호출 1건당 [engram-call] 한 줄. 동기/비동기 with 모두 사용할 수 있다."""

    def __init__(self, name: str, lane: str, register: bool | None = None):
        self.name = name
        self._depth_token = None
        self.lane = lane
        # 루프 레인(async 도구)은 run_sync_tool 이 등록하지 않으므로 여기서 등록한다.
        self._register = (lane == "loop") if register is None else register
        self._call_id = 0
        self._start = 0.0

    def __enter__(self):
        self._start = time.monotonic()
        if self.lane == "loop":
            # 다른 도구 호출 안에서 부른 도구(get_context_once -> get_context)는 중첩으로 표시한다.
            if _call_depth.get():
                self.lane = "nested"
            self._depth_token = _call_depth.set(_call_depth.get() + 1)
        if self._register:
            self._call_id = _register(self.name, self.lane)
        return self

    def __exit__(self, exc_type, exc, tb):
        elapsed = time.monotonic() - self._start
        if self._call_id:
            _unregister(self._call_id)
        if self._depth_token is not None:
            _call_depth.reset(self._depth_token)
        if exc_type is None:
            outcome = "ok"
        elif issubclass(exc_type, asyncio.CancelledError):
            outcome = "cancelled"
        else:
            outcome = f"error:{exc_type.__name__}"
        try:
            level = logging.WARNING if elapsed > _cfg_float("mcp.slow_call_warn_secs", 5) else logging.INFO
            calls_logger.log(
                level, "[engram-call] tool=%s lane=%s ms=%d outcome=%s",
                self.name, self.lane, int(elapsed * 1000), outcome,
            )
        except Exception:
            pass
        return False

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, exc_type, exc, tb):
        return self.__exit__(exc_type, exc, tb)


def wrap_sync_tool(fn, prepare=None, finish=None):
    """동기 도구를 async 래퍼로 감싼다(mcp_server 와 테스트가 같은 경로를 쓴다).

    prepare(name, args, kwargs) -> state 와 finish(state)(async) 는 루프에서 실행된다.
    fn 본체만 executor 에서 돈다."""
    name = fn.__name__

    @functools.wraps(fn)
    async def logged_sync(*a, **kw):
        state = prepare(name, a, kw) if prepare else None
        with CallTimer(name, lane_for(name), register=False):
            result = await run_sync_tool(name, fn, a, kw)
        if finish:
            await finish(state)
        return result

    return logged_sync


# -- 루프 정지 워치독 ----------------------------------------------------------
def _frame_summary(frame, limit: int = 8) -> str:
    """프레임 위치만(파일명:줄:함수). 지역 변수와 소스 줄은 읽지 않는다."""
    parts = []
    while frame is not None and len(parts) < limit:
        code = frame.f_code
        parts.append(f"{os.path.basename(code.co_filename)}:{frame.f_lineno}:{code.co_name}")
        frame = frame.f_back
    return " <- ".join(parts)


_THREAD_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]")


# 이 파일들의 프레임만 있는 스레드는 일감을 기다리는 유휴 스레드(풀 워커, Event 대기 등)다.
_IDLE_FILES = frozenset({
    "threading.py", "queue.py", "thread.py", "selectors.py", "socket.py",
    "socketserver.py", "_base.py", "process.py", "connection.py",
})


def _is_idle_thread(frame) -> bool:
    """스택 어디에도 표준 대기 모듈 밖의 코드가 없으면 유휴로 본다."""
    while frame is not None:
        if os.path.basename(frame.f_code.co_filename) not in _IDLE_FILES:
            return False
        frame = frame.f_back
    return True


def _all_threads_summary(skip_ids, max_threads: int = 40, frames_per_thread: int = 6,
                         max_chars: int = 4000) -> str:
    """다른 모든 스레드의 상위 프레임 위치만(이름 정제, 지역 변수·소스 줄 없음).

    잠금을 쥔 스레드처럼 실제 코드에 있는 스레드를 먼저 싣는다. 유휴 풀 워커가
    수십 개여도 상한에 밀려 정작 원인 스레드가 잘리지 않게 하려는 것이다."""
    frames = sys._current_frames()
    # 같은 스택의 스레드(풀 워커, 같은 루프를 도는 스레드)는 한 줄로 합쳐 개수만 적는다.
    # 그래야 똑같은 스택 수십 개가 상한을 채워 원인 스레드를 밀어내지 않는다.
    groups: dict[str, list] = {}  # stack -> [첫 이름, 개수, 유휴 여부]
    for th in threading.enumerate():
        ident = th.ident
        if ident is None or ident in skip_ids:
            continue
        frame = frames.get(ident)
        if frame is None:
            continue
        stack = _frame_summary(frame, frames_per_thread)
        group = groups.get(stack)
        if group is None:
            name = _THREAD_NAME_RE.sub("", th.name or "")[:40] or "?"
            groups[stack] = [name, 1, _is_idle_thread(frame)]
        else:
            group[1] += 1
    busy: list[tuple[int, str]] = []
    idle: list[tuple[int, str]] = []
    for stack, (name, count, is_idle) in groups.items():
        label = name if count == 1 else f"{name}*{count}"
        (idle if is_idle else busy).append((count, f"{label}={stack}"))
    # 개수가 적은(고유한) 스택을 먼저 싣는다 - 원인 스레드는 대개 하나뿐이다. sort 는 안정 정렬.
    busy.sort(key=lambda pair: pair[0])
    candidates = [item for _, item in busy] + [item for _, item in idle]
    out: list[str] = []
    total = 0
    for index, item in enumerate(candidates):
        remaining = len(candidates) - index
        # 다 못 실으면 마지막 칸은 생략 개수 표시에 남긴다.
        if len(out) >= max_threads - 1 and remaining > 1:
            out.append(f"+{remaining} more")
            break
        sep = 1 if out else 0
        if total + sep + len(item) > max_chars:
            marker = f"+{remaining} more"
            if total + sep + len(marker) <= max_chars:
                out.append(marker)
            break
        out.append(item)
        total += sep + len(item)
    return "|".join(out)


class LoopStallWatchdog:
    """루프가 일정 시간 못 돌면 한 번 경고하고, 돌아오면 한 번 복구를 기록한다.

    정지 시간은 heartbeat 가 예정보다 얼마나 늦었는가로 잰다(정상 대기 시간은 포함하지 않는다)."""

    def __init__(self, threshold_secs: float | None = None, tick_secs: float = 1.0, poll_secs: float = 0.5):
        self._threshold = threshold_secs
        self._tick = tick_secs
        self._poll = poll_secs
        self._last_tick = time.monotonic()
        self._seq = 0
        self._last_lateness = 0.0
        self._loop_thread_id = 0
        self._task: asyncio.Task | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._cfg_value = 5.0
        self._cfg_read_at = -1e9

    def _threshold_secs(self) -> float:
        if self._threshold is not None:
            return float(self._threshold)
        now = time.monotonic()
        if now - self._cfg_read_at > 5.0:
            self._cfg_value = _cfg_float("mcp.loop_stall_warn_secs", 5)
            self._cfg_read_at = now
        return self._cfg_value

    async def _heartbeat(self) -> None:
        expected = time.monotonic() + self._tick
        while True:
            await asyncio.sleep(self._tick)
            now = time.monotonic()
            self._last_lateness = max(0.0, now - expected)
            self._last_tick = now
            self._seq += 1
            expected = now + self._tick

    async def start(self) -> None:
        if self._task is not None:
            return
        self._loop_thread_id = threading.get_ident()
        self._last_tick = time.monotonic()
        self._task = asyncio.get_running_loop().create_task(self._heartbeat())
        self._thread = threading.Thread(target=self._watch, name="engram-loop-watchdog", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
            self._task = None

    def _watch(self) -> None:
        stalled_seq = None
        while not self._stop.wait(self._poll):
            try:
                seq, last_tick = self._seq, self._last_tick
                if stalled_seq is not None:
                    if seq != stalled_seq:
                        loop_logger.info("[engram-loop] recovered after %.1fs", self._last_lateness)
                        stalled_seq = None
                    continue
                lateness = time.monotonic() - last_tick - self._tick
                if lateness > self._threshold_secs():
                    stalled_seq = seq
                    flight = ",".join(f"{n}:{age:.1f}s" for n, _lane, age in in_flight_snapshot())
                    frame = sys._current_frames().get(self._loop_thread_id)
                    loop_logger.warning(
                        "[engram-loop] blocked for %.1fs; in_flight=[%s]; main_stack=%s",
                        lateness, flight, _frame_summary(frame) if frame is not None else "?",
                    )
                    try:
                        skip = {self._loop_thread_id, threading.get_ident()}
                        loop_logger.warning("[engram-loop] threads=%s", _all_threads_summary(skip))
                    except Exception:
                        pass
            except Exception:
                pass


# -- 단계 타이머 ---------------------------------------------------------------
class StepTimer:
    """이름 붙은 단계의 소요 ms 만 모은다(값은 단계 이름과 ms, 짧은 outcome 태그뿐)."""

    def __init__(self):
        self._t0 = time.perf_counter()
        self._steps: dict[str, int] = {}
        self._tags: dict[str, str] = {}

    @contextlib.contextmanager
    def step(self, name: str):
        start = time.perf_counter()
        try:
            yield self
        finally:
            self._steps[name] = self._steps.get(name, 0) + int((time.perf_counter() - start) * 1000)

    def tag(self, name: str, value: str) -> None:
        self._tags[name] = str(value)

    def total_ms(self) -> int:
        return int((time.perf_counter() - self._t0) * 1000)

    def emit(self, logger: logging.Logger, label: str) -> None:
        try:
            total = self.total_ms()
            parts = [f"total_ms={total}"]
            parts += [f"{name}={ms}" for name, ms in self._steps.items()]
            parts += [f"{name}={value}" for name, value in self._tags.items()]
            level = logging.WARNING if total > _cfg_float("mcp.slow_call_warn_secs", 5) * 1000 else logging.INFO
            logger.log(level, "[engram-steps] %s %s", label, " ".join(parts))
        except Exception:
            pass
