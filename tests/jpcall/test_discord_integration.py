"""Offline lifecycle and routing through the real Fri/discord.py command tree."""

import asyncio
import builtins
import socket
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from fri import fri as runtime
from fri.fri_protocol import contract
from fri.jpcall import devbot, discord_runtime
from fri.jpcall.contract import CallBrief, Limits


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('network is forbidden in these tests')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket, 'getaddrinfo', deny)


def module(monkeypatch, name, **attrs):
    stub = ModuleType(name)
    stub.__dict__.update(attrs)
    monkeypatch.setitem(sys.modules, name, stub)
    return stub


def config(enabled=True):
    return SimpleNamespace(enabled=enabled, limits=Limits(allowed_user_ids=frozenset({10})),
                           disabled_reasons=() if enabled else ('Missing JPCALL_OPENAI_API_KEY',))


def engine_stub(monkeypatch, cfg, events):
    engine = SimpleNamespace(open=AsyncMock(side_effect=lambda: events.append('open')),
                             close=AsyncMock(side_effect=lambda: events.append('engine close')),
                             plan=AsyncMock(side_effect=lambda request: CallBrief(
                                 request.request_id, 'book dinner.', 'AIです。', (), ())))

    def construct(received):
        assert received is cfg
        events.append('construct')
        return engine

    module(monkeypatch, 'fri.jpcall.engine', JpcallEngine=construct)
    return engine


def fri_bot(monkeypatch, tmp_path, cfg, *, load=None):
    monkeypatch.setattr(runtime.os, 'environ', {
        'GENE_GUILD_ID': '1', 'GENE_OWNER_ID': '99', 'GENE_WORKSHOP_CHANNEL_ID': '3',
        'GENE_FRI_TOKEN': 'synthetic-token', 'FRI_DATA': str(tmp_path / 'data'),
        'FRI_CHATGPT_AUTH': str(tmp_path / 'unused-auth.json'),
    })
    module(monkeypatch, 'fri.jpcall.config', load_config=load or (lambda: cfg))
    bot = runtime.Fri()
    bot.api = AsyncMock(return_value=contract())
    bot.health = SimpleNamespace(start=MagicMock(), cancel=MagicMock())
    bot.owner_reviews = SimpleNamespace(start=MagicMock(), cancel=MagicMock())
    bot.keep_watch = AsyncMock()
    bot.add_view = MagicMock()
    bot.add_dynamic_items = MagicMock()
    agent = SimpleNamespace(start=AsyncMock(), close=MagicMock(), allowed=lambda channel: False)
    module(monkeypatch, 'fri.fri_agent', Agent=lambda client: agent)
    monkeypatch.setattr(runtime, 'persistent_buttons', lambda: ())
    monkeypatch.setattr(runtime, 'HomeView', lambda client: discord.ui.View())
    session = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(runtime.aiohttp, 'ClientSession', lambda **kwargs: session)
    monkeypatch.setattr(runtime, 'sessions', lambda client: SimpleNamespace(close_all=AsyncMock()))
    return bot


def interaction(kind=discord.InteractionType.application_command, *, data=None,
                guild=1, channel=2):
    return SimpleNamespace(type=kind, data=data or {'name': 'jpcall'}, guild_id=guild,
                           channel_id=channel, channel=SimpleNamespace(id=channel, parent_id=None),
                           user=SimpleNamespace(id=10, bot=False), message=None)


@pytest.mark.parametrize('enabled', [False, True])
def test_fri_lifecycle_registers_before_sync_only_when_enabled(monkeypatch, tmp_path, enabled):
    async def run():
        cfg, events = config(enabled), []
        bot = fri_bot(monkeypatch, tmp_path, cfg)
        engine = engine_stub(monkeypatch, cfg, events)

        async def sync(**kwargs):
            commands = {command.name for command in bot.tree.get_commands(guild=kwargs['guild'])}
            assert commands == ({'fri', 'jpcall'} if enabled else {'fri'})
            assert events == (['construct', 'open'] if enabled else [])
            events.append('sync')

        bot.tree.sync = AsyncMock(side_effect=sync)
        assert bot.intents.voice_states is enabled
        # All pre-existing intents are preserved.
        assert bot.intents.guilds and bot.intents.guild_messages and bot.intents.dm_messages
        assert bot.intents.message_content and not bot.intents.members
        await bot.setup_hook()
        assert bot.api.await_count == 1  # The pre-existing startup contract read only.
        if enabled:
            assert bot.jpcall_calls.limits is cfg.limits
            assert bot.jpcall_calls.engine is engine
        else:
            assert bot.jpcall_engine is bot.jpcall_calls is None
            engine.open.assert_not_awaited()
        await bot.close()
        assert events == (['construct', 'open', 'sync', 'engine close'] if enabled else ['sync'])
        bot.session.close.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize('enabled', [False, True])
def test_dispatch_and_tree_route_only_narrow_jpcall_interactions(monkeypatch, tmp_path, enabled):
    async def run():
        cfg, events, forwarded = config(enabled), [], []
        bot = fri_bot(monkeypatch, tmp_path, cfg)
        engine_stub(monkeypatch, cfg, events)
        bot.tree.sync = AsyncMock()
        await bot.setup_hook()
        monkeypatch.setattr(discord.Client, 'dispatch',
                            lambda self, event, *args, **kwargs: forwarded.append((event, args)))
        cases = [
            (interaction(), enabled),
            (interaction(discord.InteractionType.component, data={'custom_id': 'jpcall:call'}), enabled),
            (interaction(discord.InteractionType.modal_submit,
                         data={'custom_id': 'jpcall:submit:test'}), enabled),
            (interaction(data={'name': 'fri'}), False),
            (interaction(data={'name': 'jpcall-extra'}), False),
            (interaction(data={'name': 'jpcall'}, guild=8), False),
            (interaction(data={'name': 'jpcall'}, guild=None), False),
            (interaction(discord.InteractionType.component,
                         data={'custom_id': 'other:jpcall:call'}), False),
            (interaction(discord.InteractionType.application_command,
                         data={'custom_id': 'jpcall:call', 'name': 'fri'}), False),
            (interaction(discord.InteractionType.component, data={'name': 'jpcall'}), False),
            (interaction(data={'name': 'fri'}, channel=3), True),
        ]
        for event, expected in cases:
            before = len(forwarded)
            bot.dispatch('interaction', event)
            assert (len(forwarded) > before) is expected
            assert bool(await bot.tree.interaction_check(event)) is expected
        bot.dispatch('ready')
        assert forwarded[-1] == ('ready', ())
        assert bot.api.await_count == 1
        await bot.close()
    asyncio.run(run())


def test_voice_command_uses_own_allowlist_and_engine_without_gene_calls(monkeypatch, tmp_path):
    async def run():
        cfg, events = config(), []
        bot = fri_bot(monkeypatch, tmp_path, cfg)
        engine = engine_stub(monkeypatch, cfg, events)
        bot.tree.sync = AsyncMock()
        await bot.setup_hook()
        voice = MagicMock(spec=discord.VoiceChannel)
        voice.id = 2
        message = SimpleNamespace(edit=AsyncMock())
        voice.send = AsyncMock(return_value=message)
        event = interaction()
        event.user.voice = SimpleNamespace(channel=voice)
        event.response = SimpleNamespace(defer=AsyncMock(), is_done=lambda: True)
        event.edit_original_response = AsyncMock()
        event.followup = SimpleNamespace(send=AsyncMock())
        assert await bot.tree.interaction_check(event)
        command = bot.tree.get_command('jpcall', guild=discord.Object(id=1))
        await command.callback(event, '+810000000000', 'dinner')
        engine.plan.assert_awaited_once()
        voice.send.assert_awaited_once()
        assert bot.api.await_count == 1
        event.user.id = 11
        await command.callback(event, '+810000000000', 'dinner')
        event.followup.send.assert_awaited_once()
        assert event.followup.send.call_args.kwargs['ephemeral'] is True
        assert engine.plan.await_count == bot.api.await_count == 1
        await bot.close()
    asyncio.run(run())


def test_fri_closes_engine_on_sync_failure(monkeypatch, tmp_path):
    async def run():
        cfg, events = config(), []
        bot = fri_bot(monkeypatch, tmp_path, cfg)
        engine = engine_stub(monkeypatch, cfg, events)
        bot.tree.sync = AsyncMock()
        bot.tree.sync.side_effect = RuntimeError('synthetic failure')
        with pytest.raises(RuntimeError):
            await bot.setup_hook()
        await bot.close()
        assert engine.close.await_count >= 1
        bot.session.close.assert_awaited_once()
    asyncio.run(run())


def test_fri_closes_engine_and_existing_resources_when_call_cleanup_fails(monkeypatch, tmp_path):
    async def run():
        cfg, events = config(), []
        bot = fri_bot(monkeypatch, tmp_path, cfg)
        engine = engine_stub(monkeypatch, cfg, events)
        bot.tree.sync = AsyncMock()
        await bot.setup_hook()
        bot.jpcall_calls.close = AsyncMock(side_effect=RuntimeError('synthetic failure'))
        with pytest.raises(RuntimeError):
            await bot.close()
        engine.close.assert_awaited_once()
        bot.session.close.assert_awaited_once()
        assert bot.is_closed()
    asyncio.run(run())


def test_real_devbot_lifecycle_and_configured_limits(monkeypatch):
    async def run():
        cfg, events = config(), []
        engine = engine_stub(monkeypatch, cfg, events)
        bot = devbot.DevBot(None, 1, cfg.limits, config=cfg)

        async def sync(**kwargs):
            assert bot.tree.get_command('jpcall', guild=kwargs['guild'])
            assert events == ['construct', 'open']

        bot.tree.sync = AsyncMock(side_effect=sync)
        await bot.setup_hook()
        assert bot.calls.limits is cfg.limits and bot.engine is engine
        await bot.close()
        assert events == ['construct', 'open', 'engine close']
    asyncio.run(run())


def test_fake_devbot_never_imports_engine_config_or_transport(monkeypatch):
    original = builtins.__import__

    def isolated(name, *args, **kwargs):
        assert name not in {'config', 'engine', 'fri.jpcall.config', 'fri.jpcall.engine'}
        return original(name, *args, **kwargs)

    async def run():
        monkeypatch.setattr(builtins, '__import__', isolated)
        bot = devbot.DevBot(devbot.FakeEngine(), 1, Limits())
        bot.tree.sync = AsyncMock()
        await bot.setup_hook()
        await bot.close()
    asyncio.run(run())


def test_devbot_disabled_reports_setting_names_without_values(monkeypatch, capsys):
    cfg = config(False)
    cfg.disabled_reasons += ('TWILIO_FROM_NUMBER invalid: synthetic-secret-value',)
    module(monkeypatch, 'fri.jpcall.config', load_config=lambda: cfg)
    monkeypatch.setattr(devbot.os, 'environ', {'JPCALL_DEV_DISCORD_TOKEN': 'synthetic-token',
                                             'JPCALL_DEV_GUILD_ID': '1'})
    monkeypatch.setattr(sys, 'argv', ['devbot'])
    monkeypatch.setattr(devbot.DevBot, 'run', lambda *a, **k: pytest.fail('must not connect'))
    with pytest.raises(SystemExit) as result:
        devbot.main()
    assert result.value.code == 2
    error = capsys.readouterr().err
    assert 'JPCALL_OPENAI_API_KEY' in error and 'TWILIO_FROM_NUMBER' in error
    assert 'synthetic-secret-value' not in error and 'synthetic-token' not in error


def test_devbot_main_uses_process_config_and_real_engine_mode(monkeypatch):
    cfg, seen = config(), []
    module(monkeypatch, 'fri.jpcall.config', load_config=lambda: cfg)
    monkeypatch.setattr(devbot.os, 'environ', {'JPCALL_DEV_DISCORD_TOKEN': 'synthetic-token',
                                             'JPCALL_DEV_GUILD_ID': '1'})
    monkeypatch.setattr(sys, 'argv', ['devbot'])
    monkeypatch.setattr(devbot.DevBot, 'run', lambda self, token, **kwargs: seen.append(self))
    devbot.main()
    assert len(seen) == 1 and seen[0].config is cfg
    assert seen[0].engine is None and seen[0].limits is cfg.limits


def test_optional_config_only_tolerates_the_absent_engine_module(monkeypatch):
    monkeypatch.setitem(sys.modules, 'fri.jpcall.config', None)
    assert discord_runtime.optional_config() is None

    def broken():
        raise ModuleNotFoundError('synthetic dependency missing', name='missing_dependency')

    monkeypatch.setattr(discord_runtime, 'load_config', broken)
    with pytest.raises(ModuleNotFoundError, match='synthetic dependency missing'):
        discord_runtime.optional_config()


@pytest.mark.parametrize('failure', ['open', 'sync'])
def test_real_devbot_closes_engine_on_setup_failure(monkeypatch, failure):
    async def run():
        cfg, events = config(), []
        engine = engine_stub(monkeypatch, cfg, events)
        bot = devbot.DevBot(None, 1, cfg.limits, config=cfg)
        bot.tree.sync = AsyncMock()
        if failure == 'open':
            engine.open.side_effect = RuntimeError('synthetic failure')
        else:
            bot.tree.sync.side_effect = RuntimeError('synthetic failure')
        with pytest.raises(RuntimeError):
            await bot.setup_hook()
        engine.close.assert_awaited_once()
        await bot.close()
    asyncio.run(run())


def test_real_devbot_cannot_override_configured_allowlist(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['devbot', '--allow-user', '11'])
    with pytest.raises(SystemExit):
        devbot.main()
    assert 'JPCALL_ALLOWED_USER_IDS' in capsys.readouterr().err


@pytest.mark.parametrize('enabled', [False, True])
def test_jpcall_command_errors_stay_private_and_do_not_open_legacy_panels(
        monkeypatch, tmp_path, enabled):
    async def run():
        cfg, events = config(enabled), []
        bot = fri_bot(monkeypatch, tmp_path, cfg)
        engine_stub(monkeypatch, cfg, events)
        bot.tree.sync = AsyncMock()
        await bot.setup_hook()
        legacy_reply = AsyncMock()
        monkeypatch.setattr(runtime, 'reply', legacy_reply)
        event = interaction()
        event.response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock())
        await bot.tree.on_error(event, RuntimeError('synthetic-secret'))
        if enabled:
            legacy_reply.assert_not_awaited()
            event.response.send_message.assert_awaited_once()
            assert event.response.send_message.call_args.kwargs['ephemeral'] is True
            assert event.response.send_message.call_args.args == (
                'that call did not go through. try again.',)
        else:
            legacy_reply.assert_awaited_once()
        assert bot.api.await_count == 1
        await bot.close()
    asyncio.run(run())


@pytest.mark.parametrize('value', ['private-invalid-user-id', 'private-invalid-port'])
def test_malformed_jpcall_config_keeps_fri_starting(monkeypatch, tmp_path, caplog, value):
    async def run():
        def malformed_config():
            int(value)  # The loader's exception includes the invalid value; the warning must not.

        cfg, events = config(), []
        bot = fri_bot(monkeypatch, tmp_path, cfg, load=malformed_config)
        engine = engine_stub(monkeypatch, cfg, events)
        bot.tree.sync = AsyncMock()
        await bot.setup_hook()
        assert bot.jpcall_config is bot.jpcall_engine is bot.jpcall_calls is None
        assert not bot.intents.voice_states
        assert not bot._connection.member_cache_flags.voice
        assert {cmd.name for cmd in bot.tree.get_commands(guild=discord.Object(id=1))} == {'fri'}
        bot.tree.sync.assert_awaited_once()
        bot.agent.start.assert_awaited_once()
        bot.health.start.assert_called_once()
        bot.owner_reviews.start.assert_called_once()
        assert bot.api.await_count == 1
        assert await bot.tree.interaction_check(interaction(data={'name': 'fri'}, channel=3))
        assert not await bot.tree.interaction_check(interaction())
        engine.open.assert_not_awaited()
        assert [record.getMessage() for record in caplog.records] == ['jpcall disabled: ValueError']
        assert all(record.exc_info is None for record in caplog.records)
        await bot.close()
        engine.close.assert_not_awaited()
        bot.session.close.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize('cleanup_fails', [False, True])
def test_jpcall_open_failure_disables_only_jpcall(monkeypatch, tmp_path, caplog, cleanup_fails):
    async def run():
        cfg, events = config(), []
        bot = fri_bot(monkeypatch, tmp_path, cfg)
        engine = engine_stub(monkeypatch, cfg, events)
        engine.open.side_effect = OSError('private-port-details')
        if cleanup_fails:
            engine.close.side_effect = RuntimeError('private-cleanup-details')

        async def sync(**kwargs):
            assert {cmd.name for cmd in bot.tree.get_commands(guild=kwargs['guild'])} == {'fri'}
            assert bot.jpcall_engine is bot.jpcall_calls is None
            assert not bot.intents.voice_states
            assert not bot._connection.member_cache_flags.voice

        bot.tree.sync = AsyncMock(side_effect=sync)
        await bot.setup_hook()
        engine.close.assert_awaited_once()
        bot.tree.sync.assert_awaited_once()
        bot.agent.start.assert_awaited_once()
        bot.health.start.assert_called_once()
        bot.owner_reviews.start.assert_called_once()
        assert bot.api.await_count == 1
        assert await bot.tree.interaction_check(interaction(data={'name': 'fri'}, channel=3))
        assert not await bot.tree.interaction_check(interaction())
        expected = ['jpcall disabled: OSError']
        if cleanup_fails:
            expected.insert(0, 'jpcall cleanup failed: RuntimeError')
        assert [record.getMessage() for record in caplog.records] == expected
        assert all(record.exc_info is None for record in caplog.records)
        await bot.close()
        engine.close.assert_awaited_once()
        bot.session.close.assert_awaited_once()
    asyncio.run(run())
