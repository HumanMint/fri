import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

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


def test_fri_identity_keeps_its_intents_and_invisible_status(monkeypatch):
    """Commands and controls each check their own server, channel and person (tests/test_interaction_routing.py):
    discord.py routes them before any dispatch, so Fri keeps no interaction filter there."""
    async def scenario():
        fri = configured(monkeypatch)
        assert fri.intents.message_content is True
        assert fri._connection._status == 'invisible'
        assert 'dispatch' not in vars(Fri)
        await fri.close()
    asyncio.run(scenario())
