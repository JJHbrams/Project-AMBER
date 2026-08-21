"""원격 세션 배치(skill / SessionStart hook) 검증.

배치는 원격 파일시스템을 건드리는 유일한 경로다. 여기서 검증하는 것:

- 로컬 바이너리나 터널에 없는 포트를 때리는 skill 이 섞여 나가지 않는다.
- hook 스크립트가 백엔드를 호출하지 않는다(원격에 런타임 의존성이 생기면 안 된다).
- settings.json 병합이 멱등하고, 사용자 자기 항목을 보존한다.
- 파싱 실패한 사용자 settings.json 을 덮어쓰지 않는다.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core.integrations.engram_bootstrap import SESSIONSTART_HOOK_MARKER
from core.integrations.remote_provision import (
    REMOTE_SAFE_SKILLS,
    build_remote_payload,
    collect_remote_skills,
    encode_payload,
    render_remote_installer,
    render_session_start_script,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]

# 원격에서 실패하는 절차를 모델에게 쥐어주면 그걸 시도하다 막힌다.
_EXCLUDED_SKILLS = ("engram-new-session", "engram")


def _run_installer(home: Path, payload: dict) -> subprocess.CompletedProcess:
    """배치 스크립트를 가짜 HOME 으로 실행한다 — 원격에서 도는 것과 같은 코드."""
    script = home / "_installer.py"
    script.write_text(render_remote_installer(), encoding="utf-8")
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    return subprocess.run(
        [sys.executable, str(script)],
        input=encode_payload(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def _registered_commands(settings_path: Path) -> list[str]:
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    return [
        str(hook.get("command", ""))
        for entry in data.get("hooks", {}).get("SessionStart", [])
        if isinstance(entry, dict)
        for hook in (entry.get("hooks") or [])
        if isinstance(hook, dict) and SESSIONSTART_HOOK_MARKER in str(hook.get("command", ""))
    ]


class RemoteSkillSelectionTests(unittest.TestCase):
    def test_excludes_skills_that_need_local_only_endpoints(self):
        for name in _EXCLUDED_SKILLS:
            self.assertNotIn(name, REMOTE_SAFE_SKILLS)

    def test_collected_skills_carry_no_local_only_references(self):
        collected = collect_remote_skills(_REPO_ROOT)
        self.assertTrue(collected, "원격에 보낼 skill 이 하나도 수집되지 않았다")
        # 로컬 overlay HTTP 포트는 리버스 터널에 실려 있지 않다.
        for name, body in collected.items():
            self.assertNotIn("17384", body, f"{name} 이 터널에 없는 로컬 포트를 참조한다")

    def test_skill_bodies_match_repo_source_verbatim(self):
        # 원격용으로 내용을 고쳐 보내면 로컬과 절차가 갈리고, 갈린 쪽이 조용히 낡는다.
        for name, body in collect_remote_skills(_REPO_ROOT).items():
            source = (_REPO_ROOT / ".github" / "skills" / name / "SKILL.md").read_text(
                encoding="utf-8"
            )
            self.assertEqual(body, source, f"{name} 본문이 리포 원본과 다르다")


class SessionStartScriptTests(unittest.TestCase):
    def test_posix_script_has_no_backend_dependency(self):
        script = render_session_start_script(remote_os="posix")
        self.assertTrue(script.startswith("#!/bin/sh"))
        self.assertIn(SESSIONSTART_HOOK_MARKER, script)
        self.assertIn("engram_get_context_once", script)
        # 원격에 백엔드가 없다. 스크립트가 무언가를 실행하려 들면 안 된다.
        for forbidden in ("engram-overlay", "engram_overlay_entry", "--role", "curl", "python"):
            self.assertNotIn(forbidden, script, f"hook 이 '{forbidden}' 에 의존한다")

    def test_posix_script_expands_cwd_at_run_time(self):
        script = render_session_start_script(remote_os="posix")
        self.assertIn("dir=$(pwd)", script)
        self.assertIn("cwd='$dir'", script)

    def test_windows_remote_reuses_the_local_powershell_script(self):
        from core.integrations.engram_bootstrap import render_session_start_powershell_script

        self.assertEqual(
            render_session_start_script(remote_os="windows"),
            render_session_start_powershell_script(),
        )

    def test_unknown_remote_os_is_rejected(self):
        with self.assertRaises(ValueError):
            build_remote_payload(_REPO_ROOT, remote_os="plan9")

    def test_payload_hook_command_matches_remote_os(self):
        posix = build_remote_payload(_REPO_ROOT, remote_os="posix")
        windows = build_remote_payload(_REPO_ROOT, remote_os="windows")
        self.assertTrue(posix["hook_relpath"].endswith(".sh"))
        self.assertIn("/bin/sh", posix["hook_command_template"])
        self.assertTrue(windows["hook_relpath"].endswith(".ps1"))
        self.assertIn("powershell", windows["hook_command_template"])


class RemoteInstallerTests(unittest.TestCase):
    def test_writes_skills_hook_and_registers_session_start(self):
        payload = build_remote_payload(_REPO_ROOT, remote_os="posix")
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            result = _run_installer(home, payload)
            self.assertIn("PROVISIONED", result.stdout, result.stderr)

            for name in payload["skills"]:
                skill_file = home / ".claude" / "skills" / name / "SKILL.md"
                self.assertTrue(skill_file.exists(), f"{name} 이 배치되지 않았다")

            hook_path = home / payload["hook_relpath"]
            self.assertTrue(hook_path.exists())
            self.assertEqual(len(_registered_commands(home / ".claude" / "settings.json")), 1)

    def test_merge_is_idempotent_and_keeps_user_entries(self):
        payload = build_remote_payload(_REPO_ROOT, remote_os="posix")
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            settings_path = home / ".claude" / "settings.json"
            settings_path.parent.mkdir(parents=True)
            settings_path.write_text(
                json.dumps(
                    {
                        "theme": "dark",
                        "hooks": {
                            "SessionStart": [
                                {"hooks": [{"type": "command", "command": "echo mine"}]},
                                # 구버전 engram 항목 — 걷어내고 새로 넣어야 한다.
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": f'/bin/sh "/old/{SESSIONSTART_HOOK_MARKER}.sh"',
                                        }
                                    ]
                                },
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )

            for _ in range(2):
                result = _run_installer(home, payload)
                self.assertIn("PROVISIONED", result.stdout, result.stderr)

            commands = _registered_commands(settings_path)
            self.assertEqual(len(commands), 1, "재실행이 항목을 쌓았다")
            self.assertNotIn("/old/", commands[0], "구버전 항목이 남았다")

            data = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(data["theme"], "dark", "사용자 설정이 유실됐다")
            own = [
                hook
                for entry in data["hooks"]["SessionStart"]
                for hook in (entry.get("hooks") or [])
                if hook.get("command") == "echo mine"
            ]
            self.assertEqual(len(own), 1, "사용자 자기 hook 이 유실됐다")

    def test_unparsable_user_settings_is_left_untouched(self):
        payload = build_remote_payload(_REPO_ROOT, remote_os="posix")
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            settings_path = home / ".claude" / "settings.json"
            settings_path.parent.mkdir(parents=True)
            settings_path.write_text("{ this is not json", encoding="utf-8")

            result = _run_installer(home, payload)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("SETTINGS=PARSE_FAIL", result.stdout)
            self.assertEqual(settings_path.read_text(encoding="utf-8"), "{ this is not json")

    def test_empty_payload_on_stdin_fails_loudly(self):
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            script = home / "_installer.py"
            script.write_text(render_remote_installer(), encoding="utf-8")
            env = dict(os.environ)
            env["HOME"] = str(home)
            env["USERPROFILE"] = str(home)
            result = subprocess.run(
                [sys.executable, str(script)],
                input="",
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("PAYLOAD=EMPTY", result.stdout)

    def test_payload_survives_a_bom_prefixed_stdin(self):
        # PowerShell → ssh 파이프가 선두에 BOM 을 붙일 수 있다. 이 리포가 이미 데인 자리다.
        payload = build_remote_payload(_REPO_ROOT, remote_os="posix")
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            script = home / "_installer.py"
            script.write_text(render_remote_installer(), encoding="utf-8")
            env = dict(os.environ)
            env["HOME"] = str(home)
            env["USERPROFILE"] = str(home)
            result = subprocess.run(
                [sys.executable, str(script)],
                input="﻿" + encode_payload(payload) + "\n",
                capture_output=True,
                text=True,
                env=env,
                encoding="utf-8",
            )
            self.assertIn("PROVISIONED", result.stdout, result.stderr)

    def test_payload_carries_no_token_material(self):
        # 토큰은 등록 단계에서 stdin 으로만 간다. 배치 payload 에 새면 안 된다.
        raw = base64.b64decode(encode_payload(build_remote_payload(_REPO_ROOT))).decode("utf-8")
        for forbidden in ("Authorization", "Bearer", "mcp-tokens"):
            self.assertNotIn(forbidden, raw, f"배치 payload 에 '{forbidden}' 가 들어 있다")


if __name__ == "__main__":
    unittest.main()


class SettingsBackupTests(unittest.TestCase):
    """settings.json 은 engram 전용 파일이 아니다 — 고치기 전에 백업을 남긴다."""

    def test_backup_is_written_before_merge(self):
        payload = build_remote_payload(_REPO_ROOT, remote_os="posix")
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            settings_path = home / ".claude" / "settings.json"
            settings_path.parent.mkdir(parents=True)
            original = json.dumps({"model": "opus", "theme": "dark"})
            settings_path.write_text(original, encoding="utf-8")

            result = _run_installer(home, payload)
            self.assertIn("PROVISIONED", result.stdout, result.stderr)

            backup = settings_path.with_name("settings.json.engram-bak")
            self.assertTrue(backup.exists(), "백업이 없다")
            # 백업은 병합 *전* 내용이어야 한다.
            self.assertEqual(backup.read_text(encoding="utf-8"), original)
            # 본 파일은 병합돼 hook 이 들어갔고 사용자 설정은 남아 있다.
            merged = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(merged["model"], "opus")
            self.assertEqual(len(_registered_commands(settings_path)), 1)

    def test_no_backup_needed_when_settings_absent(self):
        payload = build_remote_payload(_REPO_ROOT, remote_os="posix")
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            result = _run_installer(home, payload)
            self.assertIn("PROVISIONED", result.stdout, result.stderr)
            self.assertNotIn("BACKUP=", result.stdout)

    def test_merge_is_abandoned_when_backup_cannot_be_written(self):
        # 되돌릴 수 없는 변경을 하지 않는다.
        payload = build_remote_payload(_REPO_ROOT, remote_os="posix")
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            settings_path = home / ".claude" / "settings.json"
            settings_path.parent.mkdir(parents=True)
            original = json.dumps({"model": "opus"})
            settings_path.write_text(original, encoding="utf-8")
            # 백업 경로를 디렉토리로 점거해 write_bytes 가 실패하게 만든다.
            settings_path.with_name("settings.json.engram-bak").mkdir()

            result = _run_installer(home, payload)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("BACKUP=FAIL", result.stdout)
            self.assertEqual(settings_path.read_text(encoding="utf-8"), original)
