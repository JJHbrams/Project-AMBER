from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, TextIO

from core.integrations.policy_preflight import process_policy_request


_REQUEST_TYPES = {
    "claude-code": "claude-pretool-hook",
    "codex": "codex-pretool-hook",
}


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
    if normalized_provider not in _REQUEST_TYPES:
        reason = f"unsupported agent policy provider '{normalized_provider or provider}'"
        return "", f"Engram policy guidance: {reason}\n", 0

    try:
        result, backend_exit_code, _ = process_policy_request(
            {
                "request_type": _REQUEST_TYPES[normalized_provider],
                "cwd": _payload_cwd(raw_text, cwd or os.getcwd()),
                "hook_payload": raw_text,
            }
        )
        reason = _guidance_reason(result, backend_exit_code)
        enforce = _should_enforce(result, backend_exit_code)
    except Exception as exc:
        reason = str(exc).strip() or "policy guidance adapter error"
        enforce = False

    if not reason:
        return "", "", 0

    message = f"Engram policy {'enforcement' if enforce else 'guidance'}: {reason}"
    if enforce:
        response = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": message,
            }
        }
        return _compact_json(response), "", 0
    if normalized_provider == "codex":
        response = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": message,
            }
        }
        return _compact_json(response), "", 0
    return "", f"{message}\n", 0


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
    parser.add_argument("--provider", required=True, choices=sorted(_REQUEST_TYPES))
    parser.add_argument("--cwd", default="")
    args = parser.parse_args(argv)
    return provider_hook_main(args.provider, cwd=args.cwd)


if __name__ == "__main__":
    raise SystemExit(main())
