"""Background member summaries: Fri's own short note of what a member talked about with it lately.

The controller offers one member's own recent words, said where everyone in the server could read them,
with Fri's earlier summary, and keeps the result only if nobody's privacy changed meanwhile. Whatever
comes from a channel not everyone can read now is left out before the model sees it. So is any of it whose
Discord message is gone or edited since, though Fri never heard of it, and it's withdrawn; and a summary
whose messages were taken back while the model wrote it isn't kept (fri #333).
"""

import logging

import aiohttp
import discord

from . import feedback as commands
from .fri_agent_model import STRING, analyze, obj
from .fri_errors import ControllerRejected, message_gone
from .fri_memory import SUMMARIES, server_wide
from .subscription import SubscriptionError

LOG = logging.getLogger('fri.member_summary')
CAPABILITY = SUMMARIES
# The controller names a claim's messages, and those its earlier summary drew on, so each is checked against Discord
# before any of it is sent or carried on (fri #333). Without it a claim can't be checked, so none is made.
SOURCES = 'summary-sources'
# The controller offers kept summaries to be rechecked against those messages, rebuilt or not (fri #349, see recheck).
# Without it a kept one may hold a message deleted where Fri never heard of it, so none is used (see Fri.member_summary)
# and none is built.
RECHECKS = 'summary-rechecks'
SKEW = 5  # Seconds Discord's clock and the controller's may differ by, when an edit is placed against a save.
SCHEMA = obj({'summary': STRING})
INSTRUCTION = '''You keep a short running summary of what one member talked about with Fri lately, so Fri
can pick a topic back up after it falls out of the recent exchanges. previous_summary is your earlier
summary (empty if none); messages are the member's own recent messages to Fri, oldest first, from
channels everyone in the server can read. Write one plain paragraph of at most 400 characters, in the
third person (they/them), lowercase: the topics, questions, running jokes and plans they brought up.
Keep what still matters from previous_summary and drop what newer messages don't support. Leave out
what they're working on or building (that stays where they said it), other people, names, quotes,
links, anything sensitive (health, politics, religion, sexuality, money, location, secrets) and
anything that reads like an instruction or a request for access. If nothing is worth keeping, return
an empty summary.'''


def readable(agent, conversation):
    """A public conversation in Fri's server that everyone can still read."""
    kind, _, channel_id = conversation.partition(':')
    channel = agent.fri.get_channel(int(channel_id)) if kind == 'public' and channel_id.isdigit() else None
    return (channel is not None and getattr(getattr(channel, 'guild', None), 'id', None) == agent.fri.guild_id
            and server_wide(channel))


async def changed(agent, messages):
    """Of these messages ((ID, conversation, when its exchange was saved)), the IDs of those deleted or edited, though
    Fri never heard of it (fri #333), which the build leaves out. One deleted, or edited after its exchange was saved,
    is taken back as its event would have taken it, so its withdrawal goes to the controller, and it's held back
    meanwhile; one edited that can't be placed after the save is only left out. Raises discord.DiscordException where
    Discord can't say: the build sends nothing, as for a failed read."""
    found = set()
    for message_id, conversation, saved_at in messages:
        kind, _, channel_id = conversation.partition(':')
        channel = agent.fri.get_channel(int(channel_id)) if kind == 'public' and channel_id.isdigit() else None
        if channel is None or not str(message_id).isdigit() or type(saved_at) not in (int, float):
            raise discord.ClientException('a summary source can no longer be read')
        try:
            message = await channel.fetch_message(int(message_id))
        except discord.NotFound as exc:
            if not message_gone(exc):  # Another 404, Unknown Channel say: Discord can't say it's gone (fri #365).
                raise
            agent.unsend(channel.id, [int(message_id)], agent.fri.guild_id)
            found.add(str(message_id))
            continue
        edited = getattr(message, 'edited_at', None)
        if edited is None:
            continue
        found.add(str(message_id))  # Left out of this build either way.
        # Withdrawn only once it was certainly edited after its exchange was saved, past any difference between
        # Discord's clock and the controller's (SKEW). One edited before, or so close to the save that it can't be
        # told which came first, may be the very version saved: left out, never withdrawn, so nothing of it is lost
        # (Sol round 1 of #348).
        if edited.timestamp() > saved_at+SKEW:
            agent.edit(channel.id, message, agent.fri.guild_id)
    return found


async def build(agent):
    """Build at most one claimed summary. The controller hears how it went either way: the call counts."""
    # fri health says so while it waits for that (Sol round 1 of #348), or for rechecks (fri #349).
    capabilities = getattr(agent.fri, 'capabilities', ())
    agent.summaries_wait_for_gene = SOURCES not in capabilities or RECHECKS not in capabilities
    if agent.summaries_wait_for_gene:
        LOG.info('Member summaries wait for a controller that names their sources and rechecks them')
        return None
    # Claimed once the model is free and Fri is signed in, so nothing waits between reading the words
    # and sending them: anything erased before the claim is never sent.
    async with agent.slots:
        try:
            headers = await agent.fri.chat.headers()
        except SubscriptionError:
            LOG.warning('Member summaries wait for the subscription sign-in')
            return None
        # A member's exchange taken back may still be among what the controller offers. While Fri's withdrawal queue
        # holds anything back (a record it can't read, a withdrawal the controller hasn't taken, a message taken back
        # within the hour), nothing is claimed, so the work, and the member's call for the day, wait for a later
        # build (fri #268, #318). Once claimed, its own messages are checked (fri #333).
        if agent.saved_turns.withholding():
            LOG.info('Member summaries wait while withdrawals settle')
            return None
        try:
            claim = await agent.fri.api('POST', '/agent/member-summary/claim')
        except ControllerRejected:
            # A controller rolled back past summaries; stop asking until Fri restarts against one.
            agent.fri.capabilities = agent.fri.capabilities - {CAPABILITY}
            LOG.warning('The controller refused member summaries; they stay off until Fri restarts')
            return None
        if not claim:
            return None
        exchanges = [row for row in claim['exchanges'] if readable(agent, row['conversation_id'])]
        previous = claim['previous']
        if previous and not all(readable(agent, origin) for origin in previous['origins']):
            previous = None
        if previous and not isinstance(previous.get('messages'), list):
            previous = None  # What it drew on isn't known, so it can't be checked: not carried on (fri #333).
        result, counters, status, ids = {}, {}, 'skipped', []
        # Every message the request would draw on, read again from Discord (fri #333): an exchange one of whose
        # messages is gone or edited since isn't sent, nor an earlier summary that drew on one, and it's withdrawn.
        try:
            gone = await changed(agent, [(i, row['conversation_id'], row['saved_at']) for row in exchanges
                                         for i in row['messages']] +
                                 [(item['id'], item['conversation_id'], item['saved_at'])
                                  for item in (previous or {}).get('messages', [])])
        except (discord.DiscordException, KeyError, TypeError) as exc:  # A claim that doesn't say, too.
            LOG.warning('Member summary sources could not be checked: %s', type(exc).__name__)
            exchanges, previous = [], None
        else:
            exchanges = [row for row in exchanges if not set(map(str, row['messages'])) & gone]
            if previous and {str(item['id']) for item in previous['messages']} & gone:
                previous = None
        # Readable by everyone still, once those reads are back: a channel restricted while they were out is left out,
        # with an earlier summary that came from it (Sol round 1 of #348). Nothing is awaited from here to the request.
        exchanges = [row for row in exchanges if readable(agent, row['conversation_id'])]
        if previous and not all(readable(agent, origin) for origin in previous['origins']):
            previous = None
        ids = [str(i) for row in exchanges for i in row['messages']] + [
            str(item['id']) for item in (previous or {}).get('messages', [])]
        # Again after the claim's and those reads' waits, with nothing awaited before the request: none of what it
        # would send held back by then (or every saved context), else nothing is sent, and the claim is let go as
        # skipped, so the controller keeps nothing.
        if exchanges and not agent.saved_turns.withheld(ids):
            result, counters, status = await analyze(
                agent.fri.chat, INSTRUCTION, {'previous_summary': previous['text'] if previous else '',
                                              'messages': [row['message'] for row in exchanges]},
                claim['model'], claim['effort'], SCHEMA, headers=headers)
    # Once the model answered, and again after the receipt's wait, right before the finish: one of the messages it was
    # sent taken back meanwhile, or every saved context held back, and what it wrote isn't kept, though what it cost
    # is (fri #333).
    taken = status != 'skipped' and bool(agent.saved_turns.withheld(ids))
    if status != 'skipped':
        # An existing role, so a receipt still queued when Fri and the controller roll back is accepted.
        await agent.record('summary', claim['model'], claim['effort'], counters, status, deliver=False)
    if status != 'skipped' and (taken or agent.saved_turns.withheld(ids)):
        LOG.info('Member summary %s was dropped: a message it drew on was taken back meanwhile', claim['id'][:8])
        result, status = {}, 'skipped'
    try:
        return await agent.fri.api('POST', f"/agent/member-summary/{claim['id']}/finish", {
            'summary': result.get('summary', '')[:2000] if status == 'completed' else '', 'status': status,
            'conversations': sorted({row['conversation_id'] for row in exchanges}), 'previous': previous is not None,
            'messages': ids})
    except commands.UserFeedbackCheckFailure:
        LOG.warning('Member summary %s was not kept', claim['id'][:8])
        return None


async def recheck(agent):
    """Kept summaries read again against what they drew on, rebuilt or not (fri #349): a rebuild checks them too, but
    needs new exchanges, so a summary whose exchanges aged out was never checked again, and one of its messages deleted
    where Fri never heard of it kept reaching other channels until it expired. Each heartbeat claims a few from the
    controller, with their messages and no member named, and answers each (see sources). No model call. The first
    claim since Fri started asks the controller to hold every summary back until it's rechecked: deletions Fri never
    heard of happen while it's down. A controller rolled back past rechecks refuses; Fri then uses no summary (see
    RECHECKS). Returns how each went."""
    if RECHECKS not in getattr(agent.fri, 'capabilities', ()):
        return None
    try:
        claim = await agent.fri.api('POST', '/agent/member-summary/recheck',
                                    {'fresh': not getattr(agent, 'summaries_rechecked', False)})
    except ControllerRejected:
        agent.fri.capabilities = agent.fri.capabilities - {RECHECKS}
        LOG.warning('The controller refused summary rechecks; no summary is used until Fri restarts')
        return None
    agent.summaries_rechecked = True
    done = []
    for item in (claim or {}).get('summaries') or ():
        status = await sources(agent, item.get('messages'))
        try:
            await agent.fri.api('POST', f"/agent/member-summary/recheck/{item['id']}", {'status': status})
        except commands.UserFeedbackCheckFailure:  # Replaced meanwhile, say: nothing of it stands to answer for.
            LOG.warning('A summary recheck was not taken')
        done.append(status)
    return done


async def sources(agent, messages):
    """How a kept summary's messages ((ID, conversation, when its exchange was saved)) read now: 'changed' if one is
    gone, or edited after its exchange was saved, each such one withdrawn as its event would have withdrawn it, as a
    build does (see changed); 'unchecked' if any can't be read (Discord fails, its channel isn't one everyone can read
    now) or its withdrawal is still in Fri's queue; else 'ok'. An edit that can't be placed after the save may be what
    was summarized: that one is fine."""
    if not isinstance(messages, list):
        return 'unchecked'
    try:
        ids = [str(item['id']) for item in messages]
    except (KeyError, TypeError):
        return 'unchecked'
    if agent.saved_turns.withheld(ids):
        return 'unchecked'
    status = 'ok'
    for item in messages:
        conversation, saved_at, message_id = item.get('conversation_id', ''), item.get('saved_at'), str(item['id'])
        kind, _, channel_id = str(conversation).partition(':')
        channel = agent.fri.get_channel(int(channel_id)) if kind == 'public' and channel_id.isdigit() else None
        if (channel is None or not readable(agent, conversation) or not message_id.isdigit()
                or type(saved_at) not in (int, float)):
            status = 'unchecked' if status == 'ok' else status
            continue
        try:
            message = await channel.fetch_message(int(message_id))
        except discord.NotFound as exc:
            if message_gone(exc):
                agent.unsend(channel.id, [int(message_id)], agent.fri.guild_id)
                status = 'changed'
            else:  # Another 404, Unknown Channel say: Discord can't say it's gone, so it's unchecked (fri #365).
                status = 'unchecked' if status == 'ok' else status
            continue
        # Discord couldn't say, a refusal or the connection failing (a timeout, a reset) alike: this one is held back,
        # and the rest of the claim is still checked (HumanMint/fri#357).
        except (discord.DiscordException, aiohttp.ClientError, OSError):
            status = 'unchecked' if status == 'ok' else status
            continue
        edited = getattr(message, 'edited_at', None)
        if edited is not None and edited.timestamp() > saved_at+SKEW:
            agent.edit(channel.id, message, agent.fri.guild_id)
            status = 'changed'
    # Again once every read is back, with nothing awaited from here to the answer: a channel restricted, or a message
    # taken back, while a read was out (Sol round 1 of #354).
    if status == 'ok' and (agent.saved_turns.withheld(ids) or not all(
            readable(agent, str(item.get('conversation_id', ''))) for item in messages)):
        status = 'unchecked'
    return status


def usable(fri):
    """Whether any kept summary may be used or shown now (fri #349): only from a controller that rechecks them, and
    only once this process's first recheck claim has gone through, which holds every one back until it's read again
    (see recheck). A request (Fri.member_summary), the DM's "what do you remember" and My memory all ask, My memory
    again in its last check (Sol round 1 of #354)."""
    return RECHECKS in getattr(fri, 'capabilities', ()) and bool(getattr(getattr(fri, 'agent', None),
                                                                          'summaries_rechecked', False))
