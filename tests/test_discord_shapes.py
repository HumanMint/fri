"""Real discord.py message shapes: a gateway event and a REST fetch of the same message differ."""

from unittest.mock import MagicMock

import discord
from discord.state import ConnectionState

from fri.fri_context import discord_source, same_source
from fri.fri_mentions import fingerprint

BOT, GUILD, CHANNEL, MEMBER, MESSAGE = 99, 123, 8, 20, 1300000000000000000


def user(uid, name, global_name, bot=False):
    return {'id': str(uid), 'username': name, 'global_name': global_name, 'discriminator': '0',
            'avatar': None, 'bot': bot}


def channel():
    """Fri's intents: no members intent, so the member cache holds only Fri."""
    intents = discord.Intents.none()
    intents.guilds = intents.guild_messages = intents.dm_messages = intents.message_content = True
    state = ConnectionState(dispatch=lambda *a, **k: None, handlers={}, hooks={}, http=MagicMock(), intents=intents)
    state.user = discord.ClientUser(state=state, data={**user(BOT, 'fri', 'Fri', True),
                                                       'verified': True, 'mfa_enabled': False})
    guild = discord.Guild(state=state, data={'id': str(GUILD), 'name': 'g', 'roles': [], 'channels': [],
        'members': [{'user': user(BOT, 'fri', 'Fri', True), 'roles': [], 'joined_at': None,
                     'deaf': False, 'mute': False, 'flags': 0}]})
    state._add_guild(guild)
    text = discord.TextChannel(state=state, guild=guild, data={'id': str(CHANNEL), 'type': 0, 'name': 'general',
                                                               'position': 0, 'permission_overwrites': []})
    guild._add_channel(text)
    return state, text


def message(state, text, *, gateway, nick='server nickname', content=None, author=MEMBER, edited=None):
    data = {'id': str(MESSAGE), 'channel_id': str(CHANNEL), 'guild_id': str(GUILD), 'type': 0,
            'content': content or f'<@{BOT}> you there?', 'author': user(author, 'member', 'global name'),
            'timestamp': '2026-10-24T11:33:00+00:00', 'edited_timestamp': edited, 'tts': False,
            'mention_everyone': False, 'mentions': [user(BOT, 'fri', 'Fri', True)], 'mention_roles': [],
            'attachments': [], 'embeds': [], 'pinned': False}
    if gateway:  # MESSAGE_CREATE carries the author's guild member; GET /messages/{id} does not.
        data['member'] = {'roles': [], 'joined_at': None, 'deaf': False, 'mute': False, 'flags': 0, 'nick': nick}
    return discord.Message(state=state, channel=text, data=data)


def test_a_nicknamed_members_fetched_message_is_the_same_event():
    state, text = channel()
    event, fetched = message(state, text, gateway=True), message(state, text, gateway=False)
    # The shapes really differ: the gateway shows the server nickname, the fetch the global name.
    assert (event.author.display_name, fetched.author.display_name) == ('server nickname', 'global name')
    assert discord_source(event) != discord_source(fetched)
    assert same_source(discord_source(event), discord_source(fetched))
    assert fingerprint(event) == fingerprint(fetched)


def test_fetched_copies_still_detect_real_changes():
    state, text = channel()
    event = message(state, text, gateway=True)
    for changed in (message(state, text, gateway=False, content=f'<@{BOT}> edited'),
                    message(state, text, gateway=False, edited='2026-10-24T11:34:00+00:00'),
                    message(state, text, gateway=False, author=30)):
        assert fingerprint(changed) != fingerprint(event)
    assert not same_source(discord_source(event), discord_source(message(state, text, gateway=False, author=30)))
