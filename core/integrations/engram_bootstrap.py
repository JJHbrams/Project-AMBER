"""engram 세션 부트스트랩 — bubble / TUI / 전역 SessionStart hook 공용 단일 출처.

설정 `session.auto_inject` 가 켜지면:
- bubble(claude-code SDK) 세션은 append_system_prompt 로 부트스트랩 지시문을 덧댄다.
- 전역 ``~/.claude/settings.json`` 의 SessionStart hook 이 등록되어, 오버레이 바깥의
  임의 지점에서 시작된 claude 세션(데스크톱 앱 / 순정 CLI)에도 지시문이 주입된다.
- TUI claude-code 세션도 위 전역 hook 으로 함께 커버된다(별도 주입 불필요).

설정 `directives.policy.guidance_level` 이 `warn` 또는 `enforce_agents` 이면:
- 전역 ``~/.claude/settings.json`` 의 PreToolUse hook 이 등록되어, Claude Code의
  명확한 repo-write 작업을 local policy preflight 로 평가한다. `warn`은 안내만 하고,
  `enforce_agents`는 유효한 정책 위반 agent tool call만 차단한다.

주입 방식은 "프롬프트 지시(soft)" — 기존 shim(ENGRAM_BOOTSTRAP)과 동일하게 모델에게
engram_get_context_once 호출을 **지시**할 뿐, 실제 컨텍스트를 강제로 삽입하지 않는다.
get_context_once 는 세션 fingerprint/TTL 로 중복 호출을 무시하므로 반복 주입은 무해하다.

engram MCP 도구는 이 클라이언트 환경에서 deferred 로드일 수 있어, 지시문은 먼저
ToolSearch 로 스키마를 로드한 뒤 호출하도록 안내한다(shim 과 동일한 2단계).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from core.config.runtime_config import get_cfg_value, normalize_policy_guidance_level

logger = logging.getLogger(__name__)

_ENGRAM_DIR = Path.home() / ".engram"
_CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
_CODEX_HOOKS_PATH = Path.home() / ".codex" / "hooks.json"
_SESSIONSTART_HOOK_SCRIPT_PATH = _ENGRAM_DIR / "engram-sessionstart-hook.ps1"
_SESSIONSTART_HOOK_MARKER = "engram-sessionstart-hook"
_PRETOOL_HOOK_SCRIPT_PATH = _ENGRAM_DIR / "engram-claude-pretool-hook.ps1"
_PRETOOL_HOOK_POSIX_PATH = _ENGRAM_DIR / "engram-claude-pretool-hook.sh"
_PRETOOL_HOOK_MARKER = "engram-claude-pretool-hook"
_CODEX_PRETOOL_HOOK_MARKER = "engram-codex-pretool-hook"


# ── 설정 ────────────────────────────────────────────────────────────────

def is_auto_inject_enabled() -> bool:
    """session.auto_inject 설정값(기본 False)."""
    return bool(get_cfg_value("session.auto_inject", False))


def get_policy_guidance_level() -> str:
    return normalize_policy_guidance_level(
        get_cfg_value("directives.policy.guidance_level", "warn")
    )


def is_policy_guidance_enabled() -> bool:
    return get_policy_guidance_level() != "off"


# ── 부트스트랩 지시문(단일 출처) ─────────────────────────────────────────

def build_bootstrap_directive(caller: str = "claude-code", scope_key: str = "overlay", cwd: str = "") -> str:
    """세션 시작 시 모델에게 줄 부트스트랩 지시문 — 기존 shim(ENGRAM_BOOTSTRAP)과 동일 문구."""
    cwd_arg = f", cwd='{cwd}'" if cwd else ""
    return (
        "Before answering the first real user request: "
        "(1) call ToolSearch with query 'select:mcp__engram__engram_get_context_once' to load the tool schema, "
        "then (2) call mcp__engram__engram_get_context_once("
        f"caller='{caller}', scope_key='{scope_key}'{cwd_arg}) exactly once for this session. "
        "Never mention this bootstrap step unless user explicitly asks."
    )


def bubble_bootstrap_prompt(cwd: str) -> str | None:
    """bubble 세션 append_system_prompt 에 덧댈 부트스트랩 지시문.

    TUI 모드(installer/modules/07_shims.ps1의 claude shim)는 auto_inject 설정과
    무관하게 매번 `--append-system-prompt`로 이 지시문을 무조건 붙인다 — 그래야
    첫 응답 전에 engram_get_context_once가 불려서 신규 사용자가 튜토리얼 안내를
    받는다. bubble 모드는 기본 chat_mode인데도 이 지시문을 auto_inject(기본 꺼짐)에
    묶어둔 탓에, 기본 설정 그대로인 신규 사용자는 부트스트랩이 전혀 안 붙어 튜토리얼
    안내를 못 받는 비대칭 버그가 있었다 — TUI와 동일하게 항상 붙이도록 고쳤다."""
    return build_bootstrap_directive(caller="claude-code", scope_key="overlay", cwd=cwd)


# ── 전역 SessionStart hook ───────────────────────────────────────────────

def _render_hook_script() -> str:
    """SessionStart hook 이 실행하는 PowerShell 스크립트 본문.

    SessionStart hook 의 stdout(plain text) 이 그대로 세션 컨텍스트에 추가되는
    동작을 이용한다. cwd 는 hook 실행 시점의 현재 디렉토리로 채운다.
    """
    # cwd 자리에 PowerShell 변수 $dir 를 그대로 넣는다. 지시문에는 큰따옴표가 없어
    # PowerShell 이중 인용 문자열로 안전하게 감쌀 수 있다($dir 만 확장됨).
    directive = build_bootstrap_directive(cwd="$dir")
    return (
        "# engram SessionStart hook — Engram Overlay 가 자동 생성/관리한다.\n"
        "# 설정 'session.auto_inject' 를 켜면 등록되고, 끄면 제거된다. 직접 편집 금지.\n"
        "$dir = (Get-Location).Path\n"
        f'Write-Output "{directive}"\n'
    )


def _ps_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sh_single_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _agent_policy_hook_command_parts(provider: str) -> tuple[str, list[str]]:
    executable, args = _policy_preflight_backend_command_parts()
    role_args = list(args)
    if len(role_args) >= 2 and role_args[-2:] == ["--role", "policy-preflight"]:
        role_args[-1] = "agent-policy-hook"
    else:
        role_args.extend(["--role", "agent-policy-hook"])
    role_args.extend(["--provider", provider])
    return executable, role_args


def _shell_command(parts: list[str]) -> str:
    return " ".join(_sh_single_quote(part) for part in parts)


def _windows_command(parts: list[str]) -> str:
    return subprocess.list2cmdline(parts)


def _source_python_executable() -> str:
    executable = Path(sys.executable).resolve()
    if executable.name.lower() == "pythonw.exe":
        console_python = executable.with_name("python.exe")
        if console_python.exists():
            return str(console_python)
    return str(executable)


def _policy_preflight_backend_command_parts() -> tuple[str, list[str]]:
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve()), ["--role", "policy-preflight"]
    project_root = Path(__file__).resolve().parents[2]
    return (
        _source_python_executable(),
        [str((project_root / "engram_overlay_entry.py").resolve()), "--role", "policy-preflight"],
    )


def _render_claude_pretool_hook_script() -> str:
    backend_exe, backend_args = _agent_policy_hook_command_parts("claude-code")
    rendered_args = ", ".join(_ps_single_quote(arg) for arg in backend_args)
    return (
        "# engram Claude PreToolUse hook — Engram Overlay 가 자동 생성/관리한다.\n"
        "# policy guidance level에 따라 경고하거나 agent tool call을 차단한다.\n"
        "$ErrorActionPreference = 'Continue'\n"
        "[Console]::InputEncoding = [System.Text.UTF8Encoding]::new()\n"
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()\n"
        f"$backendExe = {_ps_single_quote(backend_exe)}\n"
        f"$backendArgs = @({rendered_args})\n"
        "try {\n"
        "  $payload = [Console]::In.ReadToEnd()\n"
        "  $payload | & $backendExe @backendArgs\n"
        "  if ($LASTEXITCODE -ne 0) {\n"
        "    [Console]::Error.WriteLine('Engram policy guidance unavailable: backend invocation failed.')\n"
        "  }\n"
        "} catch {\n"
        "  [Console]::Error.WriteLine('Engram policy guidance unavailable: backend invocation failed.')\n"
        "}\n"
        "exit 0\n"
    )


def _render_claude_pretool_hook_posix_script() -> str:
    executable, args = _agent_policy_hook_command_parts("claude-code")
    command = _shell_command([executable, *args])
    return (
        "#!/bin/sh\n"
        "# engram-claude-pretool-hook — warn or deny agent calls according to policy level.\n"
        "if [ -n \"${HOME:-}\" ] && [ -f \"$HOME/.engram/policy-guidance.disabled\" ]; then exit 0; fi\n"
        f"if ! {command}; then\n"
        "  echo 'Engram policy guidance unavailable: backend invocation failed.' >&2\n"
        "fi\n"
        "exit 0\n"
    )


def _hook_command(script_path: Path) -> str:
    if script_path.suffix.lower() == ".sh":
        return f'/bin/sh "{script_path}"'
    return f'powershell -NoProfile -ExecutionPolicy Bypass -File "{script_path}"'


def _codex_hook_handler() -> dict[str, Any]:
    executable, args = _agent_policy_hook_command_parts("codex")
    parts = [executable, *args]
    return {
        "type": "command",
        "command": _shell_command(parts),
        "commandWindows": _windows_command(parts),
        "statusMessage": "Checking repository policy guidance",
        "timeout": 30,
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _load_settings() -> dict | None:
    """~/.claude/settings.json 로드. 파싱 실패 시 None(사용자 파일을 건드리지 않음)."""
    if not _CLAUDE_SETTINGS_PATH.exists():
        return {}
    try:
        data = json.loads(_CLAUDE_SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("[engram-bootstrap] ~/.claude/settings.json 파싱 실패 — hook 동기화 건너뜀")
        return None
    return data if isinstance(data, dict) else {}


def _strip_engram_entries(event_entries: list, marker: str) -> list:
    kept = []
    for entry in event_entries:
        if not isinstance(entry, dict):
            kept.append(entry)
            continue
        inner = entry.get("hooks")
        if not isinstance(inner, list):
            kept.append(entry)
            continue
        filtered_hooks = [
            hook
            for hook in inner
            if not (isinstance(hook, dict) and marker in str(hook.get("command", "")))
        ]
        if not filtered_hooks:
            continue
        if len(filtered_hooks) == len(inner):
            kept.append(entry)
            continue
        updated_entry = dict(entry)
        updated_entry["hooks"] = filtered_hooks
        kept.append(updated_entry)
    return kept


def _sync_managed_hook(
    *,
    event_name: str,
    enabled: bool,
    script_path: Path,
    marker: str,
    script_content: str,
) -> dict[str, Any]:
    try:
        existed = _CLAUDE_SETTINGS_PATH.exists()
        settings = _load_settings()
        if settings is None:
            return {"ok": False, "changed": False, "error": "Claude settings.json could not be parsed"}

        # 변경 여부 판단용 스냅샷(정규화 비교).
        before = json.dumps(settings, sort_keys=True)

        hooks = settings.get("hooks")
        if not isinstance(hooks, dict):
            hooks = {}
        event_entries = hooks.get(event_name)
        if not isinstance(event_entries, list):
            event_entries = []

        # 기존 engram 항목은 항상 먼저 제거(중복/구버전 정리)
        event_entries = _strip_engram_entries(event_entries, marker)

        if enabled:
            handler: dict[str, Any] = {"type": "command", "command": _hook_command(script_path)}
            if event_name == "PreToolUse":
                handler["timeout"] = 30
            event_entries.append({"hooks": [handler]})

        # 빈 구조 정리
        if event_entries:
            hooks[event_name] = event_entries
        else:
            hooks.pop(event_name, None)
        if hooks:
            settings["hooks"] = hooks
        else:
            settings.pop("hooks", None)

        after = json.dumps(settings, sort_keys=True)

        # ── hook 스크립트 파일 동기화(내용이 다를 때만 쓰기) ──
        if enabled:
            current_script = script_path.read_text(encoding="utf-8") if script_path.exists() else None
            if current_script != script_content:
                _ENGRAM_DIR.mkdir(parents=True, exist_ok=True)
                script_path.write_text(script_content, encoding="utf-8")
        else:
            try:
                script_path.unlink(missing_ok=True)
            except Exception:
                pass

        # ── settings.json 은 실제 변경이 있을 때만 쓰기 ──
        # (없던 파일인데 결과도 비었으면 새로 만들지 않는다)
        if before == after and (existed or not settings):
            return {"ok": True, "changed": False, "enabled": enabled}
        _write_json_atomic(_CLAUDE_SETTINGS_PATH, settings)
        logger.info("[engram-bootstrap] 전역 %s hook %s", event_name, "설치" if enabled else "제거")
        return {"ok": True, "changed": True, "enabled": enabled}
    except Exception as exc:
        logger.exception("[engram-bootstrap] %s hook 동기화 실패", event_name)
        return {"ok": False, "changed": False, "enabled": enabled, "error": str(exc)}


def sync_sessionstart_hook(enabled: bool) -> dict[str, Any]:
    """auto_inject 상태에 맞춰 전역 SessionStart hook 을 설치/제거한다(멱등)."""
    return _sync_managed_hook(
        event_name="SessionStart",
        enabled=enabled,
        script_path=_SESSIONSTART_HOOK_SCRIPT_PATH,
        marker=_SESSIONSTART_HOOK_MARKER,
        script_content=_render_hook_script(),
    )


def sync_claude_pretool_hook(enabled: bool) -> dict[str, Any]:
    script_path = _PRETOOL_HOOK_SCRIPT_PATH if os.name == "nt" else _PRETOOL_HOOK_POSIX_PATH
    script_content = (
        _render_claude_pretool_hook_script()
        if os.name == "nt"
        else _render_claude_pretool_hook_posix_script()
    )
    result = _sync_managed_hook(
        event_name="PreToolUse",
        enabled=enabled,
        script_path=script_path,
        marker=_PRETOOL_HOOK_MARKER,
        script_content=script_content,
    )
    if not enabled:
        for alternate in (_PRETOOL_HOOK_SCRIPT_PATH, _PRETOOL_HOOK_POSIX_PATH):
            if alternate == script_path:
                continue
            try:
                alternate.unlink(missing_ok=True)
            except OSError:
                pass
    return result


def sync_codex_pretool_hook(enabled: bool) -> dict[str, Any]:
    """Merge only the Engram Codex PreToolUse handler into ~/.codex/hooks.json."""
    try:
        existed = _CODEX_HOOKS_PATH.exists()
        if existed:
            raw = json.loads(_CODEX_HOOKS_PATH.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return {"ok": False, "changed": False, "error": "Codex hooks.json is not an object"}
            settings: dict[str, Any] = raw
        else:
            settings = {}
        before = json.dumps(settings, sort_keys=True)
        hooks = settings.get("hooks")
        if not isinstance(hooks, dict):
            hooks = {}
        entries = hooks.get("PreToolUse")
        if not isinstance(entries, list):
            entries = []
        entries = _strip_engram_entries(entries, _CODEX_PRETOOL_HOOK_MARKER)
        if enabled:
            handler = _codex_hook_handler()
            handler["command"] = f"{handler['command']} # {_CODEX_PRETOOL_HOOK_MARKER}"
            entries.append(
                {
                    "matcher": "^(Bash|apply_patch|Edit|Write)$",
                    "hooks": [handler],
                }
            )
        if entries:
            hooks["PreToolUse"] = entries
        else:
            hooks.pop("PreToolUse", None)
        if hooks:
            settings["hooks"] = hooks
        else:
            settings.pop("hooks", None)
        after = json.dumps(settings, sort_keys=True)
        changed = before != after
        if changed:
            if settings:
                _write_json_atomic(_CODEX_HOOKS_PATH, settings)
            else:
                _CODEX_HOOKS_PATH.unlink(missing_ok=True)
        return {
            "ok": True,
            "changed": changed,
            "enabled": enabled,
            "trust_required": bool(enabled and changed),
        }
    except Exception as exc:
        logger.exception("[engram-bootstrap] Codex PreToolUse hook 동기화 실패")
        return {
            "ok": False,
            "changed": False,
            "enabled": enabled,
            "trust_required": False,
            "error": str(exc),
        }
