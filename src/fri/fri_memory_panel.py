"""Private, member-bound controls for a member's exact personal preferences."""

import re
import uuid

import discord

from . import feedback as commands
from .fri_context import discord_source
from .fri_memory import MOVES, clip, note_place, profiled, units

NO_MENTIONS = discord.AllowedMentions.none()
SHOWN = 25  # Discord's limit for one select menu.
COMPONENT = re.compile(r'^fri:memory:(?:(?:open|choose|remove|replace|edit|share|confirm|keep|summary|clear|rewrite):'
                       r'[a-f0-9]{32}|open:[0-9]{1,20})$')
OPEN = r'fri:memory:open:(?P<member>[0-9]{1,20})'


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


def launched(cog, interaction):
    """The scope of the conversation an Edit my memory button sits in, rebuilt from the click as Fri.talk built
    its reply's: a DM is the member's private chat, a server channel or thread its public one."""
    if interaction.guild_id is None:
        return scope_for(cog, interaction)
    parent = getattr(interaction.channel, 'parent_id', None) or interaction.channel_id
    return {'guild_id': str(interaction.guild_id), 'user_id': str(interaction.user.id), 'channel_id': str(parent),
            'conversation_id': 'public:'+str(interaction.channel_id)}


async def allowed(cog, interaction, scope, *, message=True, tell=False):
    """Whether this click or form is the member's own, here, still. `tell`: a failed member lookup says so (see
    fri_buttons.unchecked); every other refusal stays quiet."""
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
        if tell:
            from .fri_buttons import unchecked
            await unchecked(interaction)
        return False
    if interaction.guild_id:
        permissions = interaction.channel.permissions_for(member)
        return bool(permissions.view_channel and permissions.read_message_history)
    return True


async def show(cog, interaction, scope=None, *, state=None, selected=None, notice='', confirm=False):
    scope = profiled(cog, scope or scope_for(cog, interaction))
    if not await allowed(cog, interaction, scope, message=False):
        return
    state = state or await cog.api('POST', '/chat/memory/notes', scope)
    profiles = getattr(cog, 'profiles', False)
    lines = [notice] if notice else []
    lines += ['Memory is '+('on.' if state['enabled'] else 'off.'),
              'Server-wide preferences follow you around the server; the rest stay where you said them, and what '
              'you’re working on always starts there. '
              'At most three per category. Each expires after 90 days.' if profiles else
              'Up to ten preferences here, at most three per category. Each expires after 90 days.']
    summary = state.get('summary')
    if summary:  # Fri's own words, kept apart from the member's: it's the one thing here a model wrote.
        lines.append(f"**My summary of what we’ve talked about lately** (from channels everyone in the server can "
                     f"read; updated <t:{int(summary['refreshed'])}:d>, expires <t:{int(summary['expires_at'])}:d>)\n"
                     + discord.utils.escape_markdown(summary['text']))
    listed = []
    for entry in state['entries'][:SHOWN]:
        where = f" · {place(cog, scope, entry)}" if profiles and 'scope' in entry else ''
        line = (f"• {entry['category']}{where}: {discord.utils.escape_markdown(entry['evidence'])} "
                f"(expires <t:{int(entry['expires_at'])}:d>)")
        if units('\n\n'.join(lines+listed+[line])) > 3500:  # Room for the footer inside an embed's 4096.
            break
        listed.append(line)
    lines += listed
    if len(listed) < len(state['entries']):
        lines.append(f"…and {len(state['entries'])-len(listed)} more. Remove some to see the rest.")
    if not state['entries']:
        lines.append('No saved preferences.' if profiles else 'No saved preferences in this conversation.')
    lines.append('Editing or removing one clears recent saved chats where it showed, everywhere in the server for a '
                 'server-wide one'+(' or the summary' if summary else '')+'. Discord posts and backups are separate.'
                 if profiles else
                 'Editing or removing a preference clears recent saved context here. Discord posts and backups are separate.')
    from .fri_panel import card, reply
    await reply(cog, interaction, embed=card('My memory', '\n\n'.join(lines)),
                view=MemoryView(cog, scope, state, selected, confirm=confirm))


def movable(cog, scope):
    """A note moves only from a private chat, where the member sees all of it, and only if the controller can."""
    return (getattr(cog, 'profiles', False) and MOVES in getattr(cog, 'capabilities', ()) and
            scope.get('profiles') and scope['conversation_id'].startswith('private:'))


def place(cog, scope, entry):
    return note_place(cog.get_channel, entry['scope'], scope['conversation_id'],
                      public=scope['conversation_id'].startswith('public:'))


class MemoryOpen(discord.ui.DynamicItem[discord.ui.Button], template=OPEN):
    """Edit my memory, under a reply that showed it. Found again by its custom ID, so a timeout or a restart doesn't
    kill it. It opens only the clicker's own memory, for the conversation it sits in, after every check the panel
    makes (see allowed)."""

    def __init__(self, member):
        self.member = int(member)
        super().__init__(discord.ui.Button(label='Edit my memory', custom_id=f'fri:memory:open:{self.member}'))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match['member'])

    async def callback(self, interaction):
        from .fri_buttons import not_yours, tell
        cog = interaction.client
        if interaction.user.id != self.member:
            return await not_yours(interaction)
        scope = launched(cog, interaction)
        if not await allowed(cog, interaction, scope):
            return await tell(interaction, 'can’t open that from here anymore.')
        await show(cog, interaction, scope)


class MemoryLauncher(discord.ui.View):
    """A reply's Edit my memory button (see MemoryOpen). No timeout: discord.py drops a dynamic item's
    registration when a view holding one stops."""

    def __init__(self, cog, scope):
        super().__init__(timeout=None)
        self.add_item(MemoryOpen(scope['user_id']))


class MemoryView(discord.ui.View):
    def __init__(self, cog, scope, state, selected=None, *, confirm=False):
        super().__init__(timeout=600)
        self.cog, self.scope = cog, scope
        self.state = state
        self.selected = next((row for row in state['entries'] if row['id'] == selected), None)
        if state['entries']:
            choose = discord.ui.Select(placeholder='Choose a saved preference',
                custom_id='fri:memory:choose:'+uuid.uuid4().hex, options=[discord.SelectOption(
                    label=clip(row['evidence'], 100), value=str(row['id']),
                    description=clip(row['category']+(' · '+place(cog, scope, row) if getattr(cog, 'profiles', False)
                                                      and 'scope' in row else ''), 100),
                    default=row['id'] == selected) for row in state['entries'][:SHOWN]])
            async def choose_entry(interaction):
                if await self.interaction_check(interaction):
                    await show(cog, interaction, scope, selected=int(choose.values[0]))
            choose.callback = choose_entry
            self.add_item(choose)
        if state.get('summary') and scope['conversation_id'].startswith('private:'):
            rewrite = discord.ui.Button(label='Edit summary', custom_id='fri:memory:summary:'+uuid.uuid4().hex)
            rewrite.callback = self.rewrite_summary
            self.add_item(rewrite)
            clear = discord.ui.Button(label='Clear summary', style=discord.ButtonStyle.danger,
                                      custom_id='fri:memory:clear:'+uuid.uuid4().hex)
            clear.callback = self.clear_summary
            self.add_item(clear)
        if self.selected:
            remove = discord.ui.Button(label='Remove selected', style=discord.ButtonStyle.danger,
                custom_id='fri:memory:remove:'+uuid.uuid4().hex)
            remove.callback = self.remove
            self.add_item(remove)
            replace = discord.ui.Button(label='Edit selected', custom_id='fri:memory:replace:'+uuid.uuid4().hex)
            replace.callback = self.replace
            self.add_item(replace)
            if movable(cog, scope):
                # Only the member's press moves a note between their profile and this private chat.
                # Sharing widens who sees a note, so it takes a second press; keeping it here narrows, so it doesn't.
                server = self.selected.get('scope') == 'server'
                action = 'keep' if server else 'confirm' if confirm else 'share'
                move = discord.ui.Button(label={'keep': 'Keep here only', 'share': 'Share server-wide',
                                                'confirm': 'Yes, share server-wide'}[action],
                                         style=discord.ButtonStyle.danger if action == 'confirm' else
                                         discord.ButtonStyle.secondary,
                                         custom_id=f'fri:memory:{action}:'+uuid.uuid4().hex)
                move.callback = {'keep': self.keep, 'share': self.ask_share, 'confirm': self.share}[action]
                self.add_item(move)

    async def interaction_check(self, interaction):
        from .fri_buttons import answers
        if not answers(interaction, 'view'):  # The ended-view fallback took this click (fri_buttons.Ended).
            return False
        return await allowed(self.cog, interaction, self.scope, tell=True)

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

    async def ask_share(self, interaction):
        if await self.interaction_check(interaction):
            await show(self.cog, interaction, self.scope, selected=self.selected['id'], confirm=True,
                       notice='Share it server-wide? Fri could then bring it up in any channel of the server, '
                              'where others can read it. Press Yes, share server-wide to go ahead.')

    async def share(self, interaction):
        await self.move(interaction, 'share', 'Shared that preference across the server.')

    async def keep(self, interaction):
        await self.move(interaction, 'keep', 'That preference stays here now, out of the server.')

    async def move(self, interaction, action, notice):
        if not await self.interaction_check(interaction):
            return
        try:
            state = await self.cog.api('POST', '/chat/memory/notes/'+str(self.selected['id']), {
                **self.scope, 'source': discord_source(interaction).model_dump(), 'revision': self.state['revision'],
                'action': action})
            if action == 'keep':  # It no longer shows in the server, so nothing Fri holds there may keep it.
                self.cog.agent.forget(interaction.user.id)
            await show(self.cog, interaction, self.scope, state=state, notice=notice)
        except commands.UserFeedbackCheckFailure as exc:
            await show(self.cog, interaction, self.scope, notice=str(exc))

    async def replace(self, interaction):
        if await self.interaction_check(interaction):
            await interaction.response.send_modal(EditMemory(self))

    async def rewrite_summary(self, interaction):
        if await self.interaction_check(interaction):
            await interaction.response.send_modal(EditSummary(self))

    async def clear_summary(self, interaction):
        if await self.interaction_check(interaction):
            await summarized(self, interaction, 'remove', '', 'Cleared my summary.')


async def summarized(view, interaction, action, text, notice):
    """The member's edit or clear of Fri's summary; like a server-wide note's, it clears their saved chats."""
    try:
        state = await view.cog.api('POST', '/chat/memory/summary', {
            **view.scope, 'source': discord_source(interaction).model_dump(), 'revision': view.state['revision'],
            'action': action, 'text': text})
        view.cog.agent.forget(interaction.user.id)
        await show(view.cog, interaction, view.scope, state=state, notice=notice)
    except commands.UserFeedbackCheckFailure as exc:
        await show(view.cog, interaction, view.scope, notice=str(exc))


class EditSummary(discord.ui.Modal):
    def __init__(self, view):
        super().__init__(title='Edit my summary', custom_id='fri:memory:rewrite:'+uuid.uuid4().hex, timeout=600)
        self.view = view
        self.summary = discord.ui.TextInput(label='What we’ve talked about lately', style=discord.TextStyle.paragraph,
                                            default=view.state['summary']['text'], min_length=5, max_length=500)
        self.add_item(self.summary)

    async def interaction_check(self, interaction):
        return await allowed(self.view.cog, interaction, self.view.scope, message=False, tell=True)

    async def on_submit(self, interaction):
        if await self.interaction_check(interaction):
            await summarized(self.view, interaction, 'replace', self.summary.value, 'Updated my summary.')


class EditMemory(discord.ui.Modal):
    def __init__(self, view):
        super().__init__(title='Edit saved preference', custom_id='fri:memory:edit:'+uuid.uuid4().hex, timeout=600)
        self.view = view
        self.preference = discord.ui.TextInput(label='What you’re doing, e.g. I’m working on a mod' if
            view.selected['category'] == 'projects' else 'A first-person preference, e.g. I like chess',
            default=view.selected['evidence'], min_length=5, max_length=180)
        self.add_item(self.preference)

    async def interaction_check(self, interaction):
        return await allowed(self.view.cog, interaction, self.view.scope, message=False, tell=True)

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
