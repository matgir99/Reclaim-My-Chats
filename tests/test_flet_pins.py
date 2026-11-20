"""The Flet device tests must run the Flet version the desktop app ships.

`flet_integration/pyproject.toml` is a separate project that `flet test`
builds, so its Flet pin is not tied to the app's by anything else. If the two
drift apart, "Device integration (Linux)" passes or fails for a Flet version
nobody downloads.
"""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FLET_PIN = re.compile(r'(flet(?:-[a-z]+)?)(?:\[[^\]]*\])?==(\S+)')


def _toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding='utf-8'))


def _flet_pins(requirements: list[str]) -> dict[str, str]:
    pins = {}
    for requirement in requirements:
        match = FLET_PIN.fullmatch(requirement.replace(' ', ''))
        if match:
            pins[match[1]] = match[2]
    return pins


class TestFletPins(unittest.TestCase):
    def setUp(self):
        extras = _toml(ROOT / 'pyproject.toml')['project']['optional-dependencies']
        pins = [_flet_pins(extras[name]) for name in ('gui', 'gui-pack', 'gui-test')]
        versions = {version for extra in pins for version in extra.values()}
        self.assertEqual(len(versions), 1, f'the GUI extras pin different Flet versions: {pins}')
        self.app_version = versions.pop()

    def test_the_lock_installs_the_pinned_flet(self):
        packages = _toml(ROOT / 'uv.lock')['package']
        locked = {p['name']: p['version'] for p in packages if p['name'].startswith('flet')}
        self.assertTrue(locked)
        self.assertEqual(set(locked.values()), {self.app_version}, locked)

    def test_the_device_tests_use_the_app_flet_version(self):
        project = _toml(ROOT / 'flet_integration' / 'pyproject.toml')['project']
        self.assertEqual(_flet_pins(project['dependencies']), {'flet': self.app_version},
                         'update flet_integration/pyproject.toml together with the app')


if __name__ == '__main__':
    unittest.main()
