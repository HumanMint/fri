"""Pure protocol v1 clarification gates; controller owns persistence."""

import re
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .fri_context import ChatSource, addressed_content
from .gene_controls import arguments, requested
from .web_policy import public_query

TTL = 300

class Clarification(BaseModel):
    model_config = ConfigDict(extra='forbid')
    goal: Literal['play', 'volume', 'web']
    question: str = Field(min_length=3, max_length=300)
    topic: str = Field(default='', max_length=240)

def cancelled(text):
    # Song titles are data, including "Hold On" or "Nevermind". Existing explicit
    # controls keep their normal path; only a standalone cancellation aborts early.
    if any(requested(text, verb) for verb in ('play', 'volume', 'pause', 'resume', 'skip', 'stop')):
        return False
    return bool(re.fullmatch(
        r"\s*(?:(?:<@!?\d+>|fri|hey|yo|bro|ok|okay|actually|wait|nah|no|please)[,\s]+)*"
        r"(?:never\s?mind|nvm|cancel(?: (?:it|that|this|my request|the request))?|"
        r"forget (?:it|that|this)|leave it|hold (?:on|off)|not now|"
        r"(?:do not|don't|dont) play\b.*|no|nah|nope|stop)"
        r"(?: (?:please|thanks|for now))?[.!?\s]*", text, re.I))

def answer_text(text, bot_id=''):
    return ' '.join(addressed_content(text, bot_id).split())

def completion(pending, text, action, argument, source, guild, conversation, user, revision, bot_id=''):
    """Pure shared gate; runtime also refetches both real public Discord messages."""
    if not pending or action not in ('play', 'volume') or pending.get('goal') != action or cancelled(text):
        return False
    try:
        origin = ChatSource.model_validate(pending['source']).check(guild, conversation, user)
        current = ChatSource.model_validate(source).check(guild, conversation, user)
        return (origin.kind == current.kind == 'discord_message' and origin.guild_id == current.guild_id
                and 0 < int(current.id)-int(origin.id) and pending['revision'] == revision
                and -5 <= time.time()-origin.timestamp().timestamp() <= TTL
                and -5 <= time.time()-current.timestamp().timestamp() <= TTL
                and requested(pending['request_text'], action)
                and ' '.join(argument.split()).casefold() == answer_text(text, bot_id).casefold()
                and bool(arguments(action, argument)))
    except (ValueError, KeyError, TypeError):
        return False

def research_query(query, message, pending=None):
    """Export only an exact current excerpt or the pending public question.

    Neither the assistant's question nor ambient/private context is exported.
    The isolated researcher can rewrite this topic using public sources.
    """
    if pending and pending.get('goal') == 'web' and not cancelled(message):
        topic = pending['topic']
        if public_query(topic, pending['request_text']):
            if query == topic:
                return topic
            if public_query(query, message) and len(topic)+len(query) < 220:
                return topic+'; clarification: '+query
    return query if public_query(query, message) else None
