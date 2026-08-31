from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any, TextIO

from core.integrations.policy_preflight import process_policy_request

logger = logging.getLogger(__name__)


# Every CLI provider Engram ships a shim for and whose runtime exposes a pre-tool hook.
# ``event`` is the provider's hook event name; ``dialect`` selects the denial payload shape.
# Antigravity's current contract requires a top-level decision for every response.
_PROVIDERS: dict[str, dict[str, str]] = {
    "claude-code": {
        "request_type": "claude-pretool-hook",
        "event": "PreToolUse",
        "dialect": "claude",
    },
    "codex": {
        "request_type": "codex-pretool-hook",
        "event": "PreToolUse",
        "dialect": "claude",
    },
    "copilot": {
        "request_type": "copilot-pretool-hook",
        "event": "PreToolUse",
        "dialect": "claude",
    },
    "antigravity": {
        "request_type": "antigravity-pretool-hook",
        "event": "PreToolUse",
        "dialect": "antigravity",
    },
}

_REQUEST_TYPES = {name: spec["request_type"] for name, spec in _PROVIDERS.items()}


def _compact_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _payload_cwd(raw_text: str, fallback: str) -> str:
    try:
        payload = json.loads(raw_text)
    except Exception:
        return fallback
    if not isinstance(payload, dict):
        return fallback
    for key in ("cwd", "working_directory", "workspace_root"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    tool_call = payload.get("toolCall")
    if isinstance(tool_call, dict):
        args = tool_call.get("args")
        if isinstance(args, dict):
            for key in ("Cwd", "cwd", "workingDirectory", "WorkspaceRoot"):
                value = str(args.get(key) or "").strip()
                if value:
                    return value
    return fallback


def _guidance_reason(result: dict[str, Any], backend_exit_code: int) -> str:
    if result.get("guidance_enabled") is False or not result.get("classified", True):
        return ""
    policy_decision = str(result.get("policy_decision") or "").strip().lower()
    decision = str(result.get("decision") or "").strip().lower()
    should_warn = (
        backend_exit_code != 0
        or bool(result.get("would_block"))
        or policy_decision in {"blocked", "error", "workflow_required"}
        or decision in {"blocked", "error", "workflow_required"}
    )
    if not should_warn:
        return ""
    return str(result.get("reason") or "policy guidance detected a repository risk").strip()


def _should_enforce(result: dict[str, Any], backend_exit_code: int) -> bool:
    if backend_exit_code != 0 or str(result.get("guidance_level") or "warn") != "enforce_agents":
        return False
    policy_decision = str(result.get("policy_decision") or "").strip().lower()
    decision = str(result.get("decision") or "").strip().lower()
    if "error" in {policy_decision, decision}:
        return False
    return bool(result.get("would_block")) or policy_decision in {
        "blocked",
        "workflow_required",
    } or decision in {"blocked", "workflow_required"}


def process_provider_hook_input(
    raw_text: str,
    provider: str,
    *,
    cwd: str = "",
) -> tuple[str, str, int]:
    """Convert a provider PreToolUse payload into warning or agent-only denial output."""
    normalized_provider = str(provider or "").strip().lower()
    # Hidden compatibility for an already-installed legacy wrapper only.
    if normalized_provider == "gemini":
        logger.warning("Deprecated provider alias 'gemini' mapped to 'antigravity'; reinstall the managed hook.")
        normalized_provider = "antigravity"
    spec = _PROVIDERS.get(normalized_provider)
    if spec is None:
        reason = f"unsupported agent policy provider '{normalized_provider or provider}'"
        return "", f"Engram policy guidance: {reason}\n", 0

    try:
        result, backend_exit_code, _ = process_policy_request(
            {
                "request_type": spec["request_type"],
                "cwd": _payload_cwd(raw_text, cwd or os.getcwd()),
                "hook_payload": raw_text,
            }
        )
        reason = _guidance_reason(result, backend_exit_code)
        enforce = _should_enforce(result, backend_exit_code)
    except Exception as exc:
        reason = str(exc).strip() or "policy guidance adapter error"
        enforce = False

    # Antigravity only accepts top-level decision/reason.  It cannot use the
    # Claude-shaped additionalContext object, so emit its required neutral allow
    # decision when there is no policy message.
    if spec["dialect"] == "antigravity":
        message = f"Engram policy {'enforcement' if enforce else 'guidance'}: {reason}" if reason else ""
        response: dict[str, Any] = {"decision": "deny" if enforce else "allow"}
        if message:
            response["reason"] = message
        return _compact_json(response), "", 0

    if not reason:
        return "", "", 0

    message = f"Engram policy {'enforcement' if enforce else 'guidance'}: {reason}"
    event = spec["event"]
    if enforce:
        response = {
            "hookSpecificOutput": {
                "hookEventName": event,
                "permissionDecision": "deny",
                "permissionDecisionReason": message,
            }
        }
        return _compact_json(response), "", 0
    # warn: every supported runtime injects additionalContext back into the model context.
    response = {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": message,
        }
    }
    return _compact_json(response), "", 0


def provider_hook_main(
    provider: str,
    *,
    cwd: str = "",
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    input_stream = stdin or sys.stdin
    output_stream = stdout or sys.stdout
    error_stream = stderr or sys.stderr
    raw_text = input_stream.read()
    output_text, error_text, exit_code = process_provider_hook_input(raw_text, provider, cwd=cwd)
    if output_text:
        output_stream.write(output_text)
    if error_text:
        error_stream.write(error_text)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run advisory-only Engram agent policy guidance")
    parser.add_argument("--provider", required=True, choices=sorted(_PROVIDERS) + ["gemini"])
    parser.add_argument("--cwd", default="")
    args = parser.parse_args(argv)
    return provider_hook_main("antigravity" if args.provider == "gemini" else args.provider, cwd=args.cwd)


if __name__ == "__main__":
    raise SystemExit(main())
