"""Discord Bot — 자동 응답 봇.

역할:
- overlay 프로세스와 동일한 수명 (overlay ON → 온라인, OFF → 오프라인)
- 허용된 사용자가 @멘션 시 즉시 claude -p subprocess로 LLM 응답 생성
- 응답을 Discord REST API로 전송 (🕐 → ✅ 리액션 교체)
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import discord

from core.storage.db import get_connection, initialize_db
from core.memory.bus import memory_bus
from core.config.runtime_config import (
    get_discord_scope_prefix,
    get_copilot_model,
    get_copilot_allow_all_tools,
)
from overlay.config import load_cfg

log = logging.getLogger(__name__)

_ENV_PATH = Path.home() / ".engram" / ".env"
_LOG_PATH = Path.home() / ".engram" / "overlay.log"
DISCORD_SCOPE_PREFIX = get_discord_scope_prefix()
COPILOT_MODEL = get_copilot_model()
COPILOT_ALLOW_ALL_TOOLS = get_copilot_allow_all_tools()


def _setup_file_logging():
    """exe 환경에서 로그 핸들러가 없을 때만 파일 핸들러 추가 (중복 방지)."""
    root = logging.getLogger()
    if any(isinstance(h, logging.FileHandler) for h in root.handlers):
        return
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(str(_LOG_PATH), encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        root.addHandler(handler)
        root.setLevel(logging.INFO)
    except Exception:
        pass


def _load_env_file():
    """~/.engram/.env 파일을 읽어 환경변수에 주입 (없으면 스킵)."""
    if not _ENV_PATH.exists():
        return
    for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _discord_api(token: str, method: str, path: str, body=None) -> int:
    """Discord REST API 동기 호출. HTTP 상태코드 반환."""
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "EngramBot/1.0",
    }
    url = f"https://discord.com/api/v10{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def _filter_copilot_output(raw: str) -> str:
    """copilot -p 출력에서 툴콜 블록과 usage 통계를 제거하고 본문만 반환.

    copilot 출력 구조:
      ● ToolName (MCP: ...) · ...   ← 툴 호출 시작
        └ {...}                      ← 툴 결과 (들여쓰기)
                                     ← 빈 줄로 블록 끝
      실제 응답 텍스트               ← 이것만 남긴다

      Total usage est: ...           ← usage 통계 (이후 전부 제거)
    """
    # usage 통계 이후 제거
    idx = raw.find("Total usage est:")
    if idx != -1:
        raw = raw[:idx]

    lines = raw.splitlines()
    result_lines = []
    in_tool_block = False

    for line in lines:
        stripped = line.strip()

        # 툴 블록 시작: ● 로 시작
        if stripped.startswith("●"):
            in_tool_block = True
            continue

        # 툴 블록 내부: 들여쓰기 있는 줄이거나 └ 포함
        if in_tool_block:
            if stripped.startswith("└") or (line.startswith(" ") or line.startswith("\t")):
                continue
            else:
                # 들여쓰기 없는 빈 줄 → 블록 종료
                in_tool_block = False
                if stripped == "":
                    continue  # 블록 직후 빈 줄도 스킵

        result_lines.append(line)

    return "\n".join(result_lines).strip()


def _generate_and_send(token: str, channel_id: str, message_id: str, content: str, stop_event: threading.Event):
    """별도 스레드에서 LLM 응답 생성 후 Discord 전송."""
    if stop_event.is_set():
        return

    scope_key = f"{DISCORD_SCOPE_PREFIX}{channel_id}"
    session = memory_bus.start_session(scope_key=scope_key)
    memory_bus.record_user_message(session, content)

    try:
        system = memory_bus.compose_prompt_context(user_query=content, caller="copilot-cli", session=session)
        full_prompt = f"{system}\n\n{content}"

        env = os.environ.copy()
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

        # Windows에서 copilot은 .cmd 스크립트이므로 shutil.which로 전체 경로를 찾는다.
        copilot_path = shutil.which("copilot") or "copilot"
        copilot_opts: list[str] = ["--model", COPILOT_MODEL]
        if COPILOT_ALLOW_ALL_TOOLS:
            copilot_opts.append("--allow-all-tools")
        copilot_opts.extend(["-p", full_prompt])

        cmd_args: list[str]
        if os.name == "nt" and copilot_path.lower().endswith((".cmd", ".bat")):
            cmd_args = ["cmd", "/c", copilot_path, *copilot_opts]
        else:
            cmd_args = [copilot_path, *copilot_opts]

        log.info(f"[discord] copilot 실행: {cmd_args[0]}")

        proc = subprocess.Popen(
            cmd_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,  # 입력 대기 방지
            text=True,
            encoding="utf-8",
            env=env,
            creationflags=creation_flags,
        )

        # 종료 신호 오면 subprocess kill
        while proc.poll() is None:
            if stop_event.wait(timeout=0.5):
                proc.kill()
                log.info("[discord] 종료 신호로 응답 생성 취소")
                return

        stdout, stderr = proc.communicate()
        log.info(f"[discord] copilot 종료 코드: {proc.returncode}, stdout 길이: {len(stdout)}")
        if stderr:
            log.warning(f"[discord] copilot stderr: {stderr[:300]}")
        reply = _filter_copilot_output(stdout) if proc.returncode == 0 else None
        if not reply:
            reply = "(응답 생성 실패)"
            log.error(f"[discord] copilot 오류: {stderr[:200]}")
    except Exception as e:
        reply = "(응답 생성 중 오류 발생)"
        log.error(f"[discord] 응답 생성 오류: {e}", exc_info=True)

    if stop_event.is_set():
        return

    memory_bus.record_assistant_message(
        session,
        reply,
        user_content=content,
        update_working_memory=True,
    )

    # Discord 전송
    status = _discord_api(token, "POST", f"/channels/{channel_id}/messages", {"content": reply})
    if status not in (200, 201):
        log.error(f"[discord] 전송 실패: HTTP {status}")
        return

    # 🕐 → ✅ 리액션 교체
    if message_id:
        clock = urllib.parse.quote("🕐", safe="")
        check = urllib.parse.quote("✅", safe="")
        _discord_api(token, "DELETE", f"/channels/{channel_id}/messages/{message_id}/reactions/{clock}/@me")
        _discord_api(token, "PUT", f"/channels/{channel_id}/messages/{message_id}/reactions/{check}/@me")

    # DB 기록 (참고용)
    _enqueue(channel_id=channel_id, message_id=message_id, content=content, reply=reply)
    log.info(f"[discord] 응답 전송 완료: {reply[:60]}...")


class EngramDiscordBot:
    """overlay 생명주기와 연동되는 Discord 봇 래퍼."""

    def __init__(self):
        self._client: discord.Client | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cfg: dict = {}
        self._token: str = ""
        self._stop_event = threading.Event()

    # ── 공개 인터페이스 ───────────────────────────────────────

    def start(self):
        """overlay 시작 시 호출. 별도 스레드에서 이벤트 루프 실행."""
        _setup_file_logging()
        _load_env_file()
        initialize_db()
        cfg = load_cfg()
        self._cfg = cfg.get("discord", {})
        self._token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
        if not self._token:
            log.warning("[discord] DISCORD_BOT_TOKEN 환경변수 없음 — 봇 비활성화")
            return

        self._stop_event.clear()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, args=(self._token,), daemon=True, name="discord-bot")
        self._thread.start()
        log.info("[discord] 봇 스레드 시작")

    def stop(self):
        """overlay 종료 시 호출. 비블로킹으로 봇 종료 신호만 보내고 즉시 반환."""
        self._stop_event.set()
        if self._client and self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._client.close(), self._loop)
        log.info("[discord] 봇 종료 신호 전송")

    # ── 내부 구현 ─────────────────────────────────────────────

    def _run(self, token: str):
        asyncio.set_event_loop(self._loop)
        intents = discord.Intents.default()
        intents.message_content = True

        self._client = discord.Client(intents=intents)
        self._register_events(token)

        try:
            self._loop.run_until_complete(self._client.start(token))
        except Exception as e:
            log.error(f"[discord] 봇 오류: {e}")
        finally:
            self._loop.close()

    def _register_events(self, token: str):
        client = self._client
        cfg = self._cfg

        allowed_channel = str(cfg.get("channel_id", ""))
        allowed_users = [str(u) for u in cfg.get("allowed_user_ids", [])]

        @client.event
        async def on_ready():
            log.info(f"[discord] 로그인: {client.user} (id={client.user.id})")

        @client.event
        async def on_message(message: discord.Message):
            if message.author.bot:
                return
            if allowed_channel and str(message.channel.id) != allowed_channel:
                return
            if client.user not in message.mentions:
                return
            if allowed_users and str(message.author.id) not in allowed_users:
                log.warning(f"[discord] 미허가 사용자 무시: {message.author} ({message.author.id})")
                return

            # 멘션 제거
            content = message.content
            for mention in message.mentions:
                content = content.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
            content = content.strip()
            if not content:
                return

            safe_content = f"[Discord/@{message.author.name}]: {content}"
            log.info(f"[discord] 수신: {safe_content[:60]}...")

            # 미처리 큐가 5개 초과면 가장 오래된 것 삭제
            _trim_queue(max_unprocessed=5)

            # 🕐 즉시 반응
            await message.add_reaction("🕐")

            # 별도 스레드에서 LLM 호출 + 전송 (이벤트 루프 블로킹 방지)
            threading.Thread(
                target=_generate_and_send,
                args=(token, str(message.channel.id), str(message.id), safe_content, self._stop_event),
                daemon=True,
            ).start()


def _trim_queue(max_unprocessed: int = 5):
    """미처리 메시지가 max_unprocessed 초과 시 가장 오래된 것부터 삭제."""
    try:
        with get_connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM discord_queue WHERE processed=0").fetchone()[0]
            if count >= max_unprocessed:
                conn.execute(
                    "DELETE FROM discord_queue WHERE processed=0 AND id IN "
                    "(SELECT id FROM discord_queue WHERE processed=0 ORDER BY created_at ASC LIMIT ?)",
                    (count - max_unprocessed + 1,),
                )
    except Exception:
        pass


def _enqueue(channel_id: str, message_id: str, content: str, reply: str):
    """대화 기록을 DB에 저장 (참고용)."""
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO discord_queue (guild_id, channel_id, author_id, author_name, content, message_id, processed) VALUES (?,?,?,?,?,?,1)",
                ("", channel_id, "", "", content, message_id),
            )
    except Exception:
        pass


