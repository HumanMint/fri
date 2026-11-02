"""Fri can't read a forwarded post yet: each one it drops is counted in the log, by IDs and the gate only."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from compat_tests.test_fri_mentions import attachment, environment

TEXT = 'a hot take nobody should quote'


def forward(message, *, snapshot=True, typed=True):
    """`message` as Discord delivers a forward: no text or files of its own, the copied post in a snapshot."""
    message.message_snapshots = [SimpleNamespace(content=TEXT, attachments=[], embeds=[])] if snapshot else []
    message.reference = SimpleNamespace(
        type=discord.MessageReferenceType.forward if typed else discord.MessageReferenceType.default,
        message_id=555, channel_id=4242, guild_id=9999, resolved=None)
    return message


def forward_lines(caplog):
    return [line for line in caplog.messages if line.startswith('Forward ')]


@pytest.mark.parametrize('shape', ['snapshot and type', 'snapshot only', 'type only'])
def test_a_forward_in_a_configured_channel_is_counted_as_dropped(monkeypatch, tmp_path, caplog, shape):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        event = forward(message('', user=30), snapshot=shape != 'type only', typed=shape != 'snapshot only')
        try:
            await agent.handle(event)
            assert forward_lines(caplog) == [f'Forward 1/{event.id} not read: no text']
            assert not agent.messages(1)  # Still dropped: this only counts them.
            fri.chat_once.assert_not_awaited()
            log = '\n'.join(caplog.messages)
            assert TEXT not in log and 'member 30' not in log
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_forward_outside_the_configured_channels_is_counted_too(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, calls, _ = environment(monkeypatch, tmp_path)  # Channel 8: pings only.
        event = forward(message('', user=30))
        try:
            await agent.handle(event)
            assert forward_lines(caplog) == [f'Forward 8/{event.id} not read: not a visible user mention']
            assert not calls
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_forward_in_a_dm_is_counted_without_its_channel(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path)
        agent.config['support_dms'] = True
        agent.member = AsyncMock(return_value=SimpleNamespace(id=20))
        event = forward(message('', user=20))
        event.guild = None
        try:
            await agent.handle(event)
            assert forward_lines(caplog) == [f'Forward dm/{event.id} not read: no text']
            fri.chat_once.assert_not_awaited()
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_reply_an_uncaptioned_meme_or_a_bots_forward_is_not_counted(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, channel, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        reply = message('', user=30)
        reply.reference = SimpleNamespace(type=discord.MessageReferenceType.default, message_id=555,
                                          channel_id=1, guild_id=123, resolved=None)
        meme = message('', user=40, attachments=[attachment(channel.id)])
        bot = forward(message('', user=50))
        bot.author.bot = True
        try:
            for event in (reply, meme, bot):
                await agent.handle(event)
            assert forward_lines(caplog) == []
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('gate', ['dms off', 'not a member'])
def test_a_forward_refused_at_a_dm_gate_is_counted_too(monkeypatch, tmp_path, caplog, gate):
    """fri #177: a DM refused before its text is read (DMs off, or its author left the server) still counts, by its
    message ID and that gate only."""
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path)
        agent.config['support_dms'] = gate != 'dms off'
        agent.member = AsyncMock(return_value=None if gate == 'not a member' else SimpleNamespace(id=20))
        event = forward(message('', user=20))
        event.guild = None
        plain = message('just saying hi', user=20)  # Not a forward: refused the same way, and not counted.
        plain.guild = None
        try:
            await agent.handle(event)
            await agent.handle(plain)
            assert forward_lines(caplog) == [f'Forward dm/{event.id} not read: {gate}']
            fri.chat_once.assert_not_awaited()
            log = '\n'.join(caplog.messages)
            assert TEXT not in log and 'just saying hi' not in log
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_dropped_forwards_are_counted_by_gate_in_fri_health_and_its_file(monkeypatch, tmp_path, caplog):
    """The log line counts forwards across restarts; fri health and its file count them since Fri started, by the
    gate only: no IDs, names or words of the forward or the post it copies."""
    import json
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        fri.data = tmp_path
        events = [forward(message('', user=30)), forward(message('', user=31))]
        try:
            await fri.health.coro(fri)
            assert json.loads((tmp_path/'fri-health.json').read_text())['outcomes']['forwards'] == {}
            assert not [line for line in (await fri.watcher.report()).splitlines() if 'forwards' in line]
            for event in events:
                await agent.handle(event)
            await agent.handle(forward(message('', user=32), snapshot=False, typed=False))  # No forward at all.
            await fri.health.coro(fri)
            outcomes = json.loads((tmp_path/'fri-health.json').read_text())['outcomes']
            assert outcomes['forwards'] == {'no text': 2}
            lines = [line for line in (await fri.watcher.report()).splitlines() if 'forwards' in line]
            assert lines == [f"- forwards not read since <t:{int(outcomes['since'])}:R>: 2 (no text ×2)"]
            # The whole snapshot but its time, so no field of it can hold an ID, a name or a channel in any form, one
            # the markers below don't name included (fri #286).
            assert {key: value for key, value in outcomes.items() if key != 'since'} == {
                'addressed': {}, 'answered': 0, 'cooldown': 0, 'forwards': {'no text': 2}, 'in_flight': 0,
                'latency': {'p50': None, 'p90': None, 'samples': 0}, 'outage': 0, 'quiet': {}, 'told_why': {},
                'withheld': {}}
            # The times aren't member data, and digits in them can match a short ID by chance ("…755574" has
            # 555), so they're left out of what's searched.
            shown = json.dumps({key: value for key, value in outcomes.items() if key != 'since'})
            shown += lines[0].replace(f"<t:{int(outcomes['since'])}:R>", '')
            for private in (*[event.id for event in events], TEXT, 'member 30', '"30"', '4242', '9999', '555'):
                assert str(private) not in shown
        finally:
            await fri.close()
    asyncio.run(scenario())
