"""scripts/upstream_watch.py: pinned revisions, dependency releases and the report (offline)."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'upstream_watch.py'
spec = importlib.util.spec_from_file_location('upstream_watch', SCRIPT)
assert spec is not None and spec.loader is not None
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)

OLD = 'a' * 40
NEW = 'b' * 40
TODAY = date(2026, 11, 20)


def files(day: str, yanked: bool = False) -> list[dict]:
    return [{'upload_time_iso_8601': f'{day}T12:00:00.000000Z', 'yanked': yanked}]


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
                              'ahead': 3, 'latest': '2026-11-10'}], [])
        self.assertIn('| [owner/project](https://github.com/owner/project) | `aaaaaaa` | 3 |', body)
        self.assertIn(f'(https://github.com/owner/project/compare/{OLD}...main)', body)
        self.assertNotIn('Dependency releases', body)


class TestDependencyReleases(unittest.TestCase):
    def test_a_newer_flet_minor_is_reported_at_once_with_the_newest_release(self):
        releases = {'1.0.5': files('2026-10-21'), '1.0.6': files('2026-10-30'),
                    '1.1.0.dev4': files('2026-11-03'), '1.1.0': files('2026-11-05'),
                    '1.1.2': files('2026-11-06')}
        row = watch.newer_release('flet', '1.0.5', releases, date(2026, 11, 5))
        self.assertEqual(row and (row['newest'], row['released']), ('1.1.2', '2026-11-06'))

    def test_patch_releases_and_pre_releases_are_left_to_dependabot(self):
        releases = {'1.0.6': files('2026-10-30'), '1.1.0.dev4': files('2026-11-03'),
                    '1.1.0rc1': files('2026-11-04'), '1.1.0.post1': files('2026-11-04')}
        self.assertIsNone(watch.newer_release('flet', '1.0.5', releases, TODAY))

    def test_a_yanked_release_is_not_reported(self):
        releases = {'1.1.0': files('2026-11-05', yanked=True)}
        self.assertIsNone(watch.newer_release('flet', '1.0.5', releases, TODAY))

    def test_playwright_is_reported_only_when_dependabot_is_overdue(self):
        releases = {'1.64.1': files('2026-10-22'), '1.65.0': files('2026-11-09')}
        self.assertIsNone(watch.newer_release('playwright', '1.64.1', releases, TODAY))
        row = watch.newer_release('playwright', '1.64.1', releases, date(2026, 12, 24))
        self.assertEqual(row and row['newest'], '1.65.0')

    def test_overdue_counts_from_the_first_newer_release(self):
        releases = {'1.65.0': files('2026-09-01'), '1.66.0': files('2026-11-15')}
        row = watch.newer_release('playwright', '1.64.1', releases, TODAY)
        self.assertEqual(row and row['newest'], '1.66.0')

    def test_the_lock_installs_every_watched_package(self):
        locked = watch.locked_versions(watch.LOCK.read_text(encoding='utf-8'))
        self.assertTrue(all(len(set(locked.get(name, []))) == 1 for name in watch.WATCHED))
        self.assertNotIn('reclaimmychats', locked)  # its version comes from git tags

    def test_a_watched_package_missing_or_locked_twice_stops_the_watch(self):
        with self.assertRaisesRegex(ValueError, 'installs flet nowhere'):
            watch.releases_behind({'playwright': ['1.65.2']}, TODAY)
        forked = {'flet': ['1.0.5', '1.1.2'], 'playwright': ['1.65.2', '1.65.2']}
        with self.assertRaisesRegex(ValueError, 'installs flet 1.0.5 and 1.1.2'):
            watch.releases_behind(forked, TODAY)

    def test_a_package_locked_for_two_platforms_keeps_both_versions(self):
        text = ('[[package]]\nname = "x"\nversion = "1.0"\n'
                '[[package]]\nname = "x"\nversion = "2.0"\n'
                '[[package]]\nname = "reclaimmychats"\nsource = { editable = "." }\n')
        self.assertEqual(watch.locked_versions(text), {'x': ['1.0', '2.0']})

    def test_report_lists_releases_on_their_own(self):
        body = watch.report([], [{'package': 'flet', 'locked': '1.0.5', 'newest': '1.1.2',
                                  'released': '2026-11-06', 'notes': 'https://example.org/n'}])
        self.assertTrue(body.startswith('## Dependency releases'))
        self.assertIn('| flet | 1.0.5 | 1.1.2 | 2026-11-06 | [release notes](https://example.org/n) |',
                      body)


class TestIssue(unittest.TestCase):
    def run_gh(self, open_issues: dict[str, list[dict]]) -> tuple[str, list[list[str]]]:
        calls = []

        def fake(args, **_kwargs):
            calls.append(args)
            if args[:3] == ['gh', 'issue', 'list']:
                title = args[args.index('--search') + 1].split('"')[1]
                listed = [*open_issues.get(title, []), {'number': 99, 'title': title + ' (x)'}]
                return subprocess.CompletedProcess(args, 0, json.dumps(listed))
            return subprocess.CompletedProcess(args, 0, 'https://github.com/o/r/issues/12\n')

        with mock.patch.object(watch.subprocess, 'run', side_effect=fake):
            result = watch.update_issue('body')
        return result, calls

    def test_the_issue_from_before_dependency_releases_is_taken_over(self):
        result, calls = self.run_gh({watch.OLD_TITLE: [{'number': 7, 'title': watch.OLD_TITLE}]})
        self.assertEqual(result, 'updated issue #7')
        self.assertEqual(calls[-1][:6], ['gh', 'issue', 'edit', '7', '--title', watch.TITLE])

    def test_the_current_issue_is_preferred_and_a_new_one_opened_only_when_none_is_open(self):
        result, calls = self.run_gh({watch.TITLE: [{'number': 8, 'title': watch.TITLE}],
                                     watch.OLD_TITLE: [{'number': 7, 'title': watch.OLD_TITLE}]})
        self.assertEqual(result, 'updated issue #8')
        result, calls = self.run_gh({})
        self.assertEqual(result, 'opened https://github.com/o/r/issues/12')
        self.assertEqual(calls[-1][:5], ['gh', 'issue', 'create', '--title', watch.TITLE])

    def test_issues_under_either_title_close_when_nothing_is_left(self):
        open_issues = {watch.TITLE: [{'number': 8, 'title': watch.TITLE}],
                       watch.OLD_TITLE: [{'number': 7, 'title': watch.OLD_TITLE}]}
        calls = []

        def fake(args, **_kwargs):
            calls.append(args)
            title = args[args.index('--search') + 1].split('"')[1] if '--search' in args else ''
            return subprocess.CompletedProcess(args, 0, json.dumps(open_issues.get(title, [])))

        with mock.patch.object(watch.subprocess, 'run', side_effect=fake):
            self.assertEqual(watch.close_issues('All clear.'), 'All clear. Closed #8, #7.')
            open_issues.clear()
            self.assertEqual(watch.close_issues('All clear.'), 'All clear.')
        closes = [call[:4] for call in calls if call[:3] == ['gh', 'issue', 'close']]
        self.assertEqual(closes, [['gh', 'issue', 'close', '8'], ['gh', 'issue', 'close', '7']])

    def test_only_an_issue_run_closes_issues(self):
        with mock.patch.object(watch, 'changes', return_value=[]), \
                mock.patch.object(watch, 'releases_behind', return_value=[]), \
                mock.patch.object(watch, 'close_issues', return_value='closed') as close, \
                contextlib.redirect_stdout(io.StringIO()):
            watch.main([])
            close.assert_not_called()
            watch.main(['--issue'])
            close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
