"""A preview's first page is held to its turn's whole fence, as its later pages and Apply are (fri #324 item 1).

A DM to Fri can make a settings preview, a post preview or a reminder or plan preview through an ordinary reply,
drawing on another member's saved words. Its later pages and Apply were held to the turn's whole fence (fri #316,
#323): the erasures here of everyone it drew on, then Gene's check of the whole turn. The first page wasn't. Member
30 erasing while Fri looked up the requester's membership, after the model call, still let the first reply carry
their words, and it was recorded as said. Now the fence is checked after the last awaited read, right before the send,
and again before what was delivered is recorded.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from compat_tests.test_fri_edit_reanswer import model as respond_with
from compat_tests.test_fri_member_summaries import ASH
from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_panel_erasure_sources import MARKER
from compat_tests.test_fri_rerun_fence import direct
from fri.fri import PANEL_SKIPPED
from fri.fri_chat import ChatResult

FIRST = 'say ' + MARKER + ' sometimes. ' + ' '.join(['keep it dry and short.']*40)  # The marker on page one.


def owner_dm(monkeypatch, tmp_path, result, moved, during='lookup', source=None, text='sharper voice please'):
    """The owner's DM to Fri, its reply drawing on member 30's saved words, and member 30 erasing (`moved`: through
    Gene, here, or not at all) during the owner's last membership lookup (dm_closed's, after the model call and talk's
    own fence), or the send itself. `source`: the saved exchange's own message, for one deleted meanwhile."""
    fri, agent, store, _, _, _, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
    channel, message = direct(fri, agent, rows)
    store.save_chat('123', 'private:555', '10', 0, 'what did they say', 'member thirty plays the oud', 'model',
                    'high', provenance=[ASH], **({'source': source} if source else {}))
    answered = []

    async def respond(text, context, ready):
        await ready()
        answered.append(True)
        return result
    respond_with(fri, tmp_path, respond)

    def erase():
        if moved == 'gene':
            store.personal_control('30', 'disable')
        elif moved == 'local':
            agent.forget(30, erased=True)

    async def member(*args, **kwargs):
        if answered and during == 'lookup' and kwargs == {'fresh': True}:  # dm_closed's, past talk's own fence.
            erase()
        return SimpleNamespace(id=10)
    agent.member = AsyncMock(side_effect=member)
    dm = message(text)
    dm.author = SimpleNamespace(id=10, display_name='owner', bot=False)
    sent = SimpleNamespace(id=dm.id+100, content='okay', edit=AsyncMock())

    async def reply(*args, **kwargs):
        if during == 'send':
            erase()
        return sent
    dm.reply = AsyncMock(side_effect=reply)
    dm.sent = sent  # What Discord took, for a later edit.
    return fri, agent, store, dm


def said(dm):
    return str(dm.reply.await_args_list)


@pytest.mark.parametrize('moved', ['gene', 'local', 'nothing'])
def test_a_dm_settings_previews_first_page_is_held_to_its_fence(monkeypatch, tmp_path, moved):
    """The issue's repro: page one carries member 30's marker; they erase during the owner's membership lookup after
    the model call. Nothing of the preview goes out, and the owner is told it was skipped, with nothing of why."""
    async def scenario():
        fri, agent, store, dm = owner_dm(monkeypatch, tmp_path, ChatResult(
            status='completed', action={'kind': 'preview_voice', 'voice_notes': FIRST}), moved)
        try:
            await agent.handle(dm)
            if moved == 'nothing':
                assert MARKER in said(dm)
            else:
                assert MARKER not in said(dm) and PANEL_SKIPPED in said(dm)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_preview_whose_source_erased_while_it_went_out_is_not_recorded(monkeypatch, tmp_path):
    """Member 30 erases here while the first page is being sent: it's out, but what was delivered isn't recorded as
    said, so their words aren't saved with it."""
    async def scenario():
        fri, agent, store, dm = owner_dm(monkeypatch, tmp_path, ChatResult(
            status='completed', action={'kind': 'preview_voice', 'voice_notes': FIRST}), 'local', during='send')
        recorded = []
        record = fri.record_turn

        async def recording(result, sent=None):
            recorded.append(result)
            return await record(result, sent)
        fri.record_turn = recording
        try:
            await agent.handle(dm)
            assert MARKER in said(dm) and recorded == []
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['post_preview', 'social_preview'])
@pytest.mark.parametrize('moved', ['gene', 'local', 'nothing'])
def test_a_post_or_reminder_preview_is_held_to_its_fence_too(monkeypatch, tmp_path, kind, moved):
    """The same gap in the other previews a reply makes: a post's or a reminder's card, its text drawn from member
    30's words, isn't shown once they erase after the model call, and the owner is told it was skipped."""
    async def scenario():
        preview = {'id': 'p1', 'body': 'member thirty says ' + MARKER}
        fri, agent, store, dm = owner_dm(monkeypatch, tmp_path, ChatResult(
            status='completed', reply='here it is', **{kind: preview}), moved)
        shown = []

        async def show(fri, message, item, delivered=None):
            shown.append(item)
            await message.reply('card: ' + item['body'])
            if delivered:
                await delivered()
        module = 'fri.fri_workspace' if kind == 'post_preview' else 'fri.fri_social'
        monkeypatch.setattr(module + '.show_preview', show)
        try:
            await agent.handle(dm)
            if moved == 'nothing':
                assert shown and MARKER in said(dm)
            else:
                assert not shown and MARKER not in said(dm) and PANEL_SKIPPED in said(dm)
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('moved', ['gene', 'local', 'nothing'])
def test_a_dm_reply_drawing_on_someone_else_is_held_to_its_fence(monkeypatch, tmp_path, moved):
    """The same gap in an ordinary DM reply drawing on member 30's words: the owner's membership lookup comes after
    talk's own fence, and their erasure then still let it go out. It's held to the whole fence there too."""
    async def scenario():
        fri, agent, store, dm = owner_dm(monkeypatch, tmp_path, ChatResult(
            status='completed', reply='member thirty says ' + MARKER), moved)
        try:
            await agent.handle(dm)
            if moved == 'nothing':
                assert MARKER in said(dm)
            else:
                assert MARKER not in said(dm) and PANEL_SKIPPED in said(dm)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_post_card_whose_source_erased_while_it_went_out_is_not_recorded(monkeypatch, tmp_path):
    """The card is out, but isn't recorded as said once member 30 erased here while it posted."""
    async def scenario():
        fri, agent, store, dm = owner_dm(monkeypatch, tmp_path, ChatResult(
            status='completed', reply='here it is', post_preview={'id': 'p1', 'body': 'member thirty says ' + MARKER}),
            'local', during='send')
        recorded = []

        async def recording(result, sent=None):
            recorded.append(result)
        fri.record_turn = recording

        async def show(fri, message, item, delivered=None):
            await message.reply('card: ' + item['body'])
            await delivered()
        monkeypatch.setattr('fri.fri_workspace.show_preview', show)
        try:
            await agent.handle(dm)
            assert MARKER in said(dm) and recorded == []
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_the_requesters_own_erasure_during_the_fence_says_nothing(monkeypatch, tmp_path):
    """The owner erases here while Gene checks the turn's fence: nothing of it goes out, and nothing is said, as for
    any message of theirs answered after they erased."""
    async def scenario():
        fri, agent, store, dm = owner_dm(monkeypatch, tmp_path, ChatResult(
            status='completed', action={'kind': 'preview_voice', 'voice_notes': FIRST}), 'nothing')
        real, looked = fri.api, []
        lookup = agent.member.side_effect

        async def member(*args, **kwargs):
            looked.append(kwargs)
            return await lookup(*args, **kwargs)
        agent.member = AsyncMock(side_effect=member)

        async def api(method, path, payload=None):
            # The fence's own check, past the last membership lookup (the third fresh one: at the message, before the
            # model call, and dm_closed's after it), after talk's own checks.
            if path == '/chat/turn/check' and looked.count({'fresh': True}) >= 3:
                agent.forget(10, erased=True)
            return await real(method, path, payload)
        fri.api = api
        try:
            await agent.handle(dm)
            assert MARKER not in said(dm) and PANEL_SKIPPED not in said(dm)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_an_image_reply_whose_source_erased_while_its_image_was_checked_never_goes_out(monkeypatch, tmp_path):
    """A reply with an image waits for the image's own check before it sends: member 30 erasing here then holds it
    back, by the fence's part that reads nothing, checked last."""
    async def scenario():
        holder = {}

        async def current():
            holder['agent'].forget(30, erased=True)
            return True
        media = SimpleNamespace(guard=SimpleNamespace(current=current), attribution=lambda: 'via a test',
                                body=b'png', filename='a.png')
        fri, agent, store, dm = owner_dm(monkeypatch, tmp_path, ChatResult(
            status='completed', reply='member thirty says ' + MARKER, media=media), 'nothing')
        holder['agent'] = agent
        try:
            await agent.handle(dm)
            assert MARKER not in said(dm)
        finally:
            await fri.close()
    asyncio.run(scenario())

