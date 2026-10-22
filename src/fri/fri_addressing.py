"""Explicit invitations only; no ambient chat collection or timed eavesdropping."""

import re

import discord

from .fri_context import addressed_content


async def addressed_text(message, bot_id):
    content = message.content or ''
    explicit = bot_id in {user.id for user in message.mentions}
    # Quoted messages, code, URLs and Friday should not wake Fri by accident.
    visible = re.sub(r'```[\s\S]*?```|`[^`]*`|https?://\S+|(?m:^>.*$)', '', content)
    named = re.search(r'(?<![\w@/#])fri(?!\w)', visible, re.IGNORECASE)
    reference = getattr(message, 'reference', None)
    responding = False
    if not explicit and not named and reference and reference.message_id:
        original = reference.resolved
        if original is None:
            try:
                original = await message.channel.fetch_message(reference.message_id)
            except discord.HTTPException:
                return None
        responding = getattr(getattr(original, 'author', None), 'id', None) == bot_id
    if not (explicit or named or responding):
        return None
    return addressed_content(content, bot_id) or 'hey'
