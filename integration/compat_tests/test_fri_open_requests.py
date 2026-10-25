"""Fri's model context carries the speaker's own requests: titles in private, states only in public."""

import asyncio
import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import httpx
import pytest

from compat_tests.conftest import proposed
from compat_tests.test_fri_chat import CONTEXT, response_event
from compat_tests.test_fri_mentions import environment
from fri.fri_chat import Chat, ChatResult, request_view
from fri.fri_panel import InputModal
from fri.fri_request_eval import evaluate as evaluate_requests
from fri.fri_requests import DraftView
from fri.reviews import request_reference


@pytest.mark.parametrize(('channel', 'visibility', 'titled'), [(999, 'private', True), (1, 'public', False)])
def test_the_speakers_requests_reach_the_model(monkeypatch, tmp_path, channel, visibility, titled):
    async def scenario():
        fri, _, store, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=channel)
        own = store.request('123', '20', 'draft-1', '', 'Fix the modpack command\nwith details', [])
        store.request('123', '40', 'draft-2', '', 'Someone else entirely', [])
        event = message('fri is my modpack fix done yet')
        if visibility == 'private':
            event.guild = None
        fri.chat_once.return_value = ChatResult(status='completed', reply='still waiting on review')
        try:
            await fri.talk(123, channel, 20, event.content, visibility, event=event)
            requests = fri.chat_once.await_args.args[1]['requests']
            assert [row['id'] for row in requests] == [own['id']] and requests[0]['state'] == 'awaiting_owner'
            assert ('title' in requests[0]) is titled
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_requests_submitted_from_the_panel_or_a_chat_draft_are_titled_in_private(monkeypatch, tmp_path):
    async def scenario():
        fri, _, store, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=999)
        member = SimpleNamespace(id=20, display_name='bo')
        panel = Mock(spec=discord.Interaction, id=100000000000000002, guild_id=123, channel_id=456,
                     channel=SimpleNamespace(id=456, parent_id=None), user=member, message=None)
        try:
            modal = InputModal(fri, panel, 'request')
            modal.value._value = 'Add a raid timer command\nthat pings the role'
            with patch('fri.fri_panel.defer_panel', new=AsyncMock()), patch('fri.fri_panel.reply', new=AsyncMock()):
                await modal.on_submit(panel)
            fri.chat_once.return_value = ChatResult(status='completed', reply='drafted it, have a look',
                                                    draft='Fix the modpack command\nwith details')
            event = message('fri can you draft a modpack fix')
            event.guild = None
            drafted = await fri.talk(123, 999, 20, event.content, 'private', event=event)
            view = DraftView(fri, 20, drafted.draft, reviewed=True, fence=drafted.turn)
            view.interaction_check = AsyncMock(return_value=True)
            button = SimpleNamespace(guild_id=None, user=member, response=SimpleNamespace(defer=AsyncMock()),
                                     edit_original_response=AsyncMock())
            await view.submit(button)
            await fri.record_turn(drafted)
            fri.chat_once.return_value = ChatResult(status='completed', reply='both are waiting on the owner')
            event = message('fri are my requests done')
            event.guild = None
            await fri.talk(123, 999, 20, event.content, 'private', event=event)
            titles = {row.get('title') for row in fri.chat_once.await_args.args[1]['requests']}
            assert titles == {'Add a raid timer command', 'Fix the modpack command'}
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_the_model_sees_dates_and_withdrawals_not_raw_rows(store):
    store.request('123', '20', 'draft-1', '', 'Build what ash described', [{'user_id': '30', 'revision': 0}])
    store.personal_control('30', 'disable')  # Ash's privacy change withdraws it; the owner never saw it.
    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, text='data: ' + json.dumps(response_event()) + '\n\n')
    context = {**CONTEXT, 'requests': store.chat_context('123', 'private:9', '20')['requests']}
    chat = Chat(SimpleNamespace(access_token=lambda: 'plan-token'), httpx.MockTransport(handler))
    asyncio.run(chat.respond('fri what happened to my request', context))
    [row] = json.loads(sent[0]['input'][-1]['content'])['your_gene_requests']
    # Gene ends it as rejected; the model must not read that as the owner turning it down.
    assert row['state'] == 'withdrawn' and 'withdrawn' not in row and 'title' not in row
    assert dt.datetime.fromisoformat(row['created']).tzinfo == dt.timezone.utc
    assert "don't call it rejected or say the owner turned it down" in ' '.join(sent[0]['instructions'].split())


def test_an_owner_rejection_a_privacy_change_later_withdrew_is_not_denied(store):
    job = store.request('123', '20', 'draft-1', '', 'Build what ash described', [{'user_id': '30', 'revision': 0}])
    store.review_decision(job['id'], 'intake', request_reference(job), 'reject', 'owner')
    store.personal_control('30', 'disable')  # Now it reads like a request the privacy change ended.
    [row] = store.chat_context('123', 'private:9', '20')['requests']
    assert row['state'] == 'rejected' and row['withdrawn'] is True
    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, text='data: ' + json.dumps(response_event()) + '\n\n')
    chat = Chat(SimpleNamespace(access_token=lambda: 'plan-token'), httpx.MockTransport(handler))
    asyncio.run(chat.respond('fri why did my request get rejected', {**CONTEXT, 'requests': [row]}))
    [shown] = json.loads(sent[0]['input'][-1]['content'])['your_gene_requests']
    instructions = ' '.join(sent[0]['instructions'].split())
    # Gene can't tell this from a request the privacy change ended, so Fri is told to claim neither.
    assert shown['state'] == 'withdrawn' and "didn't turn it down" not in instructions


def test_a_request_withdrawn_after_it_shipped_keeps_its_state():
    row = {'id': 'a1b2c3d4e5f6', 'kind': 'feature', 'state': 'deployed', 'created': 1.0, 'updated': 2.0,
           'withdrawn': True}  # Gene keeps a finished request's state when a privacy change removes its text.
    assert request_view(row)['state'] == 'deployed' and request_view(row)['withdrawn'] is True


def test_a_request_the_owner_rejected_stays_rejected():
    row = {'id': 'a1b2c3d4e5f6', 'kind': 'feature', 'state': 'rejected', 'created': 1.0, 'updated': 2.0}
    assert request_view(row)['state'] == 'rejected' and 'withdrawn' not in request_view(row)


def careful_answer(text, rows):
    """An accurate reply for each request-status case, as a stand-in for a good model."""
    if 'raid' in text:
        return 'who approved that strategy lmao'  # Banter that merely uses request-ish words.
    if not rows:
        return "can't see it from here. check Requests in the /fri panel"
    states = [row['state'] for row in rows]
    if len(rows) > 1:
        return 'you have two: one is ' + ' and one is '.join(states) + '. which one?'
    if rows[0].get('withdrawn'):
        # Neutral: an owner's earlier rejection looks the same once a privacy change removes the text.
        return 'its text got removed after a privacy change, so it was withdrawn. want me to redraft it?'
    if 'modpack' in text and 'title' not in rows[0]:
        return "the only one i can see shipped, can't tell if that's the modpack one"
    return {'queued': 'not started, waiting for a worker', 'building': 'being built, not done',
            'approved': 'allowed but not deployed yet', 'deployed': "it shipped. can't promise nobody rolled it back",
            'awaiting_owner': 'you already have that one waiting on the owner'}[states[0]]


def test_the_request_status_evaluation_fails_wrong_answers():
    judged = []
    async def judge(data):  # Scripted stand-in for the model judge: only the accurate answer passes.
        judged.append(data)
        verdict = 'pass' if data['reply'] == careful_answer(data['message'], data['requests']) else 'fail'
        return {'verdict': verdict, 'reason': ''}, {'input_tokens': 1}, 'completed'
    def responder(reply, draft=None):
        seen = []
        async def respond(text, context):
            seen.append(context)
            return ChatResult(status='completed', reply=reply(text, context.get('requests')), draft=draft)
        return SimpleNamespace(respond=respond, seen=seen)
    async def run(chat):
        return {case['name']: case async for case, _ in evaluate_requests(chat, 'gpt-6.1-sol', 'high', '', judge=judge)}
    boastful = responder(lambda text, rows: 'btw your fix is live now' if 'raid' in text
                         else 'all done and live, daniel rejected it lol')
    verdicts = asyncio.run(run(boastful))
    graded = [name for name in verdicts if not name.endswith('_judge')]
    assert len(graded) == 11 and not any(verdicts[name]['passed'] for name in graded)
    redrafted = asyncio.run(run(responder(careful_answer, draft='Fix the modpack command')))
    assert not any(row['passed'] for row in redrafted.values()) and not any(name.endswith('_judge') for name in redrafted)
    judged.clear()
    careful = responder(careful_answer)
    verdicts = asyncio.run(run(careful))
    assert all(row['passed'] for row in verdicts.values())
    assert verdicts['public_banter_skips_requests']['evidence']['guards_passed']  # "approved" alone isn't disclosure.
    assert len(judged) == len(careful.seen) == 11
    assert sum('requests' not in context for context in careful.seen) == 1  # Only the unavailable case.
    assert judged[0]['requests'][0]['state'] == 'queued' and judged[0]['rubric'].startswith('Not started')
    assert careful.seen[0]['source']['guild_id'] is None and careful.seen[2]['source']['guild_id'] == '444'


def test_the_model_is_told_when_requests_are_unavailable():
    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, text='data: ' + json.dumps(response_event()) + '\n\n')
    chat = Chat(SimpleNamespace(access_token=lambda: 'plan-token'), httpx.MockTransport(handler))
    asyncio.run(chat.respond('fri did my fix go through', CONTEXT))  # E.g. an invitation or an older controller.
    assert json.loads(sent[0]['input'][-1]['content'])['your_gene_requests'] is None
    assert 'null means' in sent[0]['instructions']


def test_a_rolled_back_request_still_reads_deployed_so_the_model_is_told_not_to_promise_it(store):
    job = proposed(store)
    store.approve(job['id'], 'b' * 40, 'owner')
    release = store.claim_deploy()
    store.finish_deploy(release['id'], release['lease'], True, 'Healthy')
    store.rollback('guild', 'owner')
    rollback = store.claim_deploy()
    store.finish_deploy(rollback['id'], rollback['lease'], True, 'Previous version healthy')
    assert store.status()['active_sha'] == 'a' * 40
    [row] = store.chat_context('guild', 'private:9', 'member')['requests']
    assert row['id'] == job['id'] and row['state'] == 'deployed'  # The controller doesn't record the revert.
    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, text='data: ' + json.dumps(response_event()) + '\n\n')
    chat = Chat(SimpleNamespace(access_token=lambda: 'plan-token'), httpx.MockTransport(handler))
    asyncio.run(chat.respond('is my fix live', {**CONTEXT, 'requests': [row]}))
    assert "a later rollback isn't shown, so don't promise it's still live" in ' '.join(sent[0]['instructions'].split())
