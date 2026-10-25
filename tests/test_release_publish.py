"""Published downloads are immutable; incomplete drafts can be resumed."""

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import publish_release, verify_release_assets


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
        with patch.object(publish_release.subprocess, 'run', return_value=
                          subprocess.CompletedProcess([], 1, '', 'HTTP 404')):
            self.assertIsNone(publish_release.existing_release('example/repo', 'v3.3.0'))

    def test_release_response_is_read_without_exposing_authentication(self):
        response = subprocess.CompletedProcess([], 0, json.dumps(self.draft), '')
        with patch.object(publish_release.subprocess, 'run', return_value=response):
            self.assertEqual(publish_release.existing_release('example/repo', 'v3.3.0'), self.draft)
