"""Trusted Fri conversation, request and owner controls, outside feature-editable cogs."""

from __future__ import annotations

import logging

import discord

from . import feedback as commands
from .fri_notices import notice_text
from .fri_requests import DraftView as DraftView
from .panels import PrivatePanels, defer_panel
from .subscription import SubscriptionError

LOG = logging.getLogger('fri.panel')
NO_MENTIONS = discord.AllowedMentions.none()


def actor(interaction):
    return {'guild_id': str(interaction.guild_id), 'user_id': str(interaction.user.id)}


def source_url(interaction, message):
    if message is None or getattr(getattr(message, 'flags', None), 'ephemeral', False):
        return ''
    return f'https://discord.com/channels/{interaction.guild_id}/{interaction.channel_id}/{message.id}'


def card(title, description):
    return discord.Embed(title=title, description=description[:4000], color=0x5865F2)


def sessions(cog):
    if not hasattr(cog, 'private_panels'):
        cog.private_panels = PrivatePanels()
    return cog.private_panels


async def reply(cog, interaction, text=None, *, verbatim=False, **kwargs):
    panels = sessions(cog)
    view = kwargs.pop('view', None) or panels.current_view(interaction) or ScopedView(cog, interaction.user.id)
    return await panels.show(interaction, content=text if verbatim or text is None else notice_text(text), view=view, **kwargs)


async def notice(interaction, text):
    text = notice_text(text)
    if interaction.response.is_done():
        message = await interaction.followup.send(content=text, ephemeral=True, allowed_mentions=NO_MENTIONS, wait=True)
        await message.delete(delay=10)
    else:
        await interaction.response.send_message(content=text, ephemeral=True, allowed_mentions=NO_MENTIONS, delete_after=10)


async def authorized(cog, interaction, user_id=None, owner=False):
    # Component and modal callbacks have their own checks; they do not pass through
    # Red's command checks or the slash-command tree's interaction_check.
    channel = getattr(interaction.channel, 'parent_id', None) or interaction.channel_id
    in_scope = channel == cog.workshop_id or (getattr(cog, 'agent', None) and cog.agent.allowed(interaction.channel))
    if interaction.guild_id != cog.guild_id or not in_scope:
        return False
    if user_id is not None and interaction.user.id != user_id:
        await notice(interaction, 'This menu belongs to another person. Open your own Fri panel.')
        return False
    if owner and not await cog.bot.is_owner(interaction.user):
        await notice(interaction, 'Only Fri’s owner can use these settings.')
        return False
    return True


class ScopedView(discord.ui.View):
    def __init__(self, cog, user_id=None, *, owner=False, timeout=600, navigation=True):
        super().__init__(timeout=timeout)
        self.cog, self.user_id, self.owner = cog, user_id, owner
        if user_id is not None:
            if navigation:
                home = discord.ui.Button(label='Home', row=4)

                async def go_home(interaction):
                    await reply(cog, interaction, embed=card('Fri', 'Chat, request a feature or fix, or check progress.'),
                                view=HomeView(cog, user_id))
                home.callback = go_home
                self.add_item(home)
            close = discord.ui.Button(label='Close', row=4)

            async def dismiss(interaction):
                await defer_panel(interaction)
                panels = sessions(cog)
                entry = panels.entries.get(panels.key(interaction))
                if entry and entry.view is self:
                    entry.interaction = interaction
                await panels.close(panels.key(interaction), self)
            close.callback = dismiss
            self.add_item(close)

    async def interaction_check(self, interaction):
        return await authorized(self.cog, interaction, self.user_id, self.owner)

    async def on_timeout(self):
        if self.user_id is not None:
            await sessions(self.cog).close((self.cog.guild_id, self.user_id), self)

    async def on_error(self, interaction, error, item):
        LOG.warning('Panel action failed: %s', type(error).__name__)
        detail = str(error) if isinstance(error, (commands.UserFeedbackCheckFailure, SubscriptionError)) else 'That action did not complete. Try again shortly.'
        await reply(self.cog, interaction, detail[:1500])


class InputModal(discord.ui.Modal):
    def __init__(self, cog, interaction, kind, default=None):
        titles = {'chat': 'Chat with Fri', 'request': 'Request a feature or fix', 'memory': 'Save a server fact'}
        super().__init__(title=titles[kind], timeout=600)
        self.cog, self.kind, self.user_id = cog, kind, interaction.user.id
        self.original = interaction.message
        labels = {'chat': 'What’s on your mind?', 'request': 'What feature or fix would you like?',
                  'memory': 'A small fact Fri should remember'}
        self.value = discord.ui.TextInput(label=labels[kind], max_length=1000 if kind == 'memory' else 4000, default=default,
                                         style=discord.TextStyle.paragraph)
        self.add_item(self.value)

    async def interaction_check(self, interaction):
        return await authorized(self.cog, interaction, self.user_id, self.kind == 'memory')

    async def on_submit(self, interaction):
        if self.kind == 'chat':
            await self.cog.chat_interaction(interaction, self.value.value)
            return
        await defer_panel(interaction)
        payload = {**actor(interaction), 'body': self.value.value,
                   'source_url': source_url(interaction, self.original)}
        if self.kind == 'memory':
            await self.cog.api('POST', '/memory', payload)
            await reply(self.cog, interaction, 'Saved. Future development jobs can use this fact.')
        else:
            channel = getattr(interaction.channel, 'parent_id', None) or interaction.channel_id
            # Typed by the member alone: no one else's words, so the controller may show its title to them.
            payload.update(source_id=str(interaction.id), channel_id=str(channel), provenance=[])
            job = await self.cog.api('POST', '/requests', payload)
            await reply(self.cog, interaction, f"Request `{job['id']}` is awaiting owner review. "
                        'Coding starts only after the owner allows it. Use **Requests** to follow its progress.')

    async def on_error(self, interaction, error):
        await ScopedView.on_error(self, interaction, error, None)


async def show_requests(cog, interaction):
    await defer_panel(interaction)
    jobs = await cog.api('GET', '/requests')
    if not await cog.is_owner(interaction.user):
        jobs = [job for job in jobs if job['user_id'] == str(interaction.user.id)]
    if not jobs:
        await reply(cog, interaction, 'No requests yet. Choose **Request a feature or fix** to start one.')
        return
    view = ScopedView(cog, interaction.user.id)
    select = discord.ui.Select(placeholder='Choose a request', options=[
        discord.SelectOption(label=f"{j['state']} · {j['body'][:65]}", value=j['id'], description=j['id']) for j in jobs])

    async def selected(event, page=0):
        await defer_panel(event)
        job = await cog.api('GET', '/requests/' + select.values[0])
        if job['user_id'] != str(event.user.id) and not await cog.is_owner(event.user):
            return
        body = discord.utils.escape_markdown(discord.utils.escape_mentions(job['body']))
        pages = [body[start:start + 1500] for start in range(0, len(body), 1500)] or ['']
        page = min(max(page, 0), len(pages) - 1)
        description = f'Submitted request · page {page + 1}/{len(pages)}\n\n' + pages[page]
        if job.get('model'):
            description += f"\n\nWorker: `{job['model']}` · {job.get('effort', '')}"
        description += '\n\n' + (job.get('progress') or '')
        controls = ScopedView(cog, event.user.id)
        refresh = discord.ui.Button(label='Refresh progress', row=2)
        async def refresh_job(refresh_event):
            await selected(refresh_event, page)
        refresh.callback = refresh_job
        controls.add_item(refresh)
        for label, target in [('Previous page', page - 1), ('Next page', page + 1)]:
            if 0 <= target < len(pages):
                navigate = discord.ui.Button(label=label, row=3)

                async def change_page(page_event, target=target):
                    await selected(page_event, target)
                navigate.callback = change_page
                controls.add_item(navigate)
        if job['state'] == 'awaiting_owner' and page == len(pages) - 1 and await cog.bot.is_owner(event.user):
            from .reviews import request_reference
            allow = discord.ui.Button(label='Allow coding', style=discord.ButtonStyle.primary)

            async def allow_coding(approval_event):
                if not await authorized(cog, approval_event, event.user.id, owner=True):
                    return
                await defer_panel(approval_event)
                await cog.api('POST', f"/requests/{job['id']}/review", {
                    **actor(approval_event), 'phase': 'intake', 'reference': request_reference(job),
                    'decision': 'approve'})
                await reply(cog, approval_event, 'Coding allowed. Deployment still needs approval of the tested commit.')
            allow.callback = allow_coding
            controls.add_item(allow)
            description += '\n\nNo coding has started. Allow coding to prepare a tested proposal.'
        proposal = job.get('proposal')
        if proposal:
            description += f"\n\n{proposal.get('summary', '')[:800]}\n\nCommit: `{proposal['sha']}`"
            url = proposal.get('url', '')
            if url.startswith('https://github.com/HumanMint/gene/'):
                controls.add_item(discord.ui.Button(label='View code changes', url=url))
            if job['state'] == 'review' and not job.get('proposal_current', True):
                description += '\n\nA newer release is live. This saved proposal needs rebuilding before approval.'
            if job['state'] == 'review' and job.get('proposal_current', True) and await cog.bot.is_owner(event.user):
                approve = discord.ui.Button(label='Review release approval', style=discord.ButtonStyle.primary)

                async def review(approval_event):
                    if not await authorized(cog, approval_event, event.user.id, owner=True):
                        return
                    await reply(cog, approval_event, embed=card('Approve this exact commit?',
                        f"Request `{job['id']}`\n\n`{proposal['sha']}`\n\nApprove only after reviewing the code changes. "
                        'Fri will back up state, check the release, and restore the previous code if health checks fail.'),
                        view=ApprovalView(cog, event.user.id, job['id'], proposal['sha']))
                approve.callback = review
                controls.add_item(approve)
        if job.get('result'):
            description += '\n\n' + job['result'][:700]
        if job['state'] in ('awaiting_owner', 'queued', 'review') and await cog.bot.is_owner(event.user):
            reject = discord.ui.Button(label='Reject request', style=discord.ButtonStyle.danger)

            async def reject_request(rejection):
                if not await authorized(cog, rejection, event.user.id, owner=True):
                    return
                await defer_panel(rejection)
                await cog.api('POST', f"/requests/{job['id']}/reject", actor(rejection))
                await reply(cog, rejection, 'Request rejected.')
            reject.callback = reject_request
            controls.add_item(reject)
        await reply(cog, event, embed=card(f"Request · {job['state']}", description), view=controls)
    select.callback = selected
    view.add_item(select)
    await reply(cog, interaction, embed=card('Requests', 'Choose a request to see progress, errors, or its proposed release.'), view=view)


class ApprovalView(ScopedView):
    def __init__(self, cog, user_id, job_id, sha):
        super().__init__(cog, user_id, owner=True)
        self.job_id, self.sha = job_id, sha

    @discord.ui.button(label='Approve this commit', style=discord.ButtonStyle.success)
    async def approve(self, interaction, button):
        await defer_panel(interaction)
        await self.cog.api('POST', f'/requests/{self.job_id}/approve', {**actor(interaction), 'sha': self.sha})
        button.disabled = True
        await reply(self.cog, interaction, 'Approved this commit. Follow the release from **Requests**.', view=self)


async def show_models(cog, interaction, *, refresh=False, page=0):
    await defer_panel(interaction)
    state = await cog.api('POST', '/models/refresh' if refresh else '/models', actor(interaction))
    text = f"Selected: **{state['selected']}**\nReasoning: **{state['effort']}**"
    if state['refresh_pending']:
        text += '\n\nCatalog refresh queued. Use **Reload list** after the worker finishes its current job.'
    if state['error']:
        text += '\n\n' + state['error']
    if not state['selected_available']:
        text += '\n\nThe selected model is unavailable. Choose a model to resume coding.'
    await reply(cog, interaction, embed=card('Worker model', text), view=ModelsView(cog, interaction.user.id, state, page))


class ModelsView(ScopedView):
    def __init__(self, cog, user_id, state, page=0):
        super().__init__(cog, user_id, owner=True)
        self.page = page
        models = state['models'][page * 25:(page + 1) * 25]
        if models:
            choose = discord.ui.Select(placeholder='Choose a worker model', row=0, options=[
                discord.SelectOption(label=m['name'][:100], value=m['id'], default=m['id'] == state['selected']) for m in models])

            async def selected(event):
                await defer_panel(event)
                await cog.api('POST', '/models/select', {**actor(event), 'model_id': choose.values[0], 'effort': ''})
                await reply(cog, event, 'Model updated for new jobs. Reopen **Models** to choose its reasoning effort.')
            choose.callback = selected
            self.add_item(choose)
        current = next((m for m in state['models'] if m['id'] == state['selected']), None)
        if current:
            effort = discord.ui.Select(placeholder='Choose reasoning effort', row=1, options=[
                discord.SelectOption(label='Model default', value='default'),
                *[discord.SelectOption(label=e, value=e) for e in current['efforts']]])

            async def set_effort(event):
                await defer_panel(event)
                latest = await cog.api('POST', '/models', actor(event))
                if latest['selected'] != state['selected']:
                    await reply(cog, event, 'The selected model changed. Reopen **Models** before changing its effort.')
                    return
                await cog.api('POST', '/models/select', {**actor(event), 'model_id': state['selected'],
                    'effort': '' if effort.values[0] == 'default' else effort.values[0]})
                await reply(cog, event, 'Reasoning effort updated for new jobs.')
            effort.callback = set_effort
            self.add_item(effort)
        for label, target in [('Previous', page - 1), ('Next', page + 1)]:
            if 0 <= target and target * 25 < len(state['models']):
                button = discord.ui.Button(label=label, row=3)

                async def navigate(event, target=target):
                    await show_models(cog, event, page=target)
                button.callback = navigate
                self.add_item(button)

    @discord.ui.button(label='Refresh available models', row=2)
    async def refresh(self, interaction, button):
        await show_models(self.cog, interaction, refresh=True, page=self.page)

    @discord.ui.button(label='Reload list', row=2)
    async def reload(self, interaction, button):
        await show_models(self.cog, interaction, page=self.page)


class SettingsView(ScopedView):
    def __init__(self, cog, user_id):
        super().__init__(cog, user_id, owner=True)

    @discord.ui.button(label='Coding model', row=0)
    async def models(self, interaction, button):
        await show_models(self.cog, interaction)

    @discord.ui.button(label='Saved context', row=0)
    async def memories(self, interaction, button):
        await defer_panel(interaction)
        rows = await self.cog.api('POST', '/memory/list', actor(interaction))
        text = '\n\n'.join(row['body'] for row in rows) or 'No saved facts. Only explicitly approved server facts are shared across conversations.'
        await reply(self.cog, interaction, embed=card('Approved server context', text), view=ContextView(self.cog, interaction.user.id, rows))

    @discord.ui.button(label='Usage', row=0)
    async def usage(self, interaction, button):
        await defer_panel(interaction)
        coding = await self.cog.api('POST', '/usage', actor(interaction))
        chatting = await self.cog.api('POST', '/chat/usage', actor(interaction))
        text = 'Uses your ChatGPT plan. No app token budget. Counts are recorded usage, not remaining quota.\n'
        def count(value):
            return 'unknown' if value is None else f'{value:,}'
        for label, rows in [('Chat', chatting), ('Development', coding)]:
            text += '\n**' + label + '**\n'
            text += '\n'.join(f"{r['model']} · {r['attempts']} attempts · input {count(r['input_tokens'])} · "
                f"output {count(r['output_tokens'])} · cached {count(r['cached_input_tokens'])} · unknown {r['unknown_attempts']}"
                for r in rows) or 'No usage yet.'
        text += '\n\nCached tokens are included in input. Reasoning is included in output. '
        text += '[Account limits and resets](https://chatgpt.com/settings/usage)'
        await reply(self.cog, interaction, embed=card('Subscription usage', text))

    @discord.ui.button(label='Chat model', row=0)
    async def chat_models(self, interaction, button):
        await show_chat_models(self.cog, interaction)

    @discord.ui.button(label='Pause development', row=1)
    async def pause(self, interaction, button):
        await self.set_paused(interaction, True)

    @discord.ui.button(label='Resume development', row=1)
    async def resume(self, interaction, button):
        await self.set_paused(interaction, False)

    @discord.ui.button(label='Roll back Gene features', row=1)
    async def rollback(self, interaction, button):
        await defer_panel(interaction)
        state = await self.cog.api('GET', '/status')
        if not state['previous_sha']:
            await reply(self.cog, interaction, 'No previous healthy release is available.')
            return
        await reply(self.cog, interaction, embed=card('Approve rollback to this commit?',
            f"`{state['previous_sha']}`\n\nThis restores Gene’s prior feature code. Saved state and the trusted controller are unchanged."),
            view=RollbackView(self.cog, interaction.user.id, state['previous_sha']))

    async def set_paused(self, interaction, paused):
        await defer_panel(interaction)
        await self.cog.api('POST', '/pause', {**actor(interaction), 'paused': paused})
        await reply(self.cog, interaction, 'New development paused. Running work can finish.' if paused else 'Development resumed.')


class ContextView(ScopedView):
    def __init__(self, cog, user_id, rows):
        super().__init__(cog, user_id, owner=True)
        if rows:
            select = discord.ui.Select(placeholder='Forget a saved fact', row=1, options=[
                discord.SelectOption(label=row['body'][:100], value=row['id']) for row in rows[:25]])

            async def forget(event):
                await defer_panel(event)
                await cog.api('POST', '/memory/' + select.values[0] + '/forget', actor(event))
                await reply(cog, event, 'Forgot that fact. Already running jobs keep their original input.')
            select.callback = forget
            self.add_item(select)

    @discord.ui.button(label='Add a fact', style=discord.ButtonStyle.primary)
    async def remember(self, interaction, button):
        await interaction.response.send_modal(InputModal(self.cog, interaction, 'memory'))


class RollbackView(ScopedView):
    def __init__(self, cog, user_id, sha):
        super().__init__(cog, user_id, owner=True)
        self.sha = sha

    @discord.ui.button(label='Approve this rollback', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await defer_panel(interaction)
        job = await self.cog.api('POST', '/rollback', {**actor(interaction), 'sha': self.sha})
        await reply(self.cog, interaction, f"Rollback `{job['id']}` queued. Resume development if paused.",
                    view=HomeView(self.cog, interaction.user.id))


class HomeView(ScopedView):
    def __init__(self, cog, user_id=None):
        super().__init__(cog, user_id, timeout=None if user_id is None else 600, navigation=False)

    @discord.ui.button(label='Chat with Fri', style=discord.ButtonStyle.primary, custom_id='fri:home:chat', row=0)
    async def chat(self, interaction, button):
        await interaction.response.send_modal(InputModal(self.cog, interaction, 'chat'))

    @discord.ui.button(label='Request a feature or fix', style=discord.ButtonStyle.primary, custom_id='fri:home:request', row=0)
    async def request(self, interaction, button):
        await interaction.response.send_modal(InputModal(self.cog, interaction, 'request'))

    @discord.ui.button(label='Requests', custom_id='fri:home:requests', row=1)
    async def requests(self, interaction, button):
        await show_requests(self.cog, interaction)

    @discord.ui.button(label='Status', custom_id='fri:home:status', row=1)
    async def status(self, interaction, button):
        await defer_panel(interaction)
        state = await self.cog.api('GET', '/status')
        text = ('Development paused.' if state['paused'] else 'Development active.')
        text += f"\nChat: **{state['chat_model']}** · {state['chat_effort']}\nWorker: **{state['model']}** · {state['effort']}\nRelease: `{state['active_sha']}`"
        text += '\n\n' + (', '.join(f'{n} {s}' for s, n in state['jobs'].items()) or 'No requests yet.')
        await reply(self.cog, interaction, embed=card('Fri status', text))

    @discord.ui.button(label='Forget my chats', custom_id='fri:home:forget', row=2)
    async def forget_chats(self, interaction, button):
        await defer_panel(interaction)
        await self.cog.api('POST', '/chat/clear', actor(interaction))
        await reply(self.cog, interaction, 'Recent chat context cleared. Personal notes stay in My memory; '
                    'say “fri forget me” to clear those too and stop remembering. Discord posts and backups are separate.')

    @discord.ui.button(label='My memory', custom_id='fri:home:memory', row=2)
    async def my_memory(self, interaction, button):
        await self.cog.chat_interaction(interaction, 'what do you remember about me?')

    @discord.ui.button(label='Owner settings', custom_id='fri:home:settings', row=1)
    async def settings(self, interaction, button):
        if await authorized(self.cog, interaction, owner=True):
            await reply(self.cog, interaction, embed=card('Owner settings', 'Choose a setting below.'),
                        view=SettingsView(self.cog, interaction.user.id))


async def show_chat_models(cog, interaction, refresh=False, page=0):
    await defer_panel(interaction)
    if refresh:
        models = await cog.chat.models()
        await cog.api('POST', '/chat/models/update', {**actor(interaction), 'models': models})
    state = await cog.api('POST', '/chat/models', actor(interaction))
    text = f"Selected: **{state['selected']}** · **{state['effort']}**\nUses your ChatGPT subscription."
    text += '\nRefresh to discover available models. There is no automatic model fallback.'
    await reply(cog, interaction, embed=card('Fri’s chat model', text),
                view=ChatModelsView(cog, interaction.user.id, state, page))


class ChatModelsView(ScopedView):
    def __init__(self, cog, user_id, state, page=0):
        super().__init__(cog, user_id, owner=True)
        models = state['models'][page * 25:(page + 1) * 25]
        if models:
            select = discord.ui.Select(placeholder='Choose chat model', row=0, options=[
                discord.SelectOption(label=model, value=model, default=model == state['selected']) for model in models])
            async def choose(event):
                await defer_panel(event)
                await cog.api('POST', '/chat/models/select', {**actor(event), 'model_id': select.values[0], 'effort': state['effort']})
                await show_chat_models(cog, event, page=page)
            select.callback = choose
            self.add_item(select)
        effort = discord.ui.Select(placeholder='Reasoning effort', row=1, options=[
            discord.SelectOption(label=value, value=value, default=value == state['effort'])
            for value in ('low', 'medium', 'high', 'xhigh', 'max')])
        async def choose_effort(event):
            await defer_panel(event)
            latest = await cog.api('POST', '/chat/models', actor(event))
            if latest['selected'] != state['selected']:
                await reply(cog, event, 'The model changed. Reopen Chat model before changing effort.')
                return
            await cog.api('POST', '/chat/models/select', {**actor(event), 'model_id': state['selected'], 'effort': effort.values[0]})
            await show_chat_models(cog, event, page=page)
        effort.callback = choose_effort
        self.add_item(effort)
        for label, target in [('Previous', page - 1), ('Next', page + 1)]:
            if 0 <= target * 25 < len(state['models']):
                button = discord.ui.Button(label=label, row=3)
                async def navigate(event, target=target):
                    await show_chat_models(cog, event, page=target)
                button.callback = navigate
                self.add_item(button)

    @discord.ui.button(label='Refresh available models', row=2)
    async def refresh(self, interaction, button):
        await show_chat_models(self.cog, interaction, refresh=True)
