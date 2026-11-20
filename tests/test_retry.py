"""The shared request policy (core/retry.py), with synthetic responses."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from reclaim.core import retry


def responses(*items):
    """A request function returning `items` in order (exceptions are raised)."""
    queue = list(items)

    def call():
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    call.remaining = queue  # type: ignore[attr-defined]
    return call


def ok(text='{}'):
    return {'status': 200, 'text': text}


def status(code, retry_after=None):
    return {'status': code, 'text': f'body {code}', 'retryAfter': retry_after}


class TestRetry(unittest.TestCase):
    def send(self, call, **options):
        with patch.object(retry.time, 'sleep') as sleep:
            result = retry.send(call, what='Test GET', **options)
        return result, [c.args[0] for c in sleep.call_args_list]

    def test_success_is_returned(self):
        self.assertEqual(self.send(responses(ok('x')))[0]['text'], 'x')

    def test_rate_limits_follow_retry_after_and_do_not_use_up_retries(self):
        call = responses(status(429, '7'), status(429), status(429, '9999'), status(503), ok())
        result, waits = self.send(call, retries=2)
        self.assertEqual(result['status'], 200)
        self.assertEqual(waits, [7.0, retry.RATE_LIMIT_WAITS[1], retry.MAX_RATE_LIMIT_WAIT, 2])

    def test_a_service_that_keeps_rate_limiting_fails_clearly(self):
        call = responses(*[status(429)] * 5)
        with patch.object(retry.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'Test GET failed: HTTP 429'):
                retry.send(call, what='Test GET')

    def test_login_problems_stop_at_once(self):
        call = responses(status(401), ok())
        with patch.object(retry.time, 'sleep') as sleep:
            with self.assertRaisesRegex(retry.AuthError, 'Log in again'):
                retry.send(call, what='Test GET')
        sleep.assert_not_called()

    def test_server_errors_and_failed_requests_are_retried(self):
        result, waits = self.send(responses(status(502), RuntimeError('page reloaded'), ok()))
        self.assertEqual(result['status'], 200)
        self.assertEqual(waits, [2, 4])
        call = responses(status(500), status(500), status(500))
        with patch.object(retry.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 500: body 500'):
                retry.send(call, what='Test GET')

    def test_other_errors_stop_unless_the_caller_handles_them(self):
        with patch.object(retry.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 404'):
                retry.send(responses(status(404)), what='Test GET')
        self.assertEqual(self.send(responses(status(404)), give_back=(404,))[0]['status'], 404)

    def test_a_failure_carries_the_http_status_of_the_last_response(self):
        cases = [
            (responses(status(404)), 404),
            (responses(status(500), status(500), status(500)), 500),
            (responses(*[status(429)] * (len(retry.RATE_LIMIT_WAITS) + 1)), 429),
            (responses(status(500), RuntimeError('page reloaded'), RuntimeError('page reloaded')), None),
            (responses(*[RuntimeError('page reloaded')] * 3), None),
        ]
        refused = responses(status(403))
        with patch.object(retry.time, 'sleep'):
            for call, expected in cases:
                with self.subTest(expected=expected):
                    with self.assertRaises(retry.RequestFailed) as caught:
                        retry.send(call, what='Test GET')
                    self.assertEqual(caught.exception.status, expected)
            with self.assertRaises(retry.AuthError) as caught:
                retry.send(refused, what='Test GET')
        self.assertEqual(caught.exception.status, 403)
        self.assertIsInstance(caught.exception, RuntimeError)

    def test_only_an_http_404_means_not_found_whatever_the_text_says(self):
        # A chat id with 404 in it appears in the message; the body says "not found".
        what = 'GET https://x.test/conversation/6a404b00-0000-4000-8000-000000000404'
        not_found_body = {'status': 500, 'text': '404 not found', 'retryAfter': None}
        failures = {
            'a real 404': (responses(status(404)), True),
            'a server error': (responses(not_found_body, not_found_body, not_found_body), False),
            'a rate limit': (responses(*[status(429)] * 5), False),
            'a refused login': (responses(status(403)), False),
            'a dropped connection': (responses(*[RuntimeError('404 not found')] * 3), False),
        }
        with patch.object(retry.time, 'sleep'):
            for label, (call, expected) in failures.items():
                with self.subTest(label):
                    with self.assertRaises(RuntimeError) as caught:
                        retry.send(call, what=what)
                    self.assertEqual(retry.is_not_found(caught.exception), expected)
        # Only an error that carries the status counts; text alone never does.
        self.assertFalse(retry.is_not_found(RuntimeError('HTTP 404: not found')))
        self.assertFalse(retry.is_not_found(retry.RequestFailed('HTTP 404: not found')))
        self.assertTrue(retry.is_not_found(retry.RequestFailed('gone', 404)))

    def test_callers_with_fallbacks_get_every_final_response(self):
        self.assertEqual(self.send(responses(status(400)), return_errors=True)[0]['status'], 400)
        self.assertEqual(self.send(responses(status(500), status(500), status(500)),
                                   return_errors=True)[0]['status'], 500)
        result, waits = self.send(responses(status(403), status(403)), return_errors=True,
                                  auth_retries=1)
        self.assertEqual((result['status'], waits), (403, [2]))
        result, _ = self.send(responses(status(401), ok()), auth_retries=1)
        self.assertEqual(result['status'], 200)

    def test_a_service_that_keeps_rate_limiting_fails_even_for_callers_with_fallbacks(self):
        call = responses(*[status(429)] * (len(retry.RATE_LIMIT_WAITS) + 1))
        with patch.object(retry.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 429'):
                retry.send(call, what='Test GET', return_errors=True)

    def test_recording_captures_every_response_including_retries_and_skips_auth(self):
        self.assertEqual(self.send(responses(ok('plain')))[0]['text'], 'plain')
        with retry.recording() as recorded:
            call = responses(status(429, '1'), status(500), ok('{"secret": 1}'))
            result, _ = self.send(call)
            self.assertEqual(result['status'], 200)
            self.send(responses(ok('{"token": "ya29.secret"}')), record=False)
            with self.assertRaises(RuntimeError):
                self.send(responses(status(502), status(502), status(502)))
        self.assertEqual([(r['request'], r['status'], r['text']) for r in recorded], [
            ('Test GET', 429, 'body 429'),
            ('Test GET', 500, 'body 500'),
            ('Test GET', 200, '{"secret": 1}'),
            ('Test GET', 502, 'body 502'),
            ('Test GET', 502, 'body 502'),
            ('Test GET', 502, 'body 502'),
        ])


if __name__ == '__main__':
    unittest.main()
