"""Bounded conversational endings, separate from commands and meme attention."""

import re
import time

from .fri_context import ChatSource

REACTIONS = ('👍', '❤️', '😂', '😭', '😢', '🫂', '👀', '💀', '🫡')
KINDS = ('reply', 'react', 'quiet')
CLOSINGS = {'gotcha', 'gotcha gotcha', 'got it', 'thanks that worked', 'thanks thats all',
            "thanks that's all", 'alright cheers', 'all good now', 'cheers thanks'}
OPEN_QUESTION = re.compile(
    r"\?|\b(tell me|let me know|which|what|choose|pick|confirm|send me|give me|"
    r"would you|should i|want me|are you|can you|do you)\b", re.I)
SERIOUS = re.compile(
    r'\b(suicid\w*|self[- ]?harm|hurt\w*|harm\w*|kill\w*|die|dying|dead|death|'
    r'unsafe|safe|danger\w*|overdose\w*|abuse\w*|bleed\w*|emergency|hospital|'
    r'crisis|grief|grieving|depress\w*|alone|scared|afraid)\b', re.I)


def acknowledgment_allowed(context):
    return bool(context.get('acknowledgments_enabled') and
                context.get('source', {}).get('kind') == 'discord_message' and
                not context.get('pending') and not context.get('action_result') and
                not context.get('action_results'))


def quick_closure(text, context):
    """Only exact closings in a fresh, verified same-speaker exchange skip inference.

    Yes/ok/lol, compliments and song names deliberately have no shortcut. Unknown,
    old, unanswered, or serious context goes to the normal conversation model.
    """
    normalized = ' '.join(re.sub(r'[,.!]', ' ', text.casefold()).split())
    turns = context.get('turns', [])
    if (context.get('reply_target') or len(context.get('message_burst', [])) > 1 or
            normalized not in CLOSINGS or not acknowledgment_allowed(context) or not turns or
            not turns[-1].get('allow_closing', False)):
        return False
    try:
        current = ChatSource.model_validate({k: context['source'][k]
            for k in ChatSource.model_fields if k in context['source']})
        prior = turns[-1].get('source', {})
        previous = ChatSource.model_validate({k: prior[k] for k in ChatSource.model_fields if k in prior})
        if (previous.kind != 'discord_message' or
                any(getattr(current, key) != getattr(previous, key)
                    for key in ('user_id', 'channel_id', 'guild_id')) or
                not 0 < current.timestamp().timestamp()-previous.timestamp().timestamp() <= 120 or
                not -5 <= time.time()-current.timestamp().timestamp() <= 120):
            return False
    except (ValueError, KeyError, TypeError):
        return False
    if any(r.get('user_id') == current.user_id and
           int(previous.id) < int(r['id']) < int(current.id) for r in context.get('recent', [])):
        return False  # A burst may contain an unanswered question before the closing phrase.
    # A plain question can be pending without a typed tool clarification.
    replies = [t['reply'] for t in turns if t.get('response_kind', 'reply') == 'reply']
    if not replies or not replies[-1] or OPEN_QUESTION.search(replies[-1]):
        return False
    recent = ' '.join(str(t.get(k, '')) for t in turns[-2:] for k in ('input', 'reply'))
    return not SERIOUS.search(recent)


def validate_response(kind, reply, reaction, *, draft=None, notes=(), action=None):
    """Validate intent, not authority: reactions can never double as tool calls."""
    if kind not in KINDS or not isinstance(reply, str):
        raise ValueError('Invalid response kind')
    if kind == 'reply':
        if reaction is not None or (not reply.strip() and action is None):
            raise ValueError('Text replies need text and no reaction')
    elif (reply or draft is not None or notes or action is not None or
          (reaction not in REACTIONS if kind == 'react' else reaction is not None)):
        raise ValueError('Acknowledgments cannot contain text, notes, drafts or actions')
