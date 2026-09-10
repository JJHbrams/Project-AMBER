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
    # planner/coder/servant 는 아무나 쓸 이름이라, 무조건 복사는 사용자가 같은
    # 이름으로 만든 에이전트를 백업도 없이 지운다. provenance 해시로 우리가 쓴
    # 내용인지 판정하고, 아닐 때는 건드리지 않는다.
    assert "[Security.Cryptography.SHA256]::Create()" in helper
    assert "[IO.File]::OpenRead" in helper
    assert "Get-FileHash" not in helper
    assert "provenance" in helper.lower()
    assert "agent-definitions.json" in helper
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
                # 내용을 모르는 기존 파일은 provenance 에 없으므로 어느 공급자든
                # 보존돼야 한다. 이게 이 배치의 새 계약이다.
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

        # 알 수 없는 내용의 기존 파일은 하나도 바뀌지 않는다.
        for path, expected_hash in untouched_hashes.items():
            assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_hash, (
                f"{path} was overwritten; the installer must not clobber files it did not write"
            )
        assert "SKIP" in result.stdout, "건너뛴 사실을 사용자에게 말해야 한다"


# ── 진입점이 둘이다: 설치본은 configure.ps1, 소스 설치는 modules/07_shims.ps1.
#    한쪽에만 배선하면 그쪽 사용자만 받는다. 실제로 subagent 정의가 소스 경로에만
#    배선돼 있어서 AMBER 설치본 사용자는 planner/coder/servant 를 받지 못했다.
#    기존 테스트는 shims 에 있는지만 확인해 그 공백을 잡지 못했다.

def test_installed_path_also_deploys_agent_definitions():
    configure = (ROOT / "installer" / "configure.ps1").read_text(encoding="utf-8-sig")
    assert "deploy_agent_definitions.ps1" in configure, (
        "configure.ps1 is what setup.exe runs; without this the installed user "
        "never gets planner/coder/servant"
    )
    assert "-ProjectRoot $InstallDir" in configure, (
        "configure.ps1 has no $ProjectRoot; the install root is $InstallDir"
    )


def test_installer_ships_the_sources_and_the_deployer():
    iss = (ROOT / "installer" / "engram-overlay.iss").read_text(encoding="utf-8-sig")
    assert r'Source: "..\config\agents\*"' in iss, (
        "the deployer throws when a provider source is missing, so the sources "
        "have to be shipped too"
    )
    assert 'Source: "deploy_agent_definitions.ps1"' in iss


def test_provider_formats_stay_separate():
    helper = (ROOT / "installer" / "deploy_agent_definitions.ps1").read_text(encoding="utf-8-sig")
    # One shared file copied to three places was the earlier bug: the formats
    # differ, so a Claude .md dropped into ~/.codex/agents is not a definition.
    for source, destination, extension in (
        ("claude", r".claude\agents", '".md"'),
        ("copilot", r".copilot\agents", '".agent.md"'),
        ("codex", r".codex\agents", '".toml"'),
    ):
        assert source in helper
        assert destination in helper
        assert extension in helper


def test_agent_definition_failure_does_not_stop_core_setup():
    configure = (ROOT / "installer" / "configure.ps1").read_text(encoding="utf-8-sig")
    block = configure[configure.index("$AgentDefinitionsInstaller"):]
    block = block[:block.index("# ── 외부 오버레이")]
    assert "try {" in block and "catch {" in block, (
        "a missing provider source must not fail the whole install"
    )
    assert "Write-Warn" in block, "it must say what was skipped"


def _run_deploy(profile: Path, *extra: str):
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    return subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(ROOT / "installer" / "deploy_agent_definitions.ps1"),
         "-ProjectRoot", str(ROOT), "-UserProfile", str(profile),
         "-Provider", "Claude", *extra],
        capture_output=True, text=True, timeout=60,
    )


def test_a_user_authored_agent_of_the_same_name_survives_install():
    """planner/coder/servant are ordinary names; a user may already own one.

    Before provenance the deployer did Copy-Item -Force unconditionally, so an
    install silently destroyed the user's own agent with no backup.
    """
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if os.name != "nt" or not powershell:
        pytest.skip("Windows PowerShell runtime is required")

    with tempfile.TemporaryDirectory() as temp_dir:
        profile = Path(temp_dir) / "profile"
        agents = profile / ".claude" / "agents"
        agents.mkdir(parents=True)
        mine = agents / "coder.md"
        mine.write_text("# my own coder\nprecious prompt", encoding="utf-8")
        before = hashlib.sha256(mine.read_bytes()).hexdigest()

        result = _run_deploy(profile)
        assert result.returncode == 0, result.stderr or result.stdout

        assert hashlib.sha256(mine.read_bytes()).hexdigest() == before
        assert "SKIP" in result.stdout
        # 나머지는 정상 배치된다 — 하나 때문에 전체가 멈추지 않는다.
        assert (agents / "planner.md").is_file()
        assert (agents / "servant.md").is_file()


def test_our_own_previous_file_is_updated_on_reinstall():
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if os.name != "nt" or not powershell:
        pytest.skip("Windows PowerShell runtime is required")

    with tempfile.TemporaryDirectory() as temp_dir:
        profile = Path(temp_dir) / "profile"
        assert _run_deploy(profile).returncode == 0
        planner = profile / ".claude" / "agents" / "planner.md"
        source = (AGENTS_ROOT / "claude" / "planner.md").read_bytes()
        assert planner.read_bytes() == source

        # 우리가 쓴 그대로면 두 번째 설치도 조용히 갱신한다(멱등).
        result = _run_deploy(profile)
        assert result.returncode == 0
        assert planner.read_bytes() == source
        assert "SKIP" not in result.stdout


def test_edits_to_our_file_are_preserved_and_force_backs_them_up():
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if os.name != "nt" or not powershell:
        pytest.skip("Windows PowerShell runtime is required")

    with tempfile.TemporaryDirectory() as temp_dir:
        profile = Path(temp_dir) / "profile"
        assert _run_deploy(profile).returncode == 0
        planner = profile / ".claude" / "agents" / "planner.md"
        planner.write_text(planner.read_text(encoding="utf-8") + "\n# tuned by the user",
# tuned by the user",
                           encoding="utf-8")
        edited = hashlib.sha256(planner.read_bytes()).hexdigest()

        assert _run_deploy(profile).returncode == 0
        assert hashlib.sha256(planner.read_bytes()).hexdigest() == edited, (
            "once the user edits it, it is theirs"
        )

        # -Force 는 덮어쓰되 되돌릴 수 있게 남긴다.
        result = _run_deploy(profile, "-Force")
        assert result.returncode == 0
        backup = planner.with_suffix(".md.engram-bak")
        assert backup.is_file(), "an irreversible overwrite must leave a way back"
        assert hashlib.sha256(backup.read_bytes()).hexdigest() == edited
        assert "BACKUP" in result.stdout


def test_files_from_before_provenance_are_adopted_not_frozen():
    """v1.5.13 이전 배치가 깔아둔 파일에는 provenance 기록이 없다.

    기록만 보고 판정하면 그 파일들이 전부 "사용자 것"으로 분류되어 SKIP 되고,
    앞으로 정의를 개선해도 소스 설치 사용자에게 도달하지 않는다. 디스크 내용이
    배포 원본과 바이트 동일하면 사용자 작업물이 아니므로 입양한다.
    """
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if os.name != "nt" or not powershell:
        pytest.skip("Windows PowerShell runtime is required")

    with tempfile.TemporaryDirectory() as temp_dir:
        profile = Path(temp_dir) / "profile"
        agents = profile / ".claude" / "agents"
        agents.mkdir(parents=True)
        # 예전 배치 상태를 재현: 원본과 동일한 내용, provenance 없음
        for role in ROLES:
            shutil.copyfile(AGENTS_ROOT / "claude" / f"{role}.md", agents / f"{role}.md")
        assert not (profile / ".engram" / "agent-definitions.json").exists()

        result = _run_deploy(profile)
        assert result.returncode == 0, result.stderr or result.stdout
        assert "SKIP" not in result.stdout, (
            "unmodified managed files must be adopted, not frozen out of future updates"
        )
        assert (profile / ".engram" / "agent-definitions.json").is_file()

        # 입양 뒤에는 사용자 수정이 정상적으로 보호된다.
        planner = agents / "planner.md"
        planner.write_text(planner.read_text(encoding="utf-8") + "\n# mine",
                           encoding="utf-8")
        edited = hashlib.sha256(planner.read_bytes()).hexdigest()
        second = _run_deploy(profile)
        assert second.returncode == 0
        assert hashlib.sha256(planner.read_bytes()).hexdigest() == edited
        assert "SKIP" in second.stdout


def test_adoption_never_swallows_a_user_authored_file():
    """입양은 '원본과 동일할 때만' 이다. 내용이 다르면 여전히 사용자 것이다."""
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if os.name != "nt" or not powershell:
        pytest.skip("Windows PowerShell runtime is required")

    with tempfile.TemporaryDirectory() as temp_dir:
        profile = Path(temp_dir) / "profile"
        agents = profile / ".claude" / "agents"
        agents.mkdir(parents=True)
        mine = agents / "coder.md"
        mine.write_text("# my own coder", encoding="utf-8")
        before = hashlib.sha256(mine.read_bytes()).hexdigest()

        result = _run_deploy(profile)
        assert result.returncode == 0
        assert hashlib.sha256(mine.read_bytes()).hexdigest() == before
        assert "SKIP" in result.stdout

