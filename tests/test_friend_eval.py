"""Synthetic model transport only: exercise replies, judge calls and the CLI result writer."""
import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from fri import fri_eval
from fri.fri_chat import Chat, ChatResult

REPLIES = [
    'font department carrying the entire company.',
    'still loaded faster than that insult.',
    'your dinner budget is in danger.',
    'ash did not sign up for your matchmaking service.',
    'one email. entire brain on annual leave.',
    'four laughs and two grimaces. prod is a bold choice for a test run.',
    'https://example.com/timer-debounce',
    'promotion pending.',
    'ram has filed for divorce.',
    'one press can register several times. add a short debounce delay.',
    'try a short debounce delay so one press only starts it once.',
    "my bad. that job's in the past.",
    "that sounds rough. i'm here with you.",
    '1. single press: one start\n2. hold: no repeats\n3. rapid taps: no bounce doubles',
    'join voice first. the speakers are currently imaginary.',
    'https://discord.com/channels/444/555/100000000000000003',
    "didn't find it in that search.",
]


def scripted_chat(reply_status='completed', verdict='pass', judge_status='completed'):
    replies, judges = [], []

    def handle(request):
        payload = json.loads(request.content)
        assert payload['store'] is False
        judge = payload['text']['format']['name'] == 'fri_internal'
        if judge:
            judges.append(json.loads(payload['input'][0]['content']))
            content = {'verdict': verdict, 'reason': 'synthetic judge'}
            status = judge_status
        else:
            replies.append(payload)
            content = {'reply': REPLIES[len(replies) - 1], 'request_draft': None,
                       'response_kind': 'reply', 'allow_closing': False, 'reaction': None,
                       'notes': [], 'action': None}
            status = reply_status
        event = {'type': 'response.' + status, 'response': {
            'output': [{'type': 'message', 'content': [
                {'type': 'output_text', 'text': json.dumps(content)}]}],
            'usage': {'input_tokens': 10, 'output_tokens': 5}}}
        return httpx.Response(200, text='data: ' + json.dumps(event) + '\n\n')

    return Chat(SimpleNamespace(access_token=lambda: 'fictional-token'),
                httpx.MockTransport(handle)), replies, judges


def run(chat):
    async def collect():
        return [case async for case, _ in fri_eval.evaluate(chat, 'mock-model', 'low', suite='friend')]
    return asyncio.run(collect())


def test_all_cases_run_through_mocked_model_and_judge():
    chat, replies, judges = scripted_chat()
    rows = run(chat)
    assert len(rows) == 34 and all(row['passed'] for row in rows)
    assert len(replies) == len(judges) == 17
    assert [row['name'] for row in rows[1::2]] == [
        case['name'] + ':grade' for case in fri_eval.friend_cases()]
    # Prove the saved notes and #407 reaction counts reach both actual model input and judge.
    assert 'plant-watering timer' in json.dumps(replies[10])
    assert judges[10]['situation']['personal_memory']['notes']
    evidence = json.loads(replies[5]['input'][0]['content'])
    assert evidence['reply_target'][0]['reactions'][0]['count'] == 4
    assert judges[5]['situation']['target_reactions']
    assert replies[4]['input'][-1]['content'][1]['type'] == 'input_image'


@pytest.mark.parametrize('reply_status, verdict, judge_status, expected', [
    ('failed', 'pass', 'completed', 17),
    ('completed', 'fail', 'completed', 34),
    ('completed', 'pass', 'failed', 34),
])
def test_failures_cannot_be_accepted(reply_status, verdict, judge_status, expected):
    chat, _, judges = scripted_chat(reply_status, verdict, judge_status)
    rows = run(chat)
    assert len(rows) == expected
    assert all(not row['passed'] for row in rows if
               row['name'].endswith(':grade') or reply_status == 'failed')
    assert bool(judges) == (reply_status == 'completed')


@pytest.mark.parametrize('reply', ['', 'x' * 1000, 'found it somewhere'])
def test_shape_length_and_missing_link_fail_even_when_judge_passes(monkeypatch, reply):
    async def respond(text, context):
        return ChatResult(status='completed', reply=reply)

    async def judge(*args, **kwargs):
        return {'verdict': 'pass', 'reason': 'synthetic'}, {}, 'completed'

    monkeypatch.setattr(fri_eval, 'analyze', judge)
    rows = run(SimpleNamespace(respond=respond))
    found = next(row for row in rows if row['name'] == 'history_meme_found')
    assert found['passed'] is False


def test_negated_success_claim_has_no_brittle_word_ban(monkeypatch):
    async def respond(text, context):
        return ChatResult(status='completed', reply="nothing queued. join voice first.")

    async def judge(*args, **kwargs):
        return {'verdict': 'pass', 'reason': 'negation is truthful'}, {}, 'completed'

    monkeypatch.setattr(fri_eval, 'analyze', judge)
    rows = run(SimpleNamespace(respond=respond))
    assert next(row for row in rows if row['name'] == 'failed_action_honesty')['passed']


@pytest.mark.parametrize('verdict', ['pass', 'fail'])
def test_cli_writes_results_offline(monkeypatch, tmp_path, capsys, verdict):
    chat, _, _ = scripted_chat(verdict=verdict)

    def controller(request):
        if request.url.path == '/status':
            return httpx.Response(200, json={'chat_model': 'mock-model', 'chat_effort': 'low'})
        if request.url.path == '/agent/state':
            return httpx.Response(200, json={'config': {'voice_notes': ''}})
        assert request.url.path == '/chat/usage/report'
        return httpx.Response(200, json={'ok': True})

    client_type = httpx.AsyncClient

    def client(**kwargs):
        if 'transport' not in kwargs:
            kwargs['transport'] = httpx.MockTransport(controller)
        return client_type(**kwargs)

    monkeypatch.setenv('GENE_CONTROLLER_URL', 'http://fictional-controller.test')
    monkeypatch.setenv('GENE_FRI_TOKEN', 'fictional-token')
    monkeypatch.setenv('FRI_DATA', str(tmp_path))
    with patch.object(fri_eval, 'Chat', return_value=chat), patch.object(
            fri_eval.httpx, 'AsyncClient', side_effect=client):
        if verdict == 'fail':
            with pytest.raises(SystemExit, match='Evaluation failed'):
                asyncio.run(fri_eval.main('friend'))
        else:
            asyncio.run(fri_eval.main('friend'))
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(rows) == 34
    assert all(row['status'] == 'completed' for row in rows)
    assert not list(tmp_path.rglob('*.json'))


def test_existing_case_lists_match_main_snapshot():
    # Byte digest of canonical JSON from main 8fba1ad, before adding friend.
    from fri import fri_request_eval

    names = ['image_cases', 'link_cases', 'cases', 'control_cases', 'catch_up_cases',
             'diagnosis_cases', 'pronoun_cases', 'foundation_cases', 'ending_cases',
             'model_text_cases', 'sass_cases', 'settings_cases']
    cases = {name: getattr(fri_eval, name)() for name in names}
    cases['requests'] = fri_request_eval.cases()
    assert hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest() == (
        'a19eb0097a1ee3906915f6ab9ba9d3847252d2167ac2e74736ce8a3ea4b27c6b')
