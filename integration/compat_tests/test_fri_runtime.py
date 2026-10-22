import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from fri.fri import Fri


def configured(monkeypatch):
    for key, value in {'GENE_GUILD_ID': '123', 'GENE_OWNER_ID': '10',
                       'GENE_WORKSHOP_CHANNEL_ID': '456', 'GENE_FRI_TOKEN': 'private-test-value'}.items():
        monkeypatch.setenv(key, value)
    return Fri()


def event(user=10, channel=456, guild=123, name='member', kind='message', content='yo'):
    author = SimpleNamespace(id=user, display_name=name)
    if kind == 'interaction':
        return Mock(spec=discord.Interaction, id=100000000000000001, user=author,
                    channel_id=channel, guild_id=guild)
    return Mock(spec=discord.Message, id=100000000000000001, author=author, content=content,
                channel=SimpleNamespace(id=channel), guild=SimpleNamespace(id=guild) if guild else None)


def test_fri_dispatch_gates_other_servers_channels_and_dm_messages(monkeypatch):
    async def scenario():
        fri = configured(monkeypatch)
        assert fri.intents.message_content is True
        assert fri._connection._status == 'invisible'
        fri._connection.user = SimpleNamespace(id=99)
        event = SimpleNamespace(guild_id=123, channel_id=456, channel=SimpleNamespace(id=456),
            type=discord.InteractionType.component, user=SimpleNamespace(id=10),
            message=SimpleNamespace(author=SimpleNamespace(id=99)),
            data={'custom_id': 'fri:review:i:a:012345abcdef:' + 'a' * 64})
        with patch.object(discord.Client, 'dispatch') as dispatch:
            fri.dispatch('interaction', event)
            assert dispatch.call_count == 1
            event.guild_id = 999
            fri.dispatch('interaction', event)
            assert dispatch.call_count == 1
            event.guild_id = None
            fri.dispatch('interaction', event)
            assert dispatch.call_count == 2
            event.user.id = 20
            fri.dispatch('interaction', event)
            assert dispatch.call_count == 2
            event.guild_id, event.channel_id = 123, 111
            fri.dispatch('interaction', event)
            assert dispatch.call_count == 2
        fri.talk = AsyncMock()
        message = SimpleNamespace(author=SimpleNamespace(bot=False), guild=None)
        await fri.on_message(message)
        message.guild = SimpleNamespace(id=123)
        message.channel = SimpleNamespace(id=456)
        message.mentions = []
        message.content = 'just chatting'
        await fri.on_message(message)
        fri.talk.assert_not_awaited()
        await fri.close()
    asyncio.run(scenario())
