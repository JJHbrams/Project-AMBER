import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
AGENTS_ROOT = ROOT / "config" / "agents"
ROLES = ("planner", "coder", "servant")


def _read_markdown(path: Path):
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    _, frontmatter, body = text.split("---\n", 2)
    return yaml.safe_load(frontmatter), body.strip()


def test_provider_owned_agent_sources_have_valid_syntax_and_role_parity():
    expected_claude = {
        "planner": ("opus", {"Read", "Grep", "Glob"}),
        "coder": ("sonnet", {"Read", "Edit", "Grep", "Glob", "Bash"}),
        "servant": ("haiku", {"Read", "Grep", "Glob", "Bash"}),
    }
    expected_copilot = {
        "planner": ("gpt-5.3-codex", ["read", "search"]),
        "coder": ("gpt-4.1", ["read", "edit", "search", "execute"]),
        "servant": ("gpt-5-mini", ["read", "search", "execute"]),
    }
    expected_codex = {
        "planner": ("gpt-5.6-terra", "medium", "read-only"),
        "coder": ("gpt-5.6-terra", "medium", "workspace-write"),
        "servant": ("gpt-5.6-luna", "low", "read-only"),
    }

    for role in ROLES:
        claude_meta, claude_body = _read_markdown(AGENTS_ROOT / "claude" / f"{role}.md")
        copilot_meta, copilot_body = _read_markdown(
            AGENTS_ROOT / "copilot" / f"{role}.agent.md"
        )
        codex = tomllib.loads((AGENTS_ROOT / "codex" / f"{role}.toml").read_text(encoding="utf-8"))

        assert claude_meta["name"] == copilot_meta["name"] == codex["name"] == role
        assert (claude_meta["model"], set(claude_meta["tools"].split(", "))) == expected_claude[role]
        assert (copilot_meta["model"], copilot_meta["tools"]) == expected_copilot[role]
        assert (
            codex["model"],
            codex["model_reasoning_effort"],
            codex["sandbox_mode"],
        ) == expected_codex[role]
        assert claude_body == copilot_body == codex["developer_instructions"].strip()


def test_07_shims_uses_provider_specific_agent_deployment():
    shims = (ROOT / "installer" / "modules" / "07_shims.ps1").read_text(encoding="utf-8-sig")
    helper = (ROOT / "installer" / "deploy_agent_definitions.ps1").read_text(encoding="utf-8-sig")
    common = (ROOT / "installer" / "common.ps1").read_text(encoding="utf-8-sig")

    assert "deploy_agent_definitions.ps1" in shims
    assert "config\\skills" not in shims
    assert "config\\skills" not in common
    assert "Remove-Item" not in helper
    assert helper.count("Copy-Item") == 1
    assert "-LiteralPath $source -Destination $destination -Force" in helper
    for provider, destination, extension in (
        ("claude", ".claude\\agents", ".md"),
        ("copilot", ".copilot\\agents", ".agent.md"),
        ("codex", ".codex\\agents", ".toml"),
    ):
        assert f'Join-Path $agentsSourceDir "{provider}"' in helper
        assert destination in helper
        assert f'Extension = "{extension}"' in helper


def test_windows_agent_deployment_is_idempotent_and_preserves_unmanaged_files():
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if os.name != "nt" or not powershell:
        pytest.skip("Windows PowerShell runtime is required")

    helper = ROOT / "installer" / "deploy_agent_definitions.ps1"
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        user_profile = temp / "profile"
        appdata = temp / "appdata"
        user_profile.mkdir()
        appdata.mkdir()

        sentinels = []
        for relative in (".claude/agents", ".copilot/agents", ".codex/agents"):
            destination = user_profile / relative
            destination.mkdir(parents=True)
            sentinel = destination / "user-owned-sentinel.txt"
            sentinel.write_text(f"preserve {relative}", encoding="utf-8")
            sentinels.append(sentinel)
        appdata_sentinel = appdata / "user-owned-sentinel.txt"
        appdata_sentinel.write_text("preserve appdata", encoding="utf-8")
        sentinels.append(appdata_sentinel)
        sentinel_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in sentinels}

        env = os.environ.copy()
        env["USERPROFILE"] = str(user_profile)
        env["APPDATA"] = str(appdata)
        command = [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(helper),
            "-ProjectRoot",
            str(ROOT),
            "-UserProfile",
            str(user_profile),
        ]

        for _ in range(2):
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
            assert result.returncode == 0, result.stderr or result.stdout

        mappings = {
            "claude": (user_profile / ".claude" / "agents", ".md"),
            "copilot": (user_profile / ".copilot" / "agents", ".agent.md"),
            "codex": (user_profile / ".codex" / "agents", ".toml"),
        }
        for provider, (destination, extension) in mappings.items():
            for role in ROLES:
                assert (destination / f"{role}{extension}").read_bytes() == (
                    AGENTS_ROOT / provider / f"{role}{extension}"
                ).read_bytes()

        for path, expected_hash in sentinel_hashes.items():
            assert path.is_file()
            assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_hash


def test_windows_agent_deployment_can_repair_only_claude():
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if os.name != "nt" or not powershell:
        pytest.skip("Windows PowerShell runtime is required")

    helper = ROOT / "installer" / "deploy_agent_definitions.ps1"
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        user_profile = temp / "profile"
        appdata = temp / "appdata"
        appdata.mkdir(parents=True)
        mappings = {
            "claude": (user_profile / ".claude" / "agents", ".md"),
            "copilot": (user_profile / ".copilot" / "agents", ".agent.md"),
            "codex": (user_profile / ".codex" / "agents", ".toml"),
        }
        untouched_hashes = {}
        for provider, (destination, extension) in mappings.items():
            destination.mkdir(parents=True)
            sentinel = destination / "user-owned-sentinel.txt"
            sentinel.write_text(f"preserve {provider}", encoding="utf-8")
            untouched_hashes[sentinel] = hashlib.sha256(sentinel.read_bytes()).hexdigest()
            for role in ROLES:
                managed = destination / f"{role}{extension}"
                managed.write_text(f"preexisting {provider} {role}", encoding="utf-8")
                if provider != "claude":
                    untouched_hashes[managed] = hashlib.sha256(managed.read_bytes()).hexdigest()

        env = os.environ.copy()
        env["USERPROFILE"] = str(user_profile)
        env["APPDATA"] = str(appdata)
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(helper),
                "-ProjectRoot",
                str(ROOT),
                "-UserProfile",
                str(user_profile),
                "-Provider",
                "Claude",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr or result.stdout

        claude_destination, claude_extension = mappings["claude"]
        for role in ROLES:
            assert (claude_destination / f"{role}{claude_extension}").read_bytes() == (
                AGENTS_ROOT / "claude" / f"{role}{claude_extension}"
            ).read_bytes()
        for path, expected_hash in untouched_hashes.items():
            assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_hash
