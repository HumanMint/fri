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
