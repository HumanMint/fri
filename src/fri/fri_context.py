"""Author and event provenance supplied by the Discord harness, never by the model."""

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .privacy import clean


def addressed_content(content, bot_id):
    """Remove only the harness's own mention, at any position in the message."""
    return content.replace(f'<@{bot_id}>', '').replace(f'<@!{bot_id}>', '').strip() if bot_id else content.strip()


class ChatSource(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['discord_message', 'discord_interaction']
    id: str = Field(pattern=r'^[0-9]{1,25}$')
    user_id: str = Field(min_length=1, max_length=25)
    display_name: str = Field(max_length=80)
    channel_id: str = Field(min_length=1, max_length=25)
    guild_id: str | None = Field(default=None, max_length=25)

    def check(self, guild_id, conversation_id, user_id):
        if self.user_id != str(user_id) or self.channel_id != conversation_id.split(':', 1)[1]:
            raise ValueError('Discord source does not match the conversation author or channel')
        if self.guild_id is not None and self.guild_id != str(guild_id):
            raise ValueError('Discord source belongs to another server')
        if conversation_id.startswith('public:') and self.guild_id is None:
            raise ValueError('Public chat needs a server message source')
        # Validate the timestamp range before any data is persisted.
        self.timestamp()
        return self.model_copy(update={'display_name': clean(self.display_name, 80)})

    def timestamp(self):
        try:
            return dt.datetime.fromtimestamp(((int(self.id) >> 22)+1420070400000)/1000, dt.timezone.utc)
        except (ValueError, OverflowError, OSError):
            raise ValueError('Invalid Discord event timestamp') from None

    def evidence(self):
        data = self.model_dump()
        data['at'] = self.timestamp().isoformat()
        if self.kind == 'discord_message':
            data['url'] = f'https://discord.com/channels/{self.guild_id or "@me"}/{self.channel_id}/{self.id}'
        return data


def discord_source(event, text=None, bot_id=0):
    # Imports stay local: the controller need not install the Discord runtime.
    import discord
    if isinstance(event, discord.Message):
        if text is not None:
            visible = addressed_content(event.content, bot_id) or 'hey'
            if clean(text, 4000) not in (clean(event.content, 4000), clean(visible, 4000)):
                raise ValueError('Conversation text does not match its Discord message')
        author, channel, kind = event.author, event.channel.id, 'discord_message'
        guild = event.guild.id if event.guild else None
    elif isinstance(event, discord.Interaction):
        author, channel, kind = event.user, event.channel_id, 'discord_interaction'
        guild = event.guild_id
    else:
        raise ValueError('Live chat requires a Discord event. Use the isolated voice evaluator for tests.')
    return ChatSource(kind=kind, id=str(event.id), user_id=str(author.id),
        display_name=clean(author.display_name, 80), channel_id=str(channel),
        guild_id=str(guild) if guild is not None else None)
