"""Git-level tests for automatic version choice and reruns."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'next_version.py'


class TestNextVersion(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.commit_count = 0
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        self.commit()
        self.git('tag', 'v3.2.0')

    def git(self, *args: str) -> str:
        return subprocess.check_output(['git', *args], cwd=self.root, text=True).strip()

    def commit(self):
        self.commit_count += 1
        self.git('commit', '--allow-empty', '-qm', f'test {self.commit_count}')

    def version(self, *args: str):
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=self.root,
                              text=True, capture_output=True, check=False)

    def test_first_gui_release_then_patch_and_rerun(self):
        self.commit()
        self.assertEqual(self.version().stdout.strip(), '3.3.0')
        self.git('tag', 'v3.3.0')
        self.assertEqual(self.version().stdout.strip(), '3.3.0')
        self.commit()
        self.assertEqual(self.version().stdout.strip(), '3.3.1')

    def test_collision_with_tag_at_other_commit_fails(self):
        self.commit()
        self.git('tag', 'v3.3.0')
        self.git('reset', '--hard', 'HEAD~1')
        self.commit()
        result = self.version()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('already points at another commit', result.stderr)

    def test_stale_head_fails(self):
        self.commit()
        result = self.version('--require-ref', 'HEAD~1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('stale commit', result.stderr)


if __name__ == '__main__':
    unittest.main()
