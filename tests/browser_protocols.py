"""Exercise page-side provider JavaScript in a disposable, isolated browser.

Run separately from the fast unit suite:
    python -m playwright install --only-shell chromium
    python -m unittest discover -s tests -p browser_protocols.py -v

Every page request is intercepted. All account IDs, tokens, and records below
are synthetic. This never opens the user's persistent browser profile.
"""

from __future__ import annotations

import json
import unittest
from typing import ClassVar
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import Browser, Playwright, Route, sync_playwright

from reclaim.providers import chatgpt, deepseek


class TestBrowserProtocols(unittest.TestCase):
    playwright: ClassVar[Playwright]
    browser: ClassVar[Browser]

    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.responses: dict[str, dict] = {}
        self.requests: list[dict] = []
        self.unexpected: list[str] = []
        self.context = self.browser.new_context(service_workers='block')
        self.context.route('**/*', self.route)
        self.page = self.context.new_page()

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.unexpected, [], 'All browser traffic must use synthetic responses')

    def route(self, route: Route):
        request = route.request
        self.requests.append({'url': request.url, 'headers': request.all_headers(), 'method': request.method})
        path = urlsplit(request.url).path
        if path in ('/', '/favicon.ico'):
            route.fulfill(status=200, content_type='text/html', body='<html><body>Synthetic page</body></html>')
        elif path in self.responses:
            route.fulfill(status=200, content_type='application/json', body=json.dumps(self.responses[path]))
        else:
            self.unexpected.append(path)
            route.abort()

    def test_chatgpt_personal_session_headers(self):
        path = '/backend-api/conversations'
        self.responses[path] = {'items': []}
        self.page.goto('https://chatgpt.com/')
        self.assertEqual(chatgpt._get(self.page, 'synthetic-token', 'https://chatgpt.com' + path), {'items': []})
        headers = self.requests[-1]['headers']
        self.assertEqual(headers['authorization'], 'Bearer synthetic-token')
        self.assertEqual(headers['x-authorization'], 'Bearer synthetic-token')
        self.assertNotIn('chatgpt-account-id', headers)

    def test_workspace_cookie_resolves_to_account_id_and_applies_to_file_metadata(self):
        check_path = '/backend-api/accounts/check/v4-2023-04-27'
        self.responses[check_path] = {'accounts': {
            'synthetic-workspace': {'account': {'account_id': 'synthetic-account-id'}}}}
        self.responses['/backend-api/conversations'] = {'items': []}
        self.responses['/backend-api/files/download/file-synthetic'] = {'status': 'success'}
        self.context.add_cookies([{'name': '_account', 'value': 'synthetic-workspace',
                                  'domain': 'chatgpt.com', 'path': '/'}])
        self.page.goto('https://chatgpt.com/')
        for path in ('/backend-api/conversations', '/backend-api/files/download/file-synthetic'):
            chatgpt._get(self.page, 'synthetic-token', 'https://chatgpt.com' + path)
            self.assertEqual(self.requests[-1]['headers']['chatgpt-account-id'], 'synthetic-account-id')
        checks = [request for request in self.requests if urlsplit(request['url']).path == check_path]
        self.assertEqual(len(checks), 1)
        self.assertNotIn('chatgpt-account-id', checks[0]['headers'])

    def test_unresolved_workspace_stops_before_fetching_another_accounts_chats(self):
        self.responses['/backend-api/accounts/check/v4-2023-04-27'] = {'accounts': {}}
        self.context.add_cookies([{'name': '_account', 'value': 'synthetic-missing',
                                  'domain': 'chatgpt.com', 'path': '/'}])
        self.page.goto('https://chatgpt.com/')
        with patch.object(chatgpt.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'workspace could not be resolved'):
                chatgpt._get(self.page, 'synthetic-token', 'https://chatgpt.com/backend-api/conversations')
        self.assertFalse(any(urlsplit(request['url']).path == '/backend-api/conversations'
                             for request in self.requests))

    def test_deepseek_token_is_read_in_page_and_api_envelope_unwrapped(self):
        self.responses['/api/v0/chat_session/fetch_page'] = {
            'code': 0, 'data': {'biz_code': 0, 'biz_data': {'chat_sessions': [], 'has_more': False}}}
        self.page.goto('https://chat.deepseek.com/')
        self.assertFalse(self.page.evaluate(deepseek._HAS_TOKEN_JS))
        self.page.evaluate("localStorage.setItem('userToken', JSON.stringify({value: 'synthetic-token'}))")
        self.assertTrue(self.page.evaluate(deepseek._HAS_TOKEN_JS))
        data = deepseek._api_get(self.page, 'chat_session/fetch_page', {'lte_cursor.pinned': 'false'})
        self.assertEqual(data, {'chat_sessions': [], 'has_more': False})
        self.assertEqual(self.requests[-1]['headers']['authorization'], 'Bearer synthetic-token')

    def test_missing_deepseek_cache_does_not_create_a_database_or_hang(self):
        self.page.goto('https://chat.deepseek.com/')
        self.assertEqual(deepseek.read_summaries(self.page), [])
        databases = self.page.evaluate('async () => (await indexedDB.databases()).map(db => db.name)')
        self.assertNotIn('deepseek-chat', databases)

    def test_existing_deepseek_cache_returns_only_summary_fields(self):
        self.page.goto('https://chat.deepseek.com/')
        self.page.evaluate("""() => new Promise((resolve, reject) => {
            const req = indexedDB.open('deepseek-chat', 1);
            req.onupgradeneeded = () => req.result.createObjectStore('history-message', {autoIncrement: true});
            req.onerror = reject;
            req.onsuccess = () => {
                const db = req.result;
                const tx = db.transaction('history-message', 'readwrite');
                tx.objectStore('history-message').add({data: {
                    chat_session: {id: 'synthetic-cached', title: 'Synthetic cached title', updated_at: 100},
                    chat_messages: [{content: 'Synthetic cached body'}],
                }});
                tx.oncomplete = () => { db.close(); resolve(); };
                tx.onerror = reject;
            };
        })""")
        self.assertEqual(deepseek.read_summaries(self.page),
                         [{'id': 'synthetic-cached', 'title': 'Synthetic cached title', 'updated_at': 100}])


if __name__ == '__main__':
    unittest.main()
