"""A server's channel listing that survives a channel type discord.py doesn't know (fri #397).

discord.py builds one of its channel classes for every channel Guild.fetch_channels() lists, and raises InvalidData for
the whole listing when Discord returns a type it has none for. Discord added type 20 in November 2026; discord.py 2.7.1,
the newest release, and its main branch don't know it. The gateway cache only skips such a channel, so get_channel and
guild.channels keep working: only a listing fails.
"""

import discord
from discord.channel import _guild_channel_factory


class UnknownChannel:
    """A channel of a type discord.py has no class for: its ID, type and name, as Discord listed it. It's no text
    channel, so nothing posts in it or reads it. It's there, though, so nothing takes it for deleted."""

    __slots__ = ('guild', 'id', 'name', 'type')

    def __init__(self, guild, data, kind):
        self.guild, self.id, self.name, self.type = guild, int(data['id']), data.get('name'), kind

    def __repr__(self):
        return f'<UnknownChannel id={self.id} type={self.type!r}>'


async def guild_channels(guild):
    """Every channel the server has now, as Discord lists it: discord.py's own channels, and one of a type it has no
    class for as an UnknownChannel, rather than none of them. discord.py's own listing first, as before; read again
    only when it can't make one (InvalidData)."""
    try:
        return list(await guild.fetch_channels())
    except discord.InvalidData:
        data = await guild._state.http.get_all_guild_channels(guild.id)
    found = []
    for item in data:
        factory, kind = _guild_channel_factory(item['type'])
        found.append(factory(guild=guild, state=guild._state, data=item) if factory is not None else
                     UnknownChannel(guild, item, kind))
    return found
