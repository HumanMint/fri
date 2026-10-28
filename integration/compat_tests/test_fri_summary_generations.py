"""gene #91: a turn whose model request had Fri's summary of the member carries that summary's generation on its checks
and its save. A withdrawal that drops the summary moves the generation, so the controller refuses the turn and its reply
is withheld, as for other withdrawn evidence. Against the real controller; an older one runs Fri as before."""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from compat_tests.test_api import headers
from compat_tests.test_fri_chat import response_event
from compat_tests.test_fri_member_profiles import DM, GAMING, GENERAL, STAFF, model, run, turns, world
from compat_tests.test_fri_member_summaries import CONTINUATION, SUMMARY, settled, signed_in_chat, summarized, summarizer
from fri.fri_chat import ChatResult
from fri.fri_errors import ControllerRejected
from fri.fri_member_summary import build
from fri.fri_requests import DraftView

CHECK = '/chat/turn/check'
REFUSED = 'remembered about you changed'


async def summarized_from(monkeypatch, w):
    """Two readable exchanges in #general, then one background build: the messages it was built from."""
    events = [w.message(GENERAL, text) for text in ('fri raid tonight?', 'fri which build for it')]
    for event in events:
        await w.say(GENERAL, event.content, event=event)
    settled(w, 13*3600)  # Past what other channels see of it, so only the summary carries these words.
    summarizer(monkeypatch, w)
    assert await build(w.fri.agent) == {'kept': True}
    return events


def withdrawal(event):
    return {'guild_id': '123', 'channel_id': str(GENERAL), 'conversation_id': f'public:{GENERAL}', 'user_id': '20',
            'message_id': str(event.id)}


async def deleted(w, event):
    """Its author deletes it: Fri drops its saved exchange (fri #129), and with it the summary that read it."""
    w.fri.agent.unsend(GENERAL, [event.id])
    await asyncio.gather(*w.fri.agent.saved_turns.tasks)
    assert summarized(w) is None


def sent(w, path):
    return [body for p, body in w.calls if p == path]


def generations(w):
    return [body.get('summary_generation') for path, body in w.calls if path in (CHECK, '/chat/turn')]


@pytest.mark.parametrize('where', [GAMING, DM])
def test_a_summary_withdrawn_while_fri_thinks_keeps_the_reply_unsaid(monkeypatch, tmp_path, where):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            events = await summarized_from(monkeypatch, w)
            saved, checks = len(turns(w)), len(sent(w, CHECK))

            async def respond(text, context):
                assert context['member_summary']['text'] == SUMMARY
                await deleted(w, events[0])  # While the model writes a reply that may quote it.
                return ChatResult(status='completed', reply=CONTINUATION)
            model(w, respond)
            with pytest.raises(ControllerRejected, match=REFUSED):
                await w.say(where, 'fri what were we on about')
            assert len(turns(w)) == saved  # Nothing said, nothing kept.
            assert [body['summary_generation'] for body in sent(w, CHECK)[checks:]] == [0, 0]  # Before and after.
            assert summarized(w) is None
        finally:
            await w.fri.close()
    run(scenario)


def test_a_summary_withdrawn_before_the_request_never_reaches_the_model(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            events = await summarized_from(monkeypatch, w)
            requests = []

            def access_token():  # After Fri gathered context: the check right before the request refuses it.
                assert w.client.post('/chat/turn/withdraw', headers=headers('fri'),
                                     json=withdrawal(events[0])).json()['removed'] == 1
                return 'test'

            def handler(request):
                requests.append(request)
                return httpx.Response(200, text='data: '+json.dumps(response_event(content=json.dumps(
                    {'reply': CONTINUATION, 'request_draft': None})))+'\n\n')
            signed_in_chat(w, handler, access_token)
            with pytest.raises(ControllerRejected, match=REFUSED):
                await w.say(GAMING, 'fri what were we on about')
            assert requests == []
        finally:
            await w.fri.close()
    run(scenario)


def test_a_draft_built_on_a_withdrawn_summary_is_refused_on_its_next_check(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            events = await summarized_from(monkeypatch, w)

            async def respond(text, context):
                return ChatResult(status='completed', reply='on it', draft='a raid planner that remembers builds')
            model(w, respond)
            result = await w.fri.talk(123, GAMING, 20, 'fri make that a feature', 'public', 'member 20',
                                      event=w.message(GAMING, 'fri make that a feature'))
            assert result.draft and result.turn['summary_generation'] == 0
            view = DraftView(w.fri, 20, result.draft, public=True, fence=result.turn)
            await view.fenced()  # Paging or confirming checks it, as Gene still has it.
            await deleted(w, events[0])
            with pytest.raises(ControllerRejected, match=REFUSED):
                await view.fenced()
        finally:
            await w.fri.close()
    run(scenario)


def test_a_reply_after_a_tool_built_on_a_withdrawn_summary_is_not_shown(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            events = await summarized_from(monkeypatch, w)
            saved = len(turns(w))

            async def respond(text, context):
                if 'work_list' in context['actions']:
                    return ChatResult(status='completed', action={'kind': 'work_list', 'work_id': ''})
                await deleted(w, events[0])  # While the model writes its answer from the tool's result.
                return ChatResult(status='completed', reply=CONTINUATION)
            model(w, respond)
            text = 'fri anything saved from what we were on about'
            event = w.message(GAMING, text)
            w.channels[GAMING].fetch_message = AsyncMock(return_value=event)  # The tool rereads it, unchanged.
            result, _, _ = await w.say(GAMING, text, event=event)
            assert CONTINUATION not in result.reply and result.turn is None  # Only what the tool did, unsaved.
            assert len(turns(w)) == saved
            assert sent(w, CHECK)[-1]['summary_generation'] == 0
        finally:
            await w.fri.close()
    run(scenario)


def test_the_generation_rides_only_on_turns_whose_request_had_the_summary(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await summarized_from(monkeypatch, w)

            async def respond(text, context):
                return ChatResult(status='completed', reply=CONTINUATION if context.get('member_summary') else 'sure')
            model(w, respond)
            before = len(generations(w))
            _, saved, _ = await w.say(GAMING, 'fri what were we on about')
            assert saved and generations(w)[before:] == [0, 0, 0]  # Before the request, after it, and the save.
            for where, user in ((STAFF, 20), (GAMING, 30)):  # A restricted channel, another member: no summary.
                before = len(generations(w))
                await w.say(where, 'fri hi', user=user)
                assert generations(w)[before:] == [None, None, None]
        finally:
            await w.fri.close()
    run(scenario)


def test_an_older_controller_runs_as_before(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        w.fri.capabilities -= {'summary-generations'}
        try:
            events = await summarized_from(monkeypatch, w)

            async def respond(text, context):
                await deleted(w, events[0])
                return ChatResult(status='completed', reply=CONTINUATION)
            model(w, respond)
            result, saved, _ = await w.say(GAMING, 'fri what were we on about')
            assert result.reply == CONTINUATION and saved  # As before gene #91: the controller can't tell.
            assert not any(generation is not None for generation in generations(w))
            assert not any('summary_generation' in body for path, body in w.calls if path in (CHECK, '/chat/turn'))
        finally:
            await w.fri.close()
    run(scenario)
