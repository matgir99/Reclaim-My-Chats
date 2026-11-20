"""scripts/upstream_watch.py: parsing pinned revisions and the report (offline)."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'upstream_watch.py'
spec = importlib.util.spec_from_file_location('upstream_watch', SCRIPT)
assert spec is not None and spec.loader is not None
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)

OLD = 'a' * 40
NEW = 'b' * 40


class TestUpstreamWatch(unittest.TestCase):
    def test_a_later_review_replaces_the_pinned_revision(self):
        text = (f'| [x, aaaa](https://github.com/owner/project/tree/{OLD}) |\n'
                f'| [y](https://github.com/other/tool/blob/{OLD}/export.py) |\n'
                f'## November\n| [x](https://github.com/owner/project/tree/{NEW}) |\n'
                'https://github.com/owner/no-revision')
        self.assertEqual(watch.pinned(text), {'owner/project': NEW, 'other/tool': OLD})

    def test_the_review_document_pins_every_reference_project(self):
        pins = watch.pinned(watch.REVIEW.read_text(encoding='utf-8'))
        self.assertGreaterEqual(len(pins), 10)
        self.assertIn('pionxzh/chatgpt-exporter', pins)
        self.assertTrue(all(len(sha) == 40 for sha in pins.values()))

    def test_report_links_each_comparison(self):
        body = watch.report([{'repo': 'owner/project', 'sha': OLD, 'branch': 'main',
                              'ahead': 3, 'latest': '2026-11-10'}])
        self.assertIn('| [owner/project](https://github.com/owner/project) | `aaaaaaa` | 3 |', body)
        self.assertIn(f'(https://github.com/owner/project/compare/{OLD}...main)', body)


if __name__ == '__main__':
    unittest.main()
