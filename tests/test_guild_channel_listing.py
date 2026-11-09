"""A server with a channel of a type discord.py doesn't know (fri #397).

discord.py 2.7.1's Guild.fetch_channels() raises InvalidData for the whole listing when Discord returns a channel of a
type it has no class for (type 20 since November 2026). The patch notes' channel lookup failed with it. These use a real
discord.py Guild over a mocked HTTP layer, so the listing is discord.py's own.
"""
import asyncio
import types
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from fri import fri_patch_notes as notes

UNKNOWN = 20  # A channel type discord.py 2.7.1 has no class for.


def raw(channel_id, name, kind=0):
    """A channel as GET /guilds/{id}/channels answers it."""
    return {'id': str(channel_id), 'name': name, 'type': kind, 'position': 0, 'guild_id': '123',
            'permission_overwrites': []}


def guild(*channels):
    """A real discord.py Guild whose channel listing answers these."""
    state = Mock()
    found = discord.Guild(state=state, data={'id': '123', 'name': 'server', 'owner_id': '10', 'roles': [],
                                             'channels': [], 'members': []})
    state.http.get_all_guild_channels = AsyncMock(return_value=list(channels))
    return found


def test_discord_pys_own_listing_raises_for_the_whole_server():
    """What the callers met: one unknown type and nothing of the listing comes back."""
    with pytest.raises(discord.InvalidData):
        asyncio.run(guild(raw(777, 'fri-chat'), raw(888, 'something new', UNKNOWN)).fetch_channels())


@pytest.mark.parametrize('case,expected', [
    ('one text channel', 777),
    ('an unknown one with the name too', 777),
    ('only an unknown one with the name', None),
    ('two text channels', None),
])
def test_the_patch_notes_channel_is_found_beside_a_type_discord_py_doesnt_know(case, expected):
    """The patch notes go to the one text channel with the name. A channel of an unknown type is never one, and two
    text channels with the name, or none, are still refused."""
    channels = {
        'one text channel': [raw(777, 'fri-chat'), raw(888, 'something new', UNKNOWN)],
        'an unknown one with the name too': [raw(777, 'fri-chat'), raw(888, 'fri-chat', UNKNOWN)],
        'only an unknown one with the name': [raw(888, 'fri-chat', UNKNOWN), raw(777, 'general')],
        'two text channels': [raw(777, 'fri-chat'), raw(778, 'fri-chat'), raw(888, 'something new', UNKNOWN)],
    }[case]
    client = types.SimpleNamespace(fetch_guild=AsyncMock(return_value=guild(*channels)))

    async def scenario():
        return await notes.destination(client, owner_id=10, guild_id=123, channel='fri-chat')
    if expected is None:
        with pytest.raises(notes.NotesRefused):
            asyncio.run(scenario())
    else:
        found = asyncio.run(scenario())
        assert isinstance(found, discord.TextChannel) and found.id == expected
