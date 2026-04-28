"""
Copilot Chat API 래퍼.
engram(Copilot CLI 기반 연속체)에게 질문을 전달하고 응답을 받는다.
Claude Code에서 서브프로세스로 engram를 호출할 때 사용.
"""

import os
import shutil
import subprocess
import json
from pathlib import Path
from typing import List, Dict


def _get_gh_token() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    result = subprocess.run(
        ["gh", "auth", "token"], capture_output=True, text=True
    )
    if result.returncode == 0:
        return result.stdout.strip()
    raise RuntimeError(
        "GitHub 토큰을 찾을 수 없습니다. `gh auth login`을 먼저 실행하세요."
    )


def _get_copilot_token(gh_token: str) -> str:
    result = subprocess.run(
        [
            "gh", "api",
            "-H", "Accept: application/json",
            "-H", f"Authorization: token {gh_token}",
            "https://api.github.com/copilot_internal/v2/token",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Copilot 토큰 발급 실패: {result.stderr}")
    data = json.loads(result.stdout)
    return data.get("token", "")


def _build_prompt(messages: List[Dict[str, str]], system_prompt: str = "") -> str:
    parts: list[str] = []
    if system_prompt.strip():
        parts.append(f"[시스템 지침]\n{system_prompt.strip()}")

    if messages:
        transcript = []
        for message in messages:
            role = message.get("role", "user")
            label = "사용자" if role == "user" else "연속체"
            transcript.append(f"[{label}] {message.get('content', '').strip()}")
        parts.append("[대화]\n" + "\n".join(transcript))

    if not parts:
        return "안녕하세요."
    return "\n\n".join(parts)


def _resolve_engram_cmd() -> str:
    local_cmd = Path.home() / ".engram" / "engram-copilot.cmd"
    if local_cmd.exists():
        return str(local_cmd)
    path_cmd = shutil.which("engram")
    if path_cmd:
        return path_cmd
    raise RuntimeError("engram 명령을 찾을 수 없습니다. scripts/install.ps1을 다시 실행하세요.")


def _ask_copilot_cli(messages: List[Dict[str, str]], system_prompt: str = "") -> str:
    prompt = _build_prompt(messages, system_prompt)
    engram_cmd = _resolve_engram_cmd()

    # 순환 호출 방지: 독립 세션에서는 consult 도구를 비활성화.
    cmd = [
        "cmd",
        "/c",
        engram_cmd,
        "-p",
        prompt,
        "-s",
        "--allow-all-tools",
        "--deny-tool=engram(engram_consult_engram)",
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )

    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"독립 Copilot CLI 호출 실패: {err[:300]}")

    output = result.stdout.strip()
    if not output:
        raise RuntimeError("독립 Copilot CLI에서 빈 응답을 반환했습니다.")
    return output


def _ask_copilot_api(messages: List[Dict[str, str]], system_prompt: str = "") -> str:
    gh_token = _get_gh_token()

    full_messages = []
    if system_prompt:
        full_messages.append({"role": "system", "content": system_prompt})
    full_messages.extend(messages)

    payload = json.dumps(
        {"messages": full_messages, "model": "gpt-4o", "stream": False}
    )

    copilot_token = _get_copilot_token(gh_token)
    result = subprocess.run(
        [
            "gh", "api",
            "--method", "POST",
            "-H", "Content-Type: application/json",
            "-H", f"Authorization: Bearer {copilot_token}",
            "-H", "Copilot-Integration-Id: vscode-chat",
            "--input", "-",
            "https://api.githubcopilot.com/chat/completions",
        ],
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode == 0:
        data = json.loads(result.stdout)
        return data["choices"][0]["message"]["content"].strip()

    raise RuntimeError(f"Copilot API 호출 실패: {result.stderr[:300]}")


def ask_copilot(messages: List[Dict[str, str]], system_prompt: str = "") -> str:
    """
    기본 경로: 독립 Copilot CLI(engram.cmd) 호출.
    실패 시 Copilot Chat API 경로로 폴백.
    """
    try:
        return _ask_copilot_cli(messages, system_prompt=system_prompt)
    except RuntimeError as cli_error:
        try:
            return _ask_copilot_api(messages, system_prompt=system_prompt)
        except RuntimeError as api_error:
            raise RuntimeError(
                f"Copilot bridge 실패\n- CLI: {cli_error}\n- API: {api_error}"
            )
