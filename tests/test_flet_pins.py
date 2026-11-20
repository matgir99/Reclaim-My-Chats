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
FLET = re.compile(r'(flet(?:-[a-z0-9]+)*)(?=$|[\[=<>!~;@])(?:\[[^\]]*\])?(.*)')
EXACT = re.compile(r'==([0-9][\w.]*)')


def _toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding='utf-8'))


def _flet_pins(requirements: list[str]) -> dict[str, str]:
    """Exact versions of the Flet packages in `requirements`; any other form fails."""
    pins = {}
    for requirement in requirements:
        match = FLET.fullmatch(requirement.replace(' ', ''))
        if match:
            exact = EXACT.fullmatch(match[2])
            if not exact:
                raise AssertionError(f'pin Flet packages exactly (==): {requirement}')
            pins[match[1]] = exact[1]
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
        locked = [(p['name'], p['version']) for p in packages if p['name'].startswith('flet')]
        self.assertTrue(locked)
        self.assertEqual({version for _, version in locked}, {self.app_version}, locked)

    def test_a_flet_requirement_that_is_not_an_exact_pin_fails(self):
        self.assertEqual(_flet_pins(['flet[test]==1.0.5', 'flet-desktop == 1.0.5',
                                     'pyinstaller>=6', 'fletcher>=1']),
                         {'flet': '1.0.5', 'flet-desktop': '1.0.5'})
        for loose in ('flet>=1.0.5', 'flet-platform-assets~=1.0', 'flet', 'flet==1.0.*'):
            with self.subTest(loose), self.assertRaisesRegex(AssertionError, 'exactly'):
                _flet_pins([loose])

    def test_the_device_tests_use_the_app_flet_version(self):
        project = _toml(ROOT / 'flet_integration' / 'pyproject.toml')['project']
        self.assertEqual(_flet_pins(project['dependencies']), {'flet': self.app_version},
                         'update flet_integration/pyproject.toml together with the app')


if __name__ == '__main__':
    unittest.main()
