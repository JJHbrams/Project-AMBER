"""SSH 리버스 터널(-R) 상시 유지 매니저.

원격에서 engram MCP 를 쓰려면 원격 loopback 에 :remote_port 가 열려 있어야 한다.
지금까지는 사용자가 `ssh -N -R ...` 터미널을 띄워두는 방식이었는데, 그 창이 곧
터널의 수명이라 불편했다. 오버레이가 자식 프로세스로 소유·감시·재연결한다.

핵심은 `ExitOnForwardFailure=yes` 다. 이게 없으면 ssh 프로세스는 살아 있는데
-R 바인딩만 실패한 좀비가 생겨 "프로세스 생존 ≠ 터널 생존"이 된다. 켜두면
포워딩이 깨지는 순간 ssh 가 종료되므로 poll() 만으로 터널 상태를 알 수 있다.

백그라운드라 비밀번호 프롬프트를 받을 수 없으므로 BatchMode=yes 로 띄운다.
키 인증이 없으면 즉시 실패하고, 그 경우 재시도하지 않는다 — 무한 재시도로
원격 sshd 를 두드리면 안 된다.
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger("overlay.remote_tunnel")


def _pid_alive(pid: int) -> bool:
    if sys.platform != "win32" or not pid:
        return False
    try:
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    except Exception:
        return False


def hide_console_of(pid: int) -> bool:
    """자식 프로세스가 쓰는 콘솔 창을 숨긴다.

    콘솔 창은 conhost 소유라 PID 로 창을 찾을 수 없다. AttachConsole 로 그 콘솔에
    붙으면 GetConsoleWindow 가 해당 창 핸들을 준다. 숨겨도 프로세스는 계속 돈다.

    `ssh -f`(인증 후 자동 백그라운드)로 창을 없애려 했으나, Windows OpenSSH 의 -f 는
    프로세스를 재실행하는 방식이라 비밀번호 인증 세션을 이어받지 못한다(키 인증에서만
    동작하는 것을 실측). 그래서 창을 직접 숨기는 쪽을 쓴다 — Popen 핸들이 그대로
    남아 감시도 온전하다.
    """
    if sys.platform != "win32" or not pid:
        return False
    try:
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        k32.FreeConsole()
        if not k32.AttachConsole(pid):
            return False
        hwnd = k32.GetConsoleWindow()
        ok = False
        if hwnd:
            u32.ShowWindow(hwnd, 0)  # SW_HIDE
            ok = not u32.IsWindowVisible(hwnd)
        k32.FreeConsole()
        return ok
    except Exception:
        return False


def _kill_pid(pid: int) -> None:
    if sys.platform != "win32" or not pid:
        return
    try:
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
        if h:
            k32.TerminateProcess(h, 1)
            k32.CloseHandle(h)
    except Exception:
        pass


def _create_kill_on_close_job():
    """자식 ssh 를 묶어둘 Job Object. 부모가 죽으면 커널이 함께 정리한다.

    Windows 는 부모가 죽어도 자식을 자동으로 죽이지 않는다. 오버레이가 크래시하거나
    강제 종료되면 `ssh -N -R` 가 고아로 남아 원격 포트를 계속 점유하고, 다음 기동 때
    "remote port forwarding failed" 로 터널이 영영 안 붙는다. 실제로 그 상태를 겪었다.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None

        class _BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class _ExtLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimit),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = _ExtLimit()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            return None
        return job
    except Exception:
        return None

# 상태
STATE_DOWN = "down"                # 꺼져 있음(사용자가 끔)
STATE_CONNECTING = "connecting"    # 띄우는 중 / 백오프 대기 중
STATE_UP = "up"                    # 연결됨
STATE_AUTH_FAILED = "auth_failed"  # 키 인증 실패 — 재시도하지 않는다
STATE_FAILED = "failed"            # 그 외 실패 — 백오프 재시도 중

_BACKOFF_SECS = (5, 10, 20, 40, 60)
# 이 시간 이상 살아 있으면 "성공"으로 보고 백오프를 리셋한다.
_STABLE_SECS = 30.0
_STDERR_KEEP = 8

# 호스트로 허용하는 문자. ssh_config 별칭·user@host·IPv6 대괄호까지 커버하되
# 셸 메타문자(" % ! ^ & | < > 등)와 제어문자는 배제한다.
# 이 값은 overlay.user.yaml 에서도 올 수 있으므로 신뢰하지 않는다.
_SAFE_HOST_RE = re.compile(r"^[A-Za-z0-9._\-@:\[\]()가-힣]+$")


def is_safe_host(host: str) -> bool:
    """ssh 인자·셸 문자열에 실어도 되는 호스트인지.

    - 선두 '-' 는 ssh 가 옵션으로 파싱한다(-oProxyCommand=... 등).
    - 셸 메타문자는 cmd 문자열을 만드는 경로에서 명령 주입이 된다.
    """
    h = (host or "").strip()
    if not h or h.startswith("-"):
        return False
    return bool(_SAFE_HOST_RE.match(h))


def sanitize_for_display(text: str, limit: int = 200) -> str:
    """UI 에 그대로 싣지 않는다 — 원격이 제어하는 문자열이 섞여 들어온다.

    개행·제어문자를 없애 가짜 행을 만들어 내는 것을 막는다.
    """
    cleaned = "".join(ch if ch.isprintable() else " " for ch in (text or ""))
    return cleaned[:limit]


_AUTH_FAIL_RE = re.compile(
    r"permission denied|no supported authentication|too many authentication|"
    r"host key verification failed",
    re.IGNORECASE,
)
# 같은 포트를 이미 다른 세션이 잡고 있는 경우. 재시도는 하되 조용히.
_PORT_BUSY_RE = re.compile(r"remote port forwarding failed", re.IGNORECASE)


@dataclass
class TunnelStatus:
    host: str
    state: str = STATE_DOWN
    since: float = 0.0            # 현재 state 진입 시각(monotonic)
    started_at: float = 0.0       # 현재 연결이 선 시각(wall clock, UP 일 때만 의미)
    retries: int = 0
    last_error: str = ""

    def uptime_secs(self) -> float:
        if self.state != STATE_UP or not self.since:
            return 0.0
        return max(0.0, time.monotonic() - self.since)


@dataclass
class _Tunnel:
    host: str
    proc: subprocess.Popen | None = None
    status: TunnelStatus = field(default_factory=lambda: TunnelStatus(host=""))
    backoff_idx: int = 0
    next_attempt_at: float = 0.0   # monotonic
    stderr_tail: deque = field(default_factory=lambda: deque(maxlen=_STDERR_KEEP))
    reader: threading.Thread | None = None
    launched_at: float = 0.0
    # 사용자가 직접 [연결]을 눌렀는가. 키 인증이 실패하면 이 경우에만 콘솔을 띄워
    # 비밀번호를 받는다. 자동 재연결이 콘솔을 예고 없이 띄우는 일은 없어야 한다.
    user_requested: bool = False
    console_tried: bool = False
    # 연결은 됐지만 프로세스를 특정하지 못한 상태. 감시도 재시작도 하지 않는다.
    unsupervised: bool = False
    # 콘솔 로그인으로 띄운 경우의 PID. 로그인이 끝나면 이 콘솔 창을 숨긴다.
    console_pid: int = 0
    console_hidden: bool = False


class TunnelManager:
    """원하는 대상 목록을 넣으면 그 상태로 수렴시킨다."""

    def __init__(
        self,
        get_port: Callable[[], int],
        poll_interval: float = 2.0,
        get_auto_reconnect: Callable[[], bool] | None = None,
    ):
        self._get_port = get_port
        # 끊겼을 때 다시 붙을지. 기본은 끔 — 사용자가 연결을 통제한다.
        self._get_auto_reconnect = get_auto_reconnect or (lambda: False)
        self._poll_interval = poll_interval
        self._lock = threading.RLock()
        self._tunnels: dict[str, _Tunnel] = {}
        self._stopping = False
        self._thread: threading.Thread | None = None
        # 이 핸들이 닫히면(=프로세스 종료) 커널이 자식 ssh 를 전부 정리한다.
        self._job = _create_kill_on_close_job()

    # ── 공개 API ────────────────────────────────────────────────────────────
    def register(self, hosts: list[str]) -> None:
        """대상 목록만 복원한다. **연결하지 않는다.**

        오버레이 재시작(재빌드 등) 후 설정을 잃지 않는 것이 목적이고, 연결 수립은
        사용자가 [연결]을 눌러 로그인하면서 한다. 무인 자동 연결로 만들면
        비밀번호를 받을 수 없어 키 인증이 강제되는데, 그건 요구사항이 아니었다.
        """
        wanted = []
        for h in dict.fromkeys((h or "").strip() for h in hosts):
            if not h:
                continue
            if not is_safe_host(h):
                log.warning("[tunnel] 설정의 호스트를 거부: %r", h)
                continue
            wanted.append(h)
        with self._lock:
            for host in list(self._tunnels):
                if host not in wanted:
                    self._stop_locked(host)
                    self._tunnels.pop(host, None)
            for host in wanted:
                if host not in self._tunnels:
                    t = _Tunnel(host=host)
                    t.status = TunnelStatus(host=host, state=STATE_DOWN, since=time.monotonic())
                    self._tunnels[host] = t
        self._ensure_thread()

    # 이전 이름 호환 — 목록 복원 의미로만 남긴다.
    apply = register

    def start(self, host: str) -> None:
        """사용자가 [연결]을 누른 경우. 키가 없으면 콘솔로 로그인을 받는다."""
        host = (host or "").strip()
        if not host:
            return
        with self._lock:
            t = self._tunnels.get(host)
            if t is None:
                t = _Tunnel(host=host)
                self._tunnels[host] = t
            # 이미 붙어 있으면 먼저 내린다. 안 그러면 같은 포트로 두 번 붙으려다
            # ExitOnForwardFailure 로 즉시 실패한다.
            if t.proc or t.unsupervised:
                self._stop_locked(host)
            t.backoff_idx = 0
            t.next_attempt_at = 0.0
            t.status.retries = 0
            t.status.last_error = ""
            t.user_requested = True
            t.console_tried = False
            t.unsupervised = False
            self._set_state(t, STATE_CONNECTING)
        self._ensure_thread()

    def start_automatic(self, host: str) -> None:
        """키 인증만 사용하는 무인 연결을 한 번 요청한다.

        이미 연결 중/연결됨/인증 실패이거나 기존 재연결 백오프가 살아 있으면
        상태를 건드리지 않는다. 자동 경로는 ``user_requested`` 를 세우지 않으므로
        키 인증 실패 뒤 비밀번호 콘솔로 전환되지 않는다.
        """
        host = (host or "").strip()
        if not host:
            return
        now = time.monotonic()
        with self._lock:
            t = self._tunnels.get(host)
            if t is None:
                t = _Tunnel(host=host)
                t.status = TunnelStatus(host=host, state=STATE_DOWN, since=now)
                self._tunnels[host] = t
            if t.status.state in (STATE_CONNECTING, STATE_UP, STATE_AUTH_FAILED):
                return
            if t.next_attempt_at > now:
                return
            t.status.last_error = ""
            t.user_requested = False
            t.console_tried = False
            t.unsupervised = False
            self._set_state(t, STATE_CONNECTING)
        self._ensure_thread()

    def hide_console(self, host: str) -> bool:
        """콘솔 로그인 창을 숨긴다. 사용자가 로그인을 마쳤다고 알려주는 경로다."""
        with self._lock:
            t = self._tunnels.get(host)
            if not t or not t.proc or t.proc.poll() is not None:
                return False
            pid = t.console_pid or t.proc.pid
        if hide_console_of(pid):
            with self._lock:
                t.console_hidden = True
            log.info("[tunnel] %s 콘솔 창 숨김 (pid=%d)", host, pid)
            return True
        return False

    def stop(self, host: str) -> None:
        with self._lock:
            self._stop_locked(host)

    def remove(self, host: str) -> None:
        """사용자가 목록에서 완전히 제거한 경우. 정지 후 내부 상태에서도 지운다.

        stop() 만으로는 STATE_DOWN 엔트리가 딕셔너리에 남아, status() 를 보고
        "살아 있는 고아 터널"을 복원하는 주기 갱신 로직이 이걸 다시 목록에
        되살려 버린다(제거해도 자동으로 재추가되는 버그의 원인).
        """
        with self._lock:
            self._stop_locked(host)
            self._tunnels.pop(host, None)

    def stop_all(self) -> None:
        self._stopping = True
        with self._lock:
            for host in list(self._tunnels):
                self._stop_locked(host)
            self._tunnels.clear()
        th = self._thread
        if th and th.is_alive():
            th.join(timeout=3.0)
        self._thread = None

    def status(self) -> dict[str, TunnelStatus]:
        with self._lock:
            return {h: t.status for h, t in self._tunnels.items()}

    def stderr_tail(self, host: str) -> list[str]:
        with self._lock:
            t = self._tunnels.get(host)
            return list(t.stderr_tail) if t else []

    # ── 내부 ────────────────────────────────────────────────────────────────
    def _set_state(self, t: _Tunnel, state: str, error: str = "") -> None:
        if t.status.state != state:
            t.status.since = time.monotonic()
        t.status.state = state
        if error:
            t.status.last_error = error

    def _stop_locked(self, host: str) -> None:
        t = self._tunnels.get(host)
        if not t:
            return
        t.unsupervised = False
        t.console_pid = 0
        t.console_hidden = False
        proc = t.proc
        t.proc = None
        if proc and proc.poll() is None:
            # 반드시 핸들로만 죽인다. 이름(ssh.exe)으로 죽이면 사용자의 다른
            # SSH 세션까지 함께 끊긴다.
            try:
                proc.terminate()
                proc.wait(timeout=3.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            log.info("[tunnel] %s 종료", host)
        self._set_state(t, STATE_DOWN)
        t.status.started_at = 0.0

    def _ensure_thread(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stopping = False
        self._thread = threading.Thread(target=self._supervise, name="tunnel-supervisor", daemon=True)
        self._thread.start()

    def _build_cmd(self, host: str, port: int, interactive: bool = False) -> list[str]:
        # interactive=True 면 BatchMode 를 빼고 콘솔에서 비밀번호를 받는다.
        # ssh 는 비밀번호를 stdin 이 아니라 TTY 에서 읽으므로 파이프로는 줄 수 없고,
        # 실제 콘솔(CREATE_NEW_CONSOLE)을 붙여줘야 한다.
        # -f 는 쓰지 않는다. Windows OpenSSH 의 -f 는 프로세스를 재실행하는 방식이라
        # 비밀번호 인증 세션을 이어받지 못하고 그대로 매달린다(키 인증에서만 동작).
        # 대신 로그인이 끝나면 콘솔 창을 숨긴다 — hide_console_of() 참고.
        extra = [] if interactive else ["-o", "BatchMode=yes"]
        return [
            "ssh",
            "-N",
            *extra,
            # 포워딩이 깨지면 ssh 도 종료 → poll() 로 터널 상태를 알 수 있게 된다.
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-o", "ConnectTimeout=10",
            "-R", f"{port}:127.0.0.1:{port}",
            host,
        ]

    def _spawn(self, t: _Tunnel) -> None:
        # 설정 파일에서 온 값이므로 띄우기 직전에 한 번 더 검증한다.
        if not is_safe_host(t.host):
            self._set_state(t, STATE_FAILED, "허용되지 않는 호스트 문자열")
            log.warning("[tunnel] 거부된 호스트: %r", t.host)
            t.next_attempt_at = time.monotonic() + 3600  # 사실상 재시도 안 함
            return
        try:
            port = int(self._get_port())
            if not (1 <= port <= 65535):
                raise ValueError(f"포트 범위 밖: {port}")
        except Exception as exc:
            # 여기서 예외가 새면 감시 스레드가 죽어 모든 터널이 방치된다.
            self._set_state(t, STATE_FAILED, f"포트 설정 오류: {exc}")
            t.next_attempt_at = time.monotonic() + 30
            return
        # 키 인증을 먼저 조용히 시도하고, 그게 안 될 때만 콘솔을 띄운다.
        # 키가 있는 대상은 창이 아예 안 뜨고, 없는 대상만 로그인 창이 뜬다.
        interactive = bool(t.user_requested and t.console_tried)
        cmd = self._build_cmd(t.host, port, interactive=interactive)
        if interactive:
            flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            io_kwargs = {}
        else:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
            io_kwargs = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.PIPE,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
            }
        try:
            proc = subprocess.Popen(cmd, creationflags=flags, **io_kwargs)
        except Exception as exc:
            self._set_state(t, STATE_FAILED, f"spawn 실패: {exc}")
            log.warning("[tunnel] %s spawn 실패: %s", t.host, exc)
            return

        if self._job:
            try:
                import ctypes

                k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                handle = int(proc._handle)  # type: ignore[attr-defined]
                if not k32.AssignProcessToJobObject(self._job, handle):
                    log.debug("[tunnel] job object 할당 실패 (pid=%s)", proc.pid)
            except Exception:
                pass

        t.proc = proc
        t.launched_at = time.monotonic()
        t.stderr_tail.clear()
        self._set_state(t, STATE_CONNECTING)
        if proc.stderr is not None:
            t.reader = threading.Thread(
                target=self._drain_stderr, args=(t, proc), name=f"tunnel-err-{t.host}", daemon=True
            )
            t.reader.start()
        log.info(
            "[tunnel] %s 시작 (port=%d, pid=%s, %s)",
            t.host, port, proc.pid, "콘솔 로그인" if interactive else "키 인증",
        )
        if interactive:
            t.console_pid = proc.pid

    def _drain_stderr(self, t: _Tunnel, proc: subprocess.Popen) -> None:
        try:
            assert proc.stderr is not None
            for line in proc.stderr:
                line = line.rstrip()
                if not line:
                    continue
                line = sanitize_for_display(line)
                with self._lock:
                    t.stderr_tail.append(line)
                    if _AUTH_FAIL_RE.search(line):
                        t.status.last_error = line
                    elif _PORT_BUSY_RE.search(line):
                        t.status.last_error = "원격 포트가 이미 사용 중 (다른 터널이 열려 있음)"
        except Exception:
            pass

    def _classify_exit(self, t: _Tunnel) -> tuple[str, str]:
        """종료 원인을 상태와 메시지로 분류한다."""
        joined = " | ".join(t.stderr_tail)
        if _AUTH_FAIL_RE.search(joined):
            return STATE_AUTH_FAILED, "키 인증 실패 — [키 등록] 후 다시 시도"
        if _PORT_BUSY_RE.search(joined):
            return STATE_FAILED, "원격 포트가 이미 사용 중 (다른 터널이 열려 있음)"
        return STATE_FAILED, (t.stderr_tail[-1] if t.stderr_tail else "연결이 끊겼다")

    def _supervise(self) -> None:
        while not self._stopping:
            now = time.monotonic()
            with self._lock:
                for t in list(self._tunnels.values()):
                    if t.status.state in (STATE_DOWN, STATE_AUTH_FAILED):
                        continue
                    # 프로세스를 특정 못 한 연결은 건드리지 않는다.
                    # (예전엔 핸들 없는 UP 상태를 "안 떴다"로 보고 계속 재spawn 했다)
                    if t.unsupervised:
                        continue
                    # 콘솔 로그인 중. 인증 완료 시점을 프로그램이 알 방법이 없다
                    # (-f 는 비밀번호 인증에서 동작하지 않고, 콘솔 화면 읽기도 불안정).
                    # 임의 타이머로 숨기면 사용자가 타이핑하는 중에 창이 사라진다.
                    # 그래서 숨기는 시점은 사용자가 [창 숨기기]로 알려준다.
                    if t.proc is not None and t.console_tried and t.proc.poll() is None:
                        if t.console_hidden:
                            if t.status.state != STATE_UP:
                                self._set_state(t, STATE_UP)
                                t.status.started_at = time.time()
                        else:
                            waited = int(now - t.launched_at)
                            t.status.last_error = (
                                f"콘솔에서 로그인 중 ({waited}초) — 끝나면 [창 숨기기]"
                            )
                        continue

                    proc = t.proc
                    if proc is not None and proc.poll() is None:
                        # 살아 있다. 충분히 오래 살았으면 UP 확정 + 백오프 리셋.
                        if t.status.state != STATE_UP:
                            self._set_state(t, STATE_UP)
                            t.status.started_at = time.time()
                        if now - t.launched_at >= _STABLE_SECS and t.backoff_idx:
                            t.backoff_idx = 0
                        continue

                    if proc is not None:
                        # 방금 죽었다.
                        was_up = t.status.state == STATE_UP
                        t.proc = None
                        state, msg = self._classify_exit(t)

                        # 사용자가 [연결]을 눌렀는데 키 인증이 안 되면, 이번 한 번만
                        # 콘솔을 띄워 비밀번호를 받는다. 자동 경로에서는 띄우지 않는다.
                        if state == STATE_AUTH_FAILED and t.user_requested and not t.console_tried:
                            t.console_tried = True
                            t.next_attempt_at = 0.0
                            self._set_state(t, STATE_CONNECTING, "콘솔에서 로그인")
                            log.info("[tunnel] %s 키 인증 불가 — 콘솔 로그인으로 전환", t.host)
                            continue

                        self._set_state(t, state, msg)
                        if state == STATE_AUTH_FAILED:
                            log.warning("[tunnel] %s 인증 실패 — 재시도 중단", t.host)
                            t.user_requested = False
                            continue

                        # 연결돼 있던 것이 끊긴 경우에만 자동 재연결을 고려한다.
                        # 기본값은 꺼짐 — 예고 없이 다시 붙지 않는다.
                        if not (was_up and self._get_auto_reconnect()):
                            self._set_state(t, STATE_DOWN, msg)
                            t.user_requested = False
                            log.info("[tunnel] %s 끊김(%s) — 자동 재연결 꺼짐", t.host, msg)
                            continue

                        t.status.retries += 1
                        delay = _BACKOFF_SECS[min(t.backoff_idx, len(_BACKOFF_SECS) - 1)]
                        t.backoff_idx += 1
                        t.next_attempt_at = now + delay
                        log.info("[tunnel] %s 끊김(%s) — %d초 후 재연결", t.host, msg, delay)
                        continue

                    # 프로세스가 없다 → 띄울 때가 됐는지 본다.
                    if now >= t.next_attempt_at:
                        self._spawn(t)

            sleep_left = self._poll_interval
            while sleep_left > 0 and not self._stopping:
                time.sleep(min(0.25, sleep_left))
                sleep_left -= 0.25


# ── ssh_config Host 별칭 ────────────────────────────────────────────────────
def ssh_host_aliases(config_path: Path | None = None) -> list[str]:
    """~/.ssh/config 의 Host 별칭 목록. 와일드카드 항목은 제외한다.

    scripts/setup-remote.ps1 의 Get-SshHostAliases 와 같은 규칙.
    """
    path = config_path or (Path.home() / ".ssh" / "config")
    if not path.exists():
        return []
    out: list[str] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^\s*Host\s+(.+?)\s*$", line, re.IGNORECASE)
            if not m:
                continue
            for token in m.group(1).split():
                if token and not re.search(r"[*?]", token) and token not in out:
                    out.append(token)
    except Exception:
        return out
    return out
