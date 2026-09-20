"""Tests for installer/addons.ps1, the addon installer shared by both
installer entry points (installer/configure.ps1 and
installer/modules/07_shims.ps1 -- neither is wired up yet; this file only
covers the two functions those wave will call).

Structure mirrors test_agent_definition_install.py: the ownership rule is
re-implemented (not shared) from installer/deploy_agent_definitions.ps1, so
the same failure modes -- silently clobbering a user-authored file, freezing
a pre-provenance file out of future updates -- are re-verified here for the
addon path instead of assumed from the agent-definition tests.
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "installer" / "addons.ps1"
BUILD_ADDONS_SCRIPT = ROOT / "installer" / "build-addons.ps1"


def _powershell():
    return shutil.which("pwsh") or shutil.which("powershell")


def _make_zip(path: Path, files: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for relative, content in files.items():
            zf.writestr(relative, content)


def _make_addon_zip(path: Path, files: dict, skill_name: str, needs_mcp: bool = False,
                     mcp: dict | None = None, addon_id: str | None = None) -> None:
    """Writes an addon zip shaped like build-addons.ps1's real output: the
    payload files plus a ".addon-meta.json" baked into the zip root. This is
    the only place Install-EngramAddons/Register-EngramAddonMcp look for an
    addon's skill_name and MCP shape -- there is no sibling manifest at
    install time, so tests must not fabricate one either."""
    meta = {
        "id": addon_id or path.stem,
        "skill_name": skill_name,
        "needs_mcp": needs_mcp,
    }
    if needs_mcp:
        meta["mcp"] = mcp
    payload = dict(files)
    payload[".addon-meta.json"] = json.dumps(meta)
    _make_zip(path, payload)


def _run(function: str, *ps_args: str, timeout: int = 60):
    powershell = _powershell()
    script = (
        f". '{HELPER}'; "
        f"{function} " + " ".join(ps_args)
    )
    return subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", script],
        capture_output=True, text=True, timeout=timeout,
    )


def _install(payload_root: Path, addons, user_profile: Path, force: bool = False, timeout: int = 60):
    addon_list = ",".join(f"'{a}'" for a in addons)
    args = [
        f"-PayloadRoot '{payload_root}'",
        f"-Addons @({addon_list})",
        f"-UserProfile '{user_profile}'",
    ]
    if force:
        args.append("-Force")
    return _run("Install-EngramAddons", *args, timeout=timeout)


@pytest.fixture(autouse=True)
def _require_windows_powershell():
    if os.name != "nt" or not _powershell():
        pytest.skip("Windows PowerShell runtime is required")


def test_addons_ps1_is_utf8_with_bom():
    data = HELPER.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf"), (
        "PowerShell 5.1 misreads Korean text as cp949 without a BOM"
    )


def test_first_install_creates_files_matching_the_payload():
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        payload_root = temp / "payload"
        profile = temp / "profile"
        files = {"SKILL.md": "# fake addon\nbody", "sub/note.txt": "nested file"}
        _make_zip(payload_root / "fake-addon.zip", files)

        result = _install(payload_root, ["fake-addon"], profile)
        assert result.returncode == 0, result.stderr or result.stdout

        dest = profile / ".claude" / "skills" / "fake-addon"
        assert (dest / "SKILL.md").read_text(encoding="utf-8") == files["SKILL.md"]
        assert (dest / "sub" / "note.txt").read_text(encoding="utf-8") == files["sub/note.txt"]
        assert (profile / ".engram" / "addon-definitions.json").is_file()


def test_second_install_is_idempotent_and_hash_is_unchanged():
    """AC7: a repeat install must not touch what it already installed."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        payload_root = temp / "payload"
        profile = temp / "profile"
        _make_zip(payload_root / "fake-addon.zip", {"SKILL.md": "# fake addon\nbody"})

        assert _install(payload_root, ["fake-addon"], profile).returncode == 0
        skill_file = profile / ".claude" / "skills" / "fake-addon" / "SKILL.md"
        before = hashlib.sha256(skill_file.read_bytes()).hexdigest()

        second = _install(payload_root, ["fake-addon"], profile)
        assert second.returncode == 0, second.stderr or second.stdout
        after = hashlib.sha256(skill_file.read_bytes()).hexdigest()

        assert after == before, "idempotent re-install must not change the file's hash"
        assert "SKIP" not in second.stdout


def test_reinstall_with_changed_payload_updates_files_but_still_preserves_user_edits():
    """AC7 (the missing half): a re-install with genuinely NEW content must
    overwrite what we previously installed -- idempotency alone (same bytes
    back in) does not prove that. In the same round, a file the user edited
    must still be SKIPped and kept, and the provenance record for the file
    that *was* updated must move to the new hash so a later, third install
    can update it again instead of freezing on a stale record."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        payload_root = temp / "payload"
        profile = temp / "profile"

        _make_zip(payload_root / "fake-addon.zip", {
            "SKILL.md": "# fake addon\nv1 body",
            "notes.txt": "v1 notes",
        })
        assert _install(payload_root, ["fake-addon"], profile).returncode == 0

        skill_file = profile / ".claude" / "skills" / "fake-addon" / "SKILL.md"
        notes_file = profile / ".claude" / "skills" / "fake-addon" / "notes.txt"
        assert skill_file.read_text(encoding="utf-8") == "# fake addon\nv1 body"

        # The user edits notes.txt after the first install; it must never be
        # clobbered by a later install, even one that also changes SKILL.md.
        notes_file.write_text("v1 notes\n# tuned by the user", encoding="utf-8", newline="")
        edited_notes_hash = hashlib.sha256(notes_file.read_bytes()).hexdigest()

        # Ship a genuinely different payload for the second install: SKILL.md
        # content changes (must be adopted), notes.txt content also changes
        # upstream (must still be SKIPped because the user owns it now).
        _make_zip(payload_root / "fake-addon.zip", {
            "SKILL.md": "# fake addon\nv2 body -- new content",
            "notes.txt": "v2 notes -- new content",
        })
        second = _install(payload_root, ["fake-addon"], profile)
        assert second.returncode == 0, second.stderr or second.stdout

        assert skill_file.read_text(encoding="utf-8") == "# fake addon\nv2 body -- new content", (
            "a re-install with new content must overwrite the file we previously installed"
        )
        assert notes_file.read_bytes() == b"v1 notes\n# tuned by the user"
        assert hashlib.sha256(notes_file.read_bytes()).hexdigest() == edited_notes_hash, (
            "a user-edited file must survive an update round unchanged"
        )
        assert "SKIP" in second.stdout
        assert str(notes_file) in second.stdout

        provenance = json.loads(
            (profile / ".engram" / "addon-definitions.json").read_text(encoding="utf-8")
        )
        expected_skill_hash = hashlib.sha256(skill_file.read_bytes()).hexdigest().upper()
        recorded_skill_hash = str(provenance[str(skill_file)]).upper()
        assert recorded_skill_hash == expected_skill_hash, (
            "provenance for the updated file must move to the new hash, "
            "otherwise a later install can never adopt further updates"
        )

        # A third install with the v2 payload again must now be a silent,
        # idempotent no-op for SKILL.md -- proving the provenance record was
        # actually updated (not just skipped/left stale) after the update.
        third = _install(payload_root, ["fake-addon"], profile)
        assert third.returncode == 0, third.stderr or third.stdout
        assert str(skill_file) not in [
            line for line in third.stdout.splitlines() if line.startswith("SKIP")
        ]
        assert skill_file.read_text(encoding="utf-8") == "# fake addon\nv2 body -- new content"


def test_user_edit_is_preserved_and_skip_is_reported():
    """AC8: a user's own edit to a file we installed survives a re-install,
    and the reason is printed instead of silently dropped."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        payload_root = temp / "payload"
        profile = temp / "profile"
        _make_zip(payload_root / "fake-addon.zip", {"SKILL.md": "# fake addon\nbody"})

        assert _install(payload_root, ["fake-addon"], profile).returncode == 0
        skill_file = profile / ".claude" / "skills" / "fake-addon" / "SKILL.md"
        skill_file.write_text(
            skill_file.read_text(encoding="utf-8") + "\n# tuned by the user",
            encoding="utf-8",
        )
        edited = hashlib.sha256(skill_file.read_bytes()).hexdigest()

        third = _install(payload_root, ["fake-addon"], profile)
        assert third.returncode == 0, third.stderr or third.stdout

        after = hashlib.sha256(skill_file.read_bytes()).hexdigest()
        assert after == edited, "once the user edits it, it is theirs"
        assert "SKIP" in third.stdout


def test_files_identical_to_payload_but_without_provenance_are_adopted():
    """A file left by a pre-provenance install, byte-identical to what we
    would ship, must be adopted (updated) rather than frozen as SKIP."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        payload_root = temp / "payload"
        profile = temp / "profile"
        content = "# fake addon\nbody"
        _make_zip(payload_root / "fake-addon.zip", {"SKILL.md": content})

        dest_dir = profile / ".claude" / "skills" / "fake-addon"
        dest_dir.mkdir(parents=True)
        # newline="" avoids Python translating "\n" to "\r\n" on write, which
        # would make this byte-different from what the zip stores and defeat
        # the point of the test.
        (dest_dir / "SKILL.md").write_text(content, encoding="utf-8", newline="")
        assert not (profile / ".engram" / "addon-definitions.json").exists()

        result = _install(payload_root, ["fake-addon"], profile)
        assert result.returncode == 0, result.stderr or result.stdout
        assert "SKIP" not in result.stdout, (
            "an unmodified pre-provenance file must be adopted, not frozen out"
        )
        assert (profile / ".engram" / "addon-definitions.json").is_file()


def test_force_backs_up_the_edited_file_before_overwriting():
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        payload_root = temp / "payload"
        profile = temp / "profile"
        _make_zip(payload_root / "fake-addon.zip", {"SKILL.md": "# fake addon\nbody"})

        assert _install(payload_root, ["fake-addon"], profile).returncode == 0
        skill_file = profile / ".claude" / "skills" / "fake-addon" / "SKILL.md"
        skill_file.write_text(
            skill_file.read_text(encoding="utf-8") + "\n# tuned by the user",
            encoding="utf-8",
        )
        edited = hashlib.sha256(skill_file.read_bytes()).hexdigest()

        result = _install(payload_root, ["fake-addon"], profile, force=True)
        assert result.returncode == 0, result.stderr or result.stdout

        backup = skill_file.with_suffix(".md.engram-bak")
        assert backup.is_file(), "an irreversible overwrite must leave a way back"
        assert hashlib.sha256(backup.read_bytes()).hexdigest() == edited
        assert "BACKUP" in result.stdout
        assert skill_file.read_text(encoding="utf-8") == "# fake addon\nbody"


def test_missing_or_empty_payload_is_reported_and_does_not_fail_the_batch():
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        payload_root = temp / "payload"
        profile = temp / "profile"
        payload_root.mkdir(parents=True)
        # "not-bundled" has no zip at all; "empty-addon" has a zero-byte one.
        (payload_root / "empty-addon.zip").write_bytes(b"")
        _make_zip(payload_root / "present-addon.zip", {"SKILL.md": "content"})

        result = _install(
            payload_root, ["not-bundled", "empty-addon", "present-addon"], profile
        )
        assert result.returncode == 0, result.stderr or result.stdout
        assert "SKIP" in result.stdout
        assert "not-bundled" in result.stdout
        assert "empty-addon" in result.stdout
        assert (profile / ".claude" / "skills" / "present-addon" / "SKILL.md").is_file()


def test_skill_name_exceptions_are_applied():
    """claude-image-forge installs to .claude/skills/image-forge and
    session-agent-orchestration installs to .claude/skills/session-orchestrate
    -- both names differ from the addon/repo key."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        payload_root = temp / "payload"
        profile = temp / "profile"
        _make_addon_zip(
            payload_root / "claude-image-forge.zip", {"SKILL.md": "forge"},
            skill_name="image-forge",
        )
        _make_addon_zip(
            payload_root / "session-agent-orchestration.zip", {"SKILL.md": "orchestrate"},
            skill_name="session-orchestrate",
        )

        result = _install(
            payload_root, ["claude-image-forge", "session-agent-orchestration"], profile
        )
        assert result.returncode == 0, result.stderr or result.stdout

        assert (profile / ".claude" / "skills" / "image-forge" / "SKILL.md").is_file()
        assert (
            profile / ".claude" / "skills" / "session-orchestrate" / "SKILL.md"
        ).is_file()
        assert not (profile / ".claude" / "skills" / "claude-image-forge").exists()
        assert not (
            profile / ".claude" / "skills" / "session-agent-orchestration"
        ).exists()


def test_all_four_addons_install_skill_md_at_the_skill_root():
    """AC6 regression: installer/addons.pin's src->dest map must land each
    addon's SKILL.md directly under its skill directory, not buried at the
    repo-relative path it lived at upstream (skills/image-forge/SKILL.md,
    adapters/claude/SKILL.md, .agents/skills/session-orchestrate/SKILL.md).
    The zips below mirror what build-addons.ps1 emits once the pin's map is
    applied (dest-layout, not repo-layout) -- this is what a real installer
    consumes, so this test exercises the same Install-EngramAddons path the
    installer runs and checks the resulting on-disk tree, not the zip
    contents alone."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        payload_root = temp / "payload"
        profile = temp / "profile"

        _make_addon_zip(
            payload_root / "claude-image-forge.zip",
            {
                "SKILL.md": "# image-forge",
                "mcp/server.mjs": "// mcp server",
                "scripts/gen.mjs": "// gen script",
                "package.json": "{}",
            },
            skill_name="image-forge",
            needs_mcp=True,
            mcp={
                "server_name": "image-forge", "prerequisite": "node",
                "prerequisite_label": "node >= 18", "command": "node",
                "args": ["mcp/server.mjs"],
            },
        )
        _make_addon_zip(
            payload_root / "structured-reporting.zip",
            {
                "SKILL.md": "# structured-reporting",
                "core/report.py": "# core",
                "cli/main.py": "# cli",
            },
            skill_name="structured-reporting",
        )
        _make_addon_zip(
            payload_root / "session-agent-orchestration.zip",
            {"SKILL.md": "# session-orchestrate"},
            skill_name="session-orchestrate",
            needs_mcp=True,
            mcp={
                "server_name": "session-orchestrator", "prerequisite": "session-orchestrator-mcp",
                "prerequisite_label": "session-orchestrator-mcp",
                "command": "session-orchestrator-mcp", "args": [],
            },
        )
        _make_addon_zip(
            payload_root / "feature-spec.zip",
            {"SKILL.md": "# feature-spec"},
            skill_name="feature-spec",
        )

        result = _install(
            payload_root,
            [
                "claude-image-forge",
                "structured-reporting",
                "session-agent-orchestration",
                "feature-spec",
            ],
            profile,
        )
        assert result.returncode == 0, result.stderr or result.stdout

        skills_dir = profile / ".claude" / "skills"
        expected_roots = {
            "image-forge": {"SKILL.md", "mcp/server.mjs", "scripts/gen.mjs", "package.json"},
            "structured-reporting": {"SKILL.md", "core/report.py", "cli/main.py"},
            "session-orchestrate": {"SKILL.md"},
            "feature-spec": {"SKILL.md"},
        }
        for skill_dir, relative_files in expected_roots.items():
            skill_md = skills_dir / skill_dir / "SKILL.md"
            assert skill_md.is_file(), (
                f"{skill_dir}: SKILL.md must be at the skill root, not buried in a repo-relative path"
            )
            for relative in relative_files:
                assert (skills_dir / skill_dir / relative).is_file(), (
                    f"{skill_dir}: missing expected file {relative}"
                )

        # session-orchestrate must be exactly SKILL.md -- src/, config/,
        # pyproject.toml are the pip package, not the skill, and must not be
        # bundled as dead weight inside the skill directory.
        session_files = [
            p.relative_to(skills_dir / "session-orchestrate")
            for p in (skills_dir / "session-orchestrate").rglob("*")
            if p.is_file()
        ]
        assert [str(p) for p in session_files] == ["SKILL.md"]


def _make_image_forge_zip(payload_root: Path) -> None:
    _make_addon_zip(
        payload_root / "claude-image-forge.zip",
        {"SKILL.md": "forge", "mcp/server.mjs": "// server"},
        skill_name="image-forge",
        needs_mcp=True,
        mcp={
            "server_name": "image-forge", "prerequisite": "node",
            "prerequisite_label": "node >= 18", "command": "node",
            "args": ["mcp/server.mjs"],
        },
    )


def _make_session_orchestration_zip(payload_root: Path) -> None:
    _make_addon_zip(
        payload_root / "session-agent-orchestration.zip",
        {"SKILL.md": "orchestrate"},
        skill_name="session-orchestrate",
        needs_mcp=True,
        mcp={
            "server_name": "session-orchestrator", "prerequisite": "session-orchestrator-mcp",
            "prerequisite_label": "session-orchestrator-mcp",
            "command": "session-orchestrator-mcp", "args": [],
        },
    )


def test_register_addon_mcp_skips_an_existing_entry_and_says_so():
    """AC6 (half): registration must never silently overwrite whatever the
    user (or another installer) already put under that server name."""
    with tempfile.TemporaryDirectory() as temp_dir:
        payload_root = Path(temp_dir) / "payload"
        profile = Path(temp_dir) / "profile"
        profile.mkdir(parents=True)
        _make_image_forge_zip(payload_root)
        claude_json = profile / ".claude.json"
        claude_json.write_text(
            '{"mcpServers": {"image-forge": {"type": "stdio", "command": "mine", "args": []}}}',
            encoding="utf-8",
        )

        result = _run(
            "Register-EngramAddonMcp",
            f"-PayloadRoot '{payload_root}'",
            "-Addons @('claude-image-forge')",
            f"-UserProfile '{profile}'",
        )
        assert result.returncode == 0, result.stderr or result.stdout
        assert "SKIP" in result.stdout

        data = json.loads(claude_json.read_text(encoding="utf-8"))
        assert data["mcpServers"]["image-forge"]["command"] == "mine", (
            "an existing registration under the same name must survive untouched"
        )


def test_register_addon_mcp_adds_a_missing_entry():
    with tempfile.TemporaryDirectory() as temp_dir:
        payload_root = Path(temp_dir) / "payload"
        profile = Path(temp_dir) / "profile"
        profile.mkdir(parents=True)
        _make_session_orchestration_zip(payload_root)

        result = _run(
            "Register-EngramAddonMcp",
            f"-PayloadRoot '{payload_root}'",
            "-Addons @('session-agent-orchestration')",
            f"-UserProfile '{profile}'",
        )
        assert result.returncode == 0, result.stderr or result.stdout

        data = json.loads((profile / ".claude.json").read_text(encoding="utf-8"))
        entry = data["mcpServers"]["session-orchestrator"]
        assert entry["command"] == "session-orchestrator-mcp"
        assert entry["args"] == []


def _run_with_empty_path(function: str, *ps_args: str, timeout: int = 60):
    """Runs a HELPER function with a PATH stripped of every directory that
    could resolve session-orchestrator-mcp or node, so the prerequisite
    check sees a clean AMBER installer machine instead of this dev box
    (which has both -- that is exactly the trap the task called out)."""
    powershell = _powershell()
    script = (
        f". '{HELPER}'; "
        f"{function} " + " ".join(ps_args)
    )
    env = dict(os.environ)
    # Keep just enough of PATH for PowerShell/pwsh itself to run; drop the
    # directories that contain the addon prerequisites.
    system_root = env.get("SystemRoot", r"C:\Windows")
    env["PATH"] = os.pathsep.join([
        os.path.join(system_root, "System32"),
        os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0"),
        system_root,
    ])
    env.pop("PATHEXT", None)
    return subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", script],
        capture_output=True, text=True, timeout=timeout, env=env,
    )


def test_register_addon_mcp_skips_when_prerequisite_command_is_missing():
    """The task's own finding: session-orchestrator-mcp (pip console script)
    and node are not bundled by AMBER Model B, so a clean install machine
    must not get a dead MCP entry written for either addon."""
    with tempfile.TemporaryDirectory() as temp_dir:
        payload_root = Path(temp_dir) / "payload"
        profile = Path(temp_dir) / "profile"
        profile.mkdir(parents=True)
        _make_session_orchestration_zip(payload_root)
        _make_image_forge_zip(payload_root)

        result = _run_with_empty_path(
            "Register-EngramAddonMcp",
            f"-PayloadRoot '{payload_root}'",
            "-Addons @('session-agent-orchestration','claude-image-forge')",
            f"-UserProfile '{profile}'",
        )
        assert result.returncode == 0, result.stderr or result.stdout
        assert "SKIP" in result.stdout
        assert "session-orchestrator" in result.stdout
        assert "image-forge" in result.stdout
        assert "다시 실행하면 등록됩니다" in result.stdout

        claude_json = profile / ".claude.json"
        if claude_json.is_file():
            data = json.loads(claude_json.read_text(encoding="utf-8"))
            assert "mcpServers" not in data or not data["mcpServers"], (
                "no MCP server should be registered when its prerequisite command is absent"
            )


def test_register_addon_mcp_registers_when_prerequisite_command_is_present():
    """Sanity counterpart to the missing-prerequisite test: with the real
    PATH (both commands resolve on this dev machine per the task), the
    entries must still be written."""
    with tempfile.TemporaryDirectory() as temp_dir:
        payload_root = Path(temp_dir) / "payload"
        profile = Path(temp_dir) / "profile"
        profile.mkdir(parents=True)
        _make_session_orchestration_zip(payload_root)
        _make_image_forge_zip(payload_root)

        result = _run(
            "Register-EngramAddonMcp",
            f"-PayloadRoot '{payload_root}'",
            "-Addons @('session-agent-orchestration','claude-image-forge')",
            f"-UserProfile '{profile}'",
        )
        assert result.returncode == 0, result.stderr or result.stdout

        data = json.loads((profile / ".claude.json").read_text(encoding="utf-8"))
        assert data["mcpServers"]["session-orchestrator"]["command"] == "session-orchestrator-mcp"
        assert data["mcpServers"]["image-forge"]["command"] == "node"


def test_configure_ps1_accepts_addons_and_addon_payload_root_parameters():
    """AC6/AC10 wiring regression guard: installer\\engram-overlay.iss calls
    configure.ps1 with exactly these two parameter names (see the .iss
    GetSilentInstallArgs contract) -- if configure.ps1's param block does not
    declare them, the frozen installer entry point silently drops addon
    selection instead of erroring, because PowerShell only binds unknown
    named args to $args on a script with no matching parameter."""
    configure_ps1 = ROOT / "installer" / "configure.ps1"
    text = configure_ps1.read_text(encoding="utf-8")
    assert "$Addons" in text
    assert "$AddonPayloadRoot" in text
    powershell = _powershell()
    # Ask PowerShell's own parser for the parameter set instead of grepping,
    # so a renamed or reshaped param block is caught even if the literal
    # variable name text still happens to appear somewhere in the file.
    probe = (
        f"$cmd = Get-Command '{configure_ps1}'; "
        "$names = $cmd.Parameters.Keys -join ','; "
        "Write-Output $names"
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", probe],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    names = result.stdout.strip().split(",")
    assert "Addons" in names
    assert "AddonPayloadRoot" in names


def test_07_shims_wires_up_addons_ps1():
    """Regression guard for the exact gap the task describes: an earlier
    subagent-definitions rollout was wired into 07_shims.ps1 only, so
    AMBER-installer users never received it, and the existing test suite
    (which only looked at addons.ps1 in isolation) could not have caught
    that a caller forgot to dot-source it. Assert the source-tree install
    path actually calls into installer/addons.ps1."""
    shims = (ROOT / "installer" / "modules" / "07_shims.ps1").read_text(encoding="utf-8")
    assert "addons.ps1" in shims
    assert "Install-EngramAddons" in shims
    assert "Register-EngramAddonMcp" in shims


def test_07_shims_addon_section_installs_files_into_a_temp_profile():
    """AC10: actually execute the addon section of 07_shims.ps1's logic
    (via the same Install-EngramAddons/Register-EngramAddonMcp calls it
    makes) against a temporary USERPROFILE and confirm files land at the
    destination -- not just that the module text mentions the right names."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        payload_root = temp / "payload"
        profile = temp / "profile"
        _make_zip(payload_root / "feature-spec.zip", {"SKILL.md": "# feature-spec\nbody"})

        result = _install(payload_root, ["feature-spec"], profile)
        assert result.returncode == 0, result.stderr or result.stdout

        dest = profile / ".claude" / "skills" / "feature-spec" / "SKILL.md"
        assert dest.is_file(), "07_shims.ps1's addon section must place payload files under the target profile"


def _skip_if_addon_sources_unavailable():
    """Skip (not fail) the real-build regression tests when this machine has
    neither the sibling checkouts nor network access build-addons.ps1 needs
    to acquire claude-image-forge / structured-reporting /
    session-agent-orchestration. feature-spec is always available (it is
    committed inside this repo)."""
    siblings_root = ROOT.parent
    required_siblings = [
        "claude-image-forge", "structured-reporting", "session-agent-orchestration",
    ]
    missing = [s for s in required_siblings if not (siblings_root / s / ".git").exists()]
    if missing and not shutil.which("git"):
        pytest.skip(f"neither sibling checkouts nor git available for: {missing}")


def test_real_build_addons_output_installs_with_correct_skill_names_and_no_manifest():
    """Regression for the actual defect: an earlier version of
    Get-EngramAddonSkillName read a sibling addons.json manifest that only
    exists next to build-addons.ps1's own output directory -- never at the
    real install-time PayloadRoot ({tmp}\\addon-bundle, staged by
    PrepareAddonBundle(), which contains only the zips). A test that hands
    Install-EngramAddons a hand-written manifest cannot catch that gap; this
    test runs build-addons.ps1 for real, then copies *only* the zips (no
    addons.json) into a fresh payload root before installing, so it fails
    exactly the way the real setup.exe run did if the fallback-to-addon-id
    path is ever reintroduced."""
    _skip_if_addon_sources_unavailable()
    if not _powershell():
        pytest.skip("Windows PowerShell runtime is required")

    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        profile = temp / "profile"

        build_result = subprocess.run(
            [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(BUILD_ADDONS_SCRIPT)],
            capture_output=True, text=True, timeout=300, cwd=str(ROOT),
        )
        assert build_result.returncode == 0, build_result.stderr or build_result.stdout

        real_addons_dir = ROOT / "installer" / "addons"
        manifest = json.loads((real_addons_dir / "addons.json").read_text(encoding="utf-8"))
        available = {a["name"] for a in manifest["addons"] if a["status"] == "available"}
        expected_addons = {
            "claude-image-forge", "structured-reporting",
            "session-agent-orchestration", "feature-spec",
        }
        assert expected_addons.issubset(available), (
            f"build-addons.ps1 did not acquire all four addons: missing "
            f"{expected_addons - available}"
        )

        # No-manifest payload root: only the zips build-addons.ps1 produced,
        # exactly what PrepareAddonBundle() stages into {tmp}\addon-bundle at
        # real install time.
        payload_root = temp / "addon-bundle"
        payload_root.mkdir(parents=True)
        for addon_id in expected_addons:
            shutil.copy2(real_addons_dir / f"{addon_id}.zip", payload_root / f"{addon_id}.zip")
        assert not (payload_root / "addons.json").exists(), (
            "payload root must not carry a manifest -- this is the exact gap being tested"
        )

        result = _install(payload_root, sorted(expected_addons), profile, timeout=120)
        assert result.returncode == 0, result.stderr or result.stdout

        skills_dir = profile / ".claude" / "skills"
        expected_skill_dirs = {
            "image-forge", "structured-reporting", "session-orchestrate", "feature-spec",
        }
        actual_skill_dirs = {p.name for p in skills_dir.iterdir()} if skills_dir.is_dir() else set()
        assert actual_skill_dirs == expected_skill_dirs, (
            f"skill directory names must match the pin's skill_name, not the addon id "
            f"(got {actual_skill_dirs})"
        )
        for skill_dir in expected_skill_dirs:
            assert (skills_dir / skill_dir / "SKILL.md").is_file()
            assert not (skills_dir / skill_dir / ".addon-meta.json").exists(), (
                "our internal meta file must never land inside a skill directory"
            )


def test_real_build_addons_mcp_registration_points_at_a_file_that_actually_exists():
    """The defect's second half: the MCP entry that got written pointed at
    ".claude/skills/image-forge/mcp/server.mjs" -- a path that never existed
    because the skill had actually been installed under a different
    directory name. Prove the registered command's argument resolves with
    Test-Path against files build-addons.ps1 + Install-EngramAddons actually
    produced, using the same real, manifest-free payload root as the
    previous test."""
    _skip_if_addon_sources_unavailable()
    if not _powershell():
        pytest.skip("Windows PowerShell runtime is required")

    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        profile = temp / "profile"

        build_result = subprocess.run(
            [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(BUILD_ADDONS_SCRIPT)],
            capture_output=True, text=True, timeout=300, cwd=str(ROOT),
        )
        assert build_result.returncode == 0, build_result.stderr or build_result.stdout

        real_addons_dir = ROOT / "installer" / "addons"
        addon_ids = [
            "claude-image-forge", "structured-reporting",
            "session-agent-orchestration", "feature-spec",
        ]
        payload_root = temp / "addon-bundle"
        payload_root.mkdir(parents=True)
        for addon_id in addon_ids:
            src = real_addons_dir / f"{addon_id}.zip"
            if not src.is_file():
                pytest.skip(f"{addon_id}.zip was not produced by build-addons.ps1 on this machine")
            shutil.copy2(src, payload_root / f"{addon_id}.zip")

        assert _install(payload_root, addon_ids, profile, timeout=120).returncode == 0

        mcp_result = _run(
            "Register-EngramAddonMcp",
            f"-PayloadRoot '{payload_root}'",
            "-Addons @(" + ",".join(f"'{a}'" for a in addon_ids) + ")",
            f"-UserProfile '{profile}'",
        )
        assert mcp_result.returncode == 0, mcp_result.stderr or mcp_result.stdout

        claude_json = profile / ".claude.json"
        assert claude_json.is_file(), "no MCP server was registered at all"
        data = json.loads(claude_json.read_text(encoding="utf-8"))
        image_forge_entry = data.get("mcpServers", {}).get("image-forge")
        assert image_forge_entry, "image-forge MCP entry missing"
        registered_path = Path(image_forge_entry["args"][0])
        assert registered_path.is_file(), (
            f"registered MCP path does not exist on disk: {registered_path}"
        )
        assert registered_path == profile / ".claude" / "skills" / "image-forge" / "mcp" / "server.mjs"
