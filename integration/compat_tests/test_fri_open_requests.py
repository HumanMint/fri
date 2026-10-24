"""Fri's model context carries the speaker's own requests: titles in private, states only in public."""

import asyncio
import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import httpx
import pytest

from compat_tests.test_fri_chat import CONTEXT, response_event
from compat_tests.test_fri_mentions import environment
from fri.fri_chat import Chat, ChatResult
from fri.fri_panel import InputModal
from fri.fri_request_eval import evaluate as evaluate_requests
from fri.fri_requests import DraftView


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
    assert row['state'] == 'rejected' and row['withdrawn'] is True and 'title' not in row
    assert dt.datetime.fromisoformat(row['created']).tzinfo == dt.timezone.utc
    assert 'withdrawn means' in sent[0]['instructions']


def test_the_request_status_evaluation_fails_wrong_answers():
    judged = []
    async def judge(data):  # Scripted stand-in for the model judge: it rejects the boastful reply.
        judged.append(data)
        return {'verdict': 'fail' if 'all done' in data['reply'] else 'pass', 'reason': ''}, {'input_tokens': 1}, 'completed'
    def responder(reply, draft=None):
        seen = []
        async def respond(text, context):
            seen.append(context)
            return ChatResult(status='completed', reply=reply(text), draft=draft)
        return SimpleNamespace(respond=respond, seen=seen)
    async def run(chat):
        return {case['name']: case async for case, _ in evaluate_requests(chat, 'gpt-6.1-sol', 'high', '', judge=judge)}
    boastful = responder(lambda text: 'btw your fix is live now' if 'raid' in text else 'all done and live, daniel rejected it lol')
    verdicts = asyncio.run(run(boastful))
    graded = [name for name in verdicts if not name.endswith('_judge')]
    assert graded and not any(verdicts[name]['passed'] for name in graded)
    assert verdicts['public_banter_skips_requests']['evidence'] == {'guards_passed': False}  # No judge needed.
    assert 'public_banter_skips_requests_judge' not in verdicts
    assert not any(row['passed'] for row in asyncio.run(run(responder(lambda text: 'not yet', draft='Fix it again'))).values())
    judged.clear()
    careful = responder(lambda text: 'lmao that was a mess' if 'raid' in text else 'not yet, still waiting on its next step')
    assert all(row['passed'] for row in asyncio.run(run(careful)).values())
    assert len(judged) == len(careful.seen) == 7 and 'requests' not in careful.seen[5]  # The unavailable case.
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
