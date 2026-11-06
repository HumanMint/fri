"""DM and ping send fences after #375 (fri #379).

1. The DM switch wasn't read again after the whole turn's fence, its last awaited read: DMs turned off while Gene
   answered it still let the reply go, and an image's, after its own check or before a refused upload's retry.
2. A requester-only DM drew on a saved exchange of theirs, withdrawn through Gene (deleted) during the DM's last
   membership lookup. Their revision didn't move, so nothing held the reply, and its words went out. #386's
   dependency fence reads only what was taken back here, so a DM drawing on saved messages is held to the whole
   turn's fence too.
3. A ping in a chat channel carried the requester's summary, and a source of it was withdrawn through Gene during the
   ping's last membership read. Its own reads fence only the posts it read, so the reply went out. A ping drawing on
   a summary or on saved messages is held to the whole turn too.
4. A DM's settings preview: its later pages and Apply were held to the whole turn's fence alone, not to what was
   taken back here, so a saved exchange deleted here, its withdrawal not taken yet, still showed or was saved.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from compat_tests.test_fri_edit_reanswer import model as respond_with
from compat_tests.test_fri_endings import source
from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_panel_erasure_sources import MARKER, VOICE, click
from compat_tests.test_fri_preview_first_page import owner_dm, said
from compat_tests.test_fri_preview_first_page_2 import image, recorder, rejecting
from compat_tests.test_fri_rerun_fence import direct
from fri.fri import PANEL_SKIPPED
from fri.fri_chat import ChatResult


def dms_off_at(monkeypatch, agent, check):
    """DMs turned off while the whole turn's fence answers its `check`th time: 1, the one after the DM's membership
    lookup; 2, the one before an image goes out; 3, the one before a refused upload goes again."""
    from fri import fri_agent
    real = fri_agent.turn_fence

    def fence(*args, **kwargs):
        inner, calls = real(*args, **kwargs), []

        async def still():
            calls.append(True)
            standing = await inner()
            if len(calls) == check:
                agent.config['support_dms'] = False
            return standing
        still.unmoved = inner.unmoved
        return still
    monkeypatch.setattr(fri_agent, 'turn_fence', fence)


# 1. The DM switch.

@pytest.mark.parametrize('off', [True, False], ids=['dms turned off', 'nothing'])
def test_dms_turned_off_while_the_whole_turn_is_checked_send_nothing(monkeypatch, tmp_path, off):
    """The issue's repro: the owner's DM reply draws on member 30's words, so it's held to the whole turn's fence, and
    DMs go off while Gene answers it. Nothing goes out or is saved."""
    async def scenario():
        fri, agent, store, dm = owner_dm(monkeypatch, tmp_path, ChatResult(
            status='completed', reply='member thirty says '+MARKER), 'nothing')
        recorded = recorder(fri)
        if off:
            dms_off_at(monkeypatch, agent, 1)
        try:
            await agent.handle(dm)
            assert (MARKER in said(dm)) is not off and bool(recorded) is not off
            if off:
                dm.reply.assert_not_awaited()  # Quietly, as for DMs turned off at any other point.
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('retry', [False, True], ids=['first', 'retry'])
@pytest.mark.parametrize('off', [True, False], ids=['dms turned off', 'nothing'])
def test_an_image_reply_is_held_to_the_dm_switch_after_its_own_check(monkeypatch, tmp_path, off, retry):
    """The issue's image case: DMs go off while the whole turn is checked again before the image goes out, after its
    own check; or before a refused upload goes again. Nothing more goes out."""
    async def scenario():
        fri, agent, store, dm = owner_dm(monkeypatch, tmp_path, ChatResult(
            status='completed', reply='member thirty says '+MARKER, media=image(lambda: None)), 'nothing')
        if retry:
            rejecting(dm)
        if off:
            dms_off_at(monkeypatch, agent, 3 if retry else 2)
        try:
            await agent.handle(dm)
            calls = len(dm.reply.await_args_list)
            if not off:
                assert calls == (2 if retry else 1) and 'via a test' in said(dm)
            else:
                assert calls == (1 if retry else 0)  # Nothing at all, or only the refused upload.
        finally:
            await fri.close()
    asyncio.run(scenario())


# 2. A requester-only DM drawing on saved messages.

@pytest.mark.parametrize('withdrawn', [True, False], ids=['withdrawn through gene', 'nothing'])
def test_a_dm_drawing_on_a_saved_exchange_withdrawn_through_gene_says_nothing_of_it(monkeypatch, tmp_path, withdrawn):
    """The issue's repro: member 20's saved exchange holds a marker, their next DM reads it, and Gene withdraws that
    exchange (deleted) during the DM's last membership lookup. Only their own words: their revision stays."""
    async def scenario():
        fri, agent, store, _, _, _, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        fri.capabilities = frozenset((await fri.api('GET', '/fri/contract'))['capabilities'])
        _, message = direct(fri, agent, rows)
        earlier = source(age=60, channel='555')
        store.save_chat('123', 'private:555', '20', 0, 'remember '+MARKER, 'noted', 'model', 'high', source=earlier,
                        provenance=[])
        answered, taken = [], []

        async def respond(text, context, ready):
            await ready()
            assert MARKER in str(context.get('turns'))
            answered.append(True)
            return ChatResult(status='completed', reply='you said '+MARKER)
        respond_with(fri, tmp_path, respond)

        async def member(*args, **kwargs):
            if answered and withdrawn and not taken and kwargs == {'fresh': True}:  # The last lookup, past the model.
                taken.append(store.withdraw_saved('123', [earlier['id']], deleted=True))
            return SimpleNamespace(id=20)
        agent.member = AsyncMock(side_effect=member)
        recorded = recorder(fri)
        dm = message('what did i say')
        dm.reply.return_value = SimpleNamespace(id=dm.id+100, content='okay', edit=AsyncMock())
        try:
            await agent.handle(dm)
            assert (MARKER in said(dm)) is not withdrawn and bool(recorded) is not withdrawn
            assert bool(taken) is withdrawn
        finally:
            await fri.close()
    asyncio.run(scenario())


# 3. A ping carrying the requester's summary.

@pytest.mark.parametrize('withdrawn', [True, False], ids=['a source withdrawn through gene', 'nothing'])
def test_a_ping_carrying_the_requesters_summary_is_held_to_the_whole_turn(monkeypatch, tmp_path, withdrawn):
    """The issue's repro: member 20's summary, built from their exchanges in #1, a chat channel. They ping Fri there,
    and one of the summary's sources is withdrawn through Gene during the ping's last membership read: the reply
    drawn from the summary doesn't go out."""
    async def scenario():
        from compat_tests.test_fri_member_summaries import (
            CONTINUATION,
            settled,
            summarized,
            summarizer,
        )
        from fri.fri_member_summary import build
        fri, agent, store, _, channel, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        fri.capabilities = frozenset((await fri.api('GET', '/fri/contract'))['capabilities'])
        w = SimpleNamespace(fri=fri, store=store)
        # Readable by everyone, as a summary's sources must be (see server_wide).
        channel.is_nsfw, channel.overwrites, channel.guild.default_role = (lambda: False), {}, SimpleNamespace(id=123)
        sources = []
        for text in ('fri raid tonight?', 'fri which build for it'):
            posted = message(text, age=600)  # Still there in Discord, as the build reads it again.
            sources.append(str(posted.id))
            store.save_chat('123', 'public:1', '20', 0, text, 'sure', 'model', 'high', provenance=[], audience='server',
                            source={'kind': 'discord_message', 'id': str(posted.id), 'user_id': '20',
                                    'display_name': 'member 20', 'channel_id': '1', 'guild_id': '123'})
        settled(w, 13*3600)
        summarizer(monkeypatch, w)
        assert await build(agent) == {'kept': True} and summarized(w)
        answered, taken = [], []

        async def respond(text, context, ready):
            await ready()
            assert context.get('member_summary')
            answered.append(True)
            return ChatResult(status='completed', reply=CONTINUATION)
        respond_with(fri, tmp_path, respond)
        fetch = channel.guild.fetch_member.side_effect

        async def fetch_member(user):
            if answered and withdrawn and not taken:  # The ping's last membership read, past the model.
                taken.append((await fri.api('POST', '/chat/turn/withdraw', {
                    'guild_id': '123', 'channel_id': '1', 'conversation_id': 'public:1', 'user_id': '20',
                    'message_id': sources[0]}))['removed'])
            return fetch(user)
        channel.guild.fetch_member = AsyncMock(side_effect=fetch_member)
        recorded = recorder(fri)
        ping = message('<@99> what were we on about')
        ping.reply.return_value = SimpleNamespace(id=ping.id+100, content='okay', edit=AsyncMock())
        try:
            await agent.handle(ping)
            assert answered and taken == ([1] if withdrawn else [])
            assert (CONTINUATION in said(ping)) is not withdrawn and bool(recorded) is not withdrawn
        finally:
            await fri.close()
    asyncio.run(scenario())


# 4. A DM settings preview's later pages and Apply.

@pytest.mark.parametrize('step', ['next', 'apply'])
@pytest.mark.parametrize('deleted', [True, False], ids=['its source deleted here', 'nothing'])
def test_a_dm_previews_later_pages_and_apply_are_held_to_what_was_taken_back_here(monkeypatch, tmp_path, step, deleted):
    """The issue's repro: the owner's DM makes a two-page voice preview from a saved exchange, the marker on page two.
    That exchange's message is deleted here, and its withdrawal times out, so Gene still holds it. Next doesn't show
    the marker, and Apply saves nothing."""
    async def scenario():
        earlier = source(age=60, user='10', channel='555')
        fri, agent, store, dm = owner_dm(monkeypatch, tmp_path, ChatResult(
            status='completed', action={'kind': 'preview_voice', 'voice_notes': VOICE}), 'nothing', source=earlier)
        real, applied = fri.api, []

        async def api(method, path, payload=None):
            if path.startswith('/chat/turn/withdraw'):
                raise TimeoutError()  # Gene doesn't take it: it still holds the exchange.
            if path == '/agent/settings' and method == 'POST':
                applied.append(payload)
            return await real(method, path, payload)
        fri.api = api
        try:
            await agent.handle(dm)
            view = dm.reply.await_args.kwargs['view']
            assert len(view.pages) == 2 and MARKER not in view.content
            view.interaction_check = AsyncMock(return_value=True)
            if step == 'apply':
                await view.next(click())  # Every page read before anything changed: Apply is enabled.
                assert not view.confirm_button.disabled
            if deleted:
                await fri.on_raw_message_delete(SimpleNamespace(channel_id=555, guild_id=None,
                                                                message_id=int(earlier['id'])))
            if step == 'next':
                pressed = click()
                await view.next(pressed)
                content = pressed.response.edit_message.await_args.kwargs['content']
                assert (MARKER in content) is not deleted
                if deleted:
                    assert PANEL_SKIPPED in content
            else:
                pressed = SimpleNamespace(user=SimpleNamespace(id=10), response=SimpleNamespace(defer=AsyncMock()),
                                          edit_original_response=AsyncMock())
                await view.apply(pressed)
                assert bool(applied) is not deleted
        finally:
            await fri.close()
    asyncio.run(scenario())
