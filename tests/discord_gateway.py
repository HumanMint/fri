"""discord.py's own interaction routing, offline: a raw INTERACTION_CREATE goes through the client's gateway parser
(command tree, persistent and stored views, dynamic items), and what the bot would answer Discord is recorded at
discord.py's webhook adapter instead of being sent."""

import asyncio
import itertools
from contextlib import contextmanager
from unittest.mock import patch

from discord.webhook.async_ import AsyncWebhookAdapter

SNOWFLAKES = itertools.count(1_420_000_000_000_000_000)
COMMAND, COMPONENT = 2, 3


def user(user_id, name='member', bot=False):
    return {'id': str(user_id), 'username': name, 'discriminator': '0', 'avatar': None, 'global_name': None,
            'bot': bot}


def message(message_id, channel_id, author_id, custom_id=None):
    """The bot's message, carrying the clicked button: discord.py runs a dynamic item only for a component the
    message actually has."""
    buttons = [{'type': 1, 'components': [{'type': 2, 'style': 2, 'label': 'x', 'custom_id': custom_id}]}]
    return {'id': str(message_id), 'channel_id': str(channel_id), 'type': 0, 'content': '', 'flags': 0,
            'author': user(author_id, 'fri', bot=True), 'attachments': [], 'embeds': [], 'mentions': [],
            'mention_roles': [], 'pinned': False, 'mention_everyone': False, 'tts': False,
            'components': buttons if custom_id else [], 'timestamp': '2026-11-09T00:00:00+00:00',
            'edited_timestamp': None}


def interaction(kind, data, *, user_id, channel_id, guild_id=None, message_id=None, bot_id=99):
    """A guild interaction when `guild_id`, else a DM one. `message_id`: the bot's message a control sits on."""
    channel = {'id': str(channel_id), 'type': 0 if guild_id else 1, 'name': 'synthetic', 'position': 0,
               'permission_overwrites': [], 'nsfw': False, 'parent_id': None}
    payload = {'id': str(next(SNOWFLAKES)), 'application_id': str(bot_id), 'type': kind, 'token': 'synthetic',
               'version': 1, 'data': data, 'locale': 'en-US', 'attachment_size_limit': 8388608,
               'app_permissions': '0', 'entitlements': [], 'channel': channel}
    if guild_id:
        channel['guild_id'] = payload['guild_id'] = str(guild_id)
        payload['member'] = {'user': user(user_id), 'roles': [], 'joined_at': None, 'deaf': False, 'mute': False,
                             'permissions': '0', 'flags': 0}
    else:
        payload['user'], channel['recipients'] = user(user_id), [user(user_id)]
    if message_id is not None:
        payload['message'] = message(message_id, channel_id, bot_id, data.get('custom_id'))
    return payload


class Sent(list):
    """Each (method, path, payload) the bot would have sent Discord for an interaction."""

    async def request(self, adapter, route, session=None, *, payload=None, multipart=None, **kwargs):
        if payload is None and multipart:
            payload = next((part.get('value') for part in multipart if part.get('name') == 'payload_json'), None)
        self.append((route.method, route.path, payload))
        if route.path.endswith('/callback'):
            return {'interaction': {'id': str(route.webhook_id), 'type': (payload or {}).get('type', 4)}}
        if route.method == 'DELETE':
            return None
        return message(next(SNOWFLAKES), 0, 99)

    def responses(self):
        """What reached the member: interaction callbacks and edits of the original response."""
        return [payload for method, path, payload in self if method in ('POST', 'PATCH') and payload is not None]


@contextmanager
def recorded():
    sent = Sent()

    async def request(adapter, route, session=None, **kwargs):
        return await sent.request(adapter, route, session, **kwargs)
    with patch.object(AsyncWebhookAdapter, 'request', request):
        yield sent


async def deliver(client, payload):
    """Parse it as the gateway would, then let the tasks discord.py schedules for it run."""
    client._connection.parse_interaction_create(payload)
    for _ in range(30):
        await asyncio.sleep(0)
