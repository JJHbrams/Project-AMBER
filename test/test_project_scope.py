import tempfile
import unittest
from pathlib import Path

from core.context.project_scope import detect_project_root, resolve_project_key, resolve_scope_key


class ProjectScopeTests(unittest.TestCase):
    def test_explicit_project_key_uses_project_scope_prefix(self):
        self.assertEqual(resolve_scope_key(project_key="My Great Project"), "project:my-great-project")

    def test_detect_project_root_walks_up_to_git_dir(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "demo-repo"
            nested = root / "src" / "feature"
            (root / ".git").mkdir(parents=True)
            nested.mkdir(parents=True)

            detected = detect_project_root(str(nested))

            self.assertEqual(detected, root.resolve())

    def test_resolve_project_key_uses_root_name_and_hash(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "Project Intel"
            nested = root / "app"
            (root / ".git").mkdir(parents=True)
            nested.mkdir(parents=True)

            project_key = resolve_project_key(cwd=str(nested))

            self.assertRegex(project_key, r"^project-intel-[0-9a-f]{8}$")

    def test_resolve_scope_key_falls_back_to_global_when_no_project_detected(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            self.assertEqual(resolve_scope_key(cwd=tmp_dir), "global:main")


if __name__ == "__main__":
    unittest.main()

