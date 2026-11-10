"""Synthetic notes only: exercise the actual Discord callbacks and display fences."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import pytest

from fri import fri_memory_panel as panel


def fixture(count=51):
    state = {'enabled': True, 'revision': 0, 'entries': [
        {'id': i, 'category': 'games', 'evidence': f'I like synthetic game {i}',
         'expires_at': 2000000000, 'scope': 'server'} for i in range(1, count+1)]}
    cog = NS(user=NS(id=99), profiles=False, capabilities=(),
             agent=NS(member=AsyncMock(return_value=True), revision=AsyncMock(return_value=0),
                      forget=Mock(), erased={}), api=AsyncMock(return_value=state))
    scope = {'guild_id': '123', 'user_id': '20', 'channel_id': '456', 'conversation_id': 'private:900'}
    click = NS(user=NS(id=20), channel_id=900, guild_id=None, message=NS(author=cog.user),
               response=NS(is_done=lambda: False, send_modal=AsyncMock()))
    return cog, scope, state, click


def button(view, label):
    return next(item for item in view.children if getattr(item, 'label', None) == label)


def test_next_reaches_note_26_and_selects_it():
    async def scenario():
        cog, scope, state, click = fixture()
        with patch('fri.fri_panel.reply', new=AsyncMock()) as reply:
            await panel.show(cog, click, scope)
            view = reply.await_args.kwargs['view']
            assert button(view, 'previous').disabled
            await button(view, 'next').callback(click)
            view = reply.await_args.kwargs['view']
            assert [o.value for o in view.children[0].options] == [str(i) for i in range(26, 51)]
            assert 'page 2 of 3' in reply.await_args.kwargs['embed'].description
            assert await reply.await_args.kwargs['still']()
            view.children[0]._values = ['26']
            await view.children[0].callback(click)
            view = reply.await_args.kwargs['view']
            assert view.selected['id'] == 26 and view.page == 1
            assert next(o for o in view.children[0].options if o.default).value == '26'
            assert cog.api.await_count == 3  # Each navigation/selection re-reads Gene.
            await button(view, 'next').callback(click)
            view = reply.await_args.kwargs['view']
            assert [o.value for o in view.children[0].options] == ['51']
            assert button(view, 'next').disabled
            await button(view, 'previous').callback(click)
            assert reply.await_args.kwargs['view'].page == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('remaining,expected', [(26, 1), (25, 0)])
def test_remove_on_page_two_clamps_only_when_empty(remaining, expected):
    async def scenario():
        cog, scope, state, click = fixture(remaining+1)
        view = panel.MemoryView(cog, scope, state, selected=26, page=1)
        cog.api.return_value = {**state, 'entries': [r for r in state['entries'] if r['id'] != 26]}
        with patch('fri.fri_panel.reply', new=AsyncMock()) as reply, \
                patch.object(panel, 'discord_source', return_value=NS(model_dump=dict)):
            await button(view, 'Remove selected').callback(click)
            assert cog.api.await_args.args[1] == '/chat/memory/notes/26'
            assert cog.api.await_args.args[2]['action'] == 'remove'
            updated = reply.await_args.kwargs['view']
            assert updated.page == expected
            assert all(o.value != '26' for o in updated.children[0].options)
    asyncio.run(scenario())


def test_stale_page_is_reread_and_clamped():
    async def scenario():
        cog, scope, state, click = fixture()
        view = panel.MemoryView(cog, scope, state, page=1)
        cog.api.return_value = {**state, 'entries': state['entries'][:3]}
        with patch('fri.fri_panel.reply', new=AsyncMock()) as reply:
            await button(view, 'next').callback(click)
            updated = reply.await_args.kwargs['view']
            assert updated.page == 0
            assert [o.value for o in updated.children[0].options] == ['1', '2', '3']
            assert not any(getattr(b, 'label', None) in ('previous', 'next') for b in updated.children)
    asyncio.run(scenario())


def test_other_member_cannot_turn_pages():
    async def scenario():
        cog, scope, state, click = fixture()
        view = panel.MemoryView(cog, scope, state)
        click.user.id = 30
        await button(view, 'next').callback(click)
        cog.api.assert_not_awaited()
        cog.agent.member.assert_not_awaited()
    asyncio.run(scenario())


def test_pager_components_pass_dm_routing_and_fit_discord_rows():
    async def scenario():
        cog, scope, state, click = fixture()
        cog.profiles = True
        cog.capabilities = (panel.MOVES,)
        scope['profiles'] = True
        state['summary'] = {'text': 'synthetic summary'}
        state['entries'][25]['scope'] = 'public:1'
        cog.get_channel = lambda cid: NS(name='synthetic')
        view = panel.MemoryView(cog, scope, state, selected=26, page=1, confirm=True)
        view.to_components()  # discord.py validates the 5-row component budget.
        assert len(view.children[0].options) == 25
        assert button(view, 'Yes, share server-wide')
        for label in ('previous', 'next'):
            click.type = NS(value=3)
            click.data = {'custom_id': button(view, label).custom_id}
            assert panel.memory_component(click, 99)
    asyncio.run(scenario())


@pytest.mark.parametrize('action', ['keep', 'share', 'replace', 'summary', 'clear'])
def test_all_actions_keep_page_two(action):
    async def scenario():
        cog, scope, state, click = fixture()
        state['summary'] = {'text': 'synthetic summary'}
        view = panel.MemoryView(cog, scope, state, selected=26, page=1)
        with patch.object(panel, 'show', new=AsyncMock()) as show, \
                patch.object(panel, 'discord_source', return_value=NS(model_dump=dict)):
            if action == 'share':
                await view.ask_share(click)
                assert show.await_args.kwargs['confirm'] is True
                assert show.await_args.kwargs['selected'] == 26
                cog.api.assert_not_awaited()
                await view.share(click)
            elif action == 'keep':
                await view.keep(click)
            elif action == 'replace':
                await view.replace(click)
                modal = click.response.send_modal.await_args.args[0]
                modal.preference._value = 'I like synthetic chess'
                await modal.on_submit(click)
            elif action == 'summary':
                await view.rewrite_summary(click)
                modal = click.response.send_modal.await_args.args[0]
                modal.summary._value = 'synthetic edited summary'
                await modal.on_submit(click)
            else:
                await view.clear_summary(click)
            assert show.await_args.kwargs['page'] == 1
            body = cog.api.await_args.args[2]
            assert body['revision'] == 0
            if action in ('keep', 'share', 'replace'):
                assert cog.api.await_args.args[1] == '/chat/memory/notes/26'
    asyncio.run(scenario())
