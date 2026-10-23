"""Explicit invitations only; no ambient chat collection or timed eavesdropping."""

import re

import discord

from .fri_context import addressed_content


def visible_content(content):
    # Mirror the controller's deterministic invitation gate, including unfinished quotes/code.
    visible = re.sub(r'(?ms)^[ \t]*>>>.*\Z', '', content)
    visible = re.sub(r'(?m)^[ \t]*>[^\n]*', '', visible)
    visible = re.sub(r'(?s)(`{3,}|~{3,}).*?(?:\1|\Z)', '', visible)
    visible = re.sub(r'(?s)(`+).*?(?:\1|\Z)', '', visible)
    visible = re.sub(r'(?i)https?://\S+', '', visible)
    return re.sub(r'"[^"\n]*"|“[^”\n]*”|(?<!\w)\'[^\'\n]*\'', '', visible)


def explicit_mention(message, bot_id):
    mentions = getattr(message, 'mentions', ())
    return bool(bot_id and isinstance(mentions, (list, tuple)) and bot_id in {user.id for user in mentions} and
                re.search(r'(?<!\\)<@!?'+re.escape(str(bot_id))+r'>', visible_content(message.content or '')))


async def addressed_text(message, bot_id):
    content = message.content or ''
    explicit = explicit_mention(message, bot_id)
    # Quoted messages, code, URLs and Friday should not wake Fri by accident.
    visible = visible_content(content)
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
