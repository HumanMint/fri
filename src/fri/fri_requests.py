"""Request previews in the current chat, with explicit owner intake approval."""

import asyncio
import re
import uuid

import discord

from . import feedback as commands
from .fri_notices import notice_text
from .previews import PagedPreview
from .reviews import request_reference

NO_MENTIONS = discord.AllowedMentions.none()
REQUEST_COMPONENT = re.compile(r'^fri:request:(?:(?:preview|submit):[a-f0-9]{32}|[a-f0-9]{32}:(?:back|next|submit|cancel))$')


def request_dm_component(interaction, bot_id):
    """Only admit our request buttons; the view rechecks the bound member and scope."""
    return bool(bot_id and interaction.guild_id is None and
        getattr(getattr(interaction, 'type', None), 'value', None) == 3 and
        getattr(getattr(getattr(interaction, 'message', None), 'author', None), 'id', None) == bot_id and
        REQUEST_COMPONENT.fullmatch(str((interaction.data or {}).get('custom_id', ''))))


class DraftView(PagedPreview):
    def __init__(self, cog, user_id, draft, *, public=False, reviewed=False, source_id=None, fence=None):
        self.cog, self.user_id, self.draft = cog, user_id, draft
        # The staged turn's privacy fence: a draft built from other members' words is never
        # shown again or submitted once any of them erases or changes privacy.
        self.fence = dict(fence) if fence else None
        self.source_id = source_id or uuid.uuid4().hex
        self.owner = user_id == cog.owner_id
        self.reviewed = reviewed
        self.preview_interaction = None
        if reviewed:
            super().__init__('Review your request', draft,
                'Submit allows coding; deployment needs a separate review.' if self.owner else
                'Submit sends this to the owner. Coding waits for approval.',
                'Submit & allow coding' if self.owner else 'Submit for owner review',
                f'fri:request:{self.source_id}:', self.submit)
        else:
            discord.ui.View.__init__(self, timeout=600)
            self.lock, self.finished = asyncio.Lock(), False
            button = discord.ui.Button(label='Review request', style=discord.ButtonStyle.primary,
                                       custom_id=f'fri:request:preview:{self.source_id}')
            button.callback = self.preview
            self.add_item(button)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            return False
        if getattr(getattr(interaction.message, 'author', None), 'id', None) != self.cog.user.id:
            return False
        if interaction.guild_id is None:
            return bool(self.cog.agent and await self.cog.agent.member(self.user_id, fresh=True,
                                                                       guild_id=self.cog.guild_id))
        channel = getattr(interaction.channel, 'parent_id', None) or interaction.channel_id
        return bool(interaction.guild_id == self.cog.guild_id and (channel == self.cog.workshop_id or
                    (self.cog.agent and self.cog.agent.allowed(interaction.channel))))

    async def preview(self, interaction):
        if not await self.interaction_check(interaction):
            return
        await self.fenced()
        view = DraftView(self.cog, self.user_id, self.draft, reviewed=True, source_id=self.source_id,
                         fence=self.fence)
        view.preview_interaction = interaction
        if interaction.guild_id is None or getattr(getattr(interaction.message, 'flags', None), 'ephemeral', False):
            await interaction.response.edit_message(content=view.content, embed=None, attachments=[], view=view,
                                                     allowed_mentions=NO_MENTIONS)
        else:
            from .fri_panel import sessions
            await sessions(self.cog).show(interaction, content=view.content, attachments=[], view=view)

    async def submit(self, interaction):
        if not self.reviewed or not await self.interaction_check(interaction):
            return
        await interaction.response.defer()
        async with self.lock:
            if self.finished or self.confirm_button.disabled:
                return
            await self.fenced()
            channel = (getattr(interaction.channel, 'parent_id', None) or interaction.channel_id) if interaction.guild_id else self.cog.workshop_id
            identity = {'guild_id': str(self.cog.guild_id), 'user_id': str(self.user_id)}
            job = await self.cog.api('POST', '/requests', {**identity,
                'channel_id': str(channel), 'source_id': 'fri-draft:'+self.source_id, 'source_url': '', 'body': self.draft})
            # Never approve content differing from the full preview, even if a duplicate ID is returned.
            from .privacy import clean
            if job['body'] != clean(self.draft) or job['user_id'] != str(self.user_id):
                raise commands.UserFeedbackCheckFailure('The stored request differs from this preview. Review it in Requests.')
            if self.owner and job['state'] == 'awaiting_owner':
                await self.cog.api('POST', f"/requests/{job['id']}/review", {**identity, 'phase': 'intake',
                    'reference': request_reference(job), 'decision': 'approve'})
                text = f"Request `{job['id']}` saved and development approved. I’ll DM you the tested proposal."
            elif self.owner:
                text = f"Request `{job['id']}` is already {job['state'].replace('_', ' ')}. No duplicate job started."
            else:
                text = f"Request `{job['id']}` saved. I’ve queued the owner review; coding waits for approval."
            await interaction.edit_original_response(content=notice_text(text), embed=None, attachments=[], view=None,
                                                       allowed_mentions=NO_MENTIONS)
            self.finished = True
            self.stop()

    async def fenced(self):
        if self.fence:
            await self.cog.api('POST', '/chat/turn/check', self.fence)

    async def on_timeout(self):
        from .fri_panel import sessions
        await sessions(self.cog).close((self.cog.guild_id, self.user_id), self)
        if self.preview_interaction and self.preview_interaction.guild_id is None:
            try:
                await self.preview_interaction.edit_original_response(view=None)
            except discord.HTTPException:
                pass

    async def on_error(self, interaction, error, item):
        text = str(error) if isinstance(error, commands.UserFeedbackCheckFailure) else 'That request action did not finish. Check Requests before retrying.'
        text = notice_text(text)
        if interaction.response.is_done():
            await interaction.followup.send(text[:1500], ephemeral=True, allowed_mentions=NO_MENTIONS)
        else:
            await interaction.response.send_message(text[:1500], ephemeral=True, allowed_mentions=NO_MENTIONS)
