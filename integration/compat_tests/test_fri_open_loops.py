"""Fri's model context carries the speaker's own open loops, each only in the conversation it belongs to."""

import asyncio
import datetime as dt
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from gene.fri_context import ChatSource
from gene.fri_social_store import Social
from gene.fri_workspace_store import Workspace

from compat_tests.test_fri_chat import CONTEXT, response_event
from compat_tests.test_fri_mentions import environment
from fri.fri_chat import Chat, ChatResult, loops_view
from fri.fri_loops_eval import evaluate as evaluate_loops

DM = {'guild_id': '123', 'user_id': '20', 'channel_id': '456', 'conversation_id': 'private:999'}
CHANNEL = {**DM, 'channel_id': '1', 'conversation_id': 'public:1'}


def event(channel='999', guild=None, offset=0):
    return ChatSource(id=str(((int(time.time()*1000)-1420070400000) << 22) | offset), kind='discord_message',
                      user_id='20', display_name='bo', channel_id=channel, guild_id=guild)


def later(days=1):
    return dt.datetime.fromtimestamp(time.time()+days*86400, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M')


def seed(store):
    Social(store).reminder_prepare(DM, event(), 0, 'update the mods', later(), 'UTC')
    Workspace(store).save(DM, event(offset=1), 0, '', 0, 'Raid night intro', 'the intro text', 'open')
    Social(store).plan_prepare(CHANNEL, event('1', '123', 2), 0, 'Helldivers', later()+'\n'+later(2), 'UTC',
                               'help us pick a time for Helldivers')


@pytest.mark.parametrize(('channel', 'visibility'), [(999, 'private'), (1, 'public')])
def test_each_loop_reaches_the_model_only_where_it_belongs(monkeypatch, tmp_path, channel, visibility):
    async def scenario():
        fri, _, store, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=channel)
        seed(store)
        item = message('fri what do i have going on')
        if visibility == 'private':
            item.guild = None
        fri.chat_once.return_value = ChatResult(status='completed', reply='a reminder and a draft, that it')
        try:
            await fri.talk(123, channel, 20, item.content, visibility, event=item)
            context = fri.chat_once.await_args.args[1]
            shown = json.loads(json.dumps(loops_view(context['loops'], context['source'])))
            if visibility == 'private':
                assert [row['text'] for row in shown['reminders']] == ['update the mods']
                assert [row['title'] for row in shown['saved_work']] == ['Raid night intro']
                assert shown['game_nights'] is None
            else:  # Nothing said in the DM shows in public, and the card shows only in its own channel.
                assert shown['reminders'] is None and shown['saved_work'] == []
                assert [row['title'] for row in shown['game_nights']] == ['Helldivers']
                text = json.dumps(context, default=str)
                assert 'update the mods' not in text and 'Raid night intro' not in text
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_another_members_loops_never_reach_the_speaker(monkeypatch, tmp_path):
    async def scenario():
        fri, _, store, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=999)
        seed(store)
        item = message('fri what do i have going on', user=40)
        item.guild = None
        try:
            await fri.talk(123, 999, 40, item.content, 'private', event=item)
            context = fri.chat_once.await_args.args[1]
            assert context['loops'] == {'reminders': [], 'saved_work': [], 'game_nights': None}
        finally:
            await fri.close()
    asyncio.run(scenario())


def respond(context, text='fri what do i have coming up'):
    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, text='data: ' + json.dumps(response_event()) + '\n\n')
    chat = Chat(SimpleNamespace(access_token=lambda: 'plan-token'), httpx.MockTransport(handler))
    asyncio.run(chat.respond(text, {**CONTEXT, **context}))
    return json.loads(sent[0]['input'][-1]['content'])['your_open_loops'], ' '.join(sent[0]['instructions'].split())


def test_the_model_sees_dates_short_reminder_text_and_null_when_unavailable(store):
    Social(store).reminder_prepare(DM, event(), 0, 'x'*900, later(), 'UTC')
    loops = store.chat_context('123', 'private:999', '20')['loops']
    shown, instructions = respond({'loops': loops, 'source': event().evidence()})
    [row] = shown['reminders']
    assert len(row['text']) == 300 and row['state'] == 'pending' and shown['game_nights'] is None
    assert dt.datetime.fromisoformat(row['due']).tzinfo == dt.timezone.utc
    assert 'your_open_loops' in instructions and 'null means you can\'t see them here' in instructions
    assert respond({})[0] is None  # An older controller sends none: unavailable, not empty.


def test_a_private_fri_panel_never_shows_reminders(store):
    panel = ChatSource(id=event().id, kind='discord_interaction', user_id='20', display_name='bo',
                       channel_id='456', guild_id='123')
    loops = store.chat_context('123', 'private:456', '20')['loops']
    assert loops['reminders'] == []  # The controller can't tell the panel from a DM by its id.
    shown, _ = respond({'loops': loops, 'source': panel.evidence()})
    assert shown['reminders'] is None


def careful_answer(text, loops):
    """An accurate reply for each open-loop case, as a stand-in for a good model."""
    if 'raid was chaos' in text:
        return 'chaos is a strong word for watching you faceplant'
    if loops['reminders'] is None and not loops['game_nights']:
        return "can't see reminders from here, they live in our DMs"
    if loops['game_nights']:
        return 'yeah, thursday 7pm pacific per the card'
    states = [row['state'] for row in loops['reminders']]
    if loops['saved_work']:
        return 'mods reminder tomorrow at 9, and the raid night intro draft is still open'
    return {'scheduled': 'already got one for 9am tomorrow to update the mods',
            'pending': "not yet, it's waiting on your Confirm", 'will_not_send': "that one won't go out anymore, set a new one",
            'uncertain': "not sure it landed, delivery went sideways"}[states[0]]


def test_the_open_loop_evaluation_fails_wrong_answers():
    judged = []
    async def judge(data):  # Scripted stand-in for the model judge: only the accurate answer passes.
        judged.append(data)
        verdict = 'pass' if data['reply'] == careful_answer(data['message'], data['open_loops']) else 'fail'
        return {'verdict': verdict, 'reason': ''}, {'input_tokens': 1}, 'completed'
    def responder(reply, draft=None):
        seen = []
        async def answer(text, context):
            seen.append(context)
            return ChatResult(status='completed', reply=reply(text, context['loops']), draft=draft)
        return SimpleNamespace(respond=answer, seen=seen)
    async def run(chat):
        return {case['name']: case async for case, _ in evaluate_loops(chat, 'gpt-6.1-sol', 'high', '', judge=judge)}
    sloppy = asyncio.run(run(responder(lambda text, loops: 'set it, and kev and ash are going friday')))
    graded = [name for name in sloppy if not name.endswith('_judge')]
    assert len(graded) == 8 and not any(sloppy[name]['passed'] for name in graded)
    drafted = asyncio.run(run(responder(careful_answer, draft='Add a reminder command')))
    assert not any(row['passed'] for row in drafted.values()) and not any(name.endswith('_judge') for name in drafted)
    judged.clear()
    careful = responder(careful_answer)
    verdicts = asyncio.run(run(careful))
    assert all(row['passed'] for row in verdicts.values()) and len(judged) == len(careful.seen) == 8
    assert careful.seen[0]['source']['guild_id'] is None and careful.seen[5]['source']['guild_id'] == '444'
    assert judged[0]['open_loops']['reminders'][0]['state'] == 'scheduled'


def test_the_open_loop_evaluation_offers_the_dm_reminder_tools_so_a_duplicate_fails():
    """A model that prepares a reminder whenever the tool is there must fail the duplicate case."""
    from unittest.mock import Mock

    import discord

    from fri.fri_loops_eval import cases
    from fri.fri_social import available
    offered = {}

    async def judge(data):
        return {'verdict': 'pass', 'reason': ''}, {'input_tokens': 1}, 'completed'

    async def eager(text, context):
        offered[text] = context['actions']
        if 'reminder_prepare' in context['actions']:
            return ChatResult(status='completed', action={'kind': 'reminder_prepare', 'body': 'update the mods',
                                                          'local_time': '2026-10-20T09:00',
                                                          'timezone': 'America/Los_Angeles'})
        return ChatResult(status='completed', reply=careful_answer(text, context['loops']))

    async def run():
        return {case['name']: case async for case, _ in evaluate_loops(SimpleNamespace(respond=eager), 'gpt-6.1-sol',
                                                                        'high', '', judge=judge)}
    verdicts = asyncio.run(run())
    assert not verdicts['existing_reminder_is_not_duplicated']['passed']
    assert verdicts['game_night_time_from_the_card']['passed']  # Nothing offered outside the DM.
    for case in cases():
        # What the runtime offers there: in the DM, the reminder tools without cancel (nobody asks to cancel).
        dm = SimpleNamespace(event=Mock(spec=discord.Message, content=case['text'], channel=Mock(spec=discord.DMChannel)),
                             guild=123, public=False, private_evidence=False,
                             agent=SimpleNamespace(config={'support_dms': True}), fri=SimpleNamespace(guild_id=123, user=None))
        runtime = available(dm) if case.get('dm') else {}
        assert offered[case['text']].keys() == runtime.keys()
        for name, tool in runtime.items():  # The eval's own clock: the fictional message's time.
            assert offered[case['text']][name] == {**tool, **({'utc_now': '2026-10-19T14:02:00+00:00'}
                                                              if 'utc_now' in tool else {})}


@pytest.mark.parametrize('loops', [
    {'reminders': None, 'saved_work': None, 'game_nights': None},
    {'reminders': 'x', 'saved_work': {'title': 'x'}, 'game_nights': 3},
    {'reminders': [None, 'x', {'due': float('nan')}], 'saved_work': [{'updated': 1e300}],
     'game_nights': [{'options': 'abc', 'chosen': float('inf')}]},
    [], 'x', {},
])
def test_a_malformed_loops_payload_never_breaks_a_reply(loops):
    dm = event().evidence()
    shown = loops_view(loops, dm)
    assert set(shown) == {'reminders', 'saved_work', 'game_nights'}
    json.dumps(shown, allow_nan=False)  # Whatever arrives, the model input stays valid JSON.
    sent, _ = respond({'loops': loops, 'source': dm})
    assert sent == shown


def test_the_loops_grader_reads_a_baseline_run_the_same_way(monkeypatch):
    """--baseline escapes Fri's own model input, never the grader's, so both runs are judged alike."""
    import json as _json

    import httpx

    from fri import fri_agent_model, fri_chat, fri_eval, fri_web
    from fri.fri_loops_eval import cases
    judged = []

    def handler(request):
        judged.append(_json.loads(request.content)['input'][0]['content'])
        body = response_event(content=_json.dumps({'verdict': 'pass', 'reason': 'fine'}))
        return httpx.Response(200, text='data: '+_json.dumps(body)+'\n\n')
    grader = Chat(SimpleNamespace(access_token=lambda: 'subscription'), httpx.MockTransport(handler))

    async def answer(text, context):
        return ChatResult(status='completed', reply='ㅋㅋ 👍')

    async def first_grade():
        async for case, _ in evaluate_loops(SimpleNamespace(transport=grader.transport, headers=grader.headers,
                                                             respond=answer), 'gpt-6.1-sol', 'high', ''):
            if case['name'].endswith('_judge'):
                return case
    for module in (fri_agent_model, fri_chat, fri_web):
        monkeypatch.setattr(module, 'model_json', module.model_json)  # Restored after the test.
    assert len(cases()) and asyncio.run(first_grade())
    fri_eval.ascii_baseline()
    asyncio.run(first_grade())
    assert judged[0] == judged[1] and 'ㅋㅋ 👍' in judged[0]
