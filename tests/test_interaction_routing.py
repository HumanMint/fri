"""Who may use Fri's commands and controls, at the boundary discord.py actually uses.

discord.py runs the command tree, persistent views, stored views and dynamic items inside its gateway parser, before
any `dispatch('interaction')`, so each command and control checks its own server, channel and person. These feed raw
gateway payloads through a real Fri's parser, with its real /fri command, Home panel and owner review buttons."""

import asyncio
import socket
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord_gateway import COMMAND, COMPONENT, deliver, interaction, recorded

from fri import fri as runtime
from fri.fri_protocol import contract
from fri.fri_requests import DraftView
from fri.jpcall.contract import Limits

GUILD, OWNER, BOT, WORKSHOP, CONFIGURED, ELSEWHERE, OTHER_GUILD, DM = 1, 10, 99, 3, 4, 5, 7, 70
MEMBER, STRANGER = 20, 30
REVIEW = 'fri:review:i:a:0123456789ab:' + 'a' * 40


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('network is forbidden in these tests')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket, 'getaddrinfo', deny)


async def started(monkeypatch, tmp_path, jpcall=None):
    """A Fri through its real setup_hook: /fri, the persistent Home panel and owner buttons are registered as in
    production. Only the controller, the agent's channel list and Discord's HTTP are synthetic."""
    monkeypatch.setattr(runtime.os, 'environ', {
        'GENE_GUILD_ID': str(GUILD), 'GENE_OWNER_ID': str(OWNER), 'GENE_WORKSHOP_CHANNEL_ID': str(WORKSHOP),
        'GENE_FRI_TOKEN': 'synthetic-token', 'FRI_DATA': str(tmp_path / 'data'),
        'FRI_CHATGPT_AUTH': str(tmp_path / 'unused-auth.json')})
    if jpcall is not None:
        stub = ModuleType('fri.jpcall.config')
        stub.load_config = lambda: jpcall
        monkeypatch.setitem(sys.modules, 'fri.jpcall.config', stub)
        engine = ModuleType('fri.jpcall.engine')
        engine.JpcallEngine = lambda cfg: SimpleNamespace(open=AsyncMock(), close=AsyncMock())
        monkeypatch.setitem(sys.modules, 'fri.jpcall.engine', engine)
    bot = runtime.Fri()
    bot.api = AsyncMock(side_effect=lambda method, path, payload=None: contract() if path == '/fri/contract' else {})
    bot.health = SimpleNamespace(start=MagicMock(), cancel=MagicMock())
    bot.owner_reviews = SimpleNamespace(start=MagicMock(), cancel=MagicMock())
    bot.keep_watch = AsyncMock()
    bot.tree.sync = AsyncMock()
    agent = SimpleNamespace(start=AsyncMock(), close=MagicMock(), member=AsyncMock(return_value=True),
                            allowed=lambda channel: getattr(channel, 'id', None) == CONFIGURED)
    monkeypatch.setattr('fri.fri_agent.Agent', lambda client: agent)
    session = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(runtime.aiohttp, 'ClientSession', lambda **kwargs: session)
    bot._connection.user = discord.ClientUser(state=bot._connection, data={
        'id': str(BOT), 'username': 'fri', 'discriminator': '0', 'avatar': None, 'bot': True})
    await bot._async_setup_hook()
    await bot.setup_hook()
    return bot


def fri_command(channel, guild=GUILD, user=MEMBER):
    return interaction(COMMAND, {'id': '500', 'name': 'fri', 'type': 1, 'guild_id': str(guild)},
                       user_id=user, channel_id=channel, guild_id=guild)


def paths(bot):
    return [call.args[1] for call in bot.api.await_args_list if call.args[1] != '/fri/contract']


def test_fri_command_opens_in_its_channels_and_says_where_elsewhere(monkeypatch, tmp_path):
    async def run():
        bot = await started(monkeypatch, tmp_path)
        try:
            for channel in (WORKSHOP, CONFIGURED):
                with recorded() as sent:
                    await deliver(bot, fri_command(channel))
                panels = [p for p in sent.responses() if p.get('embeds')]
                assert panels and panels[-1]['embeds'][0]['title'] == 'Fri'
            # In Fri's server but outside its channels: told privately where it opens, no panel.
            with recorded() as sent:
                await deliver(bot, fri_command(ELSEWHERE))
            [answer] = sent.responses()
            assert answer['type'] == 4 and answer['data']['flags'] == 64
            assert f'<#{WORKSHOP}>' in answer['data']['content'] and not answer['data'].get('embeds')
            # Another server learns nothing.
            with recorded() as sent:
                await deliver(bot, fri_command(ELSEWHERE, guild=OTHER_GUILD))
            assert sent == []
        finally:
            with recorded():  # Closing deletes the panels it opened.
                await bot.close()
    asyncio.run(run())


def test_persistent_home_controls_act_only_in_fri_channels(monkeypatch, tmp_path):
    """'Forget my chats' clears the member's saved chats: a click from elsewhere never reaches the controller."""
    async def run():
        bot = await started(monkeypatch, tmp_path)
        forget = {'custom_id': 'fri:home:forget', 'component_type': 2}
        try:
            for guild, channel in ((OTHER_GUILD, ELSEWHERE), (GUILD, ELSEWHERE)):
                with recorded():
                    await deliver(bot, interaction(COMPONENT, forget, user_id=MEMBER, channel_id=channel,
                                                   guild_id=guild, message_id=600))
                assert '/chat/clear' not in paths(bot)
            with recorded():
                await deliver(bot, interaction(COMPONENT, forget, user_id=MEMBER, channel_id=WORKSHOP,
                                               guild_id=GUILD, message_id=600))
            assert paths(bot) == ['/chat/clear']
        finally:
            with recorded():  # Closing deletes the panels it opened.
                await bot.close()
    asyncio.run(run())


def test_owner_review_buttons_act_only_for_the_owner_in_their_dm(monkeypatch, tmp_path):
    async def run():
        bot = await started(monkeypatch, tmp_path)
        review = {'custom_id': REVIEW, 'component_type': 2}
        try:
            for who, guild in ((STRANGER, None), (OWNER, GUILD)):  # Another member's DM, or a server channel.
                with recorded() as sent:
                    await deliver(bot, interaction(COMPONENT, review, user_id=who, channel_id=DM if guild is None
                                                   else WORKSHOP, guild_id=guild, message_id=601))
                assert paths(bot) == [] and sent == []
            with recorded():
                await deliver(bot, interaction(COMPONENT, review, user_id=OWNER, channel_id=DM, message_id=601))
            assert paths(bot) == ['/requests/0123456789ab/review']
        finally:
            with recorded():  # Closing deletes the panels it opened.
                await bot.close()
    asyncio.run(run())


def test_a_members_request_draft_answers_only_them(monkeypatch, tmp_path):
    async def run():
        bot = await started(monkeypatch, tmp_path)
        try:
            view = DraftView(bot, MEMBER, 'add a synthetic button', public=True)
            bot._connection.store_view(view, 602)
            click = {'custom_id': f'fri:request:preview:{view.source_id}', 'component_type': 2}
            with recorded() as sent:
                await deliver(bot, interaction(COMPONENT, click, user_id=STRANGER, channel_id=WORKSHOP,
                                               guild_id=GUILD, message_id=602))
            [answer] = sent.responses()
            assert answer['data']['flags'] == 64 and 'Review your request' not in str(answer)
            with recorded() as sent:
                await deliver(bot, interaction(COMPONENT, click, user_id=MEMBER, channel_id=WORKSHOP,
                                               guild_id=GUILD, message_id=602))
            assert any('Review your request' in str(payload) for payload in sent.responses())
        finally:
            with recorded():  # Closing deletes the panels it opened.
                await bot.close()
    asyncio.run(run())


def test_jpcall_command_keeps_its_own_allowlist_outside_fri_channels(monkeypatch, tmp_path):
    """/jpcall skips the /fri panel's channel check, never its own allowlist and voice checks."""
    async def run():
        cfg = SimpleNamespace(enabled=True, limits=Limits(allowed_user_ids=frozenset({MEMBER})), disabled_reasons=())
        bot = await started(monkeypatch, tmp_path, jpcall=cfg)
        options = [{'name': 'number', 'type': 3, 'value': '+819000000000'},
                   {'name': 'goal', 'type': 3, 'value': 'opening hours'}]
        command = {'id': '501', 'name': 'jpcall', 'type': 1, 'guild_id': str(GUILD), 'options': options}
        try:
            told = {}
            for who in (STRANGER, MEMBER):
                with recorded() as sent:
                    await deliver(bot, interaction(COMMAND, command, user_id=who, channel_id=ELSEWHERE,
                                                   guild_id=GUILD))
                [answer] = sent.responses()
                assert answer['data']['flags'] == 64
                told[who] = answer['data']['content']
            assert told == {STRANGER: 'this command is not available to you.', MEMBER: 'join a voice channel first.'}
        finally:
            with recorded():  # Closing deletes the panels it opened.
                await bot.close()
    asyncio.run(run())
