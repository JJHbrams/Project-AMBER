"""Exercise the real source host in an isolated Windows smoke profile.

Run after Enter-EngramBuildSmokeProfile. This is finite synthetic metadata/UI
evidence, not evidence of a paid model invocation or a physical desktop click.
Only windows owned by this process are captured. The production host is untouched.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def run(output: Path, asset_fault: str | None = None, mapping_fixture: Path | None = None, settings_probe: bool = False, expected_rect=None, expected_idle_pose=None) -> int:
    if os.name != "nt" or os.environ.get("ENGRAM_BUILD_SMOKE") != "1":
        raise RuntimeError("Requires Windows Enter-EngramBuildSmokeProfile isolation")
    profile = Path(os.environ["USERPROFILE"]).resolve()
    db = Path(os.environ["ENGRAM_SMOKE_DB_DIR"]).resolve()
    if profile not in db.parents or not profile.name.startswith("engram-build-smoke-"):
        raise RuntimeError("Unexpected smoke profile/DB boundary")
    mapping_source_hash = None
    if mapping_fixture:
        # Copy authorized input into the disposable old external location. The
        # source is never selected as the new writable native mapping target.
        mapping_bytes = mapping_fixture.read_bytes()
        mapping_source_hash = hashlib.sha256(mapping_bytes).hexdigest()
        target = profile / ".engram/overlays/bolttagu-2d/mapping.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(mapping_bytes)
        cfg_path = profile / ".engram/overlay.user.yaml"
        fixture_cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        fixture_cfg.setdefault("overlay", {})["external_renderer"] = {
            "selected_renderer_id": "engram.bolttagu-2d", "mode": "replace",
        }
        cfg_path.write_text(json.dumps(fixture_cfg), encoding="utf-8")
    from owned_probe_job import contain_current_probe
    contain_current_probe()
    # Never start an optional account integration inherited from the caller.
    os.environ.pop("DISCORD_BOT_TOKEN", None)
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    import win32process
    import win32gui
    from overlay.main import OverlayApp
    from overlay.bubble.bubble_window import _toplevel_hwnd
    from preview_session_stack import capture_hwnd

    output.mkdir(parents=True, exist_ok=True)
    if asset_fault:
        from functools import partial
        from overlay import native_bolttagu
        fault_dir = output / "fault-assets"
        fault_dir.mkdir(exist_ok=True)
        if asset_fault == "corrupt":
            (fault_dir / "atlas.json").write_text("{invalid-atlas", encoding="utf-8")
        native_bolttagu.load_atlas = partial(native_bolttagu.load_atlas, fault_dir)
    report = {
        "runtime": "source", "pid": os.getpid(), "source_root": str(ROOT),
        "profile": str(profile), "asset_fault": asset_fault, "checks": [], "snapshots": [],
        "mapping_source_sha256": mapping_source_hash,
        "limitations": [
            "Synthetic metadata, not provider lifecycle evidence",
            "Owned-window mouse input is exercised; original cursor position restored",
            "No prompt is sent to a model; paid conversation remains unverified",
            "Source probe does not establish frozen installer or reboot acceptance",
            "PrintWindow captures owned HWND only, not desktop compositor transparency",
        ],
    }
    app = None
    callback_errors = []
    native_activation_calls = []
    native_button_releases = []

    def check(name, condition):
        report["checks"].append({"name": name, "pass": bool(condition)})
        if not condition:
            raise AssertionError(name)

    def tick(seconds=.1):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.root.update()
            time.sleep(.01)
        check("no_tk_callback_errors", not callback_errors)

    def snapshot(name):
        hwnd = _toplevel_hwnd(app.character.root)
        check(name + "_host_owned_hwnd", win32process.GetWindowThreadProcessId(hwnd)[1] == os.getpid())
        check(name + "_visible", win32gui.IsWindowVisible(hwnd))
        captured = capture_hwnd(hwnd)
        target = output / (name + ".png")
        captured.save(target)
        row = {
            "stage": name, "hwnd": hwnd, "rect": list(win32gui.GetWindowRect(hwnd)),
            "state": app.character._sprite_model.state,
            "work_state": app.character._sprite_model.work_state,
            "native_view": type(getattr(app.character, "_native_bolttagu", None)).__name__,
            "selected": app._session_selection.selected_key,
            "pixel_sha256": hashlib.sha256(captured.tobytes()).hexdigest(),
            "capture": str(target),
        }
        native = getattr(app.character, "_native_bolttagu", None)
        if native is not None:
            row["animator_hint"] = native.animator.display_hint
            row["animator_pose"] = native.animator.pose
            row["drawn_recipe"] = native.drawn
        report["snapshots"].append(row)

    try:
        app = OverlayApp()
        original_activate = app.character.on_activate
        def record_native_activate():
            native_activation_calls.append(time.monotonic())
            return original_activate()
        app.character.on_activate = record_native_activate
        app.character._label.bind(
            "<ButtonRelease-1>",
            lambda _event: native_button_releases.append(time.monotonic()),
            add="+",
        )
        def callback_error(exc, value, tb):
            callback_errors.append("".join(traceback.format_exception(exc, value, tb)))
        app.root.report_callback_exception = callback_error
        tick(.5)
        if asset_fault:
            check("asset_failure_uses_local_fallback", app.character._native_bolttagu is None)
            app._set_presentation("full")
            tick(.5)
            snapshot("fallback-" + asset_fault)
            check("fallback_has_input_binding", bool(app.character._label.bind("<ButtonRelease-1>")))
            check("fallback_no_external_connection", not app._overlay_events.connected)
            app._set_presentation("launcher")
            tick(.5)
            snapshot("fallback-launcher")
            app._set_presentation("full")
            tick(.5)
            snapshot("fallback-expanded")
            report["result"] = "PASS: bounded real source asset-failure smoke"
            return 0
        check("native_view_loaded", getattr(app.character, "_native_bolttagu", None) is not None)
        check("no_external_renderer_connected", not app._overlay_events.connected)
        check("not_external_replace", app._overlay_events.mode != "replace")
        if mapping_fixture:
            from overlay.bolttagu_mapping import load_mapping
            expected = load_mapping(mapping_fixture)
            actual = app.character._native_bolttagu.mapping
            for section in ("hints", "categories", "oneshots", "lifecycle"):
                check("mapping_preserved_" + section, getattr(expected, section) == getattr(actual, section))
        app._set_presentation("full")
        tick(2)
        snapshot("native-idle")
        if expected_rect or expected_idle_pose:
            if expected_idle_pose:
                check('process_restart_preserves_edited_mapping', app.character._native_bolttagu.mapping.hints['idle'] == expected_idle_pose)
            if expected_rect:
                check("process_restart_preserves_anchor", list(app.character.get_phys_rect()) == list(expected_rect))
            report["result"] = "PASS: real source process restart checks"
            return 0
        for frame in range(12):
            tick(.15)
            snapshot(f"idle-sequence-{frame:02d}")
        hashes = {item["pixel_sha256"] for item in report["snapshots"] if item["stage"].startswith("idle-sequence-")}
        check("idle_animation_changes_pixels", len(hashes) > 1)
        discovery = json.loads((profile / ".engram/overlay-state-api-v1.json").read_text(encoding="utf-8"))
        sequence = {}

        def post(key, state, category=None):
            sequence[key] = sequence.get(key, 0) + 1
            payload = {
                "provider": "mcp", "session_id": key, "state": state,
                "agent_name": "Claude", "label": "Native animation " + key,
                "semantic": {
                    "seq": sequence[key], "active_category": category,
                    "events": [{"type": "tool.started", "category": category}] if category else [],
                },
            }
            request = Request(
                f"http://127.0.0.1:{app._stm_server.port}/state",
                data=json.dumps(payload).encode(),
                headers={"Authorization": "Bearer " + discovery["token"],
                         "Content-Type": "application/json", "X-Engram-Lifecycle": "claude"},
            )
            with urlopen(request, timeout=5) as response:
                check("metadata_http_" + key + "_" + str(sequence[key]), response.status == 200)
            app._refresh_session_stack(schedule=False)
            tick(.5)

        post("native-a", "working", "search")
        check("selected_search", app.character._sprite_model.work_state == "search")
        snapshot("selected-search")
        post("native-b", "working", "read")
        check("active_session_not_stolen", app._session_selection.selected_key == "mcp:native-a")
        app._session_selection.pin("mcp:native-b")
        app._refresh_session_stack(schedule=False)
        tick(.5)
        check("selection_updates_pose", app.character._sprite_model.work_state == "memory")
        snapshot("selected-read")
        post("native-b", "ready")
        snapshot("completed-transient")
        tick(2.5)
        app._refresh_session_stack(schedule=False)
        check("completed_returns_idle", app.character._sprite_model.work_state == "idle")
        snapshot("completed-idle")

        # Tk samples actual screen coordinates for drag. lParam-only PostMessage
        # is not a valid drag oracle. Move the pointer briefly, verify its target
        # before clicking, and always restore it (never type a model prompt).
        import win32api
        import win32con
        label_hwnd = app.character._label.winfo_id()
        check("input_target_owned", win32process.GetWindowThreadProcessId(label_hwnd)[1] == os.getpid())
        original_cursor = win32api.GetCursorPos()
        try:
            def aim():
                app.root.lift()
                tick(.1)
                left, top, right, bottom = win32gui.GetWindowRect(label_hwnd)
                point = ((left + right) // 2, top + (bottom - top) * 2 // 3)
                win32api.SetCursorPos(point)
                tick(.1)
                target = win32gui.WindowFromPoint(point)
                check("physical_input_target_label", target == label_hwnd)
                return point
            point = aim()
            start_rect = app.character.get_phys_rect()
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0)
            tick(.1)
            win32api.SetCursorPos((point[0] + 30, point[1] - 20))
            tick(.2)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0)
            tick(.3)
            check("native_mouse_drag_moves_window", app.character.get_phys_rect()[:2] != start_rect[:2])
            snapshot("native-dragged")
            aim()
            win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTDOWN, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0)
            tick(.3)
            check("native_right_click_menu", app.character._context_menu_open)
            app.character._dismiss_context_menu()
            tick(.1)
            existing_native = getattr(app, "_native_bubble_host", None)
            if existing_native is not None and existing_native.composer_visible:
                existing_native.hide()
            if app._bubble_input.is_showing():
                app._bubble_input.hide()
            tick(.1)
            aim()
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0)
            tick(.5)
            native_bubble = getattr(app, "_native_bubble_host", None)
            native_composer = bool(native_bubble and native_bubble.composer_visible)
            report["native_click"] = {
                "button_releases": len(native_button_releases),
                "activation_calls": len(native_activation_calls),
                "moved": app.character._moved,
                "native_failed": bool(getattr(app, "_native_bubble_failed", False)),
                "native_visible": bool(native_bubble and native_bubble.visible),
                "native_composer": native_composer,
                "fallback_visible": app._bubble_input.is_showing(),
            }
            check("native_click_invokes_activation", bool(native_activation_calls))
            check("native_click_opens_bubble_input", native_composer or app._bubble_input.is_showing())
            if native_composer:
                native_bubble.hide()
            else:
                app._bubble_input.hide()
        finally:
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0)
            win32api.SetCursorPos(original_cursor)
        # Feed finite fixtures through the same bubble owner/event adapter used
        # by real provider callbacks; no chat submission or model inference.
        owner = app._bubble_state
        if owner is None:
            # The native shell normally creates its provider owner when it
            # starts.  A shell-unavailable click uses the Tk fallback before
            # any submit, so create that same lazy owner for the synthetic
            # adapter fixtures below.
            app._ensure_bubble_session()
            owner = app._bubble_state
        check("bubble_owner_exists", owner is not None)
        owner.event("submit")
        app._refresh_session_stack(schedule=False)
        app._session_selection.pin("claude:" + owner.session_id)
        owner.event("tool_use")
        app._on_bubble_event({"kind": "tool_use", "tool_name": "WebSearch"})
        tick(.5)
        check("bubble_search_adapter", app.character._sprite_model.work_state == "search")
        snapshot("bubble-selected-search")
        app._session_selection.pin("mcp:native-b")
        app._refresh_session_stack(schedule=False)
        app._on_bubble_event({"kind": "tool_use", "tool_name": "Bash"})
        tick(.5)
        check("unselected_bubble_does_not_steal", app._session_selection.selected_key == "mcp:native-b")
        check("unselected_bubble_does_not_animate", app.character._sprite_model.work_state == "idle")
        app._session_selection.pin("claude:" + owner.session_id)
        owner.event("turn_end")
        app._on_bubble_event({"kind": "turn_end", "is_error": False})
        tick(2.5)
        app._refresh_session_stack(schedule=False)
        check("bubble_completed_returns_idle", app.character._sprite_model.work_state == "idle")
        snapshot("bubble-completed-idle")
        app._set_presentation("launcher")
        tick(2)
        snapshot("collapsed-launcher")
        app.hide_launcher_to_tray()
        tick(.2)
        check("tray_hide", not app.root.winfo_viewable())
        app.show_launcher_from_tray()
        tick(.2)
        snapshot("tray-restored")
        app._set_presentation("full")
        tick(2)
        snapshot("reexpanded-native")
        check("still_no_external_renderer", not app._overlay_events.connected)
        if settings_probe:
            from overlay.settings_window import _SettingsWindow
            settings = _SettingsWindow(app.root, on_saved=app._reload_config)
            tick(.5)
            check("gui_default_icon_selected", settings._char_source_mode_var.get() == "Engram 아이콘")
            check("gui_icon_hides_bolttagu_controls", not settings._bolttagu_native_box.winfo_manager())
            settings._char_source_mode_var.set("볼따구-의사")
            settings._apply_character_source_mode()
            tick(.1)
            check("gui_doctor_shows_bolttagu_controls", settings._bolttagu_native_box.winfo_manager() == "grid")
            settings._edit_bolttagu_mapping()
            tick(.5)
            editor = settings._bolttagu_editor
            editor_hwnd = _toplevel_hwnd(editor.window)
            check("editor_host_owned", win32process.GetWindowThreadProcessId(editor_hwnd)[1] == os.getpid())
            capture_hwnd(editor_hwnd).save(output / "mapping-editor.png")
            from unittest.mock import patch
            exported = output / 'editor-export.json'
            with patch('overlay.bolttagu_editor.filedialog.asksaveasfilename', return_value=str(exported)):
                editor._export()
            check('editor_export_document', json.loads(exported.read_text(encoding='utf-8')) == editor._collect())
            with patch('overlay.bolttagu_editor.filedialog.askopenfilename', return_value=str(exported)):
                editor._import()
            check('editor_import_roundtrip', editor._collect() == json.loads(exported.read_text(encoding='utf-8')))
            report['limitations'].append('Import/export native file picker paths injected; editor callbacks and files are real')
            editor.variables[("hints", "idle")].set("trickcal-listening")
            editor._save()
            tick(.1)
            prepared_path = Path(settings._bolttagu_mapping_var.get())
            check("editor_writes_owned_mapping", profile in prepared_path.resolve().parents)
            settings._do_save()
            app._reload_config()
            tick(.5)
            check("gui_mapping_applied", app.character._native_bolttagu.mapping.hints["idle"] == "trickcal-listening")
            # Recreate CharacterOverlay's view from persisted settings, not editor state.
            app.character.reload_config()
            check("mapping_survives_view_reload", app.character._native_bolttagu.mapping.hints["idle"] == "trickcal-listening")
            snapshot("gui-mapping-applied")
            settings._char_source_mode_var.set("Engram 아이콘")
            settings._apply_character_source_mode()
            settings._do_save()
            app._reload_config()
            tick(.5)
            check("gui_icon_selection_applied", type(app.character._native_bolttagu).__name__ == "EngramIconView")
            settings.window.destroy()
        original_rect = app.character.get_phys_rect()
        monitors = [win32api.GetMonitorInfo(handle)['Work'] for handle, _, _ in win32api.EnumDisplayMonitors()]
        report['monitor_work_areas'] = [list(rect) for rect in monitors]
        report['monitor_moves'] = []
        for index, work in enumerate(monitors):
            x, y = work[0] + 80, work[1] + 80
            win32gui.SetWindowPos(_toplevel_hwnd(app.root), 0, x, y, 0, 0,
                                 win32con.SWP_NOSIZE | win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
            tick(.15)
            app.character._reload_image_for_current_monitor()
            tick(.3)
            x, y, width, height = app.character.get_phys_rect()
            report['monitor_moves'].append({'work': list(work), 'actual': [x, y, width, height]})
            check(f'monitor_{index}_contained', work[0] <= x and work[1] <= y and x + width <= work[2] and y + height <= work[3])
            snapshot(f'monitor-{index}')
        x, y, _, _ = original_rect
        win32gui.SetWindowPos(_toplevel_hwnd(app.root), 0, x, y, 0, 0,
                             win32con.SWP_NOSIZE | win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
        tick(.2)
        app.character._reload_image_for_current_monitor()
        # Reload preserves the preceding monitor's bottom edge. Restore x/y
        # *after* resolving the target monitor size, then use the normal drag
        # release path to persist both window geometry and launcher-relative anchor.
        win32gui.SetWindowPos(_toplevel_hwnd(app.root), 0, x, y, 0, 0,
                             win32con.SWP_NOSIZE | win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
        tick(.2)
        original_cursor = win32api.GetCursorPos()
        try:
            point = aim()
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0)
            tick(.1)
            win32api.SetCursorPos((point[0] + 12, point[1]))
            tick(.1)
            win32api.SetCursorPos(point)
            tick(.1)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0)
            tick(.2)
        finally:
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0)
            win32api.SetCursorPos(original_cursor)
        tick(.5)
        snapshot('restart-anchor')
        report['restart_rect'] = list(app.character.get_phys_rect())
        report["result"] = "PASS: bounded source smoke only"
        return 0
    except Exception:
        report["result"] = "FAIL"
        report["error"] = traceback.format_exc()
        return 1
    finally:
        if mapping_fixture:
            report["mapping_original_unchanged"] = hashlib.sha256(mapping_fixture.read_bytes()).hexdigest() == mapping_source_hash
        report["callback_errors"] = callback_errors
        if app is not None:
            try:
                app._quit_reason = "native-bolttagu-isolated-probe"
                app.quit()
                report['animation_timer_after_quit'] = app.character._animation_after_id
                report['native_exit_deadline_after_quit'] = app.character._native_exit_deadline
                report['owned_hwnd_alive_after_quit'] = any(win32gui.IsWindow(item['hwnd']) for item in report['snapshots'])
            except Exception:
                report["cleanup_error"] = traceback.format_exc()
        cleanup_failed = bool(report.get('cleanup_error') or report.get('animation_timer_after_quit')
                              or report.get('native_exit_deadline_after_quit')
                              or report.get('owned_hwnd_alive_after_quit'))
        if cleanup_failed:
            report['result'] = 'FAIL: cleanup'
        (output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({"result": report.get("result"), "report": str(output / "report.json")}, ensure_ascii=False))
        if cleanup_failed:
            return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--asset-fault", choices=("missing", "corrupt"))
    parser.add_argument("--mapping-fixture", type=Path)
    parser.add_argument("--settings", action="store_true")
    parser.add_argument("--expected-rect", type=int, nargs=4)
    parser.add_argument("--expected-idle-pose")
    args = parser.parse_args()
    raise SystemExit(run(args.output.resolve(), args.asset_fault, args.mapping_fixture, args.settings, args.expected_rect, args.expected_idle_pose))
