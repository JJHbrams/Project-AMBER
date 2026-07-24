"""Windows에서 claude_code_sdk를 안전하게 구동하기 위한 공용 헬퍼.

drt-notebookLM(backend/core/claude_client.py)의 검증된 패턴을 최소한으로 가져온 것으로,
core/identity/reflection_client.py가 최초로 증명했다. claude CLI를 SDK로 호출하는
모든 경로(반성 판단용 1회성 호출, overlay 말풍선 모드의 상주 세션 등)가 공유한다:

- Windows에서 claude.cmd를 PATH 밖에서도 찾아 node cli.js로 우회 실행
- SDK가 아직 인식하지 못하는 CLI 이벤트 타입을 걸러내는 Transport
- subprocess를 쓰려면 필요한 ProactorEventLoop 실행기
"""

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import anyio
from claude_code_sdk.types import ClaudeCodeOptions
from claude_code_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

logger = logging.getLogger(__name__)

if sys.platform == "win32":
    # SDK의 SubprocessCLITransport.connect()가 anyio.open_process()를 creationflags 없이
    # 호출한다. claude.EXE는 콘솔 서브시스템 실행 파일이라, stdin/stdout을 파이프로
    # 리다이렉트해도 CREATE_NO_WINDOW 없이는 부모(콘솔 없는 GUI 프로세스인 overlay)가
    # 자식을 띄울 때 새 콘솔 창이 그대로 튀어나온다 — bubble 모드가 "터미널 스폰 없이
    # overlay에 앵커링"하려는 설계와 어긋나므로, anyio 쪽 진입점에 플래그를 강제 주입한다.
    _original_open_process = anyio.open_process

    async def _open_process_no_console(*args, **kwargs):
        kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
        return await _original_open_process(*args, **kwargs)

    anyio.open_process = _open_process_no_console


def find_claude_cmd_parts() -> list[str]:
    """claude 실행 커맨드 파트 반환. Windows 앱 모드(PATH 미포함)에서도 동작."""
    home = Path(os.environ.get("USERPROFILE", os.path.expanduser("~")))

    found = shutil.which("claude")
    if found and not found.lower().endswith((".cmd", ".bat")):
        return [found]

    appdata = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
    exe_candidates = [
        home / ".local" / "bin" / "claude.EXE",
        home / ".local" / "bin" / "claude",
        appdata / "npm" / "claude.exe",
        home / ".npm-global" / "bin" / "claude",
    ]
    for exe in exe_candidates:
        if exe.exists():
            return [str(exe)]

    npm_dirs = [appdata / "npm", home / ".npm-global"]
    for npm_dir in npm_dirs:
        js_script = npm_dir / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
        if js_script.exists():
            node_exe = shutil.which("node")
            if not node_exe:
                for node_dir in [
                    Path("C:/Program Files/nodejs"),
                    Path("C:/Program Files (x86)/nodejs"),
                    home / "AppData/Local/Programs/nodejs",
                ]:
                    candidate = node_dir / "node.exe"
                    if candidate.exists():
                        node_exe = str(candidate)
                        break
            if node_exe:
                return [node_exe, str(js_script)]

    if found:
        return [found]
    return ["claude"]


class PassthroughCLITransport(SubprocessCLITransport):
    """SDK가 아직 인식하지 못하는 CLI 이벤트 타입을 걸러내는 Transport.

    parse_message()가 MessageParseError를 던지기 전에, 생성자로 받은
    passthrough_types에 속한 type의 메시지를 조용히 무시한다.
    """

    def __init__(
        self,
        prompt,
        options: ClaudeCodeOptions,
        cmd_parts: list[str],
        passthrough_types: frozenset[str] = frozenset(),
    ):
        super().__init__(prompt, options, cli_path=cmd_parts[0])
        self._cmd_parts = cmd_parts
        self._passthrough_types = passthrough_types

    def _build_command(self) -> list[str]:
        cmd = super()._build_command()
        if len(self._cmd_parts) > 1:
            cmd = self._cmd_parts + cmd[1:]
        return cmd

    def read_messages(self):
        return self._filtered_messages()

    async def _filtered_messages(self):
        async for msg in super().read_messages():
            if isinstance(msg, dict) and msg.get("type") in self._passthrough_types:
                logger.debug("[claude_cli_transport] %s 수신 — 무시", msg.get("type"))
                continue
            yield msg


def make_transport(
    prompt,
    options: ClaudeCodeOptions,
    passthrough_types: frozenset[str] = frozenset(),
) -> PassthroughCLITransport:
    return PassthroughCLITransport(prompt, options, find_claude_cmd_parts(), passthrough_types)


def run_in_proactor(coro):
    """새 ProactorEventLoop에서 코루틴을 실행한다 (Windows subprocess 지원 필수)."""
    if sys.platform == "win32":
        loop = asyncio.ProactorEventLoop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
    return asyncio.run(coro)
