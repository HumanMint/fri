"""Bounded personal notes, never permissions, shared server facts, or worker input."""

import re

import discord

from .privacy import clean

CATEGORIES = ('nickname', 'games', 'music', 'hobbies', 'conversation')
MAX_NOTES = 10
PER_CATEGORY = 3
TTL = 90 * 86400
PROFILES = 'member-profiles'  # The controller keeps one profile per member across readable channels.
MOVES = 'memory-moves'  # The controller moves a note into or out of the profile on the member's press.
PUBLIC_THREADS = (discord.ChannelType.public_thread, discord.ChannelType.news_thread)


def server_wide(channel):
    """Everyone in the server can read it, now. Only such a channel feeds a member's profile.

    A private thread, an age-restricted channel, or one the default role can't view keeps what was
    said in it to itself. Anything Fri can't resolve counts as restricted.
    """
    guild = getattr(channel, 'guild', None)
    if guild is None:
        return False
    if isinstance(channel, discord.Thread):
        if channel.type not in PUBLIC_THREADS:
            return False
        channel = channel.parent
    is_nsfw = getattr(channel, 'is_nsfw', None)
    if channel is None or not callable(is_nsfw) or is_nsfw():
        return False
    try:
        return bool(channel.permissions_for(guild.default_role).view_channel)
    except (AttributeError, TypeError):
        return False


def before_removal(message_id, profile):
    """Sent before the member last edited their memory, so Fri keeps it nowhere (the controller's removed_at)."""
    return discord.utils.snowflake_time(int(message_id)).timestamp() <= (profile.get('removed_at') or 0)


def personal_note(candidate, text):
    """Keep a short exact self-disclosure, not a model-written biography/instruction."""
    if not isinstance(candidate, dict) or candidate.get('category') not in CATEGORIES:
        return None
    evidence = candidate.get('evidence', '')
    if not isinstance(evidence, str) or not 5 <= len(evidence) <= 180 or evidence not in text:
        return None
    if clean(evidence, 180) != evidence or any(s in evidence for s in ('\n', '`', '<@', '://')):
        return None
    before = text[:text.index(evidence)].rstrip()
    if before.endswith(('"', "'", '“', '‘', '>', '`')):
        return None
    if not re.match(r"(?i)^(?:i (?:like|love|prefer|enjoy|play|usually|mostly)|my (?:favorite|favourite|nickname)|call me)\b", evidence):
        return None
    # Defense in depth; notes are still untrusted data even after validation.
    if re.search(r'(?i)\b(?:ignore|instructions?|system|developer|password|token|secret|address|diagnos\w*|'
                 r'politic\w*|religio\w*|sexual\w*|health|medical|permission|approv\w*|deploy|execute)\b', evidence):
        return None
    return candidate['category'], evidence


def memory_intent(text):
    """Handle direct privacy requests locally, without asking a model to authorize them."""
    text = text.lower().strip().replace('’', "'")
    text = re.sub(r'^(?:(?:hey|hi|yo|ok|okay|please)[,\s]+)*fri\b[,\s:]*', '', text)
    text = re.sub(r'[,\s]+fri[.!?]*$', '', text).strip(' .!?')
    text = re.sub(r'[,\s]+please$', '', text)
    text = re.sub(r'^(?:please |can you |could you )', '', text).strip(' .!?')
    if text in ('what do you remember about me', 'what do you know about me', 'show my memory',
                'show me my memory', 'show my memories', 'what have you remembered about me'):
        return 'show'
    if text in ('forget me', 'forget everything about me', 'forget what you know about me',
                'forget what you remember about me', 'clear my memory', 'clear my memories',
                'stop remembering me', "don't remember me", 'turn memory off', 'disable memory'):
        return 'disable'
    if text in ('remember me again', 'start remembering me', 'turn memory on', 'enable memory'):
        return 'enable'
    return None


def units(text):
    """Discord counts message and embed lengths in UTF-16 code units, so an emoji counts twice."""
    return len(text.encode('utf-16-le'))//2


def clip(text, limit):
    """At most limit UTF-16 code units, never splitting a character."""
    kept, used = [], 0
    for char in text:
        used += 2 if ord(char) > 0xFFFF else 1
        if used > limit:
            break
        kept.append(char)
    return ''.join(kept)


def profiled(fri, scope):
    """Ask for the member's profile when the controller keeps them; otherwise the request is unchanged."""
    return {**scope, 'profiles': True} if getattr(fri, 'profiles', False) else scope


def note_place(get_channel, scope, conversation, *, public=False):
    """Where a note shows, from the member's side. A public listing holds only the profile and here."""
    if scope == 'server':
        return 'across the server'
    if scope in ('here', conversation):
        return 'only in this channel' if public else 'only here'
    kind, _, channel_id = scope.partition(':')
    channel = get_channel(int(channel_id)) if kind == 'public' and channel_id.isdigit() else None
    if channel is not None:
        return 'only in #'+discord.utils.escape_markdown(channel.name)
    return 'only in a channel i can’t see anymore' if kind == 'public' else 'only in another private chat'


def describe_profile(enabled, groups, private):
    """What the member's notes look like from here, grouped by where each one shows.

    groups: [(heading, [evidence, ...])], already escaped. Public lists only what this channel can
    see; a private conversation, seen only by the member, lists everything.
    """
    if not enabled:
        return 'memory’s off, so i’m not keeping anything. say “remember me again” to turn it back on.'
    footer = ('\n\nserver stuff follows you everywhere in the server, the rest stays where you said it. notes last '
              '90 days, recent chats a week. My memory edits or removes one. “forget me” wipes all of it and turns '
              'memory off.' if private else
              '\n\nwhat you tell me in channels everyone here can read follows you around the server and into DMs. '
              'DMs, the /fri panel and restricted channels stay where they were said. ask in DMs or /fri for the full list. '
              '“forget me” wipes all of it and turns memory off.')
    if not any(notes for _, notes in groups):
        return ('memory’s on. nothing kept about you yet.' if private else
                'memory’s on. nothing kept that this channel can see yet.') + footer
    lines = ['memory’s on. everything i keep about you:' if private else 'memory’s on. what i keep that this channel can see:']
    hidden = 0
    for heading, notes in groups:
        for index, note in enumerate(notes):
            line = ('\n**'+heading+'**' if index == 0 else '')+'\n- '+note
            if hidden or units(''.join(lines)+line+footer) > 1800:
                hidden += 1  # Discord's limit. Once one is left out, the rest go too, headings intact.
            else:
                lines.append(line)
    if hidden:
        lines.append(f'\n…and {hidden} more in My memory.')
    return ''.join(lines)+footer


def describe_memory(profile):
    state = 'on' if profile['enabled'] else 'off'
    notes = profile['notes']
    body = '\n'.join('- ' + note['evidence'] for note in notes) or 'No personal notes saved for this conversation yet.'
    return (f'Memory is {state}. {"Here’s what I’ve kept from what you told me:" if notes else ""}\n{body}\n\n'
            'I keep up to ten small notes here, at most three per category, for 90 days, and six recent exchanges for seven days. '
            'Use My memory to edit or remove one preference. '
            'Private chats and other channels stay separate. Say “forget me” to erase our saved chats '
            'and personal notes everywhere and turn memory off, or “remember me again” to turn it on.')
