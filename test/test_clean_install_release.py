import unittest
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from overlay.bolttagu_editor import editor_geometry
from overlay.bolttagu_mapping import load_mapping, resolve, sprite_map
from overlay.settings_window import settings_product_identity


class CleanInstallReleaseTests(unittest.TestCase):
    def test_product_default_mapping_is_complete_and_explicit_override_wins(self):
        mapping = load_mapping(None)
        self.assertEqual(mapping.hints, {
            'default': 'trickcal-idle', 'idle': 'trickcal-idle', 'success': 'trickcal-idle',
            'hover': 'trickcal-alert', 'click': 'trickcal-alert', 'input': 'trickcal-listening',
            'generating': 'trickcal-speaking', 'thought': 'trickcal-wondering', 'search': 'trickcal-searching',
            'memory': 'trickcal-searching', 'error': 'trickcal-error', 'provider_error': 'trickcal-error',
        })
        self.assertEqual(mapping.categories, {'write': 'trickcal-writing', 'execute': 'trickcal-waiting', 'read': 'trickcal-searching', 'communication': 'speaking', 'other': 'trickcal-tool_use'})
        self.assertEqual(mapping.lifecycle, {'show': 'trickcal-enter', 'hide': 'enter'})
        self.assertEqual(mapping.oneshots, {'success': 'success'})
        schema = sprite_map()
        with patch('overlay.bolttagu_mapping.Path.is_file', return_value=True), patch(
            'overlay.bolttagu_mapping.Path.read_text',
            return_value='{"version":1,"categories":{"other":"speaking"},"lifecycle":{"hide":"exit"}}',
        ):
            override = resolve(schema, Path('mapping.json'))
        self.assertEqual(override['categories']['other'], ('speaking',))
        self.assertEqual(override['lifecycle']['hide'], ('exit',))

    def test_settings_identity_uses_four_part_resolver(self):
        with patch('overlay.settings_window.resolve_version') as resolve:
            resolve.return_value.version = '1.2.3.4'
            self.assertEqual(settings_product_identity(), 'AMBER (ENGRAM) 1.2.3.4 · DRTECH')

    def test_editor_reserves_footer_and_clamps_short_screen(self):
        self.assertEqual(editor_geometry(1920, 768), ('860x648', (720, 620)))
        source = (ROOT / 'overlay/bolttagu_editor.py').read_text(encoding='utf-8')
        self.assertLess(source.index("actions.pack(side='bottom'"), source.index("notebook.pack(side='top'"))

    def test_installer_packages_complete_skills_and_codex_root(self):
        iss = (ROOT / 'installer/engram-overlay.iss').read_text(encoding='utf-8-sig')
        configure = (ROOT / 'installer/configure.ps1').read_text(encoding='utf-8-sig')
        self.assertIn('..\\.github\\skills\\*', iss)
        self.assertIn('$codexRoot = Join-Path $env:USERPROFILE ".codex"', configure)
        self.assertIn('$codexSkillNames = @("engram-connect", "engram-hook-trust")', configure)
        for skill in ('engram-connect', 'engram-hook-trust'):
            self.assertTrue((ROOT / '.github/skills' / skill).is_dir())
            self.assertTrue((ROOT / '.github/skills' / skill / 'SKILL.md').is_file())


if __name__ == '__main__':
    unittest.main()
