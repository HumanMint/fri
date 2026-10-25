"""Small inference-free controls and one bounded continuation of accepted work."""

from dataclasses import dataclass

import discord

from . import feedback as commands
from .fri_actions import Actions
from .fri_chat import ChatResult
from .fri_errors import ControllerRejected
from .fri_notices import notice_text
from .fri_outcomes import fallback, receipt, unrecorded
from .gene_controls import current_music_context, requested
from .gene_controls import quick_control as command

MENU = ('music controls: `fri pause music`, `fri resume music`, `fri skip track`, `fri stop the music`, `fri volume 35`, '
        '`fri queue`, `fri now playing`. gene: `fri status`, `fri version`. '
        'you can use these during a chat cooldown. normal voice, dj and server permissions still apply. '
        'for songs, say `fri play` followed by the song, or open `/gene menu` during a cooldown.')


@dataclass
class Admission:
    result: ChatResult | None = None
    continuation: bool = False
    tool_receipt: dict | None = None


async def admit(fri, event, source, scope, text, context):
    """This route reserves budget only; Actions and Red retain execution authority."""
    if not fri.agent or source.kind != 'discord_message':
        return Admission()
    selected = command(text)
    pending = context.get('pending')
    recent_music = False
    candidate = command(text, music_context=True)
    if (not selected and candidate and candidate['action'] in {'pause', 'resume', 'skip', 'stop'}
            and int(scope['guild_id']) == fri.guild_id):
        recent = await fri.api('POST', '/controls/recent', scope)
        recent_music = current_music_context(recent.get('receipts', []))
        if recent_music:
            selected = candidate
    if not selected and any(requested(text, action) for action in ('play', 'volume')):
        return Admission()  # A new complete command uses the ordinary conversation path.
    if not selected and not pending:
        return Admission()
    if selected and int(scope['guild_id']) != fri.guild_id:
        return Admission(result=ChatResult(reply='gene controls are available in gene’s server or your member dm.', status='completed'))
    actions = Actions(fri.agent, int(scope['user_id']), int(source.channel_id), scope['conversation_id'].startswith('public:'))
    actions.event, actions.scope, actions.revision = event, scope, context['revision']
    actions.message, actions.model, actions.effort = text, context['model'], context['effort']
    actions.pending = pending
    actions.operational = True
    actions.music_context = recent_music
    try:
        original = await event.channel.fetch_message(event.id)
    except discord.HTTPException:
        return Admission(result=ChatResult(reply='i couldn’t verify your source message. nothing was started; send a fresh request.', status='completed'))
    if original.author.id != int(scope['user_id']) or original.webhook_id or original.content != event.content:
        return Admission(result=ChatResult(reply='your request changed. send a fresh message.', status='completed'))
    if not selected and not await actions.pending_unchanged():
        return Admission()
    if selected and selected['action'] != 'menu':
        # Gene lists its commands every few seconds while it polls. Nothing for 20 s means it's down or
        # stuck on another command: say so now, without spending the allowance or queueing anything.
        offered = (await fri.api('GET', '/controls/catalog')).get('actions', [])
        if selected['action'] not in offered:
            return Admission(result=ChatResult(reply=fri.watcher.refusal(offered), status='completed'))
    grant = await fri.api('POST', '/chat/operations/admit', {**scope, 'source': source.model_dump(),
        'revision': context['revision'], 'input': text, 'continuation': not bool(selected)})
    if not grant['allowed']:
        return Admission(result=ChatResult(reply=notice_text(grant['reason']), status='cooldown'))
    if not selected:
        return Admission(continuation=True)
    if selected['action'] == 'menu':
        return Admission(result=ChatResult(reply=MENU, status='completed'))
    try:
        value = await actions.execute({'kind': 'gene_command', **selected})
    except ControllerRejected as exc:
        value = {'blocked': str(exc)}
    except (commands.UserFeedbackCheckFailure, discord.HTTPException, TimeoutError):
        value = {'uncertain': 'Gene did not confirm the result.'}
    evidence = receipt('gene_command', value)
    # Output can contain player/channel evidence. Preserve the normal fresh access fence.
    if not await actions.valid():
        return Admission(result=ChatResult(reply=unrecorded([evidence],
            reason='access changed before i could safely show the details.'), status='completed'), tool_receipt=evidence)
    return Admission(result=ChatResult(reply=fallback([evidence]), status='completed'), tool_receipt=evidence)
