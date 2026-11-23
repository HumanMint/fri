"""The Claude fallback: only a ChatGPT 429 with a key file sends the request to Claude, offline with fakes."""

import asyncio
import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from fri import fri_chat
from fri.fri_chat import CLAUDE_MODEL, RATE_LIMITED, SCHEMA, Chat
from fri.fri_responses import REACTIONS

IMAGE = 'data:image/png;base64,iVBORw0KGgo='
REPLY = {'response_kind': 'reply', 'allow_closing': False, 'reaction': None, 'reply': 'eh, works',
         'request_draft': None, 'notes': [], 'action': None}


def context(**extra):
    return {'model': 'gpt-test', 'effort': 'medium', 'facts': [], 'source': {'channel_id': '1'},
            'turns': [{'input': 'yo', 'reply': 'sup', 'source': {'kind': 'legacy_unverified'}}], **extra}


def chat(status=429, calls=None):
    def handle(request):
        (calls if calls is not None else []).append(json.loads(request.content))
        return httpx.Response(status, text='')
    return Chat(SimpleNamespace(access_token=lambda: 'fictional-token'), httpx.MockTransport(handle))


class FakeClaude:
    """Stands in for anthropic.AsyncAnthropic: records the stream request and returns a scripted message."""
    requests, keys = [], []
    outcome = None

    def __init__(self, api_key):
        FakeClaude.keys.append(api_key)
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self.stream))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, **request):
        FakeClaude.requests.append(request)
        outcome = FakeClaude.outcome

        class Stream:
            async def __aenter__(self):
                if isinstance(outcome, Exception):
                    raise outcome
                return self

            async def __aexit__(self, *exc):
                return False

            async def get_final_message(self):
                return outcome
        return Stream()


def message(text=json.dumps(REPLY), stop_reason='end_turn', model=CLAUDE_MODEL):
    return SimpleNamespace(model=model, stop_reason=stop_reason, content=[SimpleNamespace(type='text', text=text)],
                           usage=SimpleNamespace(input_tokens=10, output_tokens=7, cache_read_input_tokens=100,
                                                 cache_creation_input_tokens=5))


@pytest.fixture
def claude(monkeypatch, tmp_path):
    key = tmp_path / 'anthropic-key'
    key.write_text('fictional-key\n')
    monkeypatch.setenv('FRI_ANTHROPIC_KEY_FILE', str(key))
    monkeypatch.setattr(anthropic, 'AsyncAnthropic', FakeClaude)
    FakeClaude.requests, FakeClaude.keys, FakeClaude.outcome = [], [], message()
    return key


def test_429_with_key_answers_through_claude_and_the_shared_parser(claude):
    calls = []
    result = asyncio.run(chat(calls=calls).respond('look at this', context(images=[IMAGE, 'https://cdn.example/a.webp'])))
    assert len(calls) == 1 and FakeClaude.keys == ['fictional-key']
    assert (result.status, result.reply, result.response_kind, result.error) == ('completed', 'eh, works', 'reply', '')
    assert (result.model, result.effort) == (CLAUDE_MODEL, 'high')
    assert result.counters == {'input_tokens': 115, 'output_tokens': 7, 'cached_input_tokens': 100,
                               'reasoning_output_tokens': None}
    request, sent = FakeClaude.requests[0], calls[0]
    assert request['model'] == CLAUDE_MODEL and request['system'] == sent['instructions']
    assert request['output_config']['effort'] == 'high' and request['cache_control'] == {'type': 'ephemeral'}
    assert request['fallbacks'] == 'default' and request['betas'] == ['server-side-fallback-2026-07-01']
    assert 'thinking' not in request and 'temperature' not in request
    schema = request['output_config']['format']['schema']
    assert 'maxLength' not in json.dumps(schema) and 'maxLength' in json.dumps(SCHEMA)
    assert schema['properties']['action'] == {'type': 'null'}  # Nothing offered: only null, as parse_reply allows.
    assert schema['properties']['reaction'] == {'anyOf': [{'type': 'null'}, {'type': 'string', 'enum': list(REACTIONS)}]}
    assert schema['properties']['request_draft'] == SCHEMA['properties']['request_draft']
    assert [m['role'] for m in request['messages']] == [m['role'] for m in sent['input']]
    assert request['messages'][:2] == [{'role': m['role'], 'content': m['content']} for m in sent['input'][:2]]
    *_, last = request['messages']
    assert last['content'][0] == {'type': 'text', 'text': sent['input'][-1]['content'][0]['text']}
    assert last['content'][1:] == [
        {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'iVBORw0KGgo='}},
        {'type': 'image', 'source': {'type': 'url', 'url': 'https://cdn.example/a.webp'}}]


def test_link_card_rule_reaches_claude_in_its_reordered_place(claude):
    calls = []
    asyncio.run(chat(calls=calls).respond('this?', context(message_link_cards=[{'title': 'x'}])))
    assert FakeClaude.requests[0]['system'] == calls[0]['instructions']
    assert calls[0]['instructions'].startswith(fri_chat.INSTRUCTIONS + fri_chat.CARDS_RULE)


@pytest.mark.parametrize('content', [None, '', '  \n'])
def test_without_a_usable_key_a_429_fails_as_before(claude, content):
    if content is None:
        claude.unlink()
    else:
        claude.write_text(content)
    result = asyncio.run(chat().respond('hi', context()))
    assert (result.status, result.error, result.model, result.counters) == ('failed', RATE_LIMITED, None, {})
    assert FakeClaude.requests == []


@pytest.mark.parametrize('outcome', [
    message(stop_reason='refusal'), message(stop_reason='max_tokens'), message(text='not json'),
    anthropic.APIConnectionError(request=httpx.Request('POST', 'https://api.example')), TimeoutError()])
def test_claude_failures_fail_like_the_rate_limit(claude, outcome):
    FakeClaude.outcome = outcome
    result = asyncio.run(chat().respond('hi', context()))
    if isinstance(outcome, SimpleNamespace) and outcome.stop_reason == 'end_turn':
        assert result.status == 'invalid_output'  # Unreadable JSON is judged as any model's would be.
    else:
        assert (result.status, result.error, result.reply) == ('failed', RATE_LIMITED, '')
    assert result.model == CLAUDE_MODEL


@pytest.mark.parametrize('status', [401, 403, 500])
def test_other_failures_never_fall_back(claude, status):
    result = asyncio.run(chat(status).respond('hi', context()))
    assert result.status == 'failed' and result.error != RATE_LIMITED and FakeClaude.requests == []


def test_force_claude_skips_chatgpt(claude):
    calls, subject = [], None
    subject = chat(200, calls)
    subject.force_claude = True
    result = asyncio.run(subject.respond('hi', context()))
    assert calls == [] and result.status == 'completed' and result.model == CLAUDE_MODEL


def test_served_by_a_fallback_model_records_that_model(claude):
    FakeClaude.outcome = message(model='claude-opus-4-8')
    assert asyncio.run(chat().respond('hi', context())).model == 'claude-opus-4-8'


def test_claude_schema_lists_only_offered_actions_and_parse_still_refuses_others(claude):
    offered = {'web_search': {}, 'image_search': {}, 'work_list': {}}
    action = fri_chat.claude_schema(SCHEMA, offered)['properties']['action']['anyOf']
    assert action[0] == {'type': 'null'}
    assert [branch['properties']['kind']['enum'] for branch in action[1:]] == [['web_search'], ['image_search'], ['work_list']]
    assert 'maxLength' not in action[2]['properties']['query']
    FakeClaude.outcome = message(text=json.dumps({**REPLY, 'reply': '', 'action': {'kind': 'web_refine', 'query': 'x'}}))
    result = asyncio.run(chat().respond('hi', context(actions=offered)))
    assert result.status == 'invalid_output'
    FakeClaude.outcome = message(text=json.dumps({**REPLY, 'reply': '', 'action': {'kind': 'web_search', 'query': 'x'}}))
    result = asyncio.run(chat().respond('hi', context(actions=offered)))
    assert result.status == 'completed' and result.action == {'kind': 'web_search', 'query': 'x'}
