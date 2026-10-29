"""Bounded personal notes, never permissions, shared server facts, or worker input."""

import re

import discord

CATEGORIES = ('nickname', 'games', 'music', 'hobbies', 'conversation', 'projects')  # Gene keeps projects where said.
MAX_NOTES = 10
PER_CATEGORY = 3
TTL = 90 * 86400
PROFILES = 'member-profiles'  # The controller keeps one profile per member across readable channels.
MOVES = 'memory-moves'  # The controller moves a note into or out of the profile on the member's press.
SUMMARIES = 'member-summaries'  # The controller keeps Fri's own short summary of each member's readable chats.
# It returns the summary's generation, which a withdrawal dropping that summary moves (gene #91).
GENERATIONS = 'summary-generations'
# It also takes that generation on writes of the model's words (saved work, reminders) and refuses a moved one (fri #171).
GENERATION_WRITES = 'summary-generation-writes'
# It reports the summaries a reply of Fri's drew on, and takes a turn's dependencies on them through Fri's earlier replies
# it carried, refusing and retiring it once one goes (fri #193).
DEPENDENCIES = 'summary-dependencies'
PUBLIC_THREADS = (discord.ChannelType.public_thread, discord.ChannelType.news_thread)


def server_wide(channel, fetched=None):
    """Everyone in the server can read it and its history, now. Only such a channel feeds a member's profile.

    A private thread, an age-restricted channel, one the default role can't view or read back, or one that
    keeps out any role or member keeps what was said in it to itself: the default role alone doesn't prove
    everyone can read it (fri #45). Anything Fri can't resolve counts as restricted.

    `fetched`: the channel (a thread's parent) as Discord has it, with the server's fetched roles (see
    Agent.can_read), judged instead of the cache, which can lag a new overwrite (fri #97).
    """
    guild = getattr(channel, 'guild', None)
    if guild is None:
        return False
    if isinstance(channel, discord.Thread):
        if channel.type not in PUBLIC_THREADS:
            return False
        channel = channel.parent
    if fetched is not None:
        channel, guild = fetched, getattr(fetched, 'guild', None)
    is_nsfw = getattr(channel, 'is_nsfw', None)
    if channel is None or not callable(is_nsfw) or is_nsfw():
        return False
    try:
        everyone = channel.permissions_for(guild.default_role)
        if not (everyone.view_channel and everyone.read_message_history):
            return False
        for overwrite in channel.overwrites.values():
            deny = overwrite.pair()[1]
            if deny.view_channel or deny.read_message_history:
                return False
    except (AttributeError, TypeError):
        return False
    return True


def place_notes(get_channel, context, conversation):
    """In public, keep where each profile note was said (the controller's profile_origins) beside the notes,
    never in them, and drop any said where not everyone can read now (fri #98). Fri checks again right before
    the model reads them (drop_unheard). The model's input never carries profile_origins.

    A private conversation is the member's own: all their notes show there, so nothing is dropped.
    """
    origins = context.pop('profile_origins', None)
    if origins is None or not conversation.startswith('public:'):
        return context  # An older controller says nothing, as before.
    notes = (context.get('personal_memory') or {}).get('notes', [])
    aligned = isinstance(origins, list) and len(origins) == len(notes)  # Otherwise every one is unknown.
    context['profile_origins'] = {note_key(note): origin for note, origin in zip(notes, origins)
                                  if note.get('scope') == 'server'} if aligned else {}
    return drop_unheard(get_channel, context)


def note_key(note):
    return f"{note.get('category')}\0{note.get('evidence')}"


def drop_unheard(get_channel, context):
    origins = context.get('profile_origins')
    if origins is not None and (memory := context.get('personal_memory')):
        memory['notes'] = [note for note in memory.get('notes', []) if note.get('scope') != 'server' or
                           said_to_everyone(get_channel, origins.get(note_key(note), 'unknown'))]
        kept = {note_key(note) for note in memory['notes']}  # Nothing of a dropped note stays behind.
        context['profile_origins'] = {key: origin for key, origin in origins.items() if key in kept}
    return context


def said_to_everyone(get_channel, origin):
    """Where a profile note was said is still readable by everyone. A note the member moved into their
    profile from a private chat (origin private:) went there on their own press; an older note (None) is
    unknown, as before. Somewhere Fri can't resolve, or that keeps a role or member out, stays out of public."""
    if origin is None or str(origin).startswith('private:'):
        return True
    kind, _, channel_id = str(origin).partition(':')
    return kind == 'public' and channel_id.isdigit() and server_wide(get_channel(int(channel_id)))


def open_to_everyone(channel):
    """server_wide, and no role or member is kept out of it or its history, so an answer quoting it
    can be read anywhere in the server. server_wide asks only the default role; a channel can still
    deny one role or one member."""
    if not server_wide(channel):
        return False
    parent = channel.parent if isinstance(channel, discord.Thread) else channel
    try:
        if not parent.permissions_for(parent.guild.default_role).read_message_history:
            return False
        for overwrite in parent.overwrites.values():
            deny = overwrite.pair()[1]
            if deny.view_channel or deny.read_message_history:
                return False
    except (AttributeError, TypeError):
        return False
    return True


def before_removal(message_id, profile):
    """Sent before the member last edited their memory, so Fri keeps it nowhere (the controller's removed_at)."""
    return discord.utils.snowflake_time(int(message_id)).timestamp() <= (profile.get('removed_at') or 0)


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
    """Ask for the member's profile (and summary) when the controller keeps them; otherwise the request is unchanged."""
    if not getattr(fri, 'profiles', False):
        return scope
    return {**scope, 'profiles': True, **({'summaries': True} if getattr(fri, 'summaries', False) else {})}


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


def describe_profile(enabled, groups, private, summary=None):
    """What the member's notes look like from here, grouped by where each one shows.

    groups: [(heading, [evidence, ...])], already escaped. Public lists only what this channel can
    see; a private conversation, seen only by the member, lists everything. summary: Fri's own summary,
    already escaped, in private; in public only that one exists, never its words.
    """
    if not enabled:
        return 'memory’s off, so i’m not keeping anything. say “remember me again” to turn it back on.'
    footer = ('\n\nserver stuff follows you everywhere in the server, the rest stays where you said it, what you’re '
              'working on included. notes last '
              '90 days, recent chats a week. My memory edits or removes one. “forget me” wipes all of it and turns '
              'memory off.' if private else
              '\n\nwhat you tell me in channels everyone here can read follows you around the server and into DMs, '
              'except what you’re working on. '
              'DMs, the /fri panel and restricted channels stay where they were said. ask in DMs or /fri for the full list. '
              '“forget me” wipes all of it and turns memory off.')
    if summary:
        footer = ('\n\n**what we’ve talked about lately** (my own summary, from channels everyone here can read, '
                  'gone 30 days after i last update it)\n'+summary if private else
                  '\n\ni also keep a short summary of what we’ve talked about lately. see it in DMs.')+footer
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
