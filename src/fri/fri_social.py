"""Natural-language preparation, persistent Discord controls and bounded reminder delivery."""

import asyncio
import datetime as dt
import json
import os
import re
import weakref

import discord
import httpx

from . import feedback as commands
from .fri_context import ChatSource, addressed_content, discord_source
from .fri_errors import ControllerRejected
from .fri_workspace import send_once
from .privacy import clean
from .subscription import private_json

NO_MENTIONS = discord.AllowedMentions.none()
COMPONENT = re.compile(r'^fri:social:(reminder|plan):([a-f0-9]{32}):(confirm|cancel|publish|vote[0-3]|final[0-3]|going|maybe|cannot)$')
SOCIAL_ACTIONS = {
    'reminder_prepare': {'body': str, 'local_time': str, 'timezone': str},
    'reminder_list': {'reminder_id': str},
    'reminder_cancel': {'reminder_id': str},
    'plan_prepare': {'title': str, 'local_times': str, 'timezone': str},
}
KINDS = set(SOCIAL_ACTIONS)
REMINDER_REQUEST = re.compile(r'(?i)\bremind(?:er|ers)?\b')
PLAN_REQUEST = re.compile(r'(?i)\b(plan|choose|organize|organise|availability|schedule|game night|set up)\b')


def cancellation_requested(text, bot_id=0):
    """Only a bounded direct current instruction authorizes natural-language cancellation.

    Quotes/code remain data placeholders, so deleting them cannot join words into
    a command. Ambiguous, negated and compound discussion needs a fresh request.
    The actual reminder ID and ownership still come from the scoped controller.
    """
    if not isinstance(text, str) or not 1 <= len(text) <= 4000:
        return False
    text = re.sub(r'```[\s\S]*?```|`[^`]*`|["“][^"”]*["”]|‘[^’]*’|(?<!\w)\x27[^\x27\n]*\x27(?!\w)|(?m:^\s*>.*$)|https?://\S+',
                  ' quoted ', text)
    text = ' '.join(addressed_content(text, bot_id).casefold().replace('’', "'").split())
    if re.search(r"\b(?:don't|dont|do not|never|not|no|can't|cannot|without|if|unless|"
                 r"hypothetically|hypothetical|for example|suppose|imagine|pretend)\b", text):
        return False
    # A separate final "also" request may accompany an image question. Its
    # text must still satisfy the complete direct-request grammar below.
    text = re.split(r'[.!?;]\s+(?:also|and)\s+', text)[-1]
    prefix = r'(?:(?:hey|hi|yo|bro|ok|okay|fri|please|pls|plz)[\s,:.!]+)*'
    request = r"(?:(?:can|could|would|will) (?:you|u) (?:please |pls )?|(?:i want|i'd like|i would like) (?:you|u) to )?"
    target = r'(?:(?:all )?(?:(?:my|the|this|that|these|those) )?(?:[a-z0-9_-]+ ){0,6}reminders?|it|that|this)'
    text = re.sub('^'+prefix, '', text)
    # A modal question about the general capability is not a request to affect
    # an owned reminder. A specific possessive/demonstrative target is required.
    if re.match(r'(?:can|could|would|will) (?:you|u) ', text) and not re.match(
            request+r'(?:cancel|delete|remove|drop) (?:all )?(?:my |the |this\b|that\b|these |those |it\b)', text):
        return False
    match = re.fullmatch(request+r'(?:cancel|delete|remove|drop) '+target+r'(?P<tail>.*)', text)
    if not match:
        return False
    tail = match['tail'].strip(' ,.!?')
    if not tail or re.fullmatch(r'(?:(?:please|pls|thanks|thank you|now)[ ,]*)+', tail):
        return True
    # Titles and activity qualifiers can follow a direct reminder request. A
    # statement such as "cancel my reminder is an example" cannot pass as one.
    return bool(re.fullmatch(r'(?:to|for|about|called|named|at|on|quoted) [^;.!?]{1,240}|quoted|[a-f0-9]{32}', tail))


def reminder_tools(clock):
    """The reminder tools a member's DM offers, as the model reads them (the loops eval offers these too)."""
    return {
        'reminder_prepare': {**clock, 'description': 'Prepare a ONE-OFF personal DM reminder only when requested. '
            'Resolve natural dates against utc_now. local_time=YYYY-MM-DDTHH:MM (24-hour), timezone=explicit IANA zone '
            'or labeled server_default_timezone. Ask ONE brief question for ambiguous AM/PM or genuinely missing details. '
            'body is the reminder text, max 1000 characters; due in 1 minute–30 days. This is only a preview; a fresh '
            'Confirm button authorizes it. No recurring jobs, other-member DMs or future tools/code/music. '
            'DST repeated/nonexistent local times require clarification or explicit UTC.'},
        'reminder_list': {'description': 'List this member’s personal DM reminders, exact IDs and delivery states. reminder_id="".'},
        'reminder_cancel': {'description': 'Cancel a reminder only when this member explicitly asks. First list to get its exact reminder_id; never guess.'},
    }


def available(actions):
    if not isinstance(actions.event, discord.Message) or actions.guild != actions.fri.guild_id:
        return {}
    clock = {'utc_now': dt.datetime.now(dt.timezone.utc).isoformat(),
             'server_default_timezone': os.getenv('FRI_TIMEZONE', 'America/Los_Angeles')}
    if not actions.public and isinstance(actions.event.channel, discord.DMChannel) and actions.agent.config['support_dms']:
        tools = reminder_tools(clock)
        if actions.private_evidence:
            tools.pop('reminder_prepare')
        if not cancellation_requested(actions.event.content, getattr(actions.fri.user, 'id', 0)):
            tools.pop('reminder_cancel')
        return tools
    if actions.public:
        if actions.private_evidence:
            return {}
        return {'plan_prepare': {**clock, 'description': 'When asked to organize a game night or choose between times, prepare '
            'an availability/RSVP card for THIS channel. title is an exact activity excerpt from the CURRENT message, max 100 characters; ask for it if missing. local_times is 2–4 '
            'YYYY-MM-DDTHH:MM options separated by newlines, timezone is an IANA zone (or labeled server default). '
            'Ask briefly for missing times or ambiguous AM/PM. Future times only, within 30 days. '
            'The organizer must click Publish before anyone votes; authenticated members change only their own availability. '
            'Only the organizer finalizes or cancels. No pings, calendar integration or unsolicited DMs.'}}
    return {}


async def preparation_text(actions, context, pattern):
    """One verified five-minute clarification can prepare a preview, never grant it."""
    if pattern.search(actions.message):
        return actions.event.content
    turns = context.get('turns', [])
    if not turns or len(actions.message) > 240 or re.search(r"(?i)\b(cancel|forget|stop|never mind|unrelated|different|don't|dont)\b", actions.message):
        return ''
    previous = turns[-1]
    if not previous.get('reply', '').rstrip().endswith('?') or not pattern.search(previous.get('input', '')):
        return ''
    try:
        proof = ChatSource.model_validate({key: value for key, value in previous.get('source', {}).items() if key in ChatSource.model_fields})
        proof.check(str(actions.guild), actions.scope['conversation_id'], str(actions.user))
        if proof.kind != 'discord_message' or not -5 <= (dt.datetime.now(dt.timezone.utc)-proof.timestamp()).total_seconds() <= 300:
            return ''
        original = await actions.event.channel.fetch_message(int(proof.id))
        if original.author.id != actions.user or original.webhook_id or original.edited_at is not None:
            return ''
        visible = addressed_content(original.content, getattr(actions.fri.user, 'id', 0))
        if clean(previous['input'], 4000) not in (clean(original.content, 4000), clean(visible, 4000)):
            return ''
    except (ValueError, discord.HTTPException):
        return ''
    return original.content+'\nClarification: '+actions.event.content


async def execute(actions, action):
    kind = action['kind']
    if kind == 'reminder_cancel' and not cancellation_requested(actions.event.content, getattr(actions.fri.user, 'id', 0)):
        return {'blocked': 'Ask directly to cancel your reminder, for example: fri cancel my reminder to update the mods.'}
    if actions.private_evidence and kind in ('reminder_prepare', 'plan_prepare'):
        return {'blocked': 'Retrieved member history cannot become a persistent reminder or game-night card. Supply the details in your own fresh message.'}
    if kind not in available(actions):
        return {'blocked': 'Personal reminders are available in your verified member DM; game-night cards are available in allowed server channels.'}
    original = await actions.event.channel.fetch_message(actions.event.id)
    if original.author.id != actions.user or original.webhook_id or original.content != actions.event.content:
        return {'blocked': 'Your original request changed. Send a fresh message.'}
    current = await actions.fri.api('POST', '/chat/context', actions.scope)
    if current['revision'] != actions.revision:
        return {'blocked': 'Privacy settings changed. Ask again for a fresh preview.'}
    if kind == 'reminder_list':
        return {'kind': kind, **await actions.fri.api('POST', '/social/reminders/list', actions.scope)}
    source = discord_source(actions.event).model_dump()
    payload = {**actions.scope, 'source': source, 'revision': actions.revision}
    if kind == 'reminder_cancel':
        item = action.get('reminder_id', '')
        if not re.fullmatch(r'[a-f0-9]{32}', item):
            return {'blocked': 'Ask explicitly to cancel an existing reminder; list your reminders to identify it.'}
        row = await actions.fri.api('POST', '/social/reminders/'+item+'/decide', {**payload, 'decision': 'cancel'})
        return {'kind': kind, 'id': row['id'], 'state': row['state'],
                'detail': 'Delivery has already started; cancellation cannot undo it.' if row['state'] == 'sending' else ''}
    request_text = await preparation_text(actions, current, REMINDER_REQUEST if kind == 'reminder_prepare' else PLAN_REQUEST)
    if not request_text:
        return {'blocked': 'Ask explicitly for a reminder or game-night preview with its details. Earlier requests need a fresh matching clarification.'}
    route = '/social/reminders/prepare' if kind == 'reminder_prepare' else '/social/plans/prepare'
    if kind == 'plan_prepare':
        payload['request_text'] = request_text
    row = await actions.fri.api('POST', route, {**payload, **{key: value for key, value in action.items() if key != 'kind'}})
    return {'social_preview': {'kind': 'reminder' if kind == 'reminder_prepare' else 'plan', 'item': row}}


def component(interaction, bot_id):
    return (getattr(getattr(getattr(interaction, 'message', None), 'author', None), 'id', None) == bot_id and
            COMPONENT.fullmatch(str((getattr(interaction, 'data', None) or {}).get('custom_id', ''))) is not None)


async def identity(fri, interaction, kind):
    if not fri.agent.config['enabled'] or interaction.user.bot:
        raise commands.UserFeedbackCheckFailure('Fri is unavailable for this action.')
    if kind == 'reminder':
        if interaction.guild_id is not None or not isinstance(interaction.channel, discord.DMChannel) or not fri.agent.config['support_dms']:
            raise commands.UserFeedbackCheckFailure('Use your original personal reminder DM.')
        channel, conversation = fri.workshop_id, 'private:'+str(interaction.channel_id)
    else:
        if interaction.guild_id != fri.guild_id or not fri.agent.allowed(interaction.channel):
            raise commands.UserFeedbackCheckFailure('This game-night card is outside the allowed server channels.')
        channel = getattr(interaction.channel, 'parent_id', None) or interaction.channel_id
        conversation = 'public:'+str(interaction.channel_id)
    member = await fri.agent.member(interaction.user.id, fresh=True)
    if not member:
        raise commands.UserFeedbackCheckFailure('Current server membership could not be verified.')
    if kind == 'plan':
        for who in (member, interaction.guild.me):
            permissions = interaction.channel.permissions_for(who)
            send = permissions.send_messages_in_threads if isinstance(interaction.channel, discord.Thread) else permissions.send_messages
            if not (permissions.view_channel and permissions.read_message_history and send):
                raise commands.UserFeedbackCheckFailure('Current channel access could not be verified.')
    scope = {'guild_id': str(fri.guild_id), 'user_id': str(interaction.user.id),
             'channel_id': str(channel), 'conversation_id': conversation}
    context = await fri.api('POST', '/chat/context', scope)
    return scope, context['revision']


def render(kind, row):
    view = discord.ui.View(timeout=None)
    if kind == 'reminder':
        content = f"**Personal reminder · {row['state']}**\n<t:{int(row['due'])}:F>\n{row['local_time'].replace('T', ' ')} · {row['timezone']}\nDestination: this DM. One message; no future commands."
        if row['state'] == 'pending':
            content += '\nNothing scheduled until Confirm. Preview expires after 15 minutes.'
            view.add_item(SocialButton(kind, row['id'], 'confirm', 'Confirm'))
        if row['state'] in ('pending', 'scheduled', 'claimed'):
            view.add_item(SocialButton(kind, row['id'], 'cancel', 'Cancel reminder'))
        if row['state'] == 'uncertain':
            content += '\nDelivery is uncertain. It will not be retried automatically.'
        return content, view
    title = discord.utils.escape_markdown(discord.utils.escape_mentions(row['title']))
    content = f"**{title} · {row['state']}**\nTimes display in your Discord timezone. Planning timezone: {row['timezone']}.\n"
    for index, timestamp in enumerate(row['slots']):
        content += f"\n{index+1}. <t:{int(timestamp)}:F> · {row['available'][index]} available"
    if row['state'] == 'pending':
        content += '\n\nOrganizer: check these exact times, then Publish. Nothing is scheduled or sent by DM. Preview expires after 15 minutes.'
        view.add_item(SocialButton(kind, row['id'], 'publish', 'Publish card'))
    elif row['state'] == 'open':
        content += '\n\nMembers: toggle each time you can make. Organizer: finalize one time below. No pings or DMs.'
        for index in range(len(row['slots'])):
            view.add_item(SocialButton(kind, row['id'], 'vote'+str(index), 'Available '+str(index+1), row=0))
        for index in range(len(row['slots'])):
            view.add_item(SocialButton(kind, row['id'], 'final'+str(index), 'Finalize '+str(index+1), row=1))
    elif row['state'] == 'finalized':
        content += f"\n\n**Chosen: <t:{int(row['slots'][row['selected']])}:F>**\nGoing: {row['rsvps']['going']} · Maybe: {row['rsvps']['maybe']} · Cannot: {row['rsvps']['cannot']}\nAvailability is not an RSVP. Choose your response below."
        for decision, label in (('going', 'Going'), ('maybe', 'Maybe'), ('cannot', 'Cannot make it')):
            view.add_item(SocialButton(kind, row['id'], decision, label, row=0))
    if row['state'] in ('pending', 'open', 'finalized'):
        view.add_item(SocialButton(kind, row['id'], 'cancel', 'Cancel plan', row=2))
    return content, view


def reminder_embed(row):
    """Keep the full exact text separate from metadata; never clip a confirmation."""
    body = discord.utils.escape_markdown(discord.utils.escape_mentions(row['body']))
    if len(body.encode('utf-16-le'))//2 > 4096:
        raise commands.UserFeedbackCheckFailure('The reminder text exceeds Discord’s embed limit. Prepare a shorter reminder.')
    return discord.Embed(description=body) if body else None


def presentation(kind, row):
    content, view = render(kind, row)
    if len(content.encode('utf-16-le'))//2 > 2000:
        raise commands.UserFeedbackCheckFailure('The preview exceeds Discord’s message limit. Prepare a shorter preview.')
    return {'content': content, 'view': view, 'embed': reminder_embed(row) if kind == 'reminder' else None,
            'allowed_mentions': NO_MENTIONS}


class SocialButton(discord.ui.DynamicItem[discord.ui.Button], template=COMPONENT):
    def __init__(self, kind, item, decision, label=None, *, row=None):
        self.kind, self.item_id, self.decision = kind, item, decision
        super().__init__(discord.ui.Button(label=label or decision.capitalize(), row=row,
            style=discord.ButtonStyle.primary if decision in ('confirm', 'publish') else discord.ButtonStyle.secondary,
            custom_id=f'fri:social:{kind}:{item}:{decision}'))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(*match.groups())

    async def callback(self, interaction):
        fri = interaction.client
        if not component(interaction, getattr(fri.user, 'id', None)):
            return
        await interaction.response.defer()
        locks = getattr(fri, 'social_locks', None)
        if locks is None:
            locks = fri.social_locks = weakref.WeakValueDictionary()
        lock = locks.setdefault((self.kind, self.item_id), asyncio.Lock())
        async with lock:
            await self.apply(interaction)

    async def apply(self, interaction):
        fri = interaction.client
        try:
            scope, revision = await identity(fri, interaction, self.kind)
            route = '/social/'+('reminders' if self.kind == 'reminder' else 'plans')+'/'+self.item_id
            row = await fri.api('POST', route+'/decide', {**scope, 'source': discord_source(interaction).model_dump(),
                'revision': revision, 'decision': self.decision, 'preview_id': str(interaction.message.id)})
            # Always render current controller state, including votes delivered while
            # the preceding request was in flight. Edits never post another card.
            row = await fri.api('POST', route+'/read', scope)
            await interaction.edit_original_response(**presentation(self.kind, row))
            if self.kind == 'plan' and self.decision.startswith('vote'):
                await interaction.followup.send('your availability was updated. click the same option again to remove it.', ephemeral=True, allowed_mentions=NO_MENTIONS)
        except (commands.UserFeedbackCheckFailure, discord.HTTPException, ValueError, KeyError):
            await interaction.followup.send('that action could not be confirmed. the card may have expired, access may have changed, or this action belongs to the organizer. check current status before trying again.', ephemeral=True, allowed_mentions=NO_MENTIONS)


async def show_preview(fri, message, preview, delivered=None):
    kind, row = preview['kind'], preview['item']
    if row['user_id'] != str(message.author.id) or row['guild_id'] != str(fri.guild_id):
        raise commands.UserFeedbackCheckFailure('That preview belongs to another member.')
    private = kind == 'reminder'
    if (private and (message.guild is not None or not isinstance(message.channel, discord.DMChannel))) or (not private and (not message.guild or message.guild.id != fri.guild_id or not fri.agent.allowed(message.channel))):
        raise commands.UserFeedbackCheckFailure('Use the original reminder DM or game-night channel.')
    if row['preview_id']:
        await message.reply('that preview already exists. use the original card, or ask for current reminder status.', allowed_mentions=NO_MENTIONS)
        if delivered:
            await delivered()
        return
    posted = await message.reply(**presentation(kind, row), mention_author=False, suppress_embeds=kind != 'reminder')
    if delivered:  # Discord accepted the card; record that even if binding fails next.
        await delivered()
    scope = {'guild_id': str(fri.guild_id), 'user_id': str(message.author.id),
             'channel_id': str(fri.workshop_id if private else (getattr(message.channel, 'parent_id', None) or message.channel.id)),
             'conversation_id': ('private:' if private else 'public:')+str(message.channel.id)}
    route = '/social/'+('reminders' if private else 'plans')+'/'+row['id']+'/bind'
    await fri.api('POST', route, {**scope, 'message_id': str(posted.id)})


async def deliver_one(fri):
    """A confirmed reminder can attempt exactly one bounded HTTP send, without a model."""
    directory = fri.data/'reminder-receipts'
    for path in sorted(directory.glob('*.json')):
        try:
            receipt = json.loads(path.read_text())
        except FileNotFoundError:
            continue
        try:
            await fri.api('POST', '/social/reminders/'+path.stem+'/finish', receipt)
        except ControllerRejected:
            pass
        path.unlink(missing_ok=True)
    if not fri.agent.config['enabled'] or not fri.agent.config['support_dms']:
        return
    row = await fri.api('POST', '/social/reminders/claim')
    if not row:
        return
    state, message_id, sending = 'failed', '', False
    try:
        if str(fri.guild_id) != row['guild_id'] or not await fri.agent.member(int(row['user_id']), fresh=True):
            raise ValueError('Current server membership could not be verified')
        dm = await fri.fetch_channel(int(row['dm_id']))
        if not isinstance(dm, discord.DMChannel) or not dm.recipient or str(dm.recipient.id) != row['user_id']:
            raise ValueError('The reminder DM no longer matches its author')
        embed = reminder_embed(row)
        await fri.api('POST', '/social/reminders/'+row['id']+'/sending', {'lease': row['lease']})
        sending = True
        result = await send_once(fri, {'id': row['id'], 'target_id': row['dm_id'],
            'body': f"Reminder\nScheduled for <t:{int(row['due'])}:F>."}, embed=embed.to_dict() if embed else None)
        message_id, state = str(int(result['id'])), 'sent'
    except (commands.UserFeedbackCheckFailure, discord.HTTPException, httpx.HTTPError, ValueError, KeyError, OSError, TimeoutError):
        state = 'uncertain' if sending else 'failed'
    receipt = {'lease': row['lease'], 'state': state, 'message_id': message_id}
    path = directory/(row['id']+'.json')
    private_json(path, receipt)
    await fri.api('POST', '/social/reminders/'+row['id']+'/finish', receipt)
    path.unlink(missing_ok=True)
    try:
        dm = await fri.fetch_channel(int(row['dm_id']))
        preview = await dm.fetch_message(int(row['preview_id']))
        if preview.author.id == fri.user.id:
            await preview.edit(**presentation('reminder', {**row, **receipt}))
    except (discord.HTTPException, ValueError):
        pass
