"""User-visible GUI behavior through Flet's device tester."""

import asyncio

import flet.testing as ftt


async def test_opens_and_shows_every_provider(flet_app: ftt.FletTestApp):
    tester = flet_app.tester
    await tester.pump_and_settle()
    for provider in ('chatgpt', 'claude', 'googlegemini', 'googleaistudio',
                     'deepseek', 'kimi'):
        assert (await tester.find_by_key(f'provider-{provider}')).count == 1
    assert (await tester.find_by_key('archive-path')).count == 1
    assert (await tester.find_by_text('0 chats archived')).count == 1


async def test_update_selected_and_dry_run(flet_app: ftt.FletTestApp):
    tester = flet_app.tester
    await tester.pump_and_settle()
    await tester.tap(await tester.find_by_key('provider-kimi'))
    await tester.tap(await tester.find_by_key('update-selected'))
    await asyncio.sleep(0.5)
    await tester.pump_and_settle()
    assert (await tester.find_by_text(
        'Update completed successfully · Google AI Studio: ok, DeepSeek: ok, '
        'ChatGPT: ok, Claude: ok, Google Gemini: ok')).count == 1
    await tester.tap(await tester.find_by_key('dry-run'))
    await asyncio.sleep(0.5)
    await tester.pump_and_settle()
    assert (await tester.find_by_text_containing('Dry run completed successfully')).count == 1
    assert (await tester.find_by_key('run-progress')).count == 1


async def test_rebuild_confirmation_and_failure(flet_app: ftt.FletTestApp):
    tester = flet_app.tester
    await tester.pump_and_settle()
    await tester.tap(await tester.find_by_key('rebuild'))
    await tester.pump_and_settle()
    assert (await tester.find_by_key('confirm-rebuild')).count == 1
    assert (await tester.find_by_text_containing('Rebuild completed')).count == 0
    await tester.tap(await tester.find_by_key('confirm-rebuild'))
    await asyncio.sleep(0.5)
    await tester.pump_and_settle()
    assert (await tester.find_by_text_containing('Rebuild completed with errors')).count == 1


async def test_settings_open_and_archive_path_visible(flet_app: ftt.FletTestApp):
    tester = flet_app.tester
    await tester.pump_and_settle()
    await tester.tap(await tester.find_by_key('settings'))
    await tester.pump_and_settle()
    assert (await tester.find_by_key('settings-archive')).count == 1
    assert (await tester.find_by_key('settings-chatgpt')).count == 1
    assert (await tester.find_by_key('save-settings')).count == 1
    await tester.tap(await tester.find_by_key('settings-kimi'))
    await tester.tap(await tester.find_by_key('save-settings'))
    await tester.pump_and_settle()
    await tester.tap(await tester.find_by_key('update-all'))
    await asyncio.sleep(0.5)
    await tester.pump_and_settle()
    assert (await tester.find_by_text(
        'Update completed successfully · Google AI Studio: ok, DeepSeek: ok, '
        'ChatGPT: ok, Claude: ok, Google Gemini: ok')).count == 1
