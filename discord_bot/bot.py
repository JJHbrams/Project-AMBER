"""Discord Bot — 자동 응답 봇.

역할:
- overlay 프로세스와 동일한 수명 (overlay ON → 온라인, OFF → 오프라인)
- 허용된 사용자가 @멘션 시 선택된 CLI provider로 LLM 응답 생성
- 응답을 Discord REST API로 전송 (🕐 → ✅ 리액션 교체)
"""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import discord

from core.storage.db import get_connection, initialize_db
from core.memory.bus import MemorySession, memory_bus
from core.config.runtime_config import (
    get_discord_scope_prefix,
    get_copilot_model,
    get_copilot_allow_all_tools,
)
from overlay.config import load_cfg, normalize_cli_provider

log = logging.getLogger(__name__)

_ENV_PATH = Path.home() / ".engram" / ".env"
_LOG_PATH = Path.home() / ".engram" / "overlay.log"
ENGRAM_CMD = Path.home() / ".engram" / "engram-copilot.cmd"
CLAUDE_MCP_CONFIG = Path.home() / ".engram" / "claude-mcp.json"
DISCORD_SCOPE_PREFIX = get_discord_scope_prefix()
COPILOT_MODEL = get_copilot_model()
COPILOT_ALLOW_ALL_TOOLS = get_copilot_allow_all_tools()
DISCORD_CLI_SESSION_PREFIX = "discord-bot"
DEFAULT_NEW_SESSION_TRIGGERS = ["/new", "/newsession", "/새세션"]
DEFAULT_SESSION_LIST_LIMIT = 8
MAX_SESSION_LIST_LIMIT = 20
SESSION_PREVIEW_MAX_CHARS = 56
DEFAULT_OLLAMA_MODEL = "qwen3.5:4b"
DEFAULT_QUEUE_MAX_PER_CHANNEL = 8
MAX_QUEUE_MAX_PER_CHANNEL = 200
DEFAULT_QUEUE_TTL_SECONDS = 180
MAX_QUEUE_TTL_SECONDS = 3600
DEFAULT_QUEUE_DROP_POLICY = "drop_oldest"
DEFAULT_MAX_PARALLEL_CHANNELS = 3
MAX_PARALLEL_CHANNELS = 16
DEFAULT_QUEUE_NOTIFY_WAITING = True
DEFAULT_QUEUE_WAIT_NOTICE_MIN_POSITION = 2
DEFAULT_QUEUE_WAIT_NOTICE_COOLDOWN_SECONDS = 20
DEFAULT_QUEUE_NOTIFY_TTL_EXPIRED = True
MAX_QUEUE_WAIT_NOTICE_COOLDOWN_SECONDS = 600
_CLAUDE_MODEL_ALIASES = {
    "default",
    "best",
    "sonnet",
    "opus",
    "haiku",
    "opusplan",
    "sonnet[1m]",
    "opus[1m]",
}


@dataclass(slots=True)
class _PendingTask:
    token: str
    channel_id: str
    message_id: str
    content: str
    provider: str
    enqueued_at: float


def _build_exec_command(executable: str, args: list[str]) -> list[str]:
    exe = str(executable or "").strip()
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", exe, *args]
    return [exe, *args]


def _provider_supports_resume(provider: str) -> bool:
    return provider in {"copilot", "claude-code", "claude-code-ollama"}


def _normalize_model_id(model_id: str) -> str:
    value = str(model_id or "").strip().lower()
    if value.endswith("[1m]"):
        return value[:-4]
    return value


def _looks_like_claude_model(model_id: str) -> bool:
    value = _normalize_model_id(model_id)
    if not value:
        return False
    if value in _CLAUDE_MODEL_ALIASES:
        return True
    return value.startswith("claude-")


def _provider_caller_name(provider: str) -> str:
    normalized = normalize_cli_provider(provider)
    if normalized in {"claude-code", "claude-code-ollama"}:
        return "claude-code"
    if normalized == "gemini":
        return "gemini-cli"
    if normalized == "ollama":
        return "ollama-cli"
    return "copilot-cli"


def _coerce_int(value: object, default: int, *, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    parsed = max(min_value, min(parsed, max_value))
    return parsed


def _coerce_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    lowered = str(value).strip().lower()
    if lowered in {"1", "true", "yes", "y", "on"}:
        return True
    if lowered in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


def _normalize_queue_drop_policy(value: object) -> str:
    policy = str(value or "").strip().lower().replace("-", "_")
    if policy in {"drop_oldest", "drop_newest"}:
        return policy
    return DEFAULT_QUEUE_DROP_POLICY


def _to_id_set(single_value: object, multi_value: object) -> set[str]:
    values: set[str] = set()

    single = str(single_value or "").strip()
    if single:
        values.add(single)

    if isinstance(multi_value, str):
        for part in multi_value.split(","):
            v = str(part or "").strip()
            if v:
                values.add(v)
        return values

    if isinstance(multi_value, (list, tuple, set)):
        for item in multi_value:
            v = str(item or "").strip()
            if v:
                values.add(v)
    return values


def _load_queue_settings(discord_cfg: dict) -> tuple[int, int, str, int]:
    queue_cfg = discord_cfg.get("queue", {}) if isinstance(discord_cfg, dict) else {}
    if not isinstance(queue_cfg, dict):
        queue_cfg = {}

    max_per_channel = _coerce_int(
        queue_cfg.get("max_per_channel", discord_cfg.get("queue_max_per_channel", DEFAULT_QUEUE_MAX_PER_CHANNEL)),
        DEFAULT_QUEUE_MAX_PER_CHANNEL,
        min_value=1,
        max_value=MAX_QUEUE_MAX_PER_CHANNEL,
    )
    ttl_seconds = _coerce_int(
        queue_cfg.get("ttl_seconds", discord_cfg.get("queue_ttl_seconds", DEFAULT_QUEUE_TTL_SECONDS)),
        DEFAULT_QUEUE_TTL_SECONDS,
        min_value=10,
        max_value=MAX_QUEUE_TTL_SECONDS,
    )
    max_parallel_channels = _coerce_int(
        queue_cfg.get("max_parallel_channels", discord_cfg.get("max_parallel_channels", DEFAULT_MAX_PARALLEL_CHANNELS)),
        DEFAULT_MAX_PARALLEL_CHANNELS,
        min_value=1,
        max_value=MAX_PARALLEL_CHANNELS,
    )
    drop_policy = _normalize_queue_drop_policy(queue_cfg.get("drop_policy", discord_cfg.get("queue_drop_policy", DEFAULT_QUEUE_DROP_POLICY)))
    return max_per_channel, ttl_seconds, drop_policy, max_parallel_channels


def _load_queue_notice_settings(discord_cfg: dict) -> tuple[bool, int, int, bool]:
    queue_cfg = discord_cfg.get("queue", {}) if isinstance(discord_cfg, dict) else {}
    if not isinstance(queue_cfg, dict):
        queue_cfg = {}

    notify_waiting = _coerce_bool(
        queue_cfg.get("notify_waiting", discord_cfg.get("queue_notify_waiting", DEFAULT_QUEUE_NOTIFY_WAITING)),
        DEFAULT_QUEUE_NOTIFY_WAITING,
    )
    wait_notice_min_position = _coerce_int(
        queue_cfg.get(
            "wait_notice_min_position",
            discord_cfg.get("queue_wait_notice_min_position", DEFAULT_QUEUE_WAIT_NOTICE_MIN_POSITION),
        ),
        DEFAULT_QUEUE_WAIT_NOTICE_MIN_POSITION,
        min_value=1,
        max_value=50,
    )
    wait_notice_cooldown_seconds = _coerce_int(
        queue_cfg.get(
            "wait_notice_cooldown_seconds",
            discord_cfg.get(
                "queue_wait_notice_cooldown_seconds",
                DEFAULT_QUEUE_WAIT_NOTICE_COOLDOWN_SECONDS,
            ),
        ),
        DEFAULT_QUEUE_WAIT_NOTICE_COOLDOWN_SECONDS,
        min_value=0,
        max_value=MAX_QUEUE_WAIT_NOTICE_COOLDOWN_SECONDS,
    )
    notify_ttl_expired = _coerce_bool(
        queue_cfg.get(
            "notify_ttl_expired",
            discord_cfg.get("queue_notify_ttl_expired", DEFAULT_QUEUE_NOTIFY_TTL_EXPIRED),
        ),
        DEFAULT_QUEUE_NOTIFY_TTL_EXPIRED,
    )
    return notify_waiting, wait_notice_min_position, wait_notice_cooldown_seconds, notify_ttl_expired


def _to_provider_overrides(raw: object) -> dict[str, str]:
    overrides: dict[str, str] = {}
    if not isinstance(raw, dict):
        return overrides

    for key, value in raw.items():
        target_id = str(key or "").strip()
        if not target_id:
            continue
        overrides[target_id] = normalize_cli_provider(str(value or "").strip())
    return overrides


def _setup_file_logging():
    """exe 환경에서 로그 핸들러가 없을 때만 파일 핸들러 추가 (중복 방지)."""
    root = logging.getLogger()
    # 핸들러 유무와 무관하게 DEBUG 레벨은 항상 설정
    root.setLevel(logging.DEBUG)
    logging.getLogger("discord_bot").setLevel(logging.DEBUG)
    if any(isinstance(h, logging.FileHandler) for h in root.handlers):
        return
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(str(_LOG_PATH), encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        root.addHandler(handler)
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


def _set_message_status_reaction(token: str, channel_id: str, message_id: str, emoji: str) -> None:
    if not message_id:
        return
    clock = urllib.parse.quote("🕐", safe="")
    target = urllib.parse.quote(str(emoji), safe="")
    _discord_api(token, "DELETE", f"/channels/{channel_id}/messages/{message_id}/reactions/{clock}/@me")
    _discord_api(token, "PUT", f"/channels/{channel_id}/messages/{message_id}/reactions/{target}/@me")


def _reply_to_message(token: str, channel_id: str, message_id: str, content: str) -> int:
    body = {
        "content": str(content),
        "allowed_mentions": {"replied_user": False},
    }
    if message_id:
        body["message_reference"] = {
            "message_id": str(message_id),
            "channel_id": str(channel_id),
            "fail_if_not_exists": False,
        }
    return _discord_api(token, "POST", f"/channels/{channel_id}/messages", body)


def _format_seconds(seconds: float) -> str:
    value = max(0, int(round(float(seconds))))
    if value < 60:
        return f"{value}s"
    mm, ss = divmod(value, 60)
    return f"{mm}m {ss}s"


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


def _build_copilot_command(prompt: str, session_name: str, use_resume: bool) -> list[str]:
    """Copilot 호출 커맨드 생성.

    우선순위:
    1) engram-copilot.cmd (overlay와 동일한 bootstrap/mcp 설정)
    2) 시스템 copilot 바이너리 직접 호출 (fallback)
    """
    if ENGRAM_CMD.exists():
        mode_args = ["--resume", session_name] if use_resume else ["--name", session_name]
        return ["cmd", "/c", str(ENGRAM_CMD), *mode_args, "-p", prompt]

    # fallback: script가 없을 때만 직접 copilot 실행
    copilot_path = shutil.which("copilot") or "copilot"
    copilot_opts: list[str] = ["--model", COPILOT_MODEL]
    if COPILOT_ALLOW_ALL_TOOLS:
        copilot_opts.append("--allow-all-tools")
    copilot_opts.extend(["--resume", session_name] if use_resume else ["--name", session_name])
    copilot_opts.extend(["-p", prompt])

    if os.name == "nt" and copilot_path.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", copilot_path, *copilot_opts]
    return [copilot_path, *copilot_opts]


def _build_claude_command(prompt: str, session_name: str, use_resume: bool, cli_cfg: dict) -> list[str]:
    claude_command = str(cli_cfg.get("claude_command") or "claude").strip() or "claude"
    claude_opts: list[str] = []

    if CLAUDE_MCP_CONFIG.exists():
        claude_opts.extend(["--mcp-config", str(CLAUDE_MCP_CONFIG)])

    model_id = str(cli_cfg.get("claude_model") or cli_cfg.get("ollama_model") or "").strip()
    if model_id and _looks_like_claude_model(model_id):
        claude_opts.extend(["--model", model_id])

    claude_opts.extend(["--resume", session_name] if use_resume else ["--name", session_name])
    claude_opts.extend(["-p", prompt])
    return _build_exec_command(claude_command, claude_opts)


def _build_gemini_command(prompt: str, cli_cfg: dict) -> list[str]:
    gemini_command = str(cli_cfg.get("gemini_command") or "gemini").strip() or "gemini"
    gemini_opts: list[str] = ["--allowed-mcp-server-names", "engram", "-p", prompt]
    return _build_exec_command(gemini_command, gemini_opts)


def _build_ollama_command(prompt: str, cli_cfg: dict) -> list[str]:
    ollama_command = str(cli_cfg.get("ollama_command") or "ollama").strip() or "ollama"
    ollama_model = str(cli_cfg.get("ollama_model") or DEFAULT_OLLAMA_MODEL).strip() or DEFAULT_OLLAMA_MODEL
    ollama_opts: list[str] = ["run", ollama_model, prompt]
    return _build_exec_command(ollama_command, ollama_opts)


def _build_provider_command(
    provider: str,
    prompt: str,
    session_name: str,
    use_resume: bool,
    cli_cfg: dict,
) -> tuple[list[str], bool]:
    normalized = normalize_cli_provider(provider)
    if normalized == "copilot":
        return _build_copilot_command(prompt, session_name, use_resume=use_resume), use_resume
    if normalized in {"claude-code", "claude-code-ollama"}:
        if normalized == "claude-code-ollama":
            configured_model = str(cli_cfg.get("ollama_model") or DEFAULT_OLLAMA_MODEL).strip() or DEFAULT_OLLAMA_MODEL
        else:
            configured_model = str(cli_cfg.get("claude_model") or cli_cfg.get("ollama_model") or "").strip()
        if configured_model and not _looks_like_claude_model(configured_model):
            return _build_ollama_command(prompt, cli_cfg=cli_cfg), False
        return _build_claude_command(prompt, session_name, use_resume=use_resume, cli_cfg=cli_cfg), use_resume
    if normalized == "gemini":
        return _build_gemini_command(prompt, cli_cfg=cli_cfg), False
    if normalized == "ollama":
        return _build_ollama_command(prompt, cli_cfg=cli_cfg), False
    return _build_copilot_command(prompt, session_name, use_resume=use_resume), use_resume


def _load_new_session_triggers(discord_cfg: dict) -> list[str]:
    raw = discord_cfg.get("new_session_triggers", []) if isinstance(discord_cfg, dict) else []
    values: list[str] = []
    if isinstance(raw, list):
        values = [str(v).strip().lower() for v in raw if str(v).strip()]
    if not values:
        values = [t.lower() for t in DEFAULT_NEW_SESSION_TRIGGERS]

    # 순서 유지 dedupe
    deduped: list[str] = []
    seen = set()
    for v in values:
        if v not in seen:
            seen.add(v)
            deduped.append(v)
    return deduped


def _parse_session_reset_command(content: str, triggers: list[str]) -> tuple[bool, str]:
    """명시적 새 세션 명령을 파싱한다.

    - 단일 토큰 트리거(`/new`)는 `/new 질문` 형태를 허용.
    - 공백 포함 트리거(`새 세션`)는 완전 일치만 허용.
    """
    text = re.sub(r"\s+", " ", (content or "").strip())
    lowered = text.lower()
    if not lowered:
        return False, ""

    parts = lowered.split(" ", 1)
    head = parts[0]
    tail = text.split(" ", 1)[1].strip() if len(text.split(" ", 1)) == 2 else ""

    for trigger in triggers:
        t = str(trigger or "").strip().lower()
        if not t:
            continue
        if " " in t:
            if lowered == t:
                return True, ""
            continue
        if head == t:
            return True, tail
    return False, text


def _parse_session_command(content: str) -> dict | None:
    """/session 명령 파싱.

    지원:
    - /session
    - /session list [N]
    - /session use <session_id>
    - /session new [질문]
    """
    text = re.sub(r"\s+", " ", (content or "").strip())
    lowered = text.lower()
    if not lowered.startswith("/session"):
        return None

    parts = text.split(" ")
    if len(parts) == 1:
        return {"action": "list", "limit": DEFAULT_SESSION_LIST_LIMIT}

    sub = parts[1].lower()
    if sub in {"list", "ls"}:
        limit = DEFAULT_SESSION_LIST_LIMIT
        if len(parts) >= 3:
            try:
                limit = int(parts[2])
            except ValueError:
                return {"action": "help", "error": "목록 개수는 숫자로 입력해줘."}
        limit = max(1, min(limit, MAX_SESSION_LIST_LIMIT))
        return {"action": "list", "limit": limit}

    if sub == "use":
        if len(parts) < 3:
            return {"action": "help", "error": "사용할 세션 ID를 입력해줘. 예: /session use 123"}
        try:
            session_id = int(parts[2])
        except ValueError:
            return {"action": "help", "error": "세션 ID는 숫자여야 해."}
        return {"action": "use", "session_id": session_id}

    if sub in {"new", "reset"}:
        remainder = text.split(" ", 2)[2].strip() if len(text.split(" ", 2)) == 3 else ""
        return {"action": "new", "content": remainder}

    return {"action": "help", "error": "알 수 없는 /session 하위 명령이야."}


def _to_session_preview(raw: str, max_chars: int = SESSION_PREVIEW_MAX_CHARS) -> str:
    text = re.sub(r"^\[Discord/@[^\]]+\]:\s*", "", str(raw or "").strip())
    text = re.sub(r"\s+", " ", text)
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max(1, max_chars - 3)].rstrip() + "..."


def _build_scoped_bootstrap_prompt(content: str, scope_key: str, caller: str) -> str:
    """첫 턴에 채널 스코프 컨텍스트 초기화를 강제하는 프롬프트를 만든다."""
    safe_scope = str(scope_key or "").replace("'", "\\'")
    safe_caller = str(caller or "copilot-cli").replace("'", "\\'")
    bootstrap = (
        "Before answering, initialize channel-scoped memory context. "
        f"Call engram_get_context_once(caller='{safe_caller}', scope_key='{safe_scope}') "
        "exactly once per CLI session. If already initialized, continue. "
        "Then answer the user message below."
    )
    return f"{bootstrap}\n\n{content}"


def _run_cli_command(
    cmd_args: list[str],
    stop_event: threading.Event,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str, str, bool]:
    """CLI subprocess를 실행하고 (code, stdout, stderr, cancelled)를 반환."""
    env = os.environ.copy()
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items() if v is not None})
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    proc = subprocess.Popen(
        cmd_args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        env=env,
        creationflags=creation_flags,
    )

    while proc.poll() is None:
        if stop_event.wait(timeout=0.5):
            proc.kill()
            log.info("[discord] 종료 신호로 응답 생성 취소")
            return -1, "", "", True

    stdout, stderr = proc.communicate()
    return proc.returncode, stdout or "", stderr or "", False


def _generate_and_send(
    bot: "EngramDiscordBot",
    token: str,
    channel_id: str,
    message_id: str,
    content: str,
    provider: str,
    stop_event: threading.Event,
):
    """별도 스레드에서 LLM 응답 생성 후 Discord 전송."""
    if stop_event.is_set():
        return

    session, channel_lock = bot._get_or_create_channel_state(channel_id)
    provider = normalize_cli_provider(provider or bot.get_cli_provider())
    cli_cfg = bot.get_cli_cfg()
    provider_caller = _provider_caller_name(provider)
    session_name = bot.build_cli_session_name(provider, channel_id, session.session_id)

    try:
        with channel_lock:
            # 채널 단위로 순차 실행해 세션 순서를 보장한다.
            use_resume = _provider_supports_resume(provider) and bot.has_provider_session(provider, session.session_id)
            memory_bus.record_user_message(session, content)

            prompt_text = content if use_resume else _build_scoped_bootstrap_prompt(content, session.scope_key, provider_caller)
            cmd_args, used_resume = _build_provider_command(
                provider,
                prompt_text,
                session_name,
                use_resume=use_resume,
                cli_cfg=cli_cfg,
            )
            mode_label = "resume" if used_resume else "fresh"
            log.info(f"[discord] {provider} 실행({mode_label}): {cmd_args[0]}")

            code, stdout, stderr, cancelled = _run_cli_command(
                cmd_args,
                stop_event,
                extra_env={"ENGRAM_SCOPE_KEY": session.scope_key},
            )
            if cancelled:
                return

            # resume 실패 시 1회 신규 세션(name)로 자동 복구
            if code != 0 and used_resume:
                log.warning(f"[discord] {provider} resume 실패로 신규 세션 생성 재시도")
                retry_prompt = _build_scoped_bootstrap_prompt(content, session.scope_key, provider_caller)
                retry_args, _ = _build_provider_command(
                    provider,
                    retry_prompt,
                    session_name,
                    use_resume=False,
                    cli_cfg=cli_cfg,
                )
                code, stdout, stderr, cancelled = _run_cli_command(
                    retry_args,
                    stop_event,
                    extra_env={"ENGRAM_SCOPE_KEY": session.scope_key},
                )
                if cancelled:
                    return

            if code == 0 and _provider_supports_resume(provider):
                bot.mark_provider_session_ready(provider, session.session_id)

        log.info(f"[discord] {provider} 종료 코드: {code}, stdout 길이: {len(stdout)}")
        if stderr:
            log.warning(f"[discord] {provider} stderr: {stderr[:300]}")
        if code == 0:
            reply = _filter_copilot_output(stdout) if normalize_cli_provider(provider) == "copilot" else (stdout or "").strip()
        else:
            reply = None
        if not reply:
            reply = "(응답 생성 실패)"
            log.error(f"[discord] {provider} 오류: {stderr[:200]}")
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
        _set_message_status_reaction(token, channel_id, message_id, "⚠️")
        return

    # 🕐 → ✅ 리액션 교체
    _set_message_status_reaction(token, channel_id, message_id, "✅")

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
        self._cli_cfg: dict = {}
        self._cli_provider: str = "copilot"
        self._channel_provider_overrides: dict[str, str] = {}
        self._guild_provider_overrides: dict[str, str] = {}
        self._allowed_guild_ids: set[str] = set()
        self._allowed_channel_ids: set[str] = set()
        self._allowed_user_ids: set[str] = set()
        self._deny_guild_ids: set[str] = set()
        self._deny_channel_ids: set[str] = set()
        self._deny_user_ids: set[str] = set()
        self._queue_max_per_channel: int = DEFAULT_QUEUE_MAX_PER_CHANNEL
        self._queue_ttl_seconds: int = DEFAULT_QUEUE_TTL_SECONDS
        self._queue_drop_policy: str = DEFAULT_QUEUE_DROP_POLICY
        self._max_parallel_channels: int = DEFAULT_MAX_PARALLEL_CHANNELS
        self._queue_notify_waiting: bool = DEFAULT_QUEUE_NOTIFY_WAITING
        self._queue_wait_notice_min_position: int = DEFAULT_QUEUE_WAIT_NOTICE_MIN_POSITION
        self._queue_wait_notice_cooldown_seconds: int = DEFAULT_QUEUE_WAIT_NOTICE_COOLDOWN_SECONDS
        self._queue_notify_ttl_expired: bool = DEFAULT_QUEUE_NOTIFY_TTL_EXPIRED
        self._queue_semaphore = threading.Semaphore(self._max_parallel_channels)
        self._token: str = ""
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._memory_sessions: dict[str, MemorySession] = {}
        self._channel_locks: dict[str, threading.Lock] = {}
        self._provider_ready_session_keys: set[str] = set()
        self._channel_queues: dict[str, deque[_PendingTask]] = {}
        self._channel_workers: dict[str, threading.Thread] = {}
        self._queue_dropped_total: int = 0
        self._queue_expired_total: int = 0
        self._queue_processed_total: int = 0
        self._queue_enqueued_total: int = 0
        self._queue_total_wait_seconds: float = 0.0
        self._queue_wait_samples: int = 0
        self._queue_total_run_seconds: float = 0.0
        self._queue_run_samples: int = 0
        self._queue_last_notice_at: dict[str, float] = {}
        self._new_session_triggers: list[str] = list(DEFAULT_NEW_SESSION_TRIGGERS)

    # ── 공개 인터페이스 ───────────────────────────────────────

    def start(self):
        """overlay 시작 시 호출. 별도 스레드에서 이벤트 루프 실행."""
        _setup_file_logging()
        _load_env_file()
        initialize_db()
        cfg = load_cfg()
        self._cfg = cfg.get("discord", {}) if isinstance(cfg.get("discord", {}), dict) else {}
        self._cli_cfg = cfg.get("cli", {}) if isinstance(cfg.get("cli", {}), dict) else {}
        self._cli_provider = normalize_cli_provider(self._cfg.get("provider") or self._cli_cfg.get("provider"))
        self._channel_provider_overrides = _to_provider_overrides(self._cfg.get("channel_cli_overrides"))
        self._guild_provider_overrides = _to_provider_overrides(self._cfg.get("guild_cli_overrides"))
        self._allowed_guild_ids = _to_id_set(self._cfg.get("guild_id"), self._cfg.get("guild_ids"))
        self._allowed_channel_ids = _to_id_set(self._cfg.get("channel_id"), self._cfg.get("channel_ids"))
        self._allowed_user_ids = _to_id_set(None, self._cfg.get("allowed_user_ids"))
        self._deny_guild_ids = _to_id_set(None, self._cfg.get("deny_guild_ids"))
        self._deny_channel_ids = _to_id_set(None, self._cfg.get("deny_channel_ids"))
        self._deny_user_ids = _to_id_set(None, self._cfg.get("deny_user_ids"))
        (
            self._queue_max_per_channel,
            self._queue_ttl_seconds,
            self._queue_drop_policy,
            self._max_parallel_channels,
        ) = _load_queue_settings(self._cfg)
        (
            self._queue_notify_waiting,
            self._queue_wait_notice_min_position,
            self._queue_wait_notice_cooldown_seconds,
            self._queue_notify_ttl_expired,
        ) = _load_queue_notice_settings(self._cfg)
        self._queue_semaphore = threading.Semaphore(self._max_parallel_channels)
        self._new_session_triggers = _load_new_session_triggers(self._cfg)
        log.info(
            "[discord] provider=%s route_override(ch=%s,g=%s) allow(g=%s,c=%s,u=%s) deny(g=%s,c=%s,u=%s) queue(max=%s, ttl=%ss, drop=%s, parallel=%s, notice=%s, ttl_notice=%s)",
            self._cli_provider,
            len(self._channel_provider_overrides),
            len(self._guild_provider_overrides),
            len(self._allowed_guild_ids),
            len(self._allowed_channel_ids),
            len(self._allowed_user_ids),
            len(self._deny_guild_ids),
            len(self._deny_channel_ids),
            len(self._deny_user_ids),
            self._queue_max_per_channel,
            self._queue_ttl_seconds,
            self._queue_drop_policy,
            self._max_parallel_channels,
            self._queue_notify_waiting,
            self._queue_notify_ttl_expired,
        )
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
        with self._state_lock:
            self._memory_sessions.clear()
            self._channel_locks.clear()
            self._provider_ready_session_keys.clear()
            self._channel_queues.clear()
            self._channel_workers.clear()
            self._queue_last_notice_at.clear()
        log.info("[discord] 봇 종료 신호 전송")

    def get_cli_provider(self) -> str:
        with self._state_lock:
            return self._cli_provider

    def get_cli_cfg(self) -> dict:
        with self._state_lock:
            return dict(self._cli_cfg)

    def _provider_session_key(self, provider: str, session_id: int) -> str:
        normalized = normalize_cli_provider(provider)
        return f"{normalized}:{int(session_id)}"

    def build_cli_session_name(self, provider: str, channel_id: str, session_id: int) -> str:
        normalized = normalize_cli_provider(provider)
        return f"{DISCORD_CLI_SESSION_PREFIX}:{normalized}:{channel_id}:s{int(session_id)}"

    def has_provider_session(self, provider: str, session_id: int) -> bool:
        key = self._provider_session_key(provider, session_id)
        with self._state_lock:
            return key in self._provider_ready_session_keys

    def mark_provider_session_ready(self, provider: str, session_id: int) -> None:
        key = self._provider_session_key(provider, session_id)
        with self._state_lock:
            self._provider_ready_session_keys.add(key)

    def _queue_stats_snapshot(self) -> dict[str, float | int]:
        with self._state_lock:
            pending = sum(len(q) for q in self._channel_queues.values())
            avg_wait = self._queue_total_wait_seconds / self._queue_wait_samples if self._queue_wait_samples > 0 else 0.0
            avg_run = self._queue_total_run_seconds / self._queue_run_samples if self._queue_run_samples > 0 else 0.0
            return {
                "pending": pending,
                "processed": self._queue_processed_total,
                "dropped": self._queue_dropped_total,
                "expired": self._queue_expired_total,
                "avg_wait_seconds": avg_wait,
                "avg_run_seconds": avg_run,
            }

    def _resolve_provider_for_target(self, channel_id: str, guild_id: str | None = None) -> tuple[str, str]:
        target_channel = str(channel_id or "").strip()
        target_guild = str(guild_id or "").strip()
        with self._state_lock:
            if target_channel and target_channel in self._channel_provider_overrides:
                return self._channel_provider_overrides[target_channel], "channel"
            if target_guild and target_guild in self._guild_provider_overrides:
                return self._guild_provider_overrides[target_guild], "guild"
            return self._cli_provider, "default"

    def _should_send_wait_notice(self, channel_id: str, queued: int) -> bool:
        if not self._queue_notify_waiting:
            return False
        if queued < self._queue_wait_notice_min_position:
            return False

        now = time.monotonic()
        with self._state_lock:
            last = self._queue_last_notice_at.get(channel_id, 0.0)
            if self._queue_wait_notice_cooldown_seconds > 0 and (now - last) < self._queue_wait_notice_cooldown_seconds:
                return False
            self._queue_last_notice_at[channel_id] = now
            return True

    def _enqueue_channel_task(self, task: _PendingTask) -> tuple[bool, int]:
        start_worker = False
        queue_length = 0
        worker: threading.Thread | None = None

        with self._state_lock:
            queue = self._channel_queues.get(task.channel_id)
            if queue is None:
                queue = deque()
                self._channel_queues[task.channel_id] = queue

            if len(queue) >= self._queue_max_per_channel:
                if self._queue_drop_policy == "drop_newest":
                    self._queue_dropped_total += 1
                    return False, len(queue)
                queue.popleft()
                self._queue_dropped_total += 1

            queue.append(task)
            self._queue_enqueued_total += 1
            queue_length = len(queue)

            existing = self._channel_workers.get(task.channel_id)
            if existing is None or not existing.is_alive():
                worker = threading.Thread(
                    target=self._run_channel_worker,
                    args=(task.channel_id,),
                    daemon=True,
                    name=f"discord-q-{task.channel_id}",
                )
                self._channel_workers[task.channel_id] = worker
                start_worker = True

        if start_worker and worker is not None:
            worker.start()

        return True, queue_length

    def _run_channel_worker(self, channel_id: str) -> None:
        while not self._stop_event.is_set():
            with self._state_lock:
                queue = self._channel_queues.get(channel_id)
                if not queue:
                    self._channel_workers.pop(channel_id, None)
                    return
                task = queue.popleft()
                if not queue:
                    self._channel_queues.pop(channel_id, None)

            while not self._stop_event.is_set():
                if self._queue_semaphore.acquire(timeout=0.5):
                    break
            if self._stop_event.is_set():
                return

            task_age = time.monotonic() - task.enqueued_at
            if task_age > self._queue_ttl_seconds:
                with self._state_lock:
                    self._queue_expired_total += 1
                _set_message_status_reaction(task.token, task.channel_id, task.message_id, "⚠️")
                if self._queue_notify_ttl_expired:
                    _reply_to_message(
                        task.token,
                        task.channel_id,
                        task.message_id,
                        f"대기열 지연으로 요청이 만료됐어 ({_format_seconds(task_age)} 경과). 다시 보내줘.",
                    )
                log.info(
                    "[discord] 큐 만료로 드롭: channel=%s age=%.1fs ttl=%ss",
                    channel_id,
                    task_age,
                    self._queue_ttl_seconds,
                )
                self._queue_semaphore.release()
                continue

            with self._state_lock:
                self._queue_total_wait_seconds += task_age
                self._queue_wait_samples += 1

            run_started = time.monotonic()
            try:
                _generate_and_send(
                    self,
                    task.token,
                    task.channel_id,
                    task.message_id,
                    task.content,
                    task.provider,
                    self._stop_event,
                )
                run_elapsed = time.monotonic() - run_started
                with self._state_lock:
                    self._queue_processed_total += 1
                    self._queue_total_run_seconds += run_elapsed
                    self._queue_run_samples += 1
            finally:
                self._queue_semaphore.release()

    def _get_or_create_channel_state(self, channel_id: str) -> tuple[MemorySession, threading.Lock]:
        scope_key = f"{DISCORD_SCOPE_PREFIX}{channel_id}"
        with self._state_lock:
            session = self._memory_sessions.get(channel_id)
            if session is None:
                session = memory_bus.start_session(scope_key=scope_key)
                self._memory_sessions[channel_id] = session

            channel_lock = self._channel_locks.get(channel_id)
            if channel_lock is None:
                channel_lock = threading.Lock()
                self._channel_locks[channel_id] = channel_lock

        return session, channel_lock

    def _reset_channel_session(self, channel_id: str) -> None:
        """명시적 요청 시 채널 세션을 신규 세션으로 전환한다."""
        scope_key = f"{DISCORD_SCOPE_PREFIX}{channel_id}"
        with self._state_lock:
            old_session = self._memory_sessions.get(channel_id)
            old_session_id = old_session.session_id if old_session else None
            self._memory_sessions[channel_id] = memory_bus.start_session(scope_key=scope_key)
            if channel_id not in self._channel_locks:
                self._channel_locks[channel_id] = threading.Lock()
            new_session_id = self._memory_sessions[channel_id].session_id
            self._channel_queues.pop(channel_id, None)
            if old_session_id is not None:
                for provider in ("copilot", "claude-code", "claude-code-ollama", "gemini", "ollama"):
                    self._provider_ready_session_keys.discard(self._provider_session_key(provider, old_session_id))
        log.info(f"[discord] 새 세션 전환: channel={channel_id} session_id={new_session_id}")

    def _list_channel_sessions(self, channel_id: str, limit: int = DEFAULT_SESSION_LIST_LIMIT) -> list[dict]:
        scope_key = f"{DISCORD_SCOPE_PREFIX}{channel_id}"
        safe_limit = max(1, min(limit, MAX_SESSION_LIST_LIMIT))
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT s.id,
                       s.started_at,
                       s.ended_at,
                       s.summary,
                       COUNT(m.id) AS message_count,
                       (
                           SELECT mu.content
                           FROM messages mu
                           WHERE mu.session_id = s.id AND mu.role = 'user'
                           ORDER BY datetime(mu.timestamp) DESC, mu.id DESC
                           LIMIT 1
                       ) AS last_user_message,
                       (
                           SELECT ma.content
                           FROM messages ma
                           WHERE ma.session_id = s.id
                           ORDER BY datetime(ma.timestamp) DESC, ma.id DESC
                           LIMIT 1
                       ) AS last_message
                FROM sessions s
                LEFT JOIN messages m ON m.session_id = s.id
                WHERE s.scope_key = ?
                GROUP BY s.id, s.started_at, s.ended_at, s.summary
                ORDER BY datetime(s.started_at) DESC, s.id DESC
                LIMIT ?
                """,
                (scope_key, safe_limit),
            ).fetchall()

        items = [dict(r) for r in rows]
        for item in items:
            preview_source = item.get("summary") or item.get("last_user_message") or item.get("last_message") or ""
            item["preview"] = _to_session_preview(str(preview_source))
        return items

    def _switch_channel_session(self, channel_id: str, session_id: int, provider: str | None = None) -> tuple[bool, str]:
        scope_key = f"{DISCORD_SCOPE_PREFIX}{channel_id}"
        selected_provider = normalize_cli_provider(provider or self.get_cli_provider())
        with get_connection() as conn:
            row = conn.execute(
                "SELECT id, started_at, ended_at FROM sessions WHERE id = ? AND scope_key = ?",
                (int(session_id), scope_key),
            ).fetchone()

        if not row:
            return False, f"해당 채널에서 찾을 수 없는 세션 ID야: {session_id}"

        switched = MemorySession(session_id=int(row["id"]), scope_key=scope_key)
        with self._state_lock:
            self._memory_sessions[channel_id] = switched
            if channel_id not in self._channel_locks:
                self._channel_locks[channel_id] = threading.Lock()
            if _provider_supports_resume(selected_provider):
                self._provider_ready_session_keys.add(self._provider_session_key(selected_provider, switched.session_id))

        status = "active" if not row["ended_at"] else "archived"
        provider_note = "resume-ready" if _provider_supports_resume(selected_provider) else "fresh-run"
        return True, f"세션 전환 완료: {switched.session_id} ({status}, {provider_note}, started={row['started_at']})"

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

        @client.event
        async def on_ready():
            log.info(f"[discord] 로그인: {client.user} (id={client.user.id})")

        @client.event
        async def on_message(message: discord.Message):
            is_dm = message.guild is None
            channel_id = str(message.channel.id)
            guild_id = str(message.guild.id) if message.guild else ""
            log.debug(
                f"[discord] on_message: author={message.author} bot={message.author.bot} channel={message.channel.id} dm={is_dm} mentions={[u.id for u in message.mentions]}"
            )
            if message.author.bot:
                return
            author_id = str(message.author.id)
            if self._deny_user_ids and author_id in self._deny_user_ids:
                log.warning(f"[discord] denylist 사용자 무시: {message.author} ({author_id})")
                return
            # DM은 채널/멘션 필터 스킵 — 서버 채널은 지정 채널 + @멘션 필요
            if not is_dm:
                if self._deny_guild_ids and guild_id in self._deny_guild_ids:
                    log.debug(f"[discord] denylist 길드 필터: {guild_id}")
                    return
                if self._deny_channel_ids and channel_id in self._deny_channel_ids:
                    log.debug(f"[discord] denylist 채널 필터: {channel_id}")
                    return
                if self._allowed_guild_ids and guild_id not in self._allowed_guild_ids:
                    log.debug(f"[discord] 길드 필터: {guild_id} not in configured guild_ids")
                    return
                if self._allowed_channel_ids and channel_id not in self._allowed_channel_ids:
                    log.debug(f"[discord] 채널 필터: {channel_id} not in configured channel_ids")
                    return
                if client.user not in message.mentions:
                    log.debug(f"[discord] 멘션 없음: {message.content[:50]}")
                    return
            if self._allowed_user_ids and author_id not in self._allowed_user_ids:
                log.warning(f"[discord] 미허가 사용자 무시: {message.author} ({message.author.id})")
                return

            # 멘션 제거 (서버 채널에서 @멘션 포함된 경우)
            content = message.content
            for mention in message.mentions:
                content = content.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
            content = content.strip()
            if not content:
                return

            route_provider, provider_source = self._resolve_provider_for_target(channel_id, guild_id)
            if provider_source != "default":
                log.debug(
                    "[discord] provider override 적용: source=%s provider=%s channel=%s guild=%s",
                    provider_source,
                    route_provider,
                    channel_id,
                    guild_id or "(dm)",
                )

            session_cmd = _parse_session_command(content)
            if session_cmd is not None:
                action = str(session_cmd.get("action", ""))
                if action == "list":
                    limit = int(session_cmd.get("limit", DEFAULT_SESSION_LIST_LIMIT))
                    items = self._list_channel_sessions(channel_id, limit=limit)
                    with self._state_lock:
                        current = self._memory_sessions.get(channel_id)
                    current_id = current.session_id if current else None

                    if not items:
                        await message.add_reaction("✅")
                        await message.reply("이 채널의 세션이 아직 없어. 첫 대화를 시작해줘.")
                        return

                    lines = ["세션 목록 (최근순):", "- 사용: /session use <id>"]
                    for item in items:
                        sid = int(item.get("id", 0))
                        mark = "*" if current_id == sid else " "
                        status = "active" if not item.get("ended_at") else "archived"
                        msg_count = int(item.get("message_count", 0) or 0)
                        started = str(item.get("started_at", ""))
                        preview = str(item.get("preview", "")).strip()
                        if preview:
                            lines.append(f"{mark} {sid} | {status} | msgs={msg_count} | {started} | {preview}")
                        else:
                            lines.append(f"{mark} {sid} | {status} | msgs={msg_count} | {started}")

                    await message.add_reaction("✅")
                    await message.reply("\n".join(lines))
                    return

                if action == "use":
                    ok, msg = self._switch_channel_session(
                        channel_id,
                        int(session_cmd.get("session_id", 0)),
                        provider=route_provider,
                    )
                    await message.add_reaction("✅" if ok else "⚠️")
                    await message.reply(msg)
                    return

                if action == "new":
                    self._reset_channel_session(channel_id)
                    parsed = str(session_cmd.get("content", "")).strip()
                    if not parsed:
                        await message.add_reaction("✅")
                        await message.reply("새 세션으로 전환했어. 이어서 질문해줘.")
                        return
                    content = parsed

                if action == "help":
                    error = str(session_cmd.get("error", "")).strip()
                    help_text = "사용법: /session | /session list [N] | /session use <id> | /session new [질문]"
                    await message.add_reaction("⚠️")
                    if error:
                        await message.reply(f"{error}\n{help_text}")
                    else:
                        await message.reply(help_text)
                    return

            # 명시적 새 세션 요청: 즉시 채널 세션 롤오버
            reset_requested, parsed_content = _parse_session_reset_command(content, self._new_session_triggers)
            if reset_requested:
                self._reset_channel_session(channel_id)
                if not parsed_content:
                    await message.add_reaction("✅")
                    await message.reply("새 세션으로 전환했어요. 이어서 질문해줘.")
                    return
                content = parsed_content

            safe_content = f"[Discord/@{message.author.name}]: {content}"
            log.info(f"[discord] 수신: {safe_content[:60]}...")

            # 미처리 큐가 5개 초과면 가장 오래된 것 삭제
            _trim_queue(max_unprocessed=5)

            # 🕐 즉시 반응
            await message.add_reaction("🕐")

            enqueued, queued = self._enqueue_channel_task(
                _PendingTask(
                    token=token,
                    channel_id=channel_id,
                    message_id=str(message.id),
                    content=safe_content,
                    provider=route_provider,
                    enqueued_at=time.monotonic(),
                )
            )
            if not enqueued:
                await message.add_reaction("⚠️")
                await message.reply("요청이 많아서 잠시 후 다시 시도해줘.")
                log.warning(
                    "[discord] 큐 포화로 드롭: channel=%s queued=%s max=%s policy=%s",
                    message.channel.id,
                    queued,
                    self._queue_max_per_channel,
                    self._queue_drop_policy,
                )
                return

            if self._should_send_wait_notice(channel_id, queued):
                stats = self._queue_stats_snapshot()
                pending = int(stats.get("pending", 0))
                avg_run = float(stats.get("avg_run_seconds", 0.0))
                eta_seconds = int(round(avg_run * max(0, queued - 1))) if avg_run > 0 else 0
                notice = f"대기열 접수: 현재 이 채널에서 {queued}번째야. 순서대로 처리할게."
                if eta_seconds > 0:
                    notice += f" 예상 대기 약 {_format_seconds(eta_seconds)}."
                notice += f" (전체 대기 {pending}건)"
                await message.reply(notice)

            log.debug(
                "[discord] 큐 적재: channel=%s queued=%s processed=%s dropped=%s expired=%s",
                message.channel.id,
                queued,
                self._queue_processed_total,
                self._queue_dropped_total,
                self._queue_expired_total,
            )


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
