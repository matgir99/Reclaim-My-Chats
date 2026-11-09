"""Regression checks for request origin validation."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from reclaim.providers import googleaistudio


class TestApiKeyCapture(unittest.TestCase):
    def test_only_the_exact_https_rpc_host_is_accepted(self):
        host = 'alkalimakersuite-pa.clients6.google.com'
        for url, accepted in [
            (f'https://{host}/$rpc/example', True),
            (f'https://{host}.example.org/rpc', False),
            (f'https://example.org/{host}', False),
            (f'https://example.org/?next=https://{host}', False),
            (f'https://{host}@example.org/rpc', False),
            (f'http://{host}/rpc', False),
        ]:
            with self.subTest(url=url):
                page = MagicMock()
                callbacks = []
                page.on.side_effect = lambda _, callback, seen=callbacks: seen.append(callback)
                request = SimpleNamespace(url=url, headers={'x-goog-api-key': 'synthetic'})
                page.goto.side_effect = (lambda *a, seen=callbacks, req=request, **kw:
                                         seen[0](req))
                with patch.object(googleaistudio.time, 'sleep'):
                    googleaistudio.init_replay(page)
                captures = [c for c in page.evaluate.call_args_list if len(c.args) == 2]
                self.assertEqual(len(captures), int(accepted))
