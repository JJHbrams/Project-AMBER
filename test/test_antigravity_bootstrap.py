import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core.integrations import engram_bootstrap as bootstrap


def test_antigravity_migrates_only_engram_legacy_hook_and_is_idempotent():
    with TemporaryDirectory() as tmp:
        home = Path(tmp)
        legacy = home / ".gemini" / "settings.json"
        hooks = home / ".gemini" / "config" / "hooks.json"
        legacy.parent.mkdir(parents=True); hooks.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"theme":"dark","mcpServers":{"engram":{"type":"http","url":"http://127.0.0.1:17385/mcp"},"custom":{"url":"https://example.test/mcp"}},"hooks":{"BeforeTool":[{"hooks":[{"command":"user-hook"}]},{"hooks":[{"command":"engram-gemini-pretool-hook"}]}]}}), encoding="utf-8")
        hooks.write_text(json.dumps({"orca-status": {"PreToolUse": []}, "user": {"PreToolUse": []}}), encoding="utf-8")
        paths = dict(bootstrap._PROVIDER_HOOK_SCRIPT_PATHS)
        paths["antigravity"] = (home / ".engram" / "anti.ps1", home / ".engram" / "anti.sh")
        paths["gemini"] = (home / ".engram" / "legacy.ps1", home / ".engram" / "legacy.sh")
        legacy_shim = home / ".engram" / "engram-gemini.cmd"
        legacy_shim.parent.mkdir(parents=True)
        legacy_shim.write_text("@echo off\nsetlocal EnableDelayedExpansion\nset \"ENGRAM_DB_DIR=x\"\ngemini --allowed-mcp-server-names engram\n", encoding="utf-8")
        with patch.object(bootstrap, "_ENGRAM_DIR", home / ".engram"), patch.object(bootstrap, "_GEMINI_SETTINGS_PATH", legacy), patch.object(bootstrap, "_ANTIGRAVITY_HOOKS_PATH", hooks), patch.object(bootstrap, "_PROVIDER_HOOK_SCRIPT_PATHS", paths), patch.object(bootstrap, "_GEMINI_PRETOOL_HOOK_SCRIPT_PATH", paths["gemini"][0]), patch.object(bootstrap, "_GEMINI_PRETOOL_HOOK_POSIX_PATH", paths["gemini"][1]):
            assert bootstrap.sync_antigravity_pretool_hook(True)["ok"]
            on_legacy = json.loads(legacy.read_text(encoding="utf-8"))
            on_hooks = json.loads(hooks.read_text(encoding="utf-8"))
            assert on_legacy["hooks"]["BeforeTool"][0]["hooks"][0]["command"] == "user-hook"
            assert "engram" in on_legacy["mcpServers"] and "custom" in on_legacy["mcpServers"]
            assert set(on_hooks) == {"orca-status", "user", "engram-antigravity-pretool-hook"}
            handler = on_hooks["engram-antigravity-pretool-hook"]["PreToolUse"][0]["hooks"][0]
            expected_relative = str(Path("..") / ".." / ".engram" / "anti.ps1")
            assert expected_relative in handler["command"]
            assert '"' not in handler["command"]
            assert legacy.with_name("settings.json.engram-bak").exists()
            assert hooks.with_name("hooks.json.engram-bak").exists()
            assert not legacy_shim.exists()
            assert not bootstrap.sync_antigravity_pretool_hook(True)["changed"]
            assert bootstrap.sync_antigravity_pretool_hook(False)["ok"]
        migrated = json.loads(legacy.read_text(encoding="utf-8"))
        assert migrated["theme"] == "dark"
        assert migrated["hooks"]["BeforeTool"][0]["hooks"][0]["command"] == "user-hook"
        assert "engram" in migrated["mcpServers"] and "custom" in migrated["mcpServers"]
        assert set(json.loads(hooks.read_text(encoding="utf-8"))) == {"orca-status", "user"}
        assert "engram-antigravity-pretool-hook" not in json.loads(hooks.read_text(encoding="utf-8"))


def test_antigravity_malformed_user_state_is_never_clobbered_or_scripted():
    with TemporaryDirectory() as tmp:
        home = Path(tmp)
        hooks = home / ".gemini" / "config" / "hooks.json"
        hooks.parent.mkdir(parents=True)
        original = b"{ malformed user hooks"
        hooks.write_bytes(original)
        paths = dict(bootstrap._PROVIDER_HOOK_SCRIPT_PATHS)
        paths["antigravity"] = (home / ".engram" / "anti.ps1", home / ".engram" / "anti.sh")
        with patch.object(bootstrap, "_ENGRAM_DIR", home / ".engram"), patch.object(bootstrap, "_GEMINI_SETTINGS_PATH", home / ".gemini" / "settings.json"), patch.object(bootstrap, "_ANTIGRAVITY_HOOKS_PATH", hooks), patch.object(bootstrap, "_PROVIDER_HOOK_SCRIPT_PATHS", paths):
            result = bootstrap.sync_antigravity_pretool_hook(True)
        assert not result["ok"]
        assert hooks.read_bytes() == original
        assert not paths["antigravity"][0].exists()


def test_antigravity_keeps_custom_named_engram_mcp_and_unowned_legacy_shim():
    with TemporaryDirectory() as tmp:
        home = Path(tmp)
        legacy = home / ".gemini" / "settings.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"mcpServers": {"engram": {"url": "https://custom.example/mcp"}}}), encoding="utf-8")
        paths = dict(bootstrap._PROVIDER_HOOK_SCRIPT_PATHS)
        paths["antigravity"] = (home / ".engram" / "anti.ps1", home / ".engram" / "anti.sh")
        shim = home / ".engram" / "engram-gemini.cmd"
        shim.parent.mkdir(parents=True); shim.write_text("my private gemini launcher", encoding="utf-8")
        with patch.object(bootstrap, "_ENGRAM_DIR", home / ".engram"), patch.object(bootstrap, "_GEMINI_SETTINGS_PATH", legacy), patch.object(bootstrap, "_ANTIGRAVITY_HOOKS_PATH", home / ".gemini" / "config" / "hooks.json"), patch.object(bootstrap, "_PROVIDER_HOOK_SCRIPT_PATHS", paths):
            assert bootstrap.sync_antigravity_pretool_hook(True)["ok"]
        assert "engram" in json.loads(legacy.read_text(encoding="utf-8"))["mcpServers"]
        assert shim.exists()


def test_antigravity_mcp_sync_preserves_user_servers_and_is_independent_of_hooks():
    with TemporaryDirectory() as tmp:
        home = Path(tmp)
        legacy = home / ".gemini" / "settings.json"
        current = home / ".gemini" / "config" / "mcp_config.json"
        legacy.parent.mkdir(parents=True); current.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"mcpServers": {"engram": {"url": "http://127.0.0.1:17385/mcp"}, "custom": {"url": "https://custom.test"}}, "theme": "dark"}), encoding="utf-8")
        current.write_text(json.dumps({"mcpServers": {"other": {"serverUrl": "https://other.test", "disabled": True}}, "orca": {"keep": True}}), encoding="utf-8")
        with patch.object(bootstrap, "_GEMINI_SETTINGS_PATH", legacy), patch.object(bootstrap, "_ANTIGRAVITY_MCP_CONFIG_PATH", current):
            first = bootstrap.sync_antigravity_mcp_config()
            second = bootstrap.sync_antigravity_mcp_config()
        saved = json.loads(current.read_text(encoding="utf-8"))
        old = json.loads(legacy.read_text(encoding="utf-8"))
        assert first == {"ok": True, "changed": True}
        assert second == {"ok": True, "changed": False}
        assert saved["orca"] == {"keep": True}
        assert saved["mcpServers"]["other"]["disabled"] is True
        assert saved["mcpServers"]["engram"] == {"disabled": False, "serverUrl": "http://127.0.0.1:17385/mcp"}
        assert old["theme"] == "dark" and old["mcpServers"] == {"custom": {"url": "https://custom.test"}}
        assert current.with_name("mcp_config.json.engram-bak").exists()


def test_antigravity_mcp_sync_leaves_malformed_files_untouched():
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / ".gemini" / "config" / "mcp_config.json"
        path.parent.mkdir(parents=True); path.write_bytes(b"{ bad")
        with patch.object(bootstrap, "_ANTIGRAVITY_MCP_CONFIG_PATH", path), patch.object(bootstrap, "_GEMINI_SETTINGS_PATH", Path(tmp) / ".gemini" / "settings.json"):
            result = bootstrap.sync_antigravity_mcp_config()
        assert not result["ok"]
        assert path.read_bytes() == b"{ bad"


def test_antigravity_mcp_sync_repairs_empty_agy_placeholder_with_backup():
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / ".gemini" / "config" / "mcp_config.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"")
        with patch.object(bootstrap, "_ANTIGRAVITY_MCP_CONFIG_PATH", path), patch.object(
            bootstrap, "_GEMINI_SETTINGS_PATH", Path(tmp) / ".gemini" / "settings.json"
        ):
            result = bootstrap.sync_antigravity_mcp_config()
        assert result == {"ok": True, "changed": True}
        assert json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["engram"] == {
            "disabled": False,
            "serverUrl": "http://127.0.0.1:17385/mcp",
        }
        assert path.with_name("mcp_config.json.engram-bak").read_bytes() == b""
