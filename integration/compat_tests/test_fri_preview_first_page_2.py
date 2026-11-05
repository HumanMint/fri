"""fri #324 item 1's round 1: what the whole-turn fence awaits, and the reads after it, never reopen a fence.

- B1: the fence's own wait let a message taken back here meanwhile go unnoticed, while its withdrawal was still on its
  way to Gene. What was taken back here is checked again after it, with nothing awaited from there to the send, and
  before what was delivered is recorded.
- B2: the first-memory notice re-sends the reply's words in an edit after the save. It's held to the whole turn first.
- B3: an image's own check reads, after the fence. The whole turn is checked after it, then what was taken back here.
- S1: a DM's reply drawing only on its requester, but on their summary, is held to the whole turn too.
- S2: the requester's own erasure through Gene during the fence is said nothing, as one here is.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from compat_tests.test_api import headers
from compat_tests.test_fri_edit_reanswer import model as respond_with
from compat_tests.test_fri_endings import source
from compat_tests.test_fri_member_profiles import DM, GENERAL, model, run, world
from compat_tests.test_fri_member_summaries import ASH, CONTINUATION
from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_panel_erasure_sources import MARKER
from compat_tests.test_fri_preview_first_page import FIRST, owner_dm, said
from compat_tests.test_fri_summary_generations import summarized_from, withdrawal
from fri.fri import PANEL_SKIPPED
from fri.fri_chat import ChatResult

NOTE = [{'category': 'games', 'evidence': 'i like chess'}]
NOTE_MINECRAFT = [{'category': 'games', 'evidence': 'I like Minecraft'}]


def results(kind):
    return {'reply': ChatResult(status='completed', reply='member thirty says ' + MARKER),
            'card': ChatResult(status='completed', reply='here it is',
                               post_preview={'id': 'p1', 'body': 'member thirty says ' + MARKER}),
            'settings preview': ChatResult(status='completed',
                                           action={'kind': 'preview_voice', 'voice_notes': FIRST})}[kind]


def recorder(fri):
    recorded, record = [], fri.record_turn

    async def recording(result, sent=None, **kwargs):
        recorded.append(result)
        return await record(result, sent, **kwargs)
    fri.record_turn = recording
    return recorded


@pytest.mark.parametrize('when', ['before the send', 'before the save'])
@pytest.mark.parametrize('kind', ['reply', 'card', 'settings preview'])
def test_a_saved_exchange_taken_back_while_the_fence_waits_is_held_back(monkeypatch, tmp_path, kind, when):
    """B1's repro: the owner's DM draws on a saved exchange holding member 30's words. Its message is deleted while
    Gene checks the whole turn, and its withdrawal times out, so Gene still holds it. Before the send, nothing of the
    reply goes out; after it, it isn't recorded."""
    async def scenario():
        earlier = source(age=60, user='10', channel='555')
        fri, agent, store, dm = owner_dm(monkeypatch, tmp_path, results(kind), 'nothing', source=earlier)
        real, looked, done = fri.api, [], []
        lookup = agent.member.side_effect

        async def member(*args, **kwargs):
            looked.append(kwargs)
            return await lookup(*args, **kwargs)
        agent.member = AsyncMock(side_effect=member)

        async def api(method, path, payload=None):
            if (path == '/chat/turn/check' and not done and looked.count({'fresh': True}) >= 3 and
                    (when == 'before the send') is (not dm.reply.await_count)):
                done.append(path)
                await fri.on_raw_message_delete(SimpleNamespace(channel_id=555, guild_id=None,
                                                                message_id=int(earlier['id'])))
            if path.startswith('/chat/turn/withdraw'):
                raise TimeoutError()  # Gene doesn't take the withdrawal yet: it still holds the exchange.
            return await real(method, path, payload)
        fri.api = api
        recorded = recorder(fri)

        async def show(fri, message, item, delivered=None):
            await message.reply('card: ' + item['body'])
            await delivered()
        monkeypatch.setattr('fri.fri_workspace.show_preview', show)
        try:
            await agent.handle(dm)
            assert done and recorded == []
            assert (MARKER in said(dm)) is (when == 'before the save')
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('who', ['30 through gene', '30 here', 'requester through gene', 'requester here', 'nothing'])
def test_a_dms_first_memory_notice_is_held_to_the_whole_turn(monkeypatch, tmp_path, who):
    """B2's repro: the owner's DM repeats member 30's marker and saves their first note. Member 30, or the owner,
    erases as the save returns: the notice's edit, which would send the reply's words again, is left out."""
    async def scenario():
        fri, agent, store, dm = owner_dm(monkeypatch, tmp_path, ChatResult(
            status='completed', reply='member thirty says ' + MARKER, notes=NOTE), 'nothing',
            text='i like chess. what did they say')
        erase(fri, agent, store, who, 10)
        try:
            await agent.handle(dm)
            assert MARKER in said(dm)
            assert dm.sent.edit.await_count == (who == 'nothing')
        finally:
            await fri.close()
    asyncio.run(scenario())


def erase(fri, agent, store, who, requester):
    """Runs `who`'s erasure as the save returns (after Gene kept it, before Fri reads the answer)."""
    real = fri.api

    async def api(method, path, payload=None):
        answer = await real(method, path, payload)
        if path == '/chat/turn' and answer.get('first_memory'):
            member = 30 if who.startswith('30') else requester
            if who.endswith('through gene'):
                store.personal_control(str(member), 'disable')
            elif who.endswith('here'):
                agent.forget(member, erased=True)
        return answer
    fri.api = api


def public(monkeypatch, tmp_path, result, text='fri what did they say'):
    """Member 20 asks in channel 1, the reply drawing on member 30's words saved there."""
    fri, agent, store, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
    store.save_chat('123', 'public:1', '20', 0, 'what did they say', 'member thirty plays the oud', 'model', 'high',
                    provenance=[ASH])

    async def respond(text, context, ready):
        await ready()
        return result
    respond_with(fri, tmp_path, respond)
    event = message(text)
    event.sent = SimpleNamespace(id=event.id+100, content='okay', edit=AsyncMock(), reply=AsyncMock())
    event.reply = AsyncMock(return_value=event.sent)
    return fri, agent, store, event


@pytest.mark.parametrize('who', ['30 through gene', '30 here', 'requester through gene', 'requester here', 'nothing'])
def test_a_public_first_memory_notice_is_held_to_the_whole_turn_too(monkeypatch, tmp_path, who):
    async def scenario():
        fri, agent, store, event = public(monkeypatch, tmp_path, ChatResult(
            status='completed', reply='member thirty says ' + MARKER, notes=NOTE), 'fri i like chess. what did they say')
        erase(fri, agent, store, who, 20)
        try:
            await agent.handle(event)
            assert MARKER in said(event)
            assert event.sent.edit.await_count == (who == 'nothing')
        finally:
            await fri.close()
    asyncio.run(scenario())


def image(erasing, retry=False):
    """An image whose own check reads (Gene's context, say), while `erasing` runs: on its first check, or on its
    second, after Discord refused the upload."""
    calls = []

    async def current():
        calls.append(True)
        if len(calls) == (2 if retry else 1):
            erasing()
        return True
    return SimpleNamespace(guard=SimpleNamespace(current=current), attribution=lambda: 'via a test', body=b'png',
                           filename='a.png')


def rejecting(target):
    """Discord refuses the first upload (400); what's sent after is kept."""
    first = []

    async def reply(*args, **kwargs):
        if not first:
            first.append(True)
            raise discord.HTTPException(SimpleNamespace(status=400, reason='Bad Request'), 'too big')
        return SimpleNamespace(id=1, content=args[0] if args else '', edit=AsyncMock())
    target.reply = AsyncMock(side_effect=reply)


@pytest.mark.parametrize('retry', [False, True], ids=['first', 'retry'])
@pytest.mark.parametrize('who', ['30 through gene', 'nothing'])
def test_a_dm_image_reply_is_held_to_the_whole_turn_after_its_own_check(monkeypatch, tmp_path, who, retry):
    """B3's repro in a DM: member 30 erases through Gene while the image's own check reads. Nothing more goes out."""
    async def scenario():
        holder = {}
        media = image(lambda: who != 'nothing' and holder['store'].personal_control('30', 'disable'), retry)
        fri, agent, store, dm = owner_dm(monkeypatch, tmp_path, ChatResult(
            status='completed', reply='member thirty says ' + MARKER, media=media), 'nothing')
        holder['store'] = store
        if retry:
            rejecting(dm)
        try:
            await agent.handle(dm)
            calls = len(dm.reply.await_args_list)
            if who == 'nothing':
                assert calls == (2 if retry else 1) and 'via a test' in said(dm)
            else:
                assert calls == (1 if retry else 0)  # Nothing at all, or only the refused upload.
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('retry', [False, True], ids=['first', 'retry'])
@pytest.mark.parametrize('who', ['30 through gene', '30 here', 'nothing'])
def test_a_public_image_reply_is_held_to_the_whole_turn_after_its_own_check(monkeypatch, tmp_path, who, retry):
    """B3 in public, where nothing held the reply to the whole turn: an erasure here reproduces it too."""
    async def scenario():
        holder = {}

        def erasing():
            if who == '30 through gene':
                holder['store'].personal_control('30', 'disable')
            elif who == '30 here':
                holder['agent'].forget(30, erased=True)
        fri, agent, store, event = public(monkeypatch, tmp_path, ChatResult(
            status='completed', reply='member thirty says ' + MARKER, media=image(erasing, retry)))
        holder.update(store=store, agent=agent)
        if retry:
            rejecting(event)
        try:
            await agent.handle(event)
            calls = len(event.reply.await_args_list)
            if who == 'nothing':
                assert calls == (2 if retry else 1) and 'via a test' in said(event)
            else:
                assert calls == (1 if retry else 0)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_the_requesters_own_erasure_through_gene_during_the_fence_says_nothing(monkeypatch, tmp_path):
    """S2: the owner erases through Gene while Gene checks the whole turn. Their erasure count here didn't move, but
    their revision did: nothing goes out, and nothing is said, as for one here."""
    async def scenario():
        fri, agent, store, dm = owner_dm(monkeypatch, tmp_path, results('reply'), 'nothing')
        real, looked = fri.api, []
        lookup = agent.member.side_effect

        async def member(*args, **kwargs):
            looked.append(kwargs)
            return await lookup(*args, **kwargs)
        agent.member = AsyncMock(side_effect=member)

        async def api(method, path, payload=None):
            if path == '/chat/turn/check' and looked.count({'fresh': True}) >= 3:
                store.personal_control('10', 'disable')
            return await real(method, path, payload)
        fri.api = api
        try:
            await agent.handle(dm)
            assert MARKER not in said(dm) and PANEL_SKIPPED not in said(dm)
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('withdrawn', [True, False])
def test_a_dm_reply_on_the_requesters_own_summary_is_held_to_the_whole_turn(monkeypatch, tmp_path, withdrawn):
    """S1's witness: member 20's summary is built only from their own messages. Their DM's reply draws on it, and one of
    its sources is withdrawn through Gene during the DM's last membership lookup: their revision stays, but the
    summary's generation moves. Nothing of the reply goes out."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            events = await summarized_from(monkeypatch, w)
            agent, answered = w.fri.agent, []
            agent.config['support_dms'] = True
            w.channels[DM].fetch_message = AsyncMock(side_effect=lambda mid: w.posted[mid])

            async def respond(text, context):
                assert context['member_summary']
                answered.append(True)
                return ChatResult(status='completed', reply=CONTINUATION)
            model(w, respond)

            async def member(*args, **kwargs):
                if answered and withdrawn and kwargs == {'fresh': True} and len(answered) == 1:
                    answered.append('withdrawn')
                    assert w.client.post('/chat/turn/withdraw', headers=headers('fri'),
                                         json=withdrawal(events[0])).json()['removed'] == 1
                return SimpleNamespace(id=20)
            agent.member = AsyncMock(side_effect=member)
            dm = w.message(DM, 'what were we on about')
            dm.reply = AsyncMock(return_value=SimpleNamespace(id=dm.id+1, content='okay', edit=AsyncMock()))
            await agent.handle(dm)
            assert (CONTINUATION in str(dm.reply.await_args_list)) is not withdrawn
            assert answered[-1] == ('withdrawn' if withdrawn else True)
        finally:
            await w.fri.close()
    run(scenario)


@pytest.mark.parametrize('who', ['through gene', 'nothing'])
def test_a_first_memory_notice_with_nothing_else_to_hold_it_to_is_held_to_genes_check(monkeypatch, tmp_path, who):
    """B2 for a save with no fence of its own (without the agent, say): the member erases through Gene as the save
    returns, and the notice's edit is left out on Gene's check of the turn."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        real = w.fri.api

        async def api(method, path, payload=None):
            answer = await real(method, path, payload)
            if path == '/chat/turn' and who != 'nothing':
                w.store.personal_control('20', 'disable')
            return answer
        w.fri.api = api
        try:
            _, saved, sent = await w.say(GENERAL, 'fri I like Minecraft', 'cubes, groundbreaking', NOTE_MINECRAFT)
            assert saved['first_memory'] and sent.edit.await_count == (who == 'nothing')
        finally:
            await w.fri.close()
    run(scenario)
