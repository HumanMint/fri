"""A time someone posted, given back as Discord timestamps: real controller, real Chat, a scripted model."""

import asyncio
import datetime as dt
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import discord
import httpx
import pytest

from compat_tests.test_fri_chat import response_event
from compat_tests.test_fri_mentions import environment
from fri.fri_chat import Chat
from fri.fri_times import ASK
from fri.fri_times_eval import cases, checked
from fri.fri_times_eval import evaluate as evaluate_times


def tomorrow_at_eight():
    """8pm tomorrow in New York, as the placeholder names it and as the epoch Discord gets."""
    day = dt.datetime.now(ZoneInfo('America/New_York')).date()+dt.timedelta(days=1)
    wall = f'{day:%Y-%m-%d}T20:00'
    return wall, int(dt.datetime.fromisoformat(wall).replace(tzinfo=ZoneInfo('America/New_York')).timestamp())


def model(fri, tmp_path, replies):
    """Fri's own model call (chat_once and Chat.respond), answering with each scripted reply in turn."""
    sent = []
    fri.data = tmp_path  # Its usage receipts.

    def handler(request):
        sent.append(json.loads(request.content))
        reply = replies[min(len(sent), len(replies))-1]
        body = {'response_kind': 'reply', 'allow_closing': False, 'reaction': None, 'reply': '',
                'request_draft': None, 'notes': [], 'action': None}
        body.update(reply if isinstance(reply, dict) else {'reply': reply})
        return httpx.Response(200, text='data: '+json.dumps(response_event(content=json.dumps(body)))+'\n\n')
    fri.chat = Chat(SimpleNamespace(access_token=lambda: 'fictional'), httpx.MockTransport(handler))
    del fri.chat_once
    return sent


def delivered(event):
    texts = []

    async def reply(text, **kwargs):
        texts.append(text)
        return SimpleNamespace(id=event.id+1, content=text)
    event.reply.side_effect = reply
    return texts


def shown(payload):
    """The current message's JSON as the model got it."""
    content = payload['input'][-1]['content']
    return json.loads(content if isinstance(content, str) else content[0]['text'])


def saved_replies(calls):
    return [payload['reply'] for path, payload in calls if path == '/chat/turn']


@pytest.mark.parametrize('configured', [True, False])
def test_a_ping_replying_to_a_time_gets_discord_timestamps_delivered_and_saved(monkeypatch, tmp_path, configured):
    async def scenario():
        fri, agent, _, _, channel, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1 if configured else 8)
        wall, epoch = tomorrow_at_eight()
        plan = message('game night 8pm EST tomorrow', user=30, age=120)
        event = message('<@99> what time is that for everyone')
        event.reference = SimpleNamespace(message_id=plan.id, channel_id=channel.id)
        sent = model(fri, tmp_path, [f"that's gonna be [[time {wall} America/New_York]] which is "
                           f'[[reltime {wall} America/New_York]]'])
        texts = delivered(event)
        expected = f"that's gonna be <t:{epoch}:F> which is <t:{epoch}:R>"
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            assert texts == [expected]
            assert saved_replies(calls) == [expected]
            # The model was told when the plan was posted, so "tomorrow" resolves against it.
            seen = shown(sent[-1])
            posted = discord.utils.snowflake_time(plan.id).isoformat(timespec='seconds')
            rows = seen['reply_target'] or seen['invited_channel_context']
            assert any(row['source']['id'] == str(plan.id) and row['source']['at'][:19] == posted[:19] for row in rows)
            assert '[[time YYYY-MM-DDTHH:MM Zone]]' in sent[-1]['instructions']
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_name_call_in_a_configured_channel_saves_the_rendered_reply(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, _, _, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        wall, epoch = tomorrow_at_eight()
        bo = message('game night 8pm EST tomorrow', user=30, age=60)
        agent.recent[1].append({'id': str(bo.id), 'user_id': '30', 'name': 'member 30', 'text': bo.content,
                                'at': time.time()-60, 'revision': 0, 'gene_error': False})  # As Fri heard it.
        event = message('fri what time is that for everyone')
        sent = model(fri, tmp_path, [f'[[time {wall} America/New_York]], so [[reltime {wall} America/New_York]]'])
        texts = delivered(event)
        expected = f'<t:{epoch}:F>, so <t:{epoch}:R>'
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            assert texts == [expected]
            assert saved_replies(calls) == [expected]
            turns = store.chat_context('123', 'public:1', '20')['turns']
            assert turns[-1]['reply'] == expected
            # The live view's times reach the model as dates, not raw epochs.
            recent = shown(sent[-1])['recent_channel_context']
            row = next(row for row in recent if row['id'] == str(bo.id))
            assert isinstance(row['at'], str) and dt.datetime.fromisoformat(row['at']).tzinfo is not None
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_read_further_up_then_the_answer_renders_too(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, channel, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        wall, epoch = tomorrow_at_eight()
        message('game night 8pm EST tomorrow', user=30, age=15*60)
        event = message('<@99> what time was game night for everyone')
        sent = model(fri, tmp_path, [{'action': {'kind': 'read_recent', 'channel_id': str(channel.id), 'window': 'recent'}},
                           f'game night is [[time {wall} America/New_York]]'])
        texts = delivered(event)
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            assert len(sent) == 2 and shown(sent[1])['action_result_untrusted']
            assert texts == [f'game night is <t:{epoch}:F>']
            assert saved_replies(calls) and '[[' not in saved_replies(calls)[0]
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('configured', [True, False])
@pytest.mark.parametrize('broken', ['[[time tomorrow 8pm EST]]', '[[time 2026-02-30T20:00 America/New_York]]',
                                    '[[time {wall} Eastern/Somewhere]]', '[[reltime {wall} America/New_York'])
def test_a_malformed_placeholder_is_never_sent_raw(monkeypatch, tmp_path, configured, broken):
    async def scenario():
        fri, agent, _, _, channel, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1 if configured else 8)
        wall, _ = tomorrow_at_eight()
        plan = message('game night 8pm EST tomorrow', user=30, age=120)
        event = message('<@99> what time is that for everyone')
        event.reference = SimpleNamespace(message_id=plan.id, channel_id=channel.id)
        model(fri, tmp_path, [f"that's {broken.format(wall=wall)} for you"])
        texts = delivered(event)
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            assert texts == [ASK]
            assert saved_replies(calls) == [ASK]
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_burst_reply_renders_its_timestamps(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, _, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        wall, epoch = tomorrow_at_eight()
        first = message('fri game night is 8pm EST tomorrow', age=2)
        second = message('what is that for everyone')
        sent = model(fri, tmp_path, [f'[[time {wall} America/New_York]] for you lot'])
        texts = delivered(second)
        burst = [(0, first, first.content), (0, second, second.content)]
        try:
            await agent.reply_once(second, second.content, burst=burst, addressed=True)
            assert [row['text'] for row in shown(sent[-1])['message_burst']] == [first.content, second.content]
            assert all(row['source']['at'] for row in shown(sent[-1])['message_burst'])
            assert texts == [f'<t:{epoch}:F> for you lot']
            assert saved_replies(calls) == [f'<t:{epoch}:F> for you lot']
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_the_times_evaluation_checks_epochs_without_a_model():
    """Scripted stand-ins: a right answer for each case passes the checks; wrong ones never reach the judge."""
    judged = []

    async def judge(data):
        judged.append(data)
        return {'verdict': 'pass', 'reason': 'ok'}, {}, 'completed'

    def right(case):
        if 'epoch' in case:
            return f"that's <t:{case['epoch']}:F>, <t:{case['epoch']}:R>"
        return 'what timezone is bro on' if case.get('asks') else 'pineapple is a choice, not a crime'

    def wrong(case):
        if 'epoch' in case:
            return f"that's <t:{case['epoch']+3600}:F>"  # An hour off, as a guessed zone would be.
        return 'that is <t:1794535200:F>' if case.get('asks') else '[[time 2026-11-12T21:00 America/New_York]]'

    async def run(answer):
        chat = SimpleNamespace(respond=AsyncMock(side_effect=lambda text, context: SimpleNamespace(
            status='completed', response_kind='reply', action=None, draft=None, counters={},
            reply=answer(next(case for case in cases() if case['text'] == text and
                              case['recent'] == context['recent'])))))
        return [case async for case, _ in evaluate_times(chat, 'gpt-6.1-sol', 'high', '', judge=judge)]

    passed = asyncio.run(run(right))
    assert [case['passed'] for case in passed if not case['name'].endswith('_judge')] == [True]*len(cases())
    assert len(judged) == len(cases())
    judged.clear()
    failed = asyncio.run(run(wrong))
    assert [case['passed'] for case in failed] == [False]*len(cases()) and not judged
    assert not checked({'epoch': 1}, SimpleNamespace(status='completed', response_kind='reply', action=None,
                                                     draft=None, reply=ASK))


def test_the_times_evaluation_shows_the_model_dated_posts():
    """Each case's posts reach the model with dates that agree with their ids, and its fixed send time."""
    seen = []

    def handler(request):
        seen.append(shown(json.loads(request.content)))
        return httpx.Response(200, text='data: '+json.dumps(response_event(content=json.dumps({
            'response_kind': 'reply', 'allow_closing': False, 'reaction': None, 'reply': 'eh',
            'request_draft': None, 'notes': [], 'action': None})))+'\n\n')

    async def judge(data):
        return {'verdict': 'pass', 'reason': 'ok'}, {}, 'completed'

    async def run():
        chat = Chat(SimpleNamespace(access_token=lambda: 'fictional'), httpx.MockTransport(handler))
        return [case async for case, _ in evaluate_times(chat, 'gpt-6.1-sol', 'high', '', judge=judge)]
    asyncio.run(run())
    assert len(seen) == len(cases())
    for case, payload in zip(cases(), seen):
        assert payload['source']['at'] == case.get('now', '2026-11-12T18:00:00+00:00')
        for row in payload['recent_channel_context']:
            assert row['at'] == discord.utils.snowflake_time(int(row['id'])).isoformat(timespec='seconds')
        for row in payload['reply_target']:
            assert row['source']['at'] == discord.utils.snowflake_time(int(row['source']['id'])).isoformat(timespec='seconds')
