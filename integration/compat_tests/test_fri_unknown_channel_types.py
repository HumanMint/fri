"""A server with a channel of a type discord.py doesn't know (fri #397).

discord.py 2.7.1's Guild.fetch_channels() raises InvalidData for the whole listing when Discord returns a channel of a
type it has no class for (type 20 since November 2026). Two more callers than the patch notes' lookup met it:

- The sweep for deletions Fri missed lists the server's channels whenever one the controller holds is missing from the
  cache. It raised on every tick, so such a deletion was never withdrawn. A channel of an unknown type is still there:
  it's never withdrawn for that.
- Workspace capabilities with no channel named listed the server's channels too. One of an unknown type isn't a text
  channel, so it's left out, as other kinds are.

The listings here are discord.py's own, over a mocked HTTP layer.
"""
import asyncio
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from compat_tests.test_fri_channel_withdrawals import (
    asked,
    gone,
    landed,
    needs_channels,
    withdrawals,
)
from compat_tests.test_fri_member_profiles import GAMING, GENERAL, note, run, world
from fri.fri_workspace import capabilities

UNKNOWN = 20  # A channel type discord.py 2.7.1 has no class for.


def raw(channel_id, name, kind=0):
    """A channel as GET /guilds/{id}/channels answers it."""
    return {'id': str(channel_id), 'name': name, 'type': kind, 'position': 0, 'guild_id': '123',
            'permission_overwrites': []}


def listing(guild, *channels):
    """The guild's channel listing as discord.py makes it, from these raw channels."""
    guild._state = SimpleNamespace(http=SimpleNamespace(get_all_guild_channels=AsyncMock(return_value=list(channels))))
    guild.fetch_channels = MethodType(discord.Guild.fetch_channels, guild)


def test_a_deletion_fri_missed_is_found_beside_a_channel_of_an_unknown_type(monkeypatch, tmp_path):
    """#gaming was deleted while Fri was down. The server also has a channel of a type discord.py doesn't know: the
    sweep still lists the server's channels, finds #gaming gone, and withdraws it."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        needs_channels(w)
        try:
            await w.say(GAMING, 'fri I like chess', 'cool', note('I like chess'))
            await w.say(GENERAL, 'fri I like Minecraft', 'cubes', note('I like Minecraft'))
            w.channels.pop(GAMING)
            guild = w.fri.get_guild(123)
            listing(guild, raw(GENERAL, 'general'), raw(888, 'something new', UNKNOWN))
            guild.fetch_channel = AsyncMock(side_effect=gone(10003))
            await landed(w)
            assert [body['channel_ids'][0] for body in withdrawals(w)] == [str(GAMING)]
            assert await asked(w) == ['I like Minecraft']
        finally:
            await w.fri.close()
    run(scenario)


def test_a_held_channel_listed_with_an_unknown_type_is_never_withdrawn(monkeypatch, tmp_path):
    """#gaming isn't in the cache (the gateway skips a type discord.py doesn't know), but the server lists it, as a
    type discord.py has no class for: it's there, so nothing is withdrawn."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        needs_channels(w)
        try:
            await w.say(GAMING, 'fri I like chess', 'cool', note('I like chess'))
            w.channels.pop(GAMING)
            guild = w.fri.get_guild(123)
            listing(guild, raw(GENERAL, 'general'), raw(GAMING, 'gaming', UNKNOWN))
            guild.fetch_channel = AsyncMock(side_effect=gone(10003))
            await landed(w)
            assert withdrawals(w) == [] and await asked(w) == ['I like chess']
            guild.fetch_channel.assert_not_awaited()
        finally:
            await w.fri.close()
    run(scenario)


def test_workspace_capabilities_list_the_server_beside_a_channel_of_an_unknown_type():
    """No channel named: the server's channels are listed, and one of an unknown type is left out, as any channel
    that isn't a text channel is."""
    async def scenario():
        state = Mock()
        state.store_user = lambda data: discord.User(state=state, data=data)
        guild = discord.Guild(state=state, data={'id': '123', 'name': 'ff', 'owner_id': '10',
            'roles': [{'id': '123', 'name': '@everyone', 'permissions': str(discord.Permissions(view_channel=True,
                read_message_history=True, send_messages=True).value)}], 'channels': [], 'members': []})
        members = {uid: discord.Member(data={'user': {'id': str(uid), 'username': str(uid), 'discriminator': '0',
                                                      'avatar': None}, 'roles': [], 'flags': 0},
                                       guild=guild, state=state) for uid in (10, 50)}
        outer = Mock(wraps=guild)
        outer.fetch_member = AsyncMock(side_effect=lambda uid: members[uid])
        outer.fetch_roles = AsyncMock(return_value=guild.roles)
        outer.id, outer.owner_id, outer._roles, outer._state = 123, 10, guild._roles, state
        outer.default_role = guild.default_role
        state.http.get_all_guild_channels = AsyncMock(return_value=[raw(777, 'general'), raw(888, 'new', UNKNOWN)])
        outer.fetch_channels = MethodType(discord.Guild.fetch_channels, outer)
        fri = SimpleNamespace(guild_id=123, owner_id=10, user=SimpleNamespace(id=50), get_guild=lambda _: outer,
                              agent=SimpleNamespace(config={'enabled': True}, state={'history_channels': []},
                                                    allowed=lambda c: False))
        result = await capabilities(fri)
        assert list(result) == ['777'] and result['777']['one_off_post']
    asyncio.run(scenario())
