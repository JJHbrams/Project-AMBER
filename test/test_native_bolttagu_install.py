import hashlib
import ast
import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from core.install.native_bolttagu import migrate_file, migrated_config, store_mapping, import_mapping
from core.install.overlay_manifest import input_files
from overlay.bolttagu_mapping import ASSET_DIR, validate_mapping_document, load_mapping
from overlay import config


class NativeMigrationTests(unittest.TestCase):
    def test_existing_external_only_exact_identity_migrates(self):
        original = {'overlay': {'character': {'source_mode': 'static', 'name': 'my.png', 'reactions': {'custom': 7}},
                                'external_renderer': {'selected_renderer_id': 'engram.bolttagu-2d', 'mode': 'replace'}}}
        result, changed = migrated_config(original)
        self.assertTrue(changed)
        self.assertEqual(result['overlay']['external_renderer']['selected_renderer_id'], '')
        self.assertEqual(result['overlay']['character']['name'], 'my.png')
        self.assertEqual(result['overlay']['character']['legacy_source_mode'], 'static')
        self.assertEqual(result['overlay']['character']['reactions'], {'custom': 7})
        self.assertEqual(original['overlay']['character']['source_mode'], 'static')

    def test_other_renderer_and_explicit_later_choice_preserved(self):
        document = {'overlay': {'external_renderer': {'selected_renderer_id': 'other.renderer', 'mode': 'replace'}}}
        result, _ = migrated_config(document)
        self.assertEqual(result['overlay']['external_renderer'], document['overlay']['external_renderer'])
        result['overlay']['external_renderer']['selected_renderer_id'] = 'engram.bolttagu-2d'
        repeated, changed = migrated_config(result)
        self.assertFalse(changed)
        self.assertEqual(repeated, result)

    def test_invalid_identity_mode_marker_and_shape_fail_closed(self):
        for overlay in ({'external_renderer': {'selected_renderer_id': 'vendor.bolttagu'}},
                        {'external_renderer': {'selected_renderer_id': 'engram.bolttagu-2d', 'mode': 'unknown'}},
                        {'native_bolttagu_migration': True}, {'character': []}):
            with self.subTest(overlay=overlay), self.assertRaises(ValueError):
                migrated_config({'overlay': overlay})

    def test_backup_atomic_idempotent_and_both_crash_points(self):
        for stage in ('backup', 'replace'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'overlay.user.yaml'
                original = b'overlay: {character: {name: custom.png, source_mode: static}}\n'
                path.write_bytes(original)
                def crash(current):
                    if current == stage:
                        raise RuntimeError('fixture crash')
                with self.assertRaises(RuntimeError):
                    migrate_file(path, checkpoint=crash)
                self.assertEqual(next(path.parent.glob('*.bak')).read_bytes(), original)
                if stage == 'backup':
                    self.assertEqual(path.read_bytes(), original)
                    self.assertTrue(migrate_file(path))
                else:
                    self.assertFalse(migrate_file(path))
                completed = path.read_bytes()
                self.assertFalse(migrate_file(path))
                self.assertEqual(path.read_bytes(), completed)

    def test_concurrent_config_edit_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'overlay.user.yaml'
            path.write_text('overlay: {}')
            def edit(stage):
                if stage == 'backup':
                    path.write_text('overlay: {hotkey: mine}')
            with self.assertRaises(RuntimeError):
                migrate_file(path, checkpoint=edit)
            self.assertEqual(path.read_text(), 'overlay: {hotkey: mine}')

    def test_auto_import_keeps_external_bytes_and_custom_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'overlay.user.yaml'
            path.write_text('overlay: {}')
            source = root / 'overlays/bolttagu-2d/mapping.json'
            source.parent.mkdir(parents=True)
            original = b'{"version":1,"hints":{"idle":"trickcal-idle"},"lifecycle":{"hide":"enter"}}'
            source.write_bytes(original)
            self.assertTrue(migrate_file(path))
            target = Path(yaml.safe_load(path.read_text())['overlay']['character']['bolttagu']['mapping_path'])
            self.assertNotEqual(target, source)
            self.assertEqual(source.read_bytes(), original)
            mapping = load_mapping(target)
            self.assertEqual(mapping.hints['idle'], 'trickcal-idle')
            self.assertEqual(mapping.lifecycle['hide'], 'enter')
            self.assertEqual(mapping.oneshots['success'], 'success')

    def test_mapping_validation_never_silently_drops_fields(self):
        for value in ({'version': 2}, {'unexpected': {}}, {'hints': {'idle': 'nonsense'}},
                      {'categories': {'search': 'waiting'}}, {'lifecycle': {'hide': None}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_mapping_document(value)

    def test_invalid_mapping_keeps_runtime_operable_without_yaml_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'overlay.user.yaml'
            original = b'overlay: {external_renderer: {selected_renderer_id: engram.bolttagu-2d, mode: replace}}'
            path.write_bytes(original)
            source = root / 'overlays/bolttagu-2d/mapping.json'
            source.parent.mkdir(parents=True)
            source.write_text('{"unsupported": {}}')
            with patch.object(config, '_USER_CONFIG_PATH', path), patch.object(config, '_STATE_PATH', root / 'state.yaml'):
                cfg = config.load_cfg(migrate_native=True)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(cfg['overlay']['character']['source_mode'], 'native_bolttagu')
            self.assertEqual(cfg['overlay']['external_renderer']['selected_renderer_id'], '')
            self.assertIn('이전 대기', cfg['overlay']['native_bolttagu_warning'])
            self.assertFalse(list(root.glob('*.bak')))

    def test_frozen_spec_collects_all_native_runtime_assets(self):
        root = Path(__file__).resolve().parents[1]
        tree = ast.parse((root / 'engram-overlay.spec').read_text(encoding='utf-8'))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_collect_character_datas')
        namespace = {'Path': Path}
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<asset-collector>', 'exec'), namespace)
        collected = {Path(source).resolve() for source, _ in namespace['_collect_character_datas']()}
        for asset in ASSET_DIR.iterdir():
            self.assertIn(asset, collected)

    def test_frozen_spec_includes_dynamically_opened_native_editor(self):
        spec = Path(__file__).resolve().parents[1] / 'engram-overlay.spec'
        source = spec.read_text(encoding='utf-8')
        for module in ('overlay.bolttagu_editor', 'overlay.native_bolttagu', 'overlay.bolttagu_mapping'):
            self.assertIn(repr(module), source)

    def test_mapping_owned_collision_and_original_preservation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.json'
            source.write_text('{"version": 1}')
            original = source.read_bytes()
            target = import_mapping(source, root / 'owned')
            self.assertEqual(import_mapping(source, root / 'owned'), target)
            self.assertEqual(source.read_bytes(), original)
            target.write_text('modified by user')
            with self.assertRaises(ValueError):
                import_mapping(source, root / 'owned')

    def test_interrupted_partial_write_never_publishes_and_retry_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_fdopen = os.fdopen
            class InterruptedWriter:
                def __init__(self, descriptor, mode):
                    self.stream = real_fdopen(descriptor, mode)
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    self.stream.close()
                def write(self, content):
                    self.stream.write(content[:len(content) // 2])
                    self.stream.flush()
                    raise RuntimeError('interrupted partial write')
            with patch('core.install.native_bolttagu.os.fdopen', InterruptedWriter):
                with self.assertRaises(RuntimeError):
                    store_mapping({'version': 1}, root)
            self.assertEqual(list(root.iterdir()), [])
            target = store_mapping({'version': 1}, root)
            self.assertEqual(json.loads(target.read_text()), {'version': 1})

    def test_failed_fsync_never_publishes_final_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('core.install.native_bolttagu.os.fsync', side_effect=OSError('fixture fsync failure')):
                with self.assertRaises(OSError):
                    store_mapping({'version': 1}, root)
            self.assertEqual(list(root.iterdir()), [])
            self.assertTrue(store_mapping({'version': 1}, root).is_file())

    def test_editor_validation_failure_does_not_create_toplevel(self):
        from overlay.bolttagu_editor import BolttaguMappingEditor
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'invalid.json'
            for content in ('{', '{"unsupported": {}}'):
                path.write_text(content)
                with patch('overlay.bolttagu_editor.tk.Toplevel') as window:
                    with self.assertRaises(ValueError):
                        BolttaguMappingEditor(None, path, root, lambda _path: None)
                    window.assert_not_called()

    def test_editor_sparse_collection_preserves_hidden_oneshot(self):
        from overlay.bolttagu_editor import sparse_mapping_document
        from overlay.bolttagu_mapping import sprite_map
        schema = sprite_map()
        document = {
            'version': 1,
            'hints': {'idle': 'trickcal-idle'},
            'oneshots': {'success': 'trickcal-success'},
        }
        selections = {
            (section.key, row.key): row.default[0] if row.default else None
            for section in schema.sections if not section.hidden for row in section.rows
        }
        selections[('hints', 'idle')] = 'trickcal-listening'
        result = sparse_mapping_document(schema, document, selections)
        self.assertEqual(result, {
            'version': 1,
            'hints': {'idle': 'trickcal-listening'},
            'oneshots': {'success': 'trickcal-success'},
        })
        with tempfile.TemporaryDirectory() as directory:
            path = store_mapping(result, Path(directory))
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), result)

    def test_editor_sparse_collection_does_not_invent_hidden_overrides(self):
        from overlay.bolttagu_editor import sparse_mapping_document
        from overlay.bolttagu_mapping import sprite_map
        schema = sprite_map()
        selections = {
            (section.key, row.key): row.default[0] if row.default else None
            for section in schema.sections if not section.hidden for row in section.rows
        }
        self.assertEqual(
            sparse_mapping_document(schema, {'version': 1}, selections),
            {'version': 1},
        )

    def test_actual_tk_invalid_editor_leaves_no_child_window(self):
        import tkinter as tk
        from overlay.bolttagu_editor import BolttaguMappingEditor
        window = tk.Tk()
        window.withdraw()
        try:
            before = window.winfo_children()
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'invalid.json'
                path.write_text('{"hints": {"idle": "not-a-pose"}}')
                with self.assertRaises(ValueError):
                    BolttaguMappingEditor(window, path, Path(directory), lambda _path: None)
                window.update_idletasks()
                self.assertEqual(window.winfo_children(), before)
        finally:
            window.destroy()

    def test_actual_tk_editor_preview_updates_and_cancels_its_timer(self):
        import tkinter as tk
        from overlay.bolttagu_editor import BolttaguMappingEditor
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f'Tk display unavailable: {exc}')
        root.withdraw()
        editor = None
        try:
            with tempfile.TemporaryDirectory() as directory:
                editor = BolttaguMappingEditor(root, None, Path(directory), lambda _path: None)
                root.update()
                first_photo = str(editor._preview_photo)
                editor.variables[('hints', 'idle')].set('writing')
                editor._select_preview(('hints', 'idle'))
                root.update()
                self.assertEqual(editor._preview_option, 'writing')
                self.assertNotEqual(str(editor._preview_photo), first_photo)
                self.assertIsNotNone(editor._preview_after_id)
                editor.window.destroy()
                root.update()
                self.assertIsNone(editor._preview_after_id)
                editor = None
        finally:
            if editor is not None and editor.window.winfo_exists():
                editor.window.destroy()
            root.destroy()

    def test_all_packaged_assets_are_hashed_build_inputs(self):
        root = Path(__file__).resolve().parents[1]
        inputs = set(input_files(root))
        manifest = json.loads((ASSET_DIR / 'provenance.json').read_text())
        self.assertEqual(len(manifest['files']), 31)
        for record in manifest['files']:
            path = ASSET_DIR / record['name']
            self.assertIn(path, inputs)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), record['sha256'])


if __name__ == '__main__':
    unittest.main()
