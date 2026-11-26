"""Published downloads are immutable; incomplete drafts can be resumed."""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
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
        client = app_dir / '_internal' / 'flet_desktop' / 'app' / 'flet' / 'flet'
        client.parent.mkdir(parents=True)
        client.write_text('#!/bin/sh\n', encoding='utf-8')
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
        win_client = win_app / '_internal' / 'flet_desktop' / 'app' / 'flet' / 'flet.exe'
        win_client.parent.mkdir(parents=True)
        win_client.write_bytes(b'MZ-flet')
        iss = pack_desktop.write_inno_script(
            win_app, icons['ico'], '3.3.5',
            out / 'Reclaim-My-Chats-3.3.5-windows-x86_64-setup.exe',
            self.root / 'ReclaimMyChats.iss',
        )
        script_text = iss.read_text(encoding='utf-8')
        self.assertIn('PrivilegesRequired=lowest', script_text)
        self.assertIn('runasoriginaluser', script_text)
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

    def test_unpack_bundled_flet_client_windows_and_linux(self):
        win_dir = self.root / 'win'
        win_app = win_dir / '_internal' / 'flet_desktop' / 'app'
        win_app.mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError, 'not found'):
            pack_desktop.unpack_bundled_flet_client(win_dir, 'windows')

        evil_zip = win_app / 'flet-windows.zip'
        with zipfile.ZipFile(evil_zip, 'w') as zf:
            zf.writestr('../evil.txt', 'bad')
        with self.assertRaisesRegex(RuntimeError, 'unsafe zip entry'):
            pack_desktop.unpack_bundled_flet_client(win_dir, 'windows')

        with zipfile.ZipFile(evil_zip, 'w') as zf:
            zf.writestr('flet/flet.exe', 'exe')
        (win_app / 'flet-windows.zip.sha256').write_text('abc 123', encoding='utf-8')
        (win_app / '.last-used').write_text('', encoding='utf-8')
        client = pack_desktop.unpack_bundled_flet_client(win_dir, 'windows')
        self.assertTrue((client / 'flet.exe').is_file())
        self.assertFalse(evil_zip.exists())
        self.assertFalse((win_app / 'flet-windows.zip.sha256').exists())
        self.assertFalse((win_app / '.last-used').exists())

        linux_dir = self.root / 'linux'
        linux_app = linux_dir / '_internal' / 'flet_desktop' / 'app'
        linux_app.mkdir(parents=True)
        stage_flet = self.root / 'stage-flet' / 'flet'
        stage_flet.mkdir(parents=True)
        (stage_flet / 'flet').write_text('#!/bin/sh\n', encoding='utf-8')
        tar_path = linux_app / 'flet-linux-ubuntu22.04-x86_64.tar.gz'
        with tarfile.open(tar_path, 'w:gz') as tf:
            tf.add(stage_flet, arcname='flet')
        (linux_app / f'{tar_path.name}.sha256').write_text('abc 123', encoding='utf-8')
        linux_client = pack_desktop.unpack_bundled_flet_client(linux_dir, 'linux')
        self.assertTrue((linux_client / 'flet').is_file())
        self.assertFalse(tar_path.exists())
        self.assertFalse((linux_app / f'{tar_path.name}.sha256').exists())

    def test_verify_desktop_artifacts_rejects_unpacked_missing_or_compressed_client(self):
        win_zip = self.root / 'bad-win.zip'
        with zipfile.ZipFile(win_zip, 'w') as zf:
            zf.writestr('ReclaimMyChats/ReclaimMyChats.exe', b'MZ' + b'\x00' * 32)
        with patch.object(verify_desktop_artifacts, 'MIN_SIZE', 10):
            with self.assertRaisesRegex(ValueError, 'missing unpacked Flet client'):
                verify_desktop_artifacts._verify_windows_zip(win_zip)

        with zipfile.ZipFile(win_zip, 'w') as zf:
            zf.writestr('ReclaimMyChats/ReclaimMyChats.exe', b'MZ')
            zf.writestr('ReclaimMyChats/_internal/flet_desktop/app/flet/flet.exe', b'MZ')
            zf.writestr('ReclaimMyChats/_internal/flet_desktop/app/flet-windows.zip', b'PK')
        with patch.object(verify_desktop_artifacts, 'MIN_SIZE', 10):
            with self.assertRaisesRegex(ValueError, 'compressed flet-windows.zip'):
                verify_desktop_artifacts._verify_windows_zip(win_zip)

        linux_tar = self.root / 'bad-linux.tar.gz'
        dummy = self.root / 'dummy'
        dummy.write_text('x', encoding='utf-8')
        with tarfile.open(linux_tar, 'w:gz') as tf:
            tf.add(dummy, arcname='ReclaimMyChats/ReclaimMyChats')
            tf.add(dummy, arcname='ReclaimMyChats/org.reclaimmychats.app.desktop')
            tf.add(dummy, arcname='ReclaimMyChats/_internal/flet_desktop/app/flet/flet')
            tf.add(
                dummy,
                arcname='ReclaimMyChats/_internal/flet_desktop/app/flet-linux-ubuntu22.04-x86_64.tar.gz',
            )
        with patch.object(verify_desktop_artifacts, 'MIN_SIZE', 10):
            with self.assertRaisesRegex(ValueError, 'compressed Flet client archive'):
                verify_desktop_artifacts._verify_linux_tarball(linux_tar)

    def test_desktop_main_configures_flet_view_path_when_frozen(self):
        main_path = Path(__file__).resolve().parents[1] / 'desktop' / 'main.py'
        spec = importlib.util.spec_from_file_location('reclaim_desktop_main', main_path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        meipass = self.root / '_internal'
        exe_name = 'flet.exe' if os.name == 'nt' else 'flet'
        client_dir = meipass / 'flet_desktop' / 'app' / 'flet'
        client_dir.mkdir(parents=True)
        (client_dir / exe_name).write_bytes(b'client')

        with (patch.object(sys, 'frozen', True, create=True),
              patch.object(sys, '_MEIPASS', str(meipass), create=True),
              patch.dict(os.environ, {}, clear=True)):
            mod._configure_frozen_env()
            self.assertEqual(os.environ.get('FLET_VIEW_PATH'), str(client_dir))

        with (patch.object(sys, 'frozen', True, create=True),
              patch.object(sys, '_MEIPASS', str(meipass), create=True),
              patch.dict(os.environ, {'FLET_VIEW_PATH': 'custom-view'}, clear=True)):
            mod._configure_frozen_env()
            self.assertEqual(os.environ.get('FLET_VIEW_PATH'), 'custom-view')

    def test_build_macos_dmg_passes_explicit_megabytes_headroom(self):
        app_bundle = self.root / 'ReclaimMyChats.app'
        app_bundle.mkdir()
        dmg_out = self.root / 'out' / 'Reclaim.dmg'
        calls = []

        def fake_run(cmd, check=False):
            calls.append(cmd)
            if cmd[0] == 'ditto':
                Path(cmd[2]).mkdir(parents=True, exist_ok=True)
                (Path(cmd[2]) / 'payload.bin').write_bytes(b'x' * 2048)

        with (patch.object(pack_desktop.subprocess, 'run', side_effect=fake_run),
              patch.object(pack_desktop.Path, 'symlink_to')):
            pack_desktop.build_macos_dmg(app_bundle, dmg_out)
        hdiutil_cmd = calls[-1]
        self.assertEqual(hdiutil_cmd[0], 'hdiutil')
        self.assertIn('-megabytes', hdiutil_cmd)
        idx = hdiutil_cmd.index('-megabytes')
        self.assertGreaterEqual(int(hdiutil_cmd[idx + 1]), 64)
