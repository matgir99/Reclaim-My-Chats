"""Published downloads are immutable; incomplete drafts can be resumed."""

import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts import pack_desktop, publish_release, verify_desktop_artifacts, verify_release_assets


class TestPublishing(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.assets = []
        for i in range(6):
            data = f'package {i}'.encode()
            path = self.directory / f'asset-{i}'
            path.write_bytes(data)
            self.assets.append({'name': path.name, 'size': len(data),
                                'digest': 'sha256:' + hashlib.sha256(data).hexdigest()})
        self.draft = {'draft': True, 'assets': self.assets}

    def test_published_release_makes_no_mutating_calls(self):
        with (patch.object(publish_release, 'existing_release', return_value={'draft': False}),
              patch.object(publish_release.subprocess, 'run') as run):
            publish_release.publish('example/repo', 'v3.3.0', self.directory)
        run.assert_not_called()

    def test_draft_is_verified_before_publication(self):
        with (patch.object(publish_release, 'existing_release', side_effect=[self.draft, self.draft]),
              patch.object(publish_release.subprocess, 'run') as run):
            publish_release.publish('example/repo', 'v3.3.0', self.directory)
        self.assertEqual([c.args[0][2] for c in run.call_args_list], ['upload', 'edit'])

    def test_new_release_stays_a_draft_until_uploads_are_verified(self):
        with (patch.object(publish_release, 'existing_release', side_effect=[None, self.draft]),
              patch.object(publish_release.subprocess, 'run') as run):
            publish_release.publish('example/repo', 'v3.3.0', self.directory)
        self.assertEqual([c.args[0][2] for c in run.call_args_list], ['create', 'upload', 'edit'])
        self.assertIn('--draft', run.call_args_list[0].args[0])

    def test_changed_bytes_with_the_same_size_block_publication(self):
        (self.directory / 'asset-0').write_bytes(b'changed!!')
        with (patch.object(publish_release, 'existing_release', side_effect=[self.draft, self.draft]),
              patch.object(publish_release.subprocess, 'run') as run):
            with self.assertRaisesRegex(ValueError, 'SHA-256 differs'):
                publish_release.publish('example/repo', 'v3.3.0', self.directory)
        self.assertEqual(len(run.call_args_list), 1)  # Upload only; no publication.

    def test_unexpected_remote_asset_is_rejected(self):
        response = {'assets': [*self.assets, {'name': 'unexpected'}]}
        with self.assertRaisesRegex(ValueError, 'names differ'):
            verify_release_assets.verify(self.directory, response)

    def test_only_a_real_404_is_treated_as_an_absent_release(self):
        for error in ['HTTP 403', 'HTTP 500', 'network unavailable']:
            with (self.subTest(error=error),
                  patch.object(publish_release.subprocess, 'run', return_value=
                               subprocess.CompletedProcess([], 1, '', error))):
                with self.assertRaises(RuntimeError):
                    publish_release.existing_release('example/repo', 'v3.3.0')
        with patch.object(publish_release.subprocess, 'run', side_effect=[
                subprocess.CompletedProcess([], 1, '', 'HTTP 404'),
                subprocess.CompletedProcess([], 0, '', '')]):
            self.assertIsNone(publish_release.existing_release('example/repo', 'v3.3.0'))

    def test_draft_missing_from_tag_endpoint_is_found_in_paginated_list(self):
        with patch.object(publish_release.subprocess, 'run', side_effect=[
                subprocess.CompletedProcess([], 1, '', 'HTTP 404'),
                subprocess.CompletedProcess([], 0, '123\n', ''),
                subprocess.CompletedProcess([], 0, json.dumps(self.draft), '')]) as run:
            self.assertEqual(publish_release.existing_release('example/repo', 'v3.3.0'), self.draft)
        self.assertIn('--paginate', run.call_args_list[1].args[0])
        self.assertEqual(run.call_args_list[2].args[0][-1], 'repos/example/repo/releases/123')

    def test_draft_list_errors_are_not_treated_as_an_absent_release(self):
        for error in ['HTTP 404', 'HTTP 403', 'HTTP 500', 'network unavailable']:
            with (self.subTest(error=error),
                  patch.object(publish_release.subprocess, 'run', side_effect=[
                      subprocess.CompletedProcess([], 1, '', 'HTTP 404'),
                      subprocess.CompletedProcess([], 1, '', error)])):
                with self.assertRaisesRegex(RuntimeError, 'cannot list release drafts'):
                    publish_release.existing_release('example/repo', 'v3.3.0')

    def test_duplicate_drafts_are_rejected(self):
        with patch.object(publish_release.subprocess, 'run', side_effect=[
                subprocess.CompletedProcess([], 1, '', 'HTTP 404'),
                subprocess.CompletedProcess([], 0, '123\n456\n', '')]):
            with self.assertRaisesRegex(RuntimeError, 'ambiguous lookup'):
                publish_release.existing_release('example/repo', 'v3.3.0')

    def test_draft_disappearing_after_listing_is_an_error(self):
        with patch.object(publish_release.subprocess, 'run', side_effect=[
                subprocess.CompletedProcess([], 1, '', 'HTTP 404'),
                subprocess.CompletedProcess([], 0, '123\n', ''),
                subprocess.CompletedProcess([], 1, '', 'HTTP 404')]):
            with self.assertRaisesRegex(RuntimeError, 'cannot read release draft'):
                publish_release.existing_release('example/repo', 'v3.3.0')

    def test_release_response_is_read_without_exposing_authentication(self):
        response = subprocess.CompletedProcess([], 0, json.dumps(self.draft), '')
        with patch.object(publish_release.subprocess, 'run', return_value=response):
            self.assertEqual(publish_release.existing_release('example/repo', 'v3.3.0'), self.draft)


class TestDesktopPackaging(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _make_linux_app(self):
        icons = pack_desktop.write_icons(self.root / 'stage')
        app_dir = self.root / 'ReclaimMyChats'
        app_dir.mkdir()
        exe = app_dir / 'ReclaimMyChats'
        exe.write_text('#!/bin/sh\necho "hello:$1"\n', encoding='utf-8')
        exe.chmod(0o755)
        desktop = self.root / f'{pack_desktop.BUNDLE_ID}.desktop'
        desktop.write_text('[Desktop Entry]\nExec=/tmp/build/ReclaimMyChats\nIcon=/tmp/icon\n',
                           encoding='utf-8')
        return app_dir, desktop, icons

    def test_write_icons_produces_valid_png_ico_icns_headers(self):
        icons = pack_desktop.write_icons(self.root)
        self.assertTrue(icons['png'].read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertTrue(icons['ico'].read_bytes().startswith(b'\x00\x00\x01\x00'))
        self.assertTrue(icons['icns'].read_bytes().startswith(b'icns'))

    def test_linux_appimage_deb_and_tarball_builders(self):
        app_dir, desktop, icons = self._make_linux_app()
        out = self.root / 'out'
        tarball = pack_desktop.build_linux_tarball(
            app_dir, desktop, icons['png'], out / 'Reclaim-My-Chats-3.3.5-linux-x86_64.tar.gz',
        )
        with tarfile.open(tarball, 'r:gz') as tar:
            names = set(tar.getnames())
        self.assertTrue({
            'ReclaimMyChats/ReclaimMyChats',
            f'ReclaimMyChats/{pack_desktop.BUNDLE_ID}.desktop',
            f'ReclaimMyChats/{pack_desktop.BUNDLE_ID}.png',
        } <= names)

        appimage = pack_desktop.build_linux_appimage(
            app_dir, desktop, icons['png'], '3.3.5',
            out / 'Reclaim-My-Chats-3.3.5-linux-x86_64.AppImage',
        )
        self.assertTrue(appimage.read_bytes().startswith(b'#!/bin/sh'))
        if os.name == 'posix':
            extract_cwd = self.root / 'extract'
            extract_cwd.mkdir()
            subprocess.run([str(appimage), '--appimage-extract'], cwd=extract_cwd, check=True)
            self.assertTrue((extract_cwd / 'squashfs-root' / 'AppRun').is_file())
            env = {**os.environ, 'XDG_CACHE_HOME': str(self.root / 'xdg-cache')}
            ran = subprocess.run([str(appimage), 'ok'], capture_output=True, text=True,
                                 env=env, check=True)
            self.assertEqual(ran.stdout.strip(), 'hello:ok')

        deb = pack_desktop.build_linux_deb(
            app_dir, desktop, icons['png'], '3.3.5', 'x86_64',
            out / 'Reclaim-My-Chats-3.3.5-linux-x86_64.deb',
        )
        self.assertTrue(deb.read_bytes().startswith(b'!<arch>\n'))
        ar_deb = out / 'fallback.deb'
        with patch.object(pack_desktop.shutil, 'which', return_value=None):
            pack_desktop.build_linux_deb(app_dir, desktop, icons['png'], '3.3.5', 'arm64', ar_deb)
        self.assertTrue(ar_deb.read_bytes().startswith(b'!<arch>\n'))

    def test_windows_inno_script_and_verify_desktop_artifacts(self):
        app_dir, desktop, icons = self._make_linux_app()
        out = self.root / 'release'
        win_app = self.root / 'win-app'
        win_app.mkdir()
        (win_app / 'ReclaimMyChats.exe').write_bytes(b'MZ-stub')
        iss = pack_desktop.write_inno_script(
            win_app, icons['ico'], '3.3.5',
            out / 'Reclaim-My-Chats-3.3.5-windows-x86_64-setup.exe',
            self.root / 'ReclaimMyChats.iss',
        )
        script_text = iss.read_text(encoding='utf-8')
        self.assertIn('PrivilegesRequired=lowest', script_text)
        self.assertIn(r'Software\Microsoft\Windows\CurrentVersion\Run', script_text)
        self.assertIn('ValueName: "ReclaimMyChats"', script_text)

        pack_desktop.build_linux_tarball(
            app_dir, desktop, icons['png'], out / 'Reclaim-My-Chats-3.3.5-linux-x86_64.tar.gz',
        )
        pack_desktop.build_linux_appimage(
            app_dir, desktop, icons['png'], '3.3.5',
            out / 'Reclaim-My-Chats-3.3.5-linux-x86_64.AppImage',
        )
        with patch.object(pack_desktop.shutil, 'which', return_value=None):
            pack_desktop.build_linux_deb(
                app_dir, desktop, icons['png'], '3.3.5', 'x86_64',
                out / 'Reclaim-My-Chats-3.3.5-linux-x86_64.deb',
            )
        pack_desktop.build_windows_zip(win_app, out / 'Reclaim-My-Chats-3.3.5-windows-x86_64.zip')
        (out / 'Reclaim-My-Chats-3.3.5-windows-x86_64-setup.exe').write_bytes(b'MZ' + b'\x00' * 32)
        for arch in ('arm64', 'x86_64'):
            mac_zip = out / f'Reclaim-My-Chats-3.3.5-macos-{arch}.zip'
            with zipfile.ZipFile(mac_zip, 'w') as zf:
                zf.writestr('ReclaimMyChats.app/Contents/MacOS/ReclaimMyChats', 'bin')
            (out / f'Reclaim-My-Chats-3.3.5-macos-{arch}.dmg').write_bytes(b'dmg-stub-0123456789')

        with patch.object(verify_desktop_artifacts, 'MIN_SIZE', 10):
            found = verify_desktop_artifacts.verify(out, '3.3.5')
        self.assertEqual(len(found), 9)
