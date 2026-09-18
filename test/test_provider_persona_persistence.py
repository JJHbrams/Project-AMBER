"""폐기된 persona 파일 블록의 **제거** 경로만 검증한다.

쓰기 경로는 없앴다. 정체성은 `engram_get_context` 응답으로만 전달하며, engram 이
안 붙은 세션에는 페르소나가 없는 게 맞다. 여기 남은 관심사는 하나다 — 이미 배포된
블록을 사람 파일을 다치지 않고 걷어내는가.
"""
import tempfile
import unittest
from pathlib import Path

from core.identity import provider_persona as subject

# subject.BEGIN/END 는 codex 마커다. claude 파일에는 claude 마커를 써야 한다.
CLAUDE_BEGIN, CLAUDE_END = subject._PROVIDERS['claude'][2:]


class ProviderPersonaRemovalTests(unittest.TestCase):
    def test_no_write_path_exists(self):
        """쓰기 함수가 되살아나면 실패한다 — 이 모듈은 지우기만 한다."""
        for name in ("render", "sync", "sync_all"):
            self.assertFalse(hasattr(subject, name), f"쓰기 경로 {name} 가 되살아났다")

    def test_block_is_removed_and_surrounding_bytes_survive(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory); root = home / '.codex'; root.mkdir()
            path = root / 'AGENTS.md'
            path.write_text('keep before\n' + subject.BEGIN + '\nname: 므네마\n' + subject.END + '\nkeep after\n',
                            encoding='utf-8')
            status = subject.remove(provider='codex', home=home)
            self.assertEqual(status['state'], 'removed')
            self.assertTrue(status['applied'])
            self.assertEqual(path.read_text(encoding='utf-8'), 'keep before\nkeep after\n')

    def test_removal_is_idempotent_and_reports_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory); root = home / '.codex'; root.mkdir()
            path = root / 'AGENTS.md'
            path.write_text('user instructions\n' + subject.BEGIN + '\nx\n' + subject.END + '\n', encoding='utf-8')
            subject.remove(provider='codex', home=home)
            second = subject.remove(provider='codex', home=home)
            self.assertEqual(second['state'], 'absent')
            self.assertFalse(second['changed'])
            self.assertFalse(second['applied'])
            self.assertEqual(path.read_text(encoding='utf-8'), 'user instructions\n')

    def test_file_without_block_is_left_byte_identical(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory); root = home / '.claude'; root.mkdir()
            path = root / 'CLAUDE.md'; original = 'only the user instructions\n'
            path.write_text(original, encoding='utf-8')
            status = subject.remove(provider='claude', home=home)
            self.assertEqual(status['state'], 'absent')
            self.assertEqual(path.read_text(encoding='utf-8'), original)

    def test_missing_provider_file_is_never_created(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory); (home / '.claude').mkdir()
            status = subject.remove(provider='claude', home=home)
            self.assertEqual(status['state'], 'absent')
            self.assertFalse((home / '.claude' / 'CLAUDE.md').exists())

    def test_removal_backs_up_the_previous_content(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory); root = home / '.claude'; root.mkdir()
            path = root / 'CLAUDE.md'
            before = 'keep\n' + CLAUDE_BEGIN + '\nname: 므네마\n' + CLAUDE_END + '\n'
            path.write_text(before, encoding='utf-8')
            subject.remove(provider='claude', home=home)
            backups = list(root.glob('CLAUDE.md.engram-persona-backup-*'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding='utf-8'), before)

    def test_dry_run_reports_without_touching_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory); root = home / '.claude'; root.mkdir()
            path = root / 'CLAUDE.md'
            before = 'keep\n' + CLAUDE_BEGIN + '\nx\n' + CLAUDE_END + '\n'
            path.write_text(before, encoding='utf-8')
            status = subject.remove(provider='claude', home=home, apply=False)
            self.assertTrue(status['changed'])
            self.assertFalse(status['applied'])
            self.assertEqual(path.read_text(encoding='utf-8'), before)
            self.assertEqual(list(root.glob('*backup*')), [])

    def test_invalid_markers_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory); root = home / '.codex'; root.mkdir()
            path = root / 'AGENTS.md'; before = subject.BEGIN + '\n' + subject.BEGIN
            path.write_text(before, encoding='utf-8')
            with self.assertRaises(ValueError):
                subject.remove(provider='codex', home=home)
            self.assertEqual(path.read_text(encoding='utf-8'), before)

    def test_remove_all_isolates_one_broken_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            broken = home / '.codex'; broken.mkdir()
            (broken / 'AGENTS.md').write_text(subject.BEGIN + '\n' + subject.BEGIN, encoding='utf-8')
            good = home / '.claude'; good.mkdir()
            (good / 'CLAUDE.md').write_text('keep\n' + CLAUDE_BEGIN + '\nx\n' + CLAUDE_END + '\n', encoding='utf-8')
            statuses = subject.remove_all(home=home)
            self.assertEqual(statuses['codex']['state'], 'error')
            self.assertTrue(statuses['claude']['applied'])
            self.assertEqual((good / 'CLAUDE.md').read_text(encoding='utf-8'), 'keep\n')

    def test_symlinked_provider_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory); root = home / '.claude'; root.mkdir()
            target = home / 'elsewhere.md'; target.write_text('x\n', encoding='utf-8')
            try:
                (root / 'CLAUDE.md').symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest('symlink 생성 권한 없음')
            with self.assertRaises(ValueError):
                subject.remove(provider='claude', home=home)


if __name__ == '__main__': unittest.main()
