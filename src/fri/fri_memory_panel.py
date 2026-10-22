"""Private, member-bound controls for one conversation's exact personal preferences."""

import re
import uuid

import discord

from . import feedback as commands
from .fri_context import discord_source

NO_MENTIONS = discord.AllowedMentions.none()
COMPONENT = re.compile(r'^fri:memory:(?:open|choose|remove|replace|edit):[a-f0-9]{32}$')


def memory_component(interaction, bot_id):
    kind = getattr(getattr(interaction, 'type', None), 'value', None)
    return bool(interaction.guild_id is None and kind in (3, 5) and
        COMPONENT.fullmatch(str((interaction.data or {}).get('custom_id', ''))) and
        (kind == 5 or getattr(getattr(getattr(interaction, 'message', None), 'author', None), 'id', None) == bot_id))


def scope_for(cog, interaction):
    parent = getattr(interaction.channel, 'parent_id', None) or interaction.channel_id
    return {'guild_id': str(interaction.guild_id or cog.guild_id), 'user_id': str(interaction.user.id),
            'channel_id': str(parent if interaction.guild_id else cog.workshop_id),
            'conversation_id': 'private:'+str(interaction.channel_id)}


async def allowed(cog, interaction, scope, *, message=True):
    if (str(interaction.user.id) != scope['user_id'] or
            str(interaction.channel_id) != scope['conversation_id'].split(':', 1)[1]):
        return False
    if message and getattr(getattr(interaction.message, 'author', None), 'id', None) != cog.user.id:
        return False
    if not cog.agent:
        return False
    if interaction.guild_id is not None and (str(interaction.guild_id) != scope['guild_id'] or
                                             not cog.agent.allowed(interaction.channel)):
        return False
    member = await cog.agent.member(interaction.user.id, fresh=True, guild_id=int(scope['guild_id']))
    if not member:
        return False
    if interaction.guild_id:
        permissions = interaction.channel.permissions_for(member)
        return bool(permissions.view_channel and permissions.read_message_history)
    return True


async def show(cog, interaction, scope=None, *, state=None, selected=None, notice=''):
    scope = scope or scope_for(cog, interaction)
    if not await allowed(cog, interaction, scope, message=False):
        return
    state = state or await cog.api('POST', '/chat/memory/notes', scope)
    lines = [notice] if notice else []
    lines += ['Memory is '+('on.' if state['enabled'] else 'off.'),
              'Up to ten preferences here, at most three per category. Each expires after 90 days.']
    for entry in state['entries']:
        lines.append(f"• {entry['category']}: {discord.utils.escape_markdown(entry['evidence'])} "
                     f"(expires <t:{int(entry['expires_at'])}:d>)")
    if not state['entries']:
        lines.append('No saved preferences in this conversation.')
    lines.append('Editing or removing a preference clears recent saved context here. Discord posts and backups are separate.')
    from .fri_panel import card, reply
    await reply(cog, interaction, embed=card('My memory', '\n\n'.join(lines)),
                view=MemoryView(cog, scope, state, selected))


class MemoryLauncher(discord.ui.View):
    def __init__(self, cog, scope):
        super().__init__(timeout=600)
        self.cog, self.scope = cog, scope
        button = discord.ui.Button(label='Edit my memory', custom_id='fri:memory:open:'+uuid.uuid4().hex)
        button.callback = self.open
        self.add_item(button)

    async def interaction_check(self, interaction):
        return await allowed(self.cog, interaction, self.scope)

    async def open(self, interaction):
        if await self.interaction_check(interaction):
            await show(self.cog, interaction, self.scope)


class MemoryView(MemoryLauncher):
    def __init__(self, cog, scope, state, selected=None):
        super().__init__(cog, scope)
        self.clear_items()
        self.state = state
        self.selected = next((row for row in state['entries'] if row['id'] == selected), None)
        if state['entries']:
            choose = discord.ui.Select(placeholder='Choose a saved preference',
                custom_id='fri:memory:choose:'+uuid.uuid4().hex, options=[discord.SelectOption(
                    label=row['evidence'][:100], description=row['category'], value=str(row['id']),
                    default=row['id'] == selected) for row in state['entries']])
            async def choose_entry(interaction):
                if await self.interaction_check(interaction):
                    await show(cog, interaction, scope, selected=int(choose.values[0]))
            choose.callback = choose_entry
            self.add_item(choose)
        if self.selected:
            remove = discord.ui.Button(label='Remove selected', style=discord.ButtonStyle.danger,
                custom_id='fri:memory:remove:'+uuid.uuid4().hex)
            remove.callback = self.remove
            self.add_item(remove)
            replace = discord.ui.Button(label='Edit selected', custom_id='fri:memory:replace:'+uuid.uuid4().hex)
            replace.callback = self.replace
            self.add_item(replace)

    async def remove(self, interaction):
        if not await self.interaction_check(interaction):
            return
        try:
            state = await self.cog.api('POST', '/chat/memory/notes/'+str(self.selected['id']), {
                **self.scope, 'source': discord_source(interaction).model_dump(), 'revision': self.state['revision'],
                'action': 'remove'})
            self.cog.agent.forget(interaction.user.id)
            await show(self.cog, interaction, self.scope, state=state, notice='Removed that preference.')
        except commands.UserFeedbackCheckFailure as exc:
            await show(self.cog, interaction, self.scope, notice=str(exc))

    async def replace(self, interaction):
        if await self.interaction_check(interaction):
            await interaction.response.send_modal(EditMemory(self))


class EditMemory(discord.ui.Modal):
    def __init__(self, view):
        super().__init__(title='Edit saved preference', custom_id='fri:memory:edit:'+uuid.uuid4().hex, timeout=600)
        self.view = view
        self.preference = discord.ui.TextInput(label='A first-person preference, e.g. I like chess',
            default=view.selected['evidence'], min_length=5, max_length=180)
        self.add_item(self.preference)

    async def interaction_check(self, interaction):
        return await allowed(self.view.cog, interaction, self.view.scope, message=False)

    async def on_submit(self, interaction):
        if not await self.interaction_check(interaction):
            return
        view = self.view
        try:
            state = await view.cog.api('POST', '/chat/memory/notes/'+str(view.selected['id']), {
                **view.scope, 'source': discord_source(interaction).model_dump(), 'revision': view.state['revision'],
                'action': 'replace', 'evidence': self.preference.value})
            view.cog.agent.forget(interaction.user.id)
            await show(view.cog, interaction, view.scope, state=state, notice='Updated that preference.')
        except commands.UserFeedbackCheckFailure as exc:
            await show(view.cog, interaction, view.scope, notice=str(exc))
