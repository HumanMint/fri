"""Every Fri button either works or says why: someone else's, or one whose view ended in a timeout or restart."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import pytest
from discord.state import ConnectionState
from discord.ui.view import ViewStore

from compat_tests.test_fri_actions import harness
from fri.fri_memory_panel import MemoryLauncher, memory_component
from fri.fri_requests import DraftView

FRI = 99


class Response:
    """An interaction's response, recording what was sent; done once anything was."""

    def __init__(self):
        self.calls = []

    def is_done(self):
        return bool(self.calls)

    async def send_message(self, *args, **kwargs):
        self.calls.append(('send', kwargs.get('content', args[0] if args else None), kwargs))

    async def edit_message(self, **kwargs):
        self.calls.append(('edit', kwargs.get('content'), kwargs))

    async def defer(self, **kwargs):
        self.calls.append(('defer', None, kwargs))


def click(fri, channel, *, user=20, custom_id='fri:memory:open:20', guild_id=123, message=None):
    followup = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(delete=AsyncMock())))
    return Mock(spec=discord.Interaction, id=1300000000000000001, guild_id=guild_id, channel_id=channel.id,
                channel=channel, user=SimpleNamespace(id=user, display_name='friend'), client=fri,
                message=message or SimpleNamespace(id=500, author=fri.user, flags=SimpleNamespace(ephemeral=False)),
                data={'custom_id': custom_id, 'component_type': 2}, type=discord.InteractionType.component,
                response=Response(), followup=followup)


def said(interaction):
    """What the member was told: the ephemeral notices, in order."""
    sent = [text for kind, text, kwargs in interaction.response.calls if kind == 'send' and kwargs.get('ephemeral')]
    return sent + [call.kwargs['content'] for call in interaction.followup.send.await_args_list
                   if call.kwargs.get('ephemeral')]


def memory_open(fri, interaction):
    from fri.fri_memory_panel import MemoryOpen
    match = MemoryOpen.__discord_ui_compiled_template__.fullmatch(interaction.data['custom_id'])
    return MemoryOpen.from_custom_id(interaction, None, match)


def test_edit_my_memory_is_keyed_by_its_member_and_never_times_out():
    from fri.fri_memory_panel import MemoryOpen
    scope = {'guild_id': '123', 'user_id': '20', 'channel_id': '1', 'conversation_id': 'public:1'}
    launcher = MemoryLauncher(SimpleNamespace(), scope)
    [button] = launcher.children
    assert isinstance(button, MemoryOpen) and button.custom_id == 'fri:memory:open:20'
    # No timeout: discord.py drops a dynamic item's registration when a view holding one stops.
    assert launcher.timeout is None and button.member == 20


@pytest.mark.parametrize('where', ['channel', 'thread', 'dm'])
def test_edit_my_memory_opens_after_a_restart_with_the_scope_rebuilt_from_the_click(monkeypatch, tmp_path, where):
    async def scenario():
        fri, agent, channels, _ = harness(monkeypatch, tmp_path)
        fri._connection.user = SimpleNamespace(id=FRI)
        channel = channels[1]
        if where == 'thread':
            channel = SimpleNamespace(**{**vars(channel), 'id': 77, 'parent_id': 1})
            agent.allowed = lambda c: c.id in (1, 77)
        elif where == 'dm':
            channel = SimpleNamespace(id=6000)
        interaction = click(fri, channel, guild_id=None if where == 'dm' else 123)
        item = await memory_open(fri, interaction)  # A fresh item from the custom ID, as after a restart.
        expected = {'channel': {'guild_id': '123', 'user_id': '20', 'channel_id': '1', 'conversation_id': 'public:1'},
                    'thread': {'guild_id': '123', 'user_id': '20', 'channel_id': '1', 'conversation_id': 'public:77'},
                    'dm': {'guild_id': '123', 'user_id': '20', 'channel_id': '456', 'conversation_id': 'private:6000'}}
        try:
            with patch('fri.fri_memory_panel.show', new=AsyncMock()) as show, \
                    patch('fri.fri_memory_panel.allowed', new=AsyncMock(return_value=True)) as allowed:
                await item.callback(interaction)
            allowed.assert_awaited_once_with(fri, interaction, expected[where])  # Every panel check, rerun.
            show.assert_awaited_once_with(fri, interaction, expected[where])
            assert said(interaction) == []
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_edit_my_memory_opens_only_the_clickers_own_and_says_why_otherwise(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channels, _ = harness(monkeypatch, tmp_path)
        fri._connection.user = SimpleNamespace(id=FRI)
        try:
            with patch('fri.fri_memory_panel.show', new=AsyncMock()) as show:
                stranger = click(fri, channels[1], user=30)
                await (await memory_open(fri, stranger)).callback(stranger)
                assert said(stranger) == ['not your button.']
                agent.member.return_value = None  # The member left: their own button can't open it either.
                left = click(fri, channels[1])
                await (await memory_open(fri, left)).callback(left)
                assert said(left) == ['can’t open that from here anymore.']
            show.assert_not_awaited()
            stranger.followup.send.assert_not_awaited()
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_dm_dispatch_admits_the_persistent_memory_button():
    interaction = SimpleNamespace(guild_id=None, type=SimpleNamespace(value=3),
        message=SimpleNamespace(author=SimpleNamespace(id=FRI)), data={'custom_id': 'fri:memory:open:20'})
    assert memory_component(interaction, FRI)
    interaction.data['custom_id'] = 'fri:memory:open:20x'
    assert not memory_component(interaction, FRI)


def test_fri_registers_the_persistent_and_ended_buttons():
    from fri.fri import persistent_buttons
    from fri.fri_buttons import Ended
    from fri.fri_memory_panel import MemoryOpen
    assert MemoryOpen in persistent_buttons() and Ended in persistent_buttons()


def drafts():
    cog = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, user=SimpleNamespace(id=FRI), api=AsyncMock(),
                          agent=SimpleNamespace(member=AsyncMock(return_value=object()), allowed=lambda c: c.id == 1))
    return cog, SimpleNamespace(id=1, parent_id=None)


@pytest.mark.parametrize('reviewed', [False, True])
def test_someone_elses_request_draft_says_so_instead_of_failing(reviewed):
    async def scenario():
        cog, channel = drafts()
        view = DraftView(cog, 20, 'Add a queue command', reviewed=reviewed)
        stranger = click(cog, channel, user=30, custom_id='fri:request:preview:'+'a'*32)
        assert not await view.interaction_check(stranger)
        assert said(stranger) == ['not your button.']
        owner = click(cog, channel, custom_id='fri:request:preview:'+'a'*32)
        assert await view.interaction_check(owner) and said(owner) == []
    asyncio.run(scenario())


def test_someone_else_clicking_the_owners_settings_preview_hears_why(monkeypatch, tmp_path):
    from fri.fri_agent import BehaviorPreview
    async def scenario():
        fri, agent, channels, _ = harness(monkeypatch, tmp_path)
        fri._connection.user = SimpleNamespace(id=FRI)
        fri.owner_id = 10
        view = BehaviorPreview(agent, {'voice_notes': 'brief'}, 1)
        try:
            stranger = click(fri, channels[1], user=30, custom_id='fri:behavior:next')
            assert not await view.interaction_check(stranger)
            assert said(stranger) == ['not your button.']
            owner = click(fri, channels[1], user=10, custom_id='fri:behavior:next')
            assert await view.interaction_check(owner) and said(owner) == []
        finally:
            await fri.close()
    asyncio.run(scenario())


def store():
    """discord.py's own view store, with Fri's ended-button fallback registered as Fri registers it."""
    from fri.fri_buttons import Ended
    intents = discord.Intents.none()
    state = ConnectionState(dispatch=lambda *a, **k: None, handlers={}, hooks={}, http=Mock(), intents=intents)
    state.user = discord.ClientUser(state=state, data={'id': str(FRI), 'username': 'fri', 'global_name': 'Fri',
        'discriminator': '0', 'avatar': None, 'bot': True, 'verified': True, 'mfa_enabled': False})
    views = ViewStore(state)
    views.add_dynamic_items(Ended)
    return state, views


def posted(state, custom_ids, *, message_id=500):
    """Fri's message as Discord shows it, with those buttons."""
    data = {'id': str(message_id), 'channel_id': '1', 'type': 0, 'content': 'here', 'tts': False,
            'author': {'id': str(FRI), 'username': 'fri', 'global_name': 'Fri', 'discriminator': '0', 'avatar': None,
                       'bot': True},
            'timestamp': '2026-10-28T11:33:00+00:00', 'edited_timestamp': None, 'mention_everyone': False,
            'mentions': [], 'mention_roles': [], 'attachments': [], 'embeds': [], 'pinned': False,
            'components': [{'type': 1, 'components': [{'type': 2, 'style': 1, 'label': name, 'custom_id': name}
                                                      for name in custom_ids]}]}
    return discord.Message(state=state, channel=SimpleNamespace(id=1, type=discord.ChannelType.text), data=data)


async def dispatched(views, interaction):
    views.dispatch_view(2, interaction.data['custom_id'], interaction)
    while tasks := list(views._ViewStore__tasks):
        await asyncio.gather(*tasks)


class Paging(discord.ui.View):
    def __init__(self, custom_id):
        super().__init__(timeout=600)
        self.clicks = []
        button = discord.ui.Button(label='Next', custom_id=custom_id)
        async def pressed(interaction):
            self.clicks.append(interaction)
            await interaction.response.edit_message(content='page 2')
        button.callback = pressed
        self.add_item(button)


@pytest.mark.parametrize('custom_id', ['fri:behavior:next', 'fri:request:'+'b'*32+':next',
                                       'fri:request:preview:'+'b'*32, 'fri:memory:choose:'+'c'*32])
def test_a_live_view_answers_alone_and_an_ended_one_says_it_expired_and_comes_off(custom_id):
    async def scenario():
        state, views = store()
        fri = SimpleNamespace(user=state.user, _connection=SimpleNamespace(_view_store=views))
        live = Paging(custom_id)
        views.add_view(live, 500)
        channel = SimpleNamespace(id=1)
        first = click(fri, channel, custom_id=custom_id, message=posted(state, [custom_id]))
        await dispatched(views, first)
        assert live.clicks == [first] and [kind for kind, *_ in first.response.calls] == ['edit']
        assert said(first) == []  # Answered once, by the view: the fallback stayed out of it.
        other = click(fri, channel, custom_id=custom_id, message=posted(state, [custom_id], message_id=501))
        await dispatched(views, other)  # The same buttons on a message whose view is gone, e.g. before a restart.
        assert other.response.calls[0][0] == 'edit' and other.response.calls[0][2] == {'view': None}
        assert said(other) == ['that one expired. ask again.'] and live.clicks == [first]
        live.stop()  # Or timed out: the first message's buttons are ended too now.
        again = click(fri, channel, custom_id=custom_id, message=posted(state, [custom_id]))
        await dispatched(views, again)
        assert said(again) == ['that one expired. ask again.'] and live.clicks == [first]
    asyncio.run(scenario())


@pytest.mark.parametrize('custom_id', ['fri:memory:open:20', 'fri:review:i:a:abcdef123456:'+'a'*16,
                                       'fri:post:'+'a'*32+':send', 'fri:request:submit', 'fri:behavior:apply',
                                       'fri:memory:edit:'+'a'*32, 'gene:home:settings'])
def test_the_fallback_leaves_persistent_and_other_buttons_alone(custom_id):
    from fri.fri_buttons import Ended
    assert not Ended.__discord_ui_compiled_template__.fullmatch(custom_id)


def test_the_fallback_never_answers_for_another_apps_message():
    async def scenario():
        state, views = store()
        fri = SimpleNamespace(user=SimpleNamespace(id=12345), _connection=SimpleNamespace(_view_store=views))
        interaction = click(fri, SimpleNamespace(id=1), custom_id='fri:behavior:next',
                            message=posted(state, ['fri:behavior:next']))
        await dispatched(views, interaction)
        assert interaction.response.calls == [] and said(interaction) == []
    asyncio.run(scenario())


def test_a_timed_out_draft_or_preview_takes_its_buttons_off_the_reply(monkeypatch, tmp_path):
    from fri.fri_agent import BehaviorPreview
    async def scenario():
        cog, _ = drafts()
        cog.private_panels = SimpleNamespace(close=AsyncMock())
        draft = DraftView(cog, 20, 'Add a queue command')
        draft.message = SimpleNamespace(edit=AsyncMock())
        await draft.on_timeout()
        draft.message.edit.assert_awaited_once_with(view=None)
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        preview = BehaviorPreview(agent, {'voice_notes': 'brief'}, 1)
        preview.message = SimpleNamespace(edit=AsyncMock(side_effect=discord.NotFound(Mock(status=404), 'gone')))
        try:
            await preview.on_timeout()  # Deleted meanwhile: nothing to take off, and nothing raised.
            preview.message.edit.assert_awaited_once_with(view=None)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_draft_previewed_in_place_leaves_its_message_to_the_preview():
    async def scenario():
        cog, channel = drafts()
        draft = DraftView(cog, 20, 'Add a queue command')
        reply = draft.message = SimpleNamespace(edit=AsyncMock())
        dm = click(cog, channel, guild_id=None, custom_id='fri:request:preview:'+draft.source_id)
        await draft.preview(dm)
        preview = dm.response.calls[0][2]['view']
        cog.private_panels = SimpleNamespace(close=AsyncMock())
        await draft.on_timeout()  # The preview shows on that message now, and may still be running.
        reply.edit.assert_not_awaited()
        assert preview.preview_interaction is dm
    asyncio.run(scenario())


def test_a_reply_hands_its_view_the_message_it_went_out_on(monkeypatch, tmp_path):
    from compat_tests.test_fri_mentions import environment
    from fri.fri_chat import ChatResult
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        event = message('fri draft a request for a queue command')
        sent = SimpleNamespace(id=event.id+1, content='here is the draft')
        event.reply.return_value = sent
        fri.chat_once.return_value = ChatResult(status='completed', reply='here is the draft',
                                                draft='Add a queue command to the music cog')
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            view = event.reply.await_args.kwargs['view']
            assert isinstance(view, DraftView) and view.message is sent
        finally:
            await fri.close()
    asyncio.run(scenario())



class Once(Response):
    """As Discord has it: an interaction is answered once, and discord.py raises on a second response."""

    def once(self):
        if self.calls:
            raise discord.InteractionResponded(None)

    async def send_message(self, *args, **kwargs):
        self.once()
        await super().send_message(*args, **kwargs)

    async def edit_message(self, **kwargs):
        self.once()
        await super().edit_message(**kwargs)

    async def defer(self, **kwargs):
        self.once()
        await super().defer(**kwargs)


def raced(fri, state, custom_id, **kwargs):
    """A click as discord.py hands it to every responder: one Interaction, with its own extras."""
    interaction = click(fri, SimpleNamespace(id=1, parent_id=None), custom_id=custom_id,
                        message=posted(state, [custom_id]), **kwargs)
    interaction.response, interaction.extras = Once(), {}
    return interaction


def test_a_click_racing_its_views_expiry_gets_one_answer():
    """fri #175: the click reaches discord.py while the draft's view is live, so the fallback and the view's own
    callback are both queued; the view ends before either runs. The fallback answers, and the view stays out."""
    async def scenario():
        state, views = store()
        fri = SimpleNamespace(user=state.user, _connection=SimpleNamespace(_view_store=views))
        cog, _ = drafts()
        view = DraftView(cog, 20, 'Add a queue command', reviewed=True)
        custom_id = f'fri:request:{view.source_id}:cancel'
        views.add_view(view, 500)
        interaction = raced(fri, state, custom_id)
        views.dispatch_view(2, custom_id, interaction)  # Both queued, with the view still live.
        view.stop()  # Then it ends, as a timeout ends it, before either has run.
        while tasks := list(views._ViewStore__tasks):
            await asyncio.gather(*tasks)
        assert interaction.followup.send.await_count == 1  # Not also "that request action did not finish".
        assert interaction.response.calls == [('edit', None, {'view': None})]
        assert said(interaction) == ['that one expired. ask again.']
        assert not view.finished  # Its Cancel never ran.
    asyncio.run(scenario())


def test_a_click_its_live_view_took_is_left_to_it_even_once_the_view_ends():
    """fri #175, the other order: the view took the click first. It ended before the fallback looked, so the
    fallback would think it expired; it stays out, since the view answers."""
    from fri.fri_buttons import Ended
    async def scenario():
        state, views = store()
        fri = SimpleNamespace(user=state.user, _connection=SimpleNamespace(_view_store=views))
        cog, _ = drafts()
        view = DraftView(cog, 20, 'Add a queue command', reviewed=True)
        custom_id = f'fri:request:{view.source_id}:cancel'
        views.add_view(view, 500)
        interaction = raced(fri, state, custom_id)
        assert await view.interaction_check(interaction)
        view.stop()
        await Ended(discord.ui.Button(custom_id=custom_id)).callback(interaction)
        assert interaction.response.calls == [] and said(interaction) == []
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['draft', 'preview', 'settings', 'memory'])
def test_every_view_the_fallback_covers_stays_out_of_a_click_it_answered(monkeypatch, tmp_path, kind):
    """fri #175: each view the fallback stands in for declines a click the fallback answered, before any other
    check, and says nothing, not even "not your button"."""
    from fri.fri_agent import BehaviorPreview
    from fri.fri_buttons import answers
    from fri.fri_memory_panel import MemoryView
    async def scenario():
        state, views = store()
        fri = SimpleNamespace(user=state.user, _connection=SimpleNamespace(_view_store=views))
        cog, _ = drafts()
        cog.agent.member = AsyncMock(return_value=object())
        closing = None
        if kind == 'settings':
            closing, agent, _, _ = harness(monkeypatch, tmp_path)
            closing._connection.user = SimpleNamespace(id=FRI)
            closing.owner_id = 20
            agent.member = cog.agent.member
            view = BehaviorPreview(agent, {'voice_notes': 'brief'}, 1)
        elif kind == 'memory':
            view = MemoryView(cog, {'guild_id': '123', 'user_id': '20', 'channel_id': '1',
                                    'conversation_id': 'public:1'}, {'entries': [], 'revision': 0})
        else:
            view = DraftView(cog, 20, 'Add a queue command', reviewed=kind == 'preview')
        try:
            for user in (20, 30):  # Its member's click, and a stranger's.
                interaction = raced(fri, state, 'fri:behavior:next', user=user)
                interaction.channel.permissions_for = lambda _: SimpleNamespace(view_channel=True,
                                                                              read_message_history=True)
                assert answers(interaction, 'ended')
                assert not await view.interaction_check(interaction)
                assert interaction.response.calls == [] and said(interaction) == []
            cog.agent.member.assert_not_awaited()
        finally:
            if closing:
                await closing.close()
    asyncio.run(scenario())


UNCHECKED = 'couldn’t check you’re still here. try again.'


@pytest.mark.parametrize('kind', ['draft', 'preview', 'settings', 'memory'])
def test_a_live_view_says_why_when_its_members_fresh_lookup_comes_back_empty(monkeypatch, tmp_path, kind):
    """fri #183: the view's own member clicks it while it's live, and Discord can't confirm they're still in the
    server (a failed fetch, or they left). The view says so, privately and once, instead of leaving Discord's "This
    interaction failed"; the fallback stays out of it, and nothing the button does runs."""
    from fri.fri_agent import BehaviorPreview
    from fri.fri_memory_panel import MemoryView
    async def scenario():
        state, views = store()
        fri = SimpleNamespace(user=state.user, _connection=SimpleNamespace(_view_store=views))
        cog, _ = drafts()
        cog.agent.member = AsyncMock(return_value=None)
        cog.api = AsyncMock()
        closing = None
        if kind == 'settings':
            closing, agent, _, _ = harness(monkeypatch, tmp_path)
            closing._connection.user = SimpleNamespace(id=FRI)
            closing.owner_id = 20
            closing.api = cog.api
            agent.member = cog.agent.member
            view, prefix = BehaviorPreview(agent, {'voice_notes': 'brief'}, 1), 'fri:behavior:submit'
        elif kind == 'memory':
            entry = {'id': 7, 'category': 'likes', 'evidence': 'I like chess', 'expires_at': 0}
            view = MemoryView(cog, {'guild_id': '123', 'user_id': '20', 'channel_id': '456',
                                    'conversation_id': 'private:1'}, {'entries': [entry], 'revision': 0}, selected=7)
            prefix = 'fri:memory:remove:'
        else:
            view = DraftView(cog, 20, 'Add a queue command', reviewed=kind == 'preview')
            prefix = f'fri:request:{view.source_id}:submit' if kind == 'preview' else 'fri:request:preview:'
        [custom_id] = [item.custom_id for item in view.children if item.custom_id.startswith(prefix)]
        views.add_view(view, 500)
        try:
            interaction = raced(fri, state, custom_id, guild_id=None)  # In their DM, where the lookup is the check.
            await dispatched(views, interaction)
            assert said(interaction) == [UNCHECKED]
            assert [kind for kind, *_ in interaction.response.calls] == ['send']
            interaction.followup.send.assert_not_awaited()
            cog.agent.member.assert_awaited_once()
            cog.api.assert_not_awaited()
        finally:
            view.stop()
            if closing:
                await closing.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('modal', ['EditMemory', 'EditSummary'])
def test_a_memory_edit_says_why_when_its_members_fresh_lookup_comes_back_empty(modal):
    """fri #183, the memory panel's edit forms: submitted while the lookup comes back empty, they say so too."""
    from fri import fri_memory_panel
    async def scenario():
        cog, channel = drafts()
        cog.agent.member = AsyncMock(return_value=None)
        entry = {'id': 7, 'category': 'likes', 'evidence': 'I like chess', 'expires_at': 0}
        view = fri_memory_panel.MemoryView(cog, {'guild_id': '123', 'user_id': '20', 'channel_id': '456',
                                                 'conversation_id': 'private:1'},
                                           {'entries': [entry], 'revision': 0, 'summary': {'text': 'chess talk'}},
                                           selected=7)
        form = getattr(fri_memory_panel, modal)(view)
        interaction = click(cog, channel, guild_id=None, custom_id=form.custom_id)
        assert not await form.interaction_check(interaction)
        assert said(interaction) == [UNCHECKED]
        view.stop()
    asyncio.run(scenario())


def test_the_memory_panel_says_why_when_its_own_recheck_finds_no_member():
    """fri #183: the panel rechecks before it shows (after a click's own check passed, or from the /fri panel), and
    an empty lookup there says so too, rather than leave the click unanswered."""
    from fri.fri_memory_panel import show
    async def scenario():
        cog, channel = drafts()
        cog.agent.member = AsyncMock(return_value=None)
        cog.api = AsyncMock()
        interaction = click(cog, channel, guild_id=None, custom_id='fri:memory:remove:'+'d'*32)
        await show(cog, interaction, {'guild_id': '123', 'user_id': '20', 'channel_id': '456',
                                      'conversation_id': 'private:1'})
        assert said(interaction) == [UNCHECKED]
        cog.api.assert_not_awaited()
    asyncio.run(scenario())


def test_a_live_views_lookup_notice_leaves_other_refusals_quiet():
    """Only the member lookup says this: a click from the wrong conversation still gets nothing extra."""
    async def scenario():
        cog, channel = drafts()
        view = DraftView(cog, 20, 'Add a queue command')
        elsewhere = click(cog, SimpleNamespace(id=2, parent_id=None), custom_id='fri:request:preview:'+view.source_id)
        cog.agent.allowed = lambda c: False
        assert not await view.interaction_check(elsewhere) and said(elsewhere) == []
        dm = click(cog, channel, guild_id=None, custom_id='fri:request:preview:'+view.source_id)
        assert await view.interaction_check(dm) and said(dm) == []  # The lookup found them: nothing said.
        view.stop()
    asyncio.run(scenario())
