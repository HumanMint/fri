"""Discord adapter for scoped work, capability inspection and explicit publication."""

import asyncio
import copy
import json
import re

import discord
import httpx

from . import feedback as commands
from .fri_context import discord_source
from .fri_errors import ControllerRejected
from .privacy import clean
from .subscription import private_json

KINDS = {'work_list', 'work_read', 'work_save', 'channel_capabilities', 'prepare_post'}
POST_ID = re.compile(r'^fri:post:(?P<post_id>[a-f0-9]{32}):(?P<decision>send|cancel)$')
NO_MENTIONS = discord.AllowedMentions.none()


async def capabilities(fri, target_id=''):
    """Fresh roles, member state and channel overwrites; never use model claims."""
    guild = fri.get_guild(fri.guild_id)
    if not guild or not fri.agent.config['enabled']:
        return {}
    members = [await guild.fetch_member(user) for user in (fri.owner_id, fri.user.id)]
    fresh_guild = copy.copy(guild)
    fresh_guild._roles = {role.id: role for role in await guild.fetch_roles()}
    channels = [await fri.fetch_channel(int(target_id))] if target_id else await guild.fetch_channels()
    result = {}
    for channel in channels:
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != fri.guild_id or channel.nsfw:
            continue
        channel = copy.copy(channel)
        channel.guild = fresh_guild
        owner, bot = [channel.permissions_for(member) for member in members]
        if not owner.view_channel:
            continue
        result[str(channel.id)] = {'channel_id': str(channel.id), 'name': clean(channel.name, 80),
            'read_history': bool(owner.read_message_history and bot.view_channel and bot.read_message_history),
            'ambient_reply': fri.agent.allowed(channel),
            'configured_read': str(channel.id) in fri.agent.state['history_channels'] or fri.agent.allowed(channel),
            'one_off_post': bool(owner.send_messages and bot.view_channel and bot.send_messages)}
    return result


def in_owner_dm(actions):
    return (actions.owner and isinstance(actions.event, discord.Message) and actions.event.guild is None
            and isinstance(actions.event.channel, discord.DMChannel))


def available(actions):
    if not isinstance(actions.event, discord.Message):
        return {}
    tools = {
        'work_list': {'description': 'List your saved work in this conversation. work_id="". No private work crosses into public chat.'},
        'work_read': {'description': 'Read exact saved work by work_id, including version and status. Never guess an ID.'},
        'work_save': {'description': 'When asked to save/update a draft or task, persist exact text for later. '
            'New work: work_id="", version=0. Edit: first read its current version. title, body, state=open/completed/cancelled. '
            'In public, the body must be exact text from your current message, or unchanged existing work; title is derived from that text. '
            'Use DMs to compose freely. 30 items per member, 30 day retention. Saving does not schedule or authorize execution.'}}
    if in_owner_dm(actions):
        tools['channel_capabilities'] = {'description': 'Check actual server channel capabilities. channel_id="" lists channels. '
            'Read access, ambient replies and owner-authorized one-off posts are distinct.'}
        tools['prepare_post'] = {'description': 'Prepare a single owner DM preview of exact saved work (work_id, version, channel_id). '
            'Only after the owner asks to send/post. Nothing is sent until they click Send once. '
            'May target a permitted server text/announcement channel outside ambient reply scope. '
            'No crossposting to followers, pings, DMs to others or permission changes. Max 1900 Discord characters.'}
    return tools


async def execute(actions, action):
    fri, kind = actions.fri, action['kind']
    if kind not in available(actions):
        return {'blocked': 'That capability is unavailable in this conversation.'}
    original = await actions.event.channel.fetch_message(actions.event.id)
    if (original.author.id != actions.user or original.webhook_id or original.content != actions.event.content):
        return {'blocked': 'Your original request changed. Send a fresh message.'}
    current = await fri.api('POST', '/chat/context', actions.scope)
    if current['revision'] != actions.revision or not current['personal_memory']['enabled']:
        return {'blocked': 'Saved work is unavailable while memory is off or changing.'}
    if actions.private_evidence and kind in ('work_save', 'prepare_post'):
        return {'blocked': 'Read member evidence cannot be copied into saved work or a post. Provide the request in a fresh message.'}
    if kind == 'work_list':
        return {'kind': kind, **await fri.api('POST', '/workspace/list', actions.scope)}
    if kind == 'work_read':
        return {'kind': kind, **await fri.api('POST', '/workspace/read/'+action['work_id'], actions.scope)}
    if kind == 'channel_capabilities':
        return {'kind': kind, 'channels': list((await capabilities(fri, action['channel_id'])).values())}
    source = discord_source(actions.event).model_dump()
    payload = {**actions.scope, 'source': source, 'revision': actions.revision}
    if kind == 'work_save':
        if not re.search(r'\b(save|keep|hold|draft|store|remember|update|edit|change|mark|cancel|complete|post|send)\b', actions.message, re.I):
            return {'blocked': 'Ask explicitly to save or update this work first.'}
        return {'kind': kind, **await fri.api('POST', '/workspace/save',
            {**payload, **{k: v for k, v in action.items() if k != 'kind'}, 'request_text': actions.event.content})}
    if not re.search(r'\b(post|send|publish|introduce|share)\b', actions.message, re.I):
        return {'blocked': 'Ask to prepare a post first; saving a draft does not request delivery.'}
    targets = await capabilities(fri, action['channel_id'])
    if not targets.get(action['channel_id'], {}).get('one_off_post'):
        return {'blocked': 'You and Fri both need permission to post in that server channel.'}
    post = await fri.api('POST', '/workspace/posts/prepare', {**payload, 'work_id': action['work_id'],
        'version': action['version'], 'target_id': action['channel_id']})
    return {'kind': kind, 'post_preview': post}


def post_component(interaction, bot_id, owner_id):
    return (getattr(interaction, 'guild_id', None) is None and
        getattr(getattr(interaction, 'user', None), 'id', None) == owner_id and
        getattr(getattr(getattr(interaction, 'message', None), 'author', None), 'id', None) == bot_id and
        POST_ID.fullmatch(str((getattr(interaction, 'data', None) or {}).get('custom_id', ''))) is not None)


class PostButton(discord.ui.DynamicItem[discord.ui.Button], template=POST_ID):
    def __init__(self, post_id, decision):
        self.post_id, self.decision = post_id, decision
        super().__init__(discord.ui.Button(label='Send once' if decision == 'send' else 'Cancel',
            style=discord.ButtonStyle.primary if decision == 'send' else discord.ButtonStyle.secondary,
            custom_id=f'fri:post:{post_id}:{decision}'))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(**match.groupdict())

    async def callback(self, interaction):
        fri = interaction.client
        if not post_component(interaction, getattr(fri.user, 'id', None), fri.owner_id):
            return
        await interaction.response.defer()
        try:
            post = await fri.api('GET', '/workspace/posts/'+self.post_id)
            if self.decision == 'send':
                targets = await capabilities(fri, post['target_id'])
                if not targets.get(post['target_id'], {}).get('one_off_post'):
                    raise commands.UserFeedbackCheckFailure('Channel access changed. Nothing sent.')
            result = await fri.api('POST', f'/workspace/posts/{self.post_id}/decide', {
                'source': discord_source(interaction).model_dump(), 'preview_id': str(interaction.message.id),
                'approve': self.decision == 'send'})
            await interaction.edit_original_response(content='post '+result['state']+'.', view=None,
                                                     allowed_mentions=NO_MENTIONS)
            if result['state'] == 'authorized':
                await deliver_one(fri)
                result = await fri.api('GET', '/workspace/posts/'+self.post_id)
                await interaction.edit_original_response(content=delivery_text(result), view=None,
                                                         allowed_mentions=NO_MENTIONS)
        except (commands.UserFeedbackCheckFailure, discord.HTTPException, ValueError):
            await interaction.followup.send('i couldn’t confirm that action. ask for the saved post status before retrying.',
                                             ephemeral=True, allowed_mentions=NO_MENTIONS)


def delivery_text(post):
    if post['state'] == 'sent':
        return f"posted: https://discord.com/channels/{post['guild_id']}/{post['target_id']}/{post['message_id']}"
    return ('post '+post['state']+'. '+ ('i will not resend automatically.' if post['state'] == 'uncertain' else ''))


async def show_preview(fri, message, post, delivered=None):
    if message.guild is not None or message.author.id != fri.owner_id or not isinstance(message.channel, discord.DMChannel):
        raise commands.UserFeedbackCheckFailure('Open the post preview in your owner DM.')
    if post['state'] != 'pending' or post['preview_id']:
        await message.reply(delivery_text(post)+' ask for a fresh preview if needed.', allowed_mentions=NO_MENTIONS)
        if delivered:
            await delivered()
        return
    view = discord.ui.View(timeout=None)
    for decision in ('send', 'cancel'):
        view.add_item(PostButton(post['id'], decision))
    # Full text stays inline on mobile. No file download or clipped preview.
    embed = discord.Embed(description=discord.utils.escape_markdown(discord.utils.escape_mentions(post['body'])))
    embed.set_footer(text=f"Draft v{post['version']} · expires in 15 minutes · pings disabled")
    preview = await message.reply(f"**send once to <#{post['target_id']}>**\nnothing posted yet. this does not enable ambient replies there.",
        embed=embed, view=view, mention_author=False, allowed_mentions=NO_MENTIONS)
    if delivered:  # Discord accepted the card; record that even if binding fails next.
        await delivered()
    await fri.api('POST', f"/workspace/posts/{post['id']}/bind", {'message_id': str(preview.id)})


async def deliver_one(fri):
    """No model involved. Persist before sending; ambiguous outcomes never auto-retry."""
    directory = fri.data/'post-receipts'
    for path in sorted(directory.glob('*.json')):
        try:
            receipt = json.loads(path.read_text())
        except FileNotFoundError:
            continue
        try:
            await fri.api('POST', '/workspace/posts/'+path.stem+'/finish', receipt)
        except ControllerRejected:
            pass  # Erased or invalidated grants cannot be resurrected by a local receipt.
        path.unlink(missing_ok=True)
    post = await fri.api('POST', '/workspace/outbox/claim')
    if not post:
        return
    state, message_id, sending = 'failed', '', False
    try:
        if str(fri.owner_id) != post['user_id'] or str(fri.guild_id) != post['guild_id']:
            raise ValueError('Owner or server changed')
        targets = await capabilities(fri, post['target_id'])
        if not targets.get(post['target_id'], {}).get('one_off_post'):
            raise ValueError('Permissions changed')
        # Deletion/edit of the originating owner request revokes pending delivery.
        dm = await fri.fetch_channel(int(post['dm_id']))
        source = json.loads(post['source'])
        original = await dm.fetch_message(int(source['id']))
        if original.author.id != fri.owner_id or original.edited_at is not None:
            raise ValueError('Original request changed')
        await fri.api('POST', f"/workspace/posts/{post['id']}/sending", {'lease': post['lease']})
        sending = True
        result = await send_once(fri, post)
        message_id, state = str(int(result['id'])), 'sent'
    except (commands.UserFeedbackCheckFailure, discord.HTTPException, httpx.HTTPError, ValueError, KeyError, OSError, TimeoutError):
        state = 'uncertain' if sending else 'failed'
    receipt = {'lease': post['lease'], 'state': state, 'message_id': message_id}
    path = directory/(post['id']+'.json')
    private_json(path, receipt)
    await fri.api('POST', f"/workspace/posts/{post['id']}/finish", receipt)
    path.unlink(missing_ok=True)
    try:
        dm = await fri.fetch_channel(int(post['dm_id']))
        preview = await dm.fetch_message(int(post['preview_id']))
        if preview.author.id == fri.user.id:
            await preview.edit(content=delivery_text({**post, **receipt}), view=None,
                               allowed_mentions=NO_MENTIONS)
    except (discord.HTTPException, ValueError):
        pass  # The durable receipt remains available even if its DM preview was deleted.


async def send_once(fri, post, *, transport=None, embed=None):
    """Do not use discord.py's retrying HTTPClient for non-repeatable publication.

    No redirects, proxy inheritance, rate-limit sleep or retries. Even a 429/5xx is
    returned to the outbox as uncertainty, never used to initiate another attempt.
    """
    target = str(int(post['target_id']))
    if len(post['body'].encode('utf-16-le'))//2 > 2000:
        raise ValueError('Message content exceeds Discord’s UTF-16 limit')
    payload = {'content': post['body'], 'allowed_mentions': {'parse': []},
               'nonce': str(int(post['id'][:20], 16)), 'enforce_nonce': True, 'flags': 4}
    if embed is not None:
        if len(embed.get('description', '').encode('utf-16-le'))//2 > 4096:
            raise ValueError('Reminder embed exceeds Discord’s UTF-16 limit')
        payload.update(embeds=[embed], flags=0)
    async with asyncio.timeout(25), httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False, transport=transport) as client:
        response = await client.post(f'https://discord.com/api/v10/channels/{target}/messages',
            headers={'Authorization': 'Bot '+fri.http.token},
            json=payload)
        response.raise_for_status()
        return response.json()
