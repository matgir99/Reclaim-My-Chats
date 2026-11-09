"""Synthetic regressions for provider formats reviewed in October 2026.

No live accounts, credentials, chats, or external network calls are used.
Source formats and remaining limits are recorded in docs/UPSTREAM_REVIEW.md.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from reclaim.core import browser
from reclaim.core.batchexecute import decode_response
from reclaim.core.writer import write_chat
from reclaim.providers import chatgpt, chatgpt_import, claude, deepseek, googlegemini, kimi

FIXTURES = Path(__file__).parent / 'fixtures'


def rpc_response(rpcid: str, payload: list) -> str:
    # Real envelope: telemetry and an RPC record can share the same frame.
    frame = json.dumps([['di', 1], ['wrb.fr', rpcid, json.dumps(payload), None]])
    return ")]}'\n\n" + str(len(frame.encode('utf-16-le')) // 2) + '\n' + frame


def message(text: str, **metadata) -> dict:
    return {'message': {'author': {'role': 'assistant'},
                        'content': {'content_type': 'text', 'parts': [text]},
                        'metadata': metadata}}


class TestVisibleAnswers(unittest.TestCase):
    def test_saved_answers_survive_mixed_reasoning_messages(self):
        claude_data = json.loads((FIXTURES / 'claude_sample.json').read_text())
        kimi_data = json.loads((FIXTURES / 'kimi_messages_sample.json').read_text())
        gemini_data = json.loads((FIXTURES / 'gemini_read_sample.json').read_text())
        kimi_chat, _ = kimi.parse_chat({'id': 'synthetic-kimi'}, list(reversed(kimi_data['messages'])))
        chats = [
            (claude.parse_chat(claude_data, 'synthetic-claude', 'https://example.com'),
             'Current answer', 'hidden reasoning trace'),
            (kimi_chat, 'Here is the answer:', 'reasoning about e...'),
            (googlegemini.read_to_chat(googlegemini.parse_read(gemini_data),
                                      'synthetic-gemini', 'https://example.com'),
             'e ≈ 2.71828', ''),
        ]
        with tempfile.TemporaryDirectory() as directory:
            for chat, answer, hidden in chats:
                with self.subTest(provider=chat.provider):
                    saved = write_chat(chat, Path(directory) / chat.provider)
                    markdown = Path(saved['md']).read_text()
                    canonical = json.loads((Path(saved['dir']) / 'chat.json').read_text())
                    visible = '\n'.join(turn['text'] for turn in canonical['turns'])
                    self.assertIn(answer, markdown)
                    self.assertIn(answer, visible)
                    self.assertTrue(canonical['had_thoughts'])
                    if hidden:
                        self.assertNotIn(hidden, markdown)
                        self.assertNotIn(hidden, visible)

    def test_reasoning_only_messages_keep_omission_metadata(self):
        data = {'chat_messages': [{'sender': 'assistant', 'content': [
            {'type': 'thinking', 'thinking': 'synthetic hidden reasoning'}]}]}
        chat = claude.parse_chat(data, 'synthetic', 'https://example.com')
        self.assertTrue(chat.had_thoughts)
        self.assertEqual(chat.visible_turns(), [])


class TestGoogleTransport(unittest.TestCase):
    def test_rpc_record_can_be_in_the_first_and_only_frame(self):
        payload = [None, None, [['c_synthetic', 'A title']]]
        self.assertEqual(decode_response(rpc_response('MaZiqc', payload), 'MaZiqc'), payload)

    def test_selects_requested_rpc_among_other_results_and_telemetry(self):
        records = [['wrb.fr', 'other', json.dumps(['wrong'])],
                   ['wrb.fr', 'MaZiqc', json.dumps(['correct'])]]
        self.assertEqual(decode_response(")]}'\n" + json.dumps(records), 'MaZiqc'), ['correct'])

    def test_line_delimited_response_with_unicode(self):
        records = json.dumps([['wrb.fr', 'MaZiqc', json.dumps(['𐐀'])]], ensure_ascii=False)
        text = ")]}'\n5\n[[0]]\n999\n" + records + '\n'
        self.assertEqual(decode_response(text, 'MaZiqc'), ['𐐀'])

    def test_wrong_rpc_missing_payload_and_login_html_fail(self):
        for text in (rpc_response('other', []), '<html>Sign in</html>',
                     json.dumps([['wrb.fr', 'MaZiqc', None]])):
            with self.subTest(text=text), self.assertRaises(RuntimeError):
                decode_response(text, 'MaZiqc')


class TestGoogleLogin(unittest.TestCase):
    def test_sign_in_page_carrying_the_destination_is_not_logged_in(self):
        ready = browser.on_host('aistudio.google.com')
        self.assertFalse(ready('https://accounts.google.com/v3/signin/accountchooser'
                               '?continue=https://aistudio.google.com/library'))
        self.assertFalse(ready('https://aistudio.google.com.example.net/library'))
        self.assertTrue(ready('https://aistudio.google.com/library'))


class TestGeminiCompatibility(unittest.TestCase):
    @staticmethod
    def turn(reply_id: str, when: int, question: str, answer: str, thinking=None) -> list:
        candidate: list = [None] * 38
        candidate[1] = [answer]
        candidate[37] = thinking
        return [['c_synthetic', reply_id], None, [[question]], [[candidate]], [when, 0]]

    def test_current_metadata_shape_and_newest_first_order(self):
        newest = self.turn('r2', 1780000100, 'Second question', 'Second answer')
        oldest = self.turn('r1', 1780000000, 'First question', 'First answer', ['synthetic hidden'])
        chat = googlegemini.read_to_chat(googlegemini.parse_read([[newest, oldest]]),
                                        'c_synthetic', 'https://example.com')
        self.assertEqual([turn.text for turn in chat.visible_turns()],
                         ['First question', 'First answer', 'Second question', 'Second answer'])
        self.assertTrue(chat.had_thoughts)
        self.assertNotIn('synthetic hidden', repr(chat))

    def test_cursor_pagination_and_payload(self):
        pages = [rpc_response('MaZiqc', [None, 'next &/=', [['c_one', 'First']]]),
                 rpc_response('MaZiqc', [None, None, [['c_two', 'Second']]])]
        with patch.object(googlegemini, 'rpc_call', side_effect=[(200, text) for text in pages]) as call:
            chats, reqid = googlegemini.list_chats(None, {}, 100)
        self.assertEqual([chat['id'] for chat in chats], ['c_one', 'c_two'])
        self.assertEqual(reqid, 102)
        self.assertEqual(call.call_args_list[1].args[3], [100, 'next &/=', [0, None, 1]])

    def test_repeated_cursor_and_unknown_shape_fail(self):
        for payloads in ([[None, 'same', [['c_one', 'First']]]] * 2,
                         [[None, None, {'changed': 'shape'}]]):
            with self.subTest(payloads=payloads):
                responses = [(200, rpc_response('MaZiqc', payload)) for payload in payloads]
                with patch.object(googlegemini, 'rpc_call', side_effect=responses):
                    with self.assertRaises(RuntimeError):
                        googlegemini.list_chats(None, {}, 1)

    def test_empty_or_unrecognized_read_cannot_create_successful_archive(self):
        with patch.object(googlegemini, 'rpc_call', return_value=(200, rpc_response('hNvQHb', []))):
            with self.assertRaisesRegex(RuntimeError, 'no readable messages'):
                googlegemini.fetch_chat(None, {}, 'c_synthetic', 1)


class TestChatGPTCompatibility(unittest.TestCase):
    def test_null_and_underestimated_totals_do_not_truncate(self):
        pages = [{'items': [{'id': 'first'}], 'total': None},
                 {'items': [{'id': 'second'}], 'total': 1}, {'items': [], 'total': None}]
        with patch.object(chatgpt, '_get', side_effect=pages) as get:
            chats = chatgpt.list_conversations(None, 'synthetic-token')
        self.assertEqual([chat['id'] for chat in chats], ['first', 'second'])
        self.assertIn('offset=2', get.call_args.args[2])

    def test_repeated_page_cannot_report_complete_history(self):
        with patch.object(chatgpt, '_get', return_value={'items': [{'id': 'same'}], 'total': None}):
            with self.assertRaisesRegex(RuntimeError, 'no progress'):
                chatgpt.list_conversations(None, 'synthetic-token')

    def test_interactive_reply_markdown_used_by_native_and_offline_import(self):
        node = message('<box>DIL.useState(...)</box>',
                       model_dil_v2={'fallbackMarkdown': '# Answer\n\nUse `x <escape><</escape> 2`.'})
        native, _ = chatgpt._node_to_turn(node)
        imported = chatgpt_import._node_to_turn(node)
        assert native is not None and imported is not None
        self.assertEqual(native.text, '# Answer\n\nUse `x < 2`.')
        self.assertEqual(native.text, imported.text)

    def test_code_content_and_thinking_preambles(self):
        node = message('ignored parts')
        node['message']['content'] = {'content_type': 'code', 'language': 'python',
                                      'text': 'print("synthetic")'}
        native, _ = chatgpt._node_to_turn(node)
        assert native is not None
        self.assertIn('```python\nprint("synthetic")\n```', native.text)
        node['message']['metadata']['is_thinking_preamble_message'] = True
        self.assertEqual(chatgpt._node_to_turn(node), (None, []))
        self.assertIsNone(chatgpt_import._node_to_turn(node))

    def test_chat_fetched_by_url_keeps_its_project_folder(self):
        def fetched(_page, _token, conv_id, _title=''):
            chat = chatgpt_import.parse_conversation(
                {'id': conv_id, 'title': f'Chat {conv_id}', 'current_node': 'one',
                 'mapping': {'one': message('Synthetic answer')}})
            assert chat is not None
            return chat, {'gizmo_type': 'snorlax', 'gizmo_id': 'g-p-synthetic'}
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(chatgpt, 'fetch_conversation', side_effect=fetched), \
                patch.object(chatgpt.time, 'sleep'):
            out = Path(tmp)
            chatgpt.run(None, 'synthetic-token', [{'id': 'url', 'title': ''}], out,
                        save_raw=False, project_names={'g-p-synthetic': 'Research'})
            chatgpt.run(None, 'synthetic-token', [{'id': 'listed', 'title': '', '_project': None}],
                        out, save_raw=False, project_names={'g-p-synthetic': 'Research'})
            self.assertTrue((out / 'Research' / 'Chat url' / 'Chat url.md').exists())
            self.assertTrue((out / 'Chat listed' / 'Chat listed.md').exists())

    def test_rate_limits_wait_for_retry_after_and_report_429(self):
        page = Mock()
        page.evaluate.side_effect = [{'status': 429, 'text': '', 'retryAfter': '7'},
                                     {'status': 429, 'text': '', 'retryAfter': None},
                                     {'status': 200, 'text': '{"ok": true}', 'retryAfter': None}]
        with patch.object(chatgpt.time, 'sleep') as sleep:
            self.assertEqual(chatgpt._get(page, 'synthetic-token', 'https://chatgpt.com/x'),
                             {'ok': True})
        self.assertEqual([c.args[0] for c in sleep.call_args_list],
                         [7.0, chatgpt.RATE_LIMIT_WAITS[1]])
        page.evaluate.side_effect = None
        page.evaluate.return_value = {'status': 429, 'text': '', 'retryAfter': None}
        with patch.object(chatgpt.time, 'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'HTTP 429: rate limited'):
                chatgpt._get(page, 'synthetic-token', 'https://chatgpt.com/x')
        self.assertEqual(sleep.call_count, len(chatgpt.RATE_LIMIT_WAITS))

    def test_tool_calls_are_skipped_but_missing_recipient_is_kept(self):
        search = message('{"search_query": [{"q": "synthetic"}]}')
        search['message']['recipient'] = 'web.run'
        search['message']['content'] = {'content_type': 'code', 'language': 'unknown',
                                        'text': '{"search_query": [{"q": "synthetic"}]}'}
        memory = message('Remember the synthetic preference.')
        memory['message']['recipient'] = 'bio'
        for node in (search, memory):
            self.assertEqual(chatgpt._node_to_turn(node), (None, []))
            self.assertIsNone(chatgpt_import._node_to_turn(node))
        answer = message('Visible answer')
        answer['message']['recipient'] = 'all'
        exported = message('Exported answer')  # official exports may omit recipient
        native, _ = chatgpt._node_to_turn(answer)
        imported = chatgpt_import._node_to_turn(exported)
        assert native is not None and imported is not None
        self.assertEqual(native.text, 'Visible answer')
        self.assertEqual(imported.text, 'Exported answer')

    def test_content_reference_markers_become_displayed_text(self):
        cite = '\ue200cite\ue202turn0search3\ue201'
        memory = '\ue200memcite\ue202turn0memory1\ue201'
        files = ['\ue200filecite\ue202turn1file0\ue201', '\ue200filecite\ue202turn1file1\ue201']
        link = '\ue200url\ue202Release notes\ue202turn0search0\ue201'
        entity = '\ue200entity\ue202["turn0business0","Cafe [One]"]\ue201'
        widget = '\ue200genui\ue202{"suggest_automation": {"label": "Remind me"}}\ue201'
        text = (f'Rates rose – twice. {cite}{memory}\nSee rules {files[0]} {files[1]}.\n'
                f'Read {link}. Visit {entity}.\n{widget}\nEnd \ue200unknown\ue202x\ue201.')
        refs = [
            {'type': 'grouped_webpages', 'matched_text': cite,
             'items': [{'title': 'Rates', 'url': 'https://example.com/rates',
                        'attribution': 'Example News'}]},
            {'type': 'hidden', 'matched_text': memory, 'safe_urls': []},
            *({'type': 'file', 'matched_text': m, 'name': 'rules.pdf'} for m in files),
            {'type': 'url', 'matched_text': link, 'title': 'Release notes',
             'safe_urls': ['https://example.com/release'],
             'item': {'url': 'https://example.com/release?utm_source=chatgpt.com'}},
            {'type': 'entity', 'matched_text': entity, 'name': 'Cafe [One]'},
            {'type': 'dil', 'matched_text': widget, 'alt': None},
            {'type': 'sources_footnote', 'matched_text': ' ',
             'sources': [{'title': 'Rates', 'url': 'https://example.com/rates',
                          'attribution': 'Example News'}]},
        ]
        node = message(text, content_references=refs)
        expected = ('Rates rose – twice. ([Example News](<https://example.com/rates>))\n'
                    'See rules (rules.pdf).\n'
                    'Read [Release notes](<https://example.com/release>). Visit Cafe \\[One\\].\n'
                    '\nEnd .\n\n'
                    '**Sources:**\n\n- [Example News](<https://example.com/rates>)')
        native, _ = chatgpt._node_to_turn(node)
        imported = chatgpt_import._node_to_turn(node)
        assert native is not None and imported is not None
        self.assertEqual(native.text, expected)
        self.assertEqual(imported.text, expected)

    def test_unclosed_marker_cannot_stall_the_parser(self):
        # CodeQL py/redos: the old pattern needed ~45 s for this input.
        import time
        from reclaim.providers import chatgpt_references
        text = '\ue200cite' + '\ue202' * 30 + ' trailing text'
        started = time.perf_counter()
        resolved = chatgpt_references.resolve(text, {})
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertEqual(resolved, 'cite trailing text')

    def test_unreferenced_markers_in_interactive_markdown(self):
        fallback = ('Go to \ue200url\ue202Docs\ue202https://example.com/docs\ue201 or '
                    '\ue200url\ue202Notes\ue202turn0search1\ue201 near '
                    '\ue200entity\ue202["turn0place0","Old Town"]\ue201.\n'
                    '\ue200image_group\ue202{"query": ["synthetic"]}\ue201'
                    '\ue200cite\ue202turn0search9\ue201 Done.')
        node = message('<box>widget</box>', model_dil_v2={'fallbackMarkdown': fallback})
        native, _ = chatgpt._node_to_turn(node)
        assert native is not None
        self.assertEqual(native.text, 'Go to [Docs](<https://example.com/docs>) or Notes '
                                      'near Old Town.\n Done.')

    def test_uploads_in_metadata_are_materialized_with_pdf_extension(self):
        node = message('Uploaded document', attachments=[{'id': 'file-synthetic', 'name': 'notes'}])
        conversation = {'mapping': {'one': node}, 'current_node': 'one'}
        with (patch.object(chatgpt, '_get', return_value=conversation),
              patch.object(chatgpt, '_download_asset',
                           return_value=(b'%PDF synthetic', {'mime_type': 'application/pdf'}))):
            chat, _ = chatgpt.fetch_conversation(None, 'synthetic-token', 'synthetic')
        self.assertEqual(chat.turns[0].attachments[0].filename, 'notes.pdf')
        self.assertEqual(chat.turns[0].attachments[0].data, b'%PDF synthetic')

    def test_project_cursor_is_encoded_and_cannot_loop(self):
        pages = [{'items': [], 'cursor': 'next &/='}] * 2
        with patch.object(chatgpt, '_get', side_effect=pages) as get:
            with self.assertRaisesRegex(RuntimeError, 'repeated project cursor'):
                chatgpt.list_projects(None, 'synthetic-token')
        self.assertIn('cursor=next+%26%2F%3D', get.call_args.args[2])


class TestClaudeCompatibility(unittest.TestCase):
    def test_short_offset_pages_do_not_hide_remaining_chats(self):
        pages = [[{'uuid': 'one'}], [{'uuid': 'two'}], []]
        with patch.object(claude, '_get', side_effect=pages) as get:
            chats = claude.list_chats(None, 'synthetic-org')
        self.assertEqual([chat['id'] for chat in chats], ['one', 'two'])
        self.assertIn('offset=2', get.call_args.args[1])

    def test_cursor_envelopes_and_legacy_offset_lists(self):
        pages = [{'conversations': [{'uuid': 'one', 'name': 'First'}], 'nextCursor': 'next &/='},
                 {'items': [{'uuid': 'two', 'name': 'Second'}]}]
        with patch.object(claude, '_get', side_effect=pages) as get:
            chats = claude.list_chats(None, 'synthetic-org')
        self.assertEqual([chat['id'] for chat in chats], ['one', 'two'])
        self.assertIn('cursor=next+%26%2F%3D', get.call_args.args[1])
        with (patch.object(claude, 'LIST_LIMIT', 1),
              patch.object(claude, '_get', side_effect=[[{'uuid': 'one'}], []]) as get):
            self.assertEqual(len(claude.list_chats(None, 'synthetic-org')), 1)
        self.assertIn('offset=1', get.call_args.args[1])

    def test_cycles_do_not_hang_message_traversal(self):
        messages = [{'uuid': 'one', 'parent_message_uuid': 'two'},
                    {'uuid': 'two', 'parent_message_uuid': 'one'}]
        self.assertEqual(len(claude.active_lineage(messages, 'two')), 2)
        mapping = {'root': {'children': ['one']}, 'one': {'parent': 'root', 'children': ['one']}}
        self.assertEqual(len(chatgpt_import._linearize(mapping, None)), 2)


class TestKimiCompatibility(unittest.TestCase):
    def test_string_text_blocks_and_tool_messages(self):
        turn, _ = kimi._msg_to_turn({'role': 'assistant', 'blocks': [{'type': 'TEXT', 'text': 'An answer'}]})
        assert turn is not None
        self.assertEqual(turn.text, 'An answer')
        self.assertEqual(kimi._msg_to_turn({'role': 'tool', 'content': 'Internal output'}), (None, []))

    def test_empty_final_page_ends_listing_but_unknown_shape_fails(self):
        pages = [{'chats': [{'id': 'one'}], 'nextPageToken': 'next'}, {}]
        with patch.object(kimi, '_api', side_effect=pages):
            self.assertEqual([c['id'] for c in kimi.list_chats(None, 'synthetic-token')], ['one'])
        with patch.object(kimi, '_api', return_value={'error': 'synthetic'}):
            with self.assertRaisesRegex(RuntimeError, 'list shape'):
                kimi.list_chats(None, 'synthetic-token')

    def test_repeated_cursor_and_reported_message_truncation_fail(self):
        data = {'chats': [{'id': 'one'}], 'nextPageToken': 'same'}
        with patch.object(kimi, '_api', return_value=data):
            with self.assertRaisesRegex(RuntimeError, 'repeated conversation cursor'):
                kimi.list_chats(None, 'synthetic-token')
        with patch.object(kimi, '_api', return_value={'messages': [], 'nextPageToken': 'more'}):
            with self.assertRaisesRegex(RuntimeError, 'refusing to save an incomplete chat'):
                kimi.list_messages(None, 'synthetic-token', 'synthetic-chat')


class TestDeepSeekCompatibility(unittest.TestCase):
    def test_single_pass_cursor_follows_each_rows_pinned_flag(self):
        # Shape seen live: pinned=true lists pinned chats first, then unpinned ones.
        pages = [{'chat_sessions': [{'id': 'pinned', 'pinned': True, 'updated_at': 300},
                                    {'id': 'pinned-old', 'pinned': True, 'updated_at': 50}],
                  'has_more': True},
                 {'chat_sessions': [{'id': 'pinned-old', 'pinned': True, 'updated_at': 50},
                                    {'id': 'recent', 'pinned': False, 'updated_at': 200}],
                  'has_more': True},
                 {'chat_sessions': [{'id': 'recent', 'pinned': False, 'updated_at': 200},
                                    {'id': 'older', 'pinned': False, 'updated_at': 100}],
                  'has_more': False}]
        with patch.object(deepseek, '_api_get', side_effect=pages) as get:
            chats = deepseek.list_chats(None)
        self.assertEqual([chat['id'] for chat in chats], ['pinned', 'pinned-old', 'recent', 'older'])
        self.assertEqual([call.args[2] for call in get.call_args_list], [
            {'lte_cursor.pinned': 'true'},
            {'lte_cursor.pinned': 'true', 'lte_cursor.updated_at': 50},
            {'lte_cursor.pinned': 'false', 'lte_cursor.updated_at': 200}])

    def test_stuck_pagination_is_an_error(self):
        # Live failure of the old two-pass listing: the same page again with has_more.
        data = {'chat_sessions': [{'id': 'same', 'pinned': False, 'updated_at': 100}],
                'has_more': True}
        with patch.object(deepseek, '_api_get', return_value=data):
            with self.assertRaisesRegex(RuntimeError, 'database may be incomplete'):
                deepseek.list_chats(None)
        missing_time = {'chat_sessions': [{'id': 'one'}], 'has_more': True}
        with patch.object(deepseek, '_api_get', return_value=missing_time):
            with self.assertRaisesRegex(RuntimeError, 'database may be incomplete'):
                deepseek.list_chats(None)

    def test_business_error_cannot_fall_back_to_cached_success(self):
        page = Mock()
        page.evaluate.return_value = {'status': 200, 'text': json.dumps(
            {'code': 0, 'data': {'biz_code': 7, 'biz_data': {'chat_sessions': []}}})}
        with self.assertRaisesRegex(RuntimeError, 'rejected the request'):
            deepseek._api_get(page, 'chat_session/fetch_page')

    def test_direct_content_and_active_branch_exclude_reasoning(self):
        record = {'code': 0, 'data': {'biz_data': {
            'chat_session': {'id': 'synthetic', 'current_message_id': 3},
            'chat_messages': [
                {'message_id': 1, 'parent_id': None, 'role': 'USER', 'content': 'A question'},
                {'message_id': 2, 'parent_id': 1, 'role': 'ASSISTANT', 'content': 'An old branch'},
                {'message_id': 3, 'parent_id': 1, 'role': 'ASSISTANT', 'content': 'The current answer',
                 'thinking_content': 'Synthetic hidden reasoning'},
            ]}}}
        chat = deepseek.record_to_chat(record)
        self.assertEqual([turn.text for turn in chat.visible_turns()], ['A question', 'The current answer'])
        self.assertTrue(chat.had_thoughts)
        self.assertNotIn('Synthetic hidden reasoning', repr(chat))

    def test_preview_merges_cached_ids_without_downloading_messages_or_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'archive'
            args = deepseek.parse_args(['--dry-run', '-o', str(output)])
            with (patch.object(deepseek, 'ensure_logged_in'),
                  patch.object(deepseek, 'list_chats', return_value=[{'id': 'remote', 'title': 'Remote'}]),
                  patch.object(deepseek, 'read_summaries', return_value=[{'id': 'cached', 'title': 'Cached'}]),
                  patch.object(deepseek, 'fetch_record') as fetch,
                  contextlib.redirect_stdout(io.StringIO()) as stdout):
                self.assertEqual(deepseek.run_session(None, args), 0)
            self.assertIn('Remote', stdout.getvalue())
            self.assertIn('Cached', stdout.getvalue())
            fetch.assert_not_called()
            self.assertFalse(output.exists())

    def test_selected_remote_chat_is_fetched_and_written(self):
        record = {'data': {'chat_session': {'id': 'remote', 'title': 'Synthetic remote'},
                           'chat_messages': [{'role': 'USER', 'content': 'A remote question'},
                                             {'role': 'ASSISTANT', 'content': 'A remote answer'}]}}
        summary = {'data': {'chat_session': {'id': 'remote', 'title': 'Synthetic remote'}}}
        with tempfile.TemporaryDirectory() as directory:
            with (patch.object(deepseek, 'fetch_record', return_value=record) as fetch,
                  contextlib.redirect_stdout(io.StringIO())):
                results = deepseek.run(None, [summary], Path(directory), fetch_missing=True, save_raw=False)
            self.assertTrue(results[0]['ok'])
            fetch.assert_called_once_with(None, 'remote')
            markdown = next(Path(directory).rglob('*.md')).read_text()
            self.assertIn('A remote question', markdown)
            self.assertIn('A remote answer', markdown)


if __name__ == '__main__':
    unittest.main()
