"""scripts/apt_install.sh: CI package installs that survive a stalled mirror.

The script runs here against a fake apt-get that can stall or fail, so the
retry, time limit and mirror switch are checked without touching the system.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'apt_install.sh'
WORKFLOWS = ROOT / '.github' / 'workflows'

FAKE_APT_GET = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
case " $* " in
  *" update "*)
    n=$(cat "$FAKE_STATE" 2>/dev/null || echo 0)
    echo $((n + 1)) > "$FAKE_STATE"
    if [ "$n" -lt "${FAKE_STALLS:-0}" ]; then sleep 30; fi ;;
esac
if [ -n "${FAKE_FAIL:-}" ]; then exit 100; fi
"""

AZURE = 'deb http://azure.archive.ubuntu.com/ubuntu/ jammy main universe\n'


@unittest.skipUnless(shutil.which('bash') and shutil.which('timeout'),
                     'needs bash and coreutils timeout (as on the Ubuntu runners)')
class TestAptInstall(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._temp.name)
        self.fake = self.tmp / 'apt-get'
        self.fake.write_text(FAKE_APT_GET, encoding='utf-8')
        self.fake.chmod(0o755)
        self.log = self.tmp / 'calls.log'
        self.sources = self.tmp / 'sources.list'
        self.sources.write_text(AZURE, encoding='utf-8')

    def tearDown(self):
        self._temp.cleanup()

    def run_script(self, *packages: str, **fake: str) -> subprocess.CompletedProcess:
        env = {**os.environ, 'SUDO': '', 'APT_GET': str(self.fake),
               'APT_SOURCES': str(self.sources), 'APT_ATTEMPT_LIMIT': '1s',
               'FAKE_LOG': str(self.log), 'FAKE_STATE': str(self.tmp / 'updates'), **fake}
        return subprocess.run(['bash', str(SCRIPT), *packages], env=env, capture_output=True,
                              text=True, timeout=60, check=False)

    def calls(self) -> list[str]:
        return self.log.read_text(encoding='utf-8').splitlines() if self.log.exists() else []

    def test_a_quick_mirror_installs_from_the_cache_in_one_attempt(self):
        done = self.run_script('xvfb', 'libgtk-3-0')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        actions = [call.split(' -o ')[-1].split(' ', 1)[-1] for call in self.calls()]
        self.assertEqual(actions, ['update', 'install -y --download-only xvfb libgtk-3-0',
                                   'install -y --no-download xvfb libgtk-3-0'])
        self.assertEqual(self.sources.read_text(encoding='utf-8'), AZURE)

    def test_a_stalled_mirror_is_cut_off_and_the_main_mirror_used(self):
        started = time.monotonic()
        done = self.run_script('xvfb', FAKE_STALLS='1')
        self.assertLess(time.monotonic() - started, 20)  # the 30 s stall was cut at 1 s
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('attempt 1 of 3 failed or took longer than 1s', done.stdout)
        self.assertEqual(self.sources.read_text(encoding='utf-8'),
                         AZURE.replace('azure.archive.ubuntu.com', 'archive.ubuntu.com'))
        calls = self.calls()
        self.assertEqual(sum(' update' in call for call in calls), 2)
        self.assertTrue(calls[-1].endswith('install -y --no-download xvfb'))

    def test_a_mirror_that_keeps_failing_stops_without_installing(self):
        done = self.run_script('xvfb', FAKE_FAIL='1')
        self.assertEqual(done.returncode, 1)
        self.assertIn('could not download the packages in 3 attempts', done.stdout)
        calls = self.calls()
        self.assertEqual(sum(' update' in call for call in calls), 3)
        self.assertFalse([call for call in calls if '--no-download' in call])

    def test_no_packages_is_a_usage_error(self):
        self.assertEqual(self.run_script().returncode, 2)


class TestWorkflowsUseIt(unittest.TestCase):
    def test_every_linux_package_install_goes_through_the_script(self):
        for name in ('gui-test.yml', 'desktop-test.yml', 'release.yml'):
            text = (WORKFLOWS / name).read_text(encoding='utf-8')
            self.assertIn('scripts/apt_install.sh xvfb', text, name)
            self.assertNotIn('apt-get install', text, name)

    def test_changing_the_script_runs_the_desktop_builds(self):
        text = (WORKFLOWS / 'desktop-test.yml').read_text(encoding='utf-8')
        self.assertIn('- scripts/apt_install.sh', text)


if __name__ == '__main__':
    unittest.main()
