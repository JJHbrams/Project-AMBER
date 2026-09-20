"""Regression tests for the Startup/Programs known-folder resolution defect.

Background: [Environment]::GetFolderPath('Startup') can return an empty
string (no exception) on some machines (redirected/policy-managed profiles).
Every caller that fed that empty string straight into Join-Path crashed the
whole installer with a bare "Runtime error" -- this had already been "fixed"
twice at individual call sites and reproduced again each time, because the
fix never covered every caller. These tests exercise the shared resolver
(installer/joint-startup.ps1's Get-EngramKnownFolder) and prove the callers
that matter most (Set-EngramJointStartup, and configure.ps1 end-to-end)
survive the failure instead of merely checking that one call site is guarded.

installer/joint-startup.ps1 supports a test-only override,
$env:ENGRAM_TEST_FORCE_EMPTY_KNOWNFOLDER (comma-separated folder names), that
forces Get-EngramKnownFolder to skip the real GetFolderPath call and behave
as if it returned empty -- the only reliable way to reproduce the defect
deterministically, since normal dev/CI machines never see GetFolderPath fail.
"""

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
JOINT_STARTUP = ROOT / "installer" / "joint-startup.ps1"
CONFIGURE = ROOT / "installer" / "configure.ps1"
DIST_EXE = ROOT / "dist" / "engram-overlay" / "engram-overlay.exe"


def _powershell():
    return shutil.which("powershell") or shutil.which("pwsh")


@pytest.fixture(autouse=True)
def _require_windows_powershell():
    if os.name != "nt" or not _powershell():
        pytest.skip("Windows PowerShell runtime is required")


def _run_ps(script, timeout=60, env=None):
    powershell = _powershell()
    return subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", script],
        capture_output=True, encoding="utf-8", errors="replace",
        timeout=timeout, cwd=str(ROOT), env=env,
    )


def test_joint_startup_ps1_has_no_bom_regression():
    """joint-startup.ps1 has no BOM today; this only guards against silently
    reintroducing mixed encoding while it stays plain ASCII/UTF-8 comments."""
    data = JOINT_STARTUP.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf"), (
        "joint-startup.ps1 had no BOM before this change; if Korean text was "
        "added, add a BOM too (PowerShell 5.1 misreads it as cp949 without one)"
    )


def test_get_engram_known_folder_returns_null_and_warns_once_when_unresolved():
    """The resolver must return null (not an empty string) on failure, and
    must not repeat the same warning for repeated calls to the same folder."""
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f". '{JOINT_STARTUP}'; "
        "$env:ENGRAM_TEST_FORCE_EMPTY_KNOWNFOLDER = 'Startup'; "
        "$first = Get-EngramKnownFolder -Name 'Startup'; "
        "$second = Get-EngramKnownFolder -Name 'Startup'; "
        "if ($null -ne $first) { throw ('expected null, got: ' + $first) }; "
        "if ($null -ne $second) { throw 'expected null on second call too' }; "
        "Write-Output 'NULL_OK'"
    )
    result = _run_ps(script)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "NULL_OK" in result.stdout
    warn_count = result.stdout.count("Windows") + result.stderr.count("Windows")
    assert warn_count == 1, (
        "expected exactly one warning for two calls to the same folder, got "
        + str(warn_count) + ": " + repr(result.stdout) + " " + repr(result.stderr)
    )


def test_get_engram_known_folder_resolves_normally_without_override():
    """Sanity counterpart: with a real, unmodified profile, the resolver must
    still return the real path (this repo's own test conftest.py permanently
    overrides USERPROFILE/HOME/APPDATA/LOCALAPPDATA for isolation, and that
    override alone is enough to make GetFolderPath('Startup') return empty --
    so this test must run with those four variables removed, not just the
    inherited, already-isolated os.environ)."""
    env = dict(os.environ)
    for name in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA"):
        env.pop(name, None)
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f". '{JOINT_STARTUP}'; "
        "$path = Get-EngramKnownFolder -Name 'Startup'; "
        "if (-not $path) { throw 'expected a real Startup path on this dev machine' }; "
        "Write-Output ('RESOLVED:' + $path)"
    )
    result = _run_ps(script, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESOLVED:" in result.stdout


@pytest.mark.parametrize("mode", ["on", "off"])
def test_set_engram_joint_startup_does_not_throw_when_startup_is_unresolved(mode):
    """This is the exact call (configure.ps1:616 / modules/10_shortcuts.ps1)
    that crashed with 'Runtime error (at 79:1441)': Set-EngramJointStartup
    called with no explicit -StartupDirectory, so it fell back to the
    then-unguarded parameter default of [Environment]::GetFolderPath('Startup')."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        state_dir = temp / "state"
        external_root = temp / "external"
        host_exe = temp / "host" / "engram-overlay.exe"
        host_exe.parent.mkdir(parents=True)
        host_exe.write_bytes(b"not a real exe, existence is all that's checked")
        state_dir.mkdir()
        external_root.mkdir()

        script = (
            "$ErrorActionPreference = 'Stop'; "
            f". '{JOINT_STARTUP}'; "
            "$env:ENGRAM_TEST_FORCE_EMPTY_KNOWNFOLDER = 'Startup'; "
            f"Set-EngramJointStartup -Mode {mode} -HostExecutable '{host_exe}' "
            f"-StateDirectory '{state_dir}' -ExternalRoot '{external_root}'; "
            "Write-Output 'NO_THROW'"
        )
        result = _run_ps(script)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "NO_THROW" in result.stdout


def _skip_if_frozen_bundle_unavailable():
    if not DIST_EXE.is_file():
        pytest.skip(
            "dist/engram-overlay/engram-overlay.exe not built; run "
            "installer/build-overlay.ps1 first for this end-to-end test"
        )


def test_configure_ps1_completes_with_exit_0_when_startup_and_programs_are_unresolved():
    """The actual regression: reproduce GetFolderPath('Startup')/('Programs')
    returning empty and prove configure.ps1 runs to completion (exit 0)
    instead of dying with the bare 'Runtime error' the task reported -- not
    just that one call site is guarded."""
    _skip_if_frozen_bundle_unavailable()

    with tempfile.TemporaryDirectory() as temp_dir:
        fixture = Path(temp_dir) / ("engram-cfg-test-" + uuid.uuid4().hex)
        fixture.mkdir()
        # Junction (not copy) the real dist/ and installer/ trees: dist/ alone
        # is roughly 1GB and configure.ps1 needs the real frozen exe to
        # actually perform DB bootstrap via --role install-bootstrap.
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(fixture / "dist"), str(ROOT / "dist")],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(fixture / "installer"), str(ROOT / "installer")],
            check=True, capture_output=True, text=True,
        )

        profile_dir = fixture / "profile"
        for rel in ("", ".engram", "AppData/Roaming", "AppData/Local"):
            (profile_dir / rel).mkdir(parents=True, exist_ok=True)
        db_dir = fixture / "db"

        env = dict(os.environ)
        env["USERPROFILE"] = str(profile_dir)
        env["HOME"] = str(profile_dir)
        env["APPDATA"] = str(profile_dir / "AppData" / "Roaming")
        env["LOCALAPPDATA"] = str(profile_dir / "AppData" / "Local")
        env["ENGRAM_TEST_FORCE_EMPTY_KNOWNFOLDER"] = "Startup,Programs"

        result = subprocess.run(
            [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(CONFIGURE),
             "-InstallDir", str(fixture),
             "-DbDir", str(db_dir), "-WorkDir", str(db_dir),
             "-CliProvider", "claude-code", "-ExternalOverlayMode", "none",
             "-AutoStart", "on"],
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=180, env=env,
        )

        assert result.returncode == 0, (
            "configure.ps1 must complete (exit 0) even when Startup/Programs "
            "can't be resolved -- those are optional features, not install "
            "failures.\nstdout:\n" + result.stdout + "\nstderr:\n" + result.stderr
        )
        # Both known-folder warnings must actually have fired -- otherwise
        # this test would pass vacuously without exercising the failure path.
        assert "Programs" in result.stdout
        assert "Startup" in result.stdout
