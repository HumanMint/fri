"""Replies on either provider through fri_provider: failover both ways, offline with fakes for both transports."""

import asyncio
import json
import logging
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from fri import fri_chat, fri_provider
from fri.fri_chat import (
    CUT_OFF,
    FIZZLED,
    IMAGE_LINK,
    RATE_LIMITED,
    REFUSED,
    SCHEMA,
    UNREADABLE,
    Chat,
)
from fri.fri_provider import Failover, call, cost_micros, route
from fri.fri_responses import REACTIONS
from fri.subscription import SubscriptionError

IMAGE = 'data:image/png;base64,iVBORw0KGgo='
REPLY = {'response_kind': 'reply', 'allow_closing': False, 'reaction': None, 'reply': 'eh, works',
         'request_draft': None, 'notes': [], 'action': None}
OPUS = 'claude-opus-5-5'


def context(**extra):
    return {'model': 'gpt-6.1-sol', 'effort': 'medium', 'facts': [], 'source': {'channel_id': '1'},
            'turns': [{'input': 'yo', 'reply': 'sup', 'source': {'kind': 'legacy_unverified'}}], **extra}


def sse(*events):
    return ''.join(f'data: {json.dumps(event)}\n\n' for event in events)


def completed(reply=REPLY):
    return sse({'type': 'response.completed', 'response': {'status': 'completed', 'usage': {'input_tokens': 3}, 'output': [
        {'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(reply)}]}]}})


def chat(*responses, calls=None, token=lambda: 'fictional-token'):
    """A Chat whose plan answers with these, in order: a status code, (status, body), or an exception to raise."""
    queue = list(responses) or [429]

    def handle(request):
        (calls if calls is not None else []).append(json.loads(request.content))
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        status, body = answer if isinstance(answer, tuple) else (answer, '')
        return httpx.Response(status, text=body)
    return Chat(SimpleNamespace(access_token=token), httpx.MockTransport(handle))


class MidStream(Exception):
    """Raised from get_final_message, after the stream opened."""


class FakeClaude:
    """Stands in for anthropic.AsyncAnthropic: records the stream request and returns a scripted message."""
    requests, keys, options = [], [], []
    outcome = None

    def __init__(self, api_key, **options):
        FakeClaude.keys.append(api_key)
        FakeClaude.options.append(options)
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self.stream))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, **request):
        FakeClaude.requests.append(request)
        outcome = FakeClaude.outcome.pop(0) if isinstance(FakeClaude.outcome, list) else FakeClaude.outcome

        class Stream:
            async def __aenter__(self):
                if isinstance(outcome, Exception) and not isinstance(outcome, MidStream):
                    raise outcome
                return self

            async def __aexit__(self, *exc):
                return False

            async def get_final_message(self):
                if isinstance(outcome, MidStream):
                    raise anthropic.APIConnectionError(request=httpx.Request('POST', 'https://api.example'))
                return outcome
        return Stream()


def message(text=json.dumps(REPLY), stop_reason='end_turn', model=OPUS):
    return SimpleNamespace(model=model, stop_reason=stop_reason, content=[SimpleNamespace(type='text', text=text)],
                           usage=SimpleNamespace(input_tokens=10, output_tokens=7, cache_read_input_tokens=100,
                                                 cache_creation_input_tokens=5))


def status_error(cls, status, text='nope', body=None):
    response = httpx.Response(status, request=httpx.Request('POST', 'https://api.example'))
    return cls(text, response=response, body=body)


def bad_request(text):
    return status_error(anthropic.BadRequestError, 400, text)


@pytest.fixture
def claude(monkeypatch, tmp_path):
    key = tmp_path / 'anthropic-key'
    key.write_text('fictional-key\n')
    monkeypatch.setenv('FRI_ANTHROPIC_KEY_FILE', str(key))
    monkeypatch.setattr(anthropic, 'AsyncAnthropic', FakeClaude)
    FakeClaude.requests, FakeClaude.keys, FakeClaude.options, FakeClaude.outcome = [], [], [], message()
    return key


def test_429_with_key_answers_through_claude_laid_out_for_the_cache(claude):
    calls = []
    result = asyncio.run(chat(calls=calls).respond('look at this', context(images=[IMAGE])))
    assert len(calls) == 1 and FakeClaude.keys == ['fictional-key'] and FakeClaude.options[0]['max_retries'] == 0
    assert (result.status, result.reply, result.response_kind, result.error) == ('completed', 'eh, works', 'reply', '')
    assert (result.model, result.effort, result.provider) == (OPUS, 'medium', 'claude')
    assert result.counters == {'input_tokens': 115, 'output_tokens': 7, 'cached_input_tokens': 100,
                               'reasoning_output_tokens': None}
    assert result.cost_micros == cost_micros(OPUS, 10, 7, 100, 5) == 10*4 + 7*20 + 20 + 25
    request, sent = FakeClaude.requests[0], calls[0]
    # Breakpoint 1: INSTRUCTIONS alone, byte for byte; the turn's variable rules after it, uncached.
    first, second = request['system']
    assert first == {'type': 'text', 'text': fri_chat.INSTRUCTIONS, 'cache_control': {'type': 'ephemeral'}}
    assert 'cache_control' not in second and first['text'] + second['text'] == sent['instructions']
    assert 'cache_control' not in request  # No automatic breakpoint after the changing last message.
    assert request['model'] == OPUS and request['output_config']['effort'] == 'medium'
    assert request['fallbacks'] == 'default' and request['betas'] == ['server-side-fallback-2026-07-01']
    assert 'thinking' not in request and 'temperature' not in request
    schema = request['output_config']['format']['schema']
    assert 'maxLength' not in json.dumps(schema) and 'maxLength' in json.dumps(SCHEMA)
    assert schema['properties']['action'] == {'type': 'null'}  # Nothing offered: only null, as parse_reply allows.
    assert schema['properties']['reaction'] == {'anyOf': [{'type': 'null'}, {'type': 'string', 'enum': list(REACTIONS)}]}
    user, assistant, last = request['messages']
    assert user == {'role': 'user', 'content': sent['input'][0]['content']}
    # Breakpoint 2: the last saved turn's assistant message.
    assert assistant == {'role': 'assistant', 'content': [{'type': 'text', 'text': 'sup',
                                                           'cache_control': {'type': 'ephemeral'}}]}
    assert last['content'][0] == {'type': 'text', 'text': sent['input'][-1]['content'][0]['text']}
    assert last['content'][1:] == [
        {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'iVBORw0KGgo='}}]
    assert not any('cache_control' in json.dumps(block) for block in last['content'])


def test_without_history_only_the_system_breakpoint_is_set(claude):
    asyncio.run(chat().respond('hi', context(turns=[])))
    request = FakeClaude.requests[0]
    assert [m['role'] for m in request['messages']] == ['user']
    assert json.dumps(request).count('cache_control') == 1


def test_an_image_by_url_refuses_without_any_claude_request(claude):
    result = asyncio.run(chat().respond('look', context(images=[IMAGE, 'https://cdn.example/a.webp'])))
    assert (result.status, result.error, FakeClaude.requests) == ('failed', IMAGE_LINK, [])


def test_link_card_rule_reaches_claude_in_its_reordered_place(claude):
    calls = []
    asyncio.run(chat(calls=calls).respond('this?', context(message_link_cards=[{'title': 'x'}])))
    assert ''.join(block['text'] for block in FakeClaude.requests[0]['system']) == calls[0]['instructions']
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


# plan -> Claude: each failover class, then exactly one Claude request.
@pytest.mark.parametrize('answer,reason', [
    (429, 'rate_limited'), (500, 'overloaded'), (503, 'overloaded'),
    (httpx.ConnectError('refused'), 'connection'), (httpx.ConnectTimeout('slow'), 'connection'),
    ((200, sse({'type': 'response.created', 'response': {}}, {'type': 'response.failed', 'response': {
        'status': 'failed', 'output': [], 'error': {'code': 'subscription_sharing_usage_limit_exceeded'}}})),
     'usage_limit')])
def test_plan_failovers_try_claude_once(claude, answer, reason):
    calls, seen = [], []
    result = asyncio.run(chat(answer, calls=calls).respond('hi', context(fence={'record': lambda *a: seen.append(a)})))
    assert (result.status, result.reply, result.model) == ('completed', 'eh, works', OPUS)
    assert len(calls) == 1 and len(FakeClaude.requests) == 1
    assert [(a[0], a[4]) for a in seen] == [('chatgpt', reason), ('claude', None)]


def test_a_refused_sign_in_fails_over_after_the_full_checks(claude):
    def refused():
        raise SubscriptionError('reconnect me')
    order = []

    async def ready():
        order.append('ready')

    async def recheck():
        order.append('recheck')
    seen = []
    result = asyncio.run(chat(token=refused).respond('hi', context(fence={
        'recheck': recheck, 'record': lambda *a: seen.append(a)}), ready=ready))
    assert result.status == 'completed' and len(FakeClaude.requests) == 1
    assert order == ['ready']  # The full checks once, right before the only request; nothing to recheck.
    assert [(a[0], a[3].error, a[4]) for a in seen] == [('chatgpt', 'reconnect me', 'auth'), ('claude', '', None)]


@pytest.mark.parametrize('answer', [400, 401, 403, 404,
                                    (200, sse({'type': 'response.failed', 'response': {'status': 'failed', 'output': [
                                        {'type': 'message'}], 'error': {'code': 'usage_limit_reached'}}})),
                                    (200, sse({'type': 'response.failed', 'response': {'status': 'failed',
                                                                                       'error': {'code': 'server_error'}}})),
                                    (200, completed({'nope': 1}))])
def test_other_plan_failures_never_fall_back(claude, answer):
    result = asyncio.run(chat(answer).respond('hi', context()))
    assert result.status != 'completed' and result.error != RATE_LIMITED and FakeClaude.requests == []


def test_an_unreadable_plan_reply_says_why_without_its_content(claude, caplog):
    caplog.set_level(logging.INFO, 'fri.fri_chat')
    result = asyncio.run(chat((200, completed({**REPLY, 'reply': 'secret words', 'notes': 'x'}))).respond('hi', context()))
    assert (result.status, result.error, result.detail) == ('invalid_output', UNREADABLE, 'Invalid notes')
    assert 'secret words' not in caplog.text and 'Invalid notes' in caplog.text


def test_a_plan_stream_cut_off_after_it_answered_does_not_fall_back(claude):
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"type": "response.output_text.delta", "delta": "x"}\n\n'
            raise httpx.ReadError('gone')
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Broken()))
    result = asyncio.run(Chat(SimpleNamespace(access_token=lambda: 't'), transport).respond('hi', context()))
    assert result.error == CUT_OFF and FakeClaude.requests == []


# Claude -> plan, with Claude primary.
@pytest.mark.parametrize('error,reason', [
    (status_error(anthropic.RateLimitError, 429), 'rate_limited'),
    (status_error(anthropic.InternalServerError, 529, 'overloaded'), 'overloaded'),
    (status_error(anthropic.InternalServerError, 500), 'overloaded'),
    (anthropic.APIConnectionError(request=httpx.Request('POST', 'https://api.example')), 'connection'),
    (TimeoutError(), 'connection'),
    (status_error(anthropic.BadRequestError, 400, 'Your credit balance is too low to access the API'), 'billing'),
    (status_error(anthropic.APIStatusError, 402), 'billing'),
    (status_error(anthropic.AuthenticationError, 401), 'auth'),
    (status_error(anthropic.PermissionDeniedError, 403), 'auth')])
def test_claude_failovers_try_the_plan_once(claude, error, reason):
    FakeClaude.outcome, calls, seen = error, [], []
    subject = chat((200, completed()), calls=calls)
    result = asyncio.run(subject.respond('hi', context(provider='claude', fence={
        'record': lambda *a: seen.append(a)})))
    assert (result.status, result.reply, result.provider) == ('completed', 'eh, works', 'chatgpt')
    assert len(FakeClaude.requests) == 1 and len(calls) == 1
    assert [(a[0], a[1], a[4]) for a in seen] == [('claude', OPUS, reason), ('chatgpt', 'gpt-6.1-sol', None)]
    assert bool(subject.latched()) == (reason in ('billing', 'auth'))


@pytest.mark.parametrize('broken', ['key', 'sdk'])
def test_no_claude_key_or_sdk_goes_to_the_plan_without_a_claude_request(claude, monkeypatch, broken):
    if broken == 'key':
        claude.unlink()
    else:
        monkeypatch.delitem(__import__('sys').modules, 'anthropic')
        monkeypatch.setattr(fri_chat.importlib, 'import_module', lambda name: (_ for _ in ()).throw(ImportError(name)))
    calls, seen = [], []
    result = asyncio.run(chat((200, completed()), calls=calls).respond('hi', context(provider='claude', fence={
        'record': lambda *a: seen.append(a)})))
    assert result.status == 'completed' and len(calls) == 1 and FakeClaude.requests == []
    assert [a[0] for a in seen] == ['chatgpt']  # Nothing went to Claude, so there's nothing of it to record.


@pytest.mark.parametrize('outcome,error', [
    (message(stop_reason='max_tokens'), FIZZLED), (MidStream(), FIZZLED),
    (bad_request('messages: invalid image'), FIZZLED), (json.JSONDecodeError('bad', '', 0), FIZZLED),
    (IndexError(), FIZZLED), (OSError('read error'), FIZZLED)])
def test_other_claude_failures_never_fall_back(claude, outcome, error):
    FakeClaude.outcome, calls = outcome, []
    result = asyncio.run(chat((200, completed()), calls=calls).respond('hi', context(provider='claude')))
    assert (result.status, result.error, result.model, calls) == ('failed', error, OPUS, [])


def test_unreadable_claude_output_is_judged_like_the_plans(claude):
    FakeClaude.outcome = message(text='not json')
    result = asyncio.run(chat((200, completed())).respond('hi', context(provider='claude')))
    assert (result.status, result.error, result.detail) == ('invalid_output', UNREADABLE, 'JSONDecodeError')


def test_a_refusal_declines_in_voice_and_is_recorded_as_one(claude):
    FakeClaude.outcome, calls, seen = message(stop_reason='refusal'), [], []
    result = asyncio.run(chat((200, completed()), calls=calls).respond('hi', context(provider='claude', fence={
        'record': lambda *a: seen.append(a)})))
    assert (result.status, result.error, result.failover) == ('failed', REFUSED, 'refusal')
    assert REFUSED != RATE_LIMITED and calls == [] and len(seen) == 1


@pytest.mark.parametrize('primary', ['chatgpt', 'claude'])
def test_a_refused_recheck_sends_nothing_to_the_second_provider(claude, primary):
    """fri #418: what was taken back while the first provider failed never reaches the other."""
    FakeClaude.outcome = status_error(anthropic.RateLimitError, 429)
    calls, seen = [], []

    async def recheck():
        raise PermissionError('taken back')
    subject = chat(429, calls=calls)
    with pytest.raises(PermissionError):
        asyncio.run(subject.respond('hi', context(provider=primary, fence={
            'recheck': recheck, 'record': lambda *a: seen.append(a)})))
    assert (len(calls), len(FakeClaude.requests)) == ((1, 0) if primary == 'chatgpt' else (0, 1))
    assert [a[0] for a in seen] == [primary]  # The first attempt's usage is still kept.


def test_the_billing_latch_skips_claude_for_fifteen_minutes(claude, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(fri_chat.time, 'monotonic', lambda: clock[0])
    FakeClaude.outcome = status_error(anthropic.APIStatusError, 402)
    subject = chat((200, completed()))
    asyncio.run(subject.respond('hi', context(provider='claude')))
    assert subject.latched()['reason'] == 'billing' and len(FakeClaude.requests) == 1
    FakeClaude.outcome = message()
    clock[0] += fri_chat.CLAUDE_LATCH - 1
    assert asyncio.run(subject.respond('hi', context(provider='claude'))).provider == 'chatgpt'
    assert len(FakeClaude.requests) == 1  # Skipped: no doomed round trip.
    clock[0] += 2
    assert subject.latched() is None
    assert asyncio.run(subject.respond('hi', context(provider='claude'))).provider == 'claude'


def test_the_plans_cache_hint_comes_back_after_an_hour(claude, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(fri_chat.time, 'monotonic', lambda: clock[0])
    calls = []
    subject = chat(400, calls=calls)
    for step in (0, fri_chat.CACHE_RETRY - 1, 2):
        clock[0] += step
        asyncio.run(subject.respond('hi', context()))
    assert ['prompt_cache_key' in call for call in calls] == [True, False, True]


def test_force_provider_answers_on_that_provider_alone(claude):
    calls = []
    subject = chat(200, calls=calls)
    subject.force_provider, subject.claude_chat_model = 'claude', 'claude-sonnet-5-5'
    result = asyncio.run(subject.respond('hi', context()))
    assert calls == [] and result.status == 'completed' and result.provider == 'claude'
    assert FakeClaude.requests[0]['model'] == 'claude-sonnet-5-5' and result.elapsed_ms >= 0
    FakeClaude.outcome = status_error(anthropic.RateLimitError, 429)
    assert asyncio.run(subject.respond('hi', context())).error == RATE_LIMITED and calls == []  # No failover.


def test_served_by_a_fallback_model_records_that_model(claude):
    FakeClaude.outcome = message(model='claude-opus-4-8')
    assert asyncio.run(chat().respond('hi', context())).model == 'claude-opus-4-8'


def test_route_follows_the_controller_and_the_tier_map():
    assert route({}, 'gpt-6.1-sol', 'high') == [('chatgpt', 'gpt-6.1-sol', 'high'), ('claude', OPUS, 'high')]
    assert route({'provider': 'claude'}, 'gpt-6-luna', 'low') == [('claude', 'claude-haiku-5-5', 'low'),
                                                                  ('chatgpt', 'gpt-6-luna', 'low')]
    assert route({'provider': 'claude', 'claude_chat_model': 'claude-sonnet-5-5'}, 'gpt-6.2-sol', 'high')[0] == (
        'claude', 'claude-sonnet-5-5', 'high')
    assert route({'provider': 'claude'}, 'gpt-unknown', 'high')[0] == ('claude', OPUS, 'high')


def test_call_tries_the_second_provider_once_and_only_after_a_failover():
    tried = []

    async def attempt(provider, model, effort, gate):
        tried.append((provider, gate))
        if provider == 'a':
            raise Failover('overloaded', 'first')
        raise Failover('rate_limited', 'second')
    result, attempts = asyncio.run(call([('a', 'm', 'e'), ('b', 'm', 'e'), ('c', 'm', 'e')], attempt, 'gate'))
    assert tried == [('a', None), ('b', 'gate')] and result == 'second'
    assert [(a[0], a[4]) for a in attempts] == [('a', 'overloaded'), ('b', 'rate_limited')]

    async def answers(provider, model, effort, gate):
        return provider
    assert asyncio.run(call([('a', 'm', 'e'), ('b', 'm', 'e')], answers))[0] == 'a'


def test_cost_counts_cache_reads_and_writes_at_their_own_price():
    assert cost_micros('claude-haiku-5-5', 1000, 100, 1000, 0) == round(1000*0.1 + 100*0.5 + 1000*0.01)
    assert cost_micros('gpt-6.1-sol', 1, 1) is None
    assert cost_micros(OPUS, 10**9, 10**9) == fri_provider.MAX_COST_MICROS
    assert set(fri_provider.TIERS.values()) == set(fri_provider.PRICES)


def test_claude_schema_lists_only_offered_actions_and_parse_still_refuses_others(claude):
    offered = {'web_search': {}, 'image_search': {}, 'work_list': {}}
    action = fri_chat.claude_schema(SCHEMA, offered)['properties']['action']['anyOf']
    assert action[0] == {'type': 'null'}
    assert [branch['properties']['kind']['enum'] for branch in action[1:]] == [['web_search'], ['image_search'], ['work_list']]
    assert 'maxLength' not in action[2]['properties']['query']
    FakeClaude.outcome = message(text=json.dumps({**REPLY, 'reply': '', 'action': {'kind': 'web_refine', 'query': 'x'}}))
    result = asyncio.run(chat().respond('hi', context(actions=offered)))
    assert result.status == 'invalid_output' and result.detail == 'Invalid or unavailable action'
    FakeClaude.outcome = message(text=json.dumps({**REPLY, 'reply': '', 'action': {'kind': 'web_search', 'query': 'x'}}))
    result = asyncio.run(chat().respond('hi', context(actions=offered)))
    assert result.status == 'completed' and result.action == {'kind': 'web_search', 'query': 'x'}


def test_same_named_action_fields_agree_so_a_flat_action_can_hold_them():
    seen = {}
    for branch in SCHEMA['properties']['action']['anyOf'][1:]:
        for key, value in branch['properties'].items():
            if key != 'kind':
                assert seen.setdefault(key, fri_chat.claude_clean(value)) == fri_chat.claude_clean(value), key


FLAT_FIELDS = {'action': '', 'argument': '', 'query': '', 'author': '', 'after': '', 'before': '', 'has': '', 'cursor': ''}


def test_a_too_large_grammar_retries_once_flat_and_keeps_only_the_kinds_fields(claude):
    offered = {'read_recent': {}, 'gene_command': {}, 'history_search': {}}
    flat_reply = {**REPLY, 'reply': '', 'action': {'kind': 'read_recent', 'channel_id': '5', 'window': 'pins', **FLAT_FIELDS}}
    FakeClaude.outcome = [bad_request('The compiled grammar is too large, which would cause performance issues.'),
                          message(text=json.dumps(flat_reply))]
    result = asyncio.run(chat().respond('whats pinned', context(actions=offered)))
    assert result.status == 'completed' and result.action == {'kind': 'read_recent', 'channel_id': '5', 'window': 'pins'}
    first, second = [request['output_config']['format']['schema']['properties']['action'] for request in FakeClaude.requests]
    assert len(first['anyOf']) == 4
    null, flat = second['anyOf']
    assert null == {'type': 'null'} and flat['additionalProperties'] is False
    assert flat['properties']['kind']['enum'] == ['none', 'read_recent', 'gene_command', 'history_search']
    assert set(flat['required']) == set(flat['properties']) and 'channel_id' in flat['properties']
    assert flat['properties']['window'] == {'type': 'string', 'enum': ['recent', 'since_my_last_message', 'pins']}
    assert not any(isinstance(field.get('type'), list) or 'anyOf' in field for field in flat['properties'].values())


def test_422a_a_flat_owner_draft_with_kind_none_is_a_draft(claude):
    """fri #422 (a): on the flat path every action field is required, so a draft came back with an action beside it."""
    offered = {'read_recent': {}, 'gene_command': {}}
    draft = 'Make Gene show the remaining play time in the queue.'
    filled = {**REPLY, 'reply': 'drafted it', 'request_draft': draft,
              'action': {'kind': 'none', 'channel_id': '', 'window': 'recent', **FLAT_FIELDS}}
    FakeClaude.outcome = [bad_request('Schema is too complex.'), message(text=json.dumps(filled))]
    result = asyncio.run(chat().respond('fix gene', context(actions=offered, requester_is_owner=True)))
    assert (result.status, result.draft, result.action) == ('completed', draft, None)
    # The same draft beside a real action is still refused, and says which check refused it.
    FakeClaude.outcome = [bad_request('Schema is too complex.'), message(text=json.dumps(
        {**filled, 'action': {**filled['action'], 'kind': 'read_recent', 'channel_id': '5'}}))]
    result = asyncio.run(chat().respond('fix gene', context(actions=offered)))
    assert (result.status, result.detail) == ('invalid_output', 'An action with a reply, draft or notes')


@pytest.mark.parametrize('draft', ['tiny', 'x' * 4001])
def test_422b_the_draft_bounds_are_stated_and_still_checked(claude, draft):
    """fri #422 (b): Claude's schema has no maxLength, so the bounds are said in words; parse_reply still holds them."""
    schema = fri_chat.claude_schema(SCHEMA, {})
    assert '5 to 4000 characters' in schema['properties']['request_draft']['description']
    assert schema['properties']['request_draft']['type'] == ['string', 'null']
    FakeClaude.outcome = message(text=json.dumps({**REPLY, 'request_draft': draft}))
    result = asyncio.run(chat().respond('fix gene', context()))
    assert (result.status, result.detail) == ('invalid_output', 'Invalid reply shape')


@pytest.mark.parametrize('action', [{'kind': 'read_recent', 'channel_id': '5'}, {'kind': 'unknown', 'channel_id': '5'}])
def test_a_flat_action_missing_its_own_field_or_of_an_unknown_kind_is_still_refused(claude, action):
    FakeClaude.outcome = [bad_request('Schema is too complex.'), message(text=json.dumps({**REPLY, 'reply': '', 'action': action}))]
    result = asyncio.run(chat().respond('whats pinned', context(actions={'read_recent': {}})))
    assert result.status == 'invalid_output' and len(FakeClaude.requests) == 2


def test_other_bad_requests_and_a_second_grammar_failure_do_not_retry_again(claude):
    FakeClaude.outcome = [bad_request('messages: invalid image'), message()]
    assert asyncio.run(chat().respond('hi', context())).error == FIZZLED and len(FakeClaude.requests) == 1
    FakeClaude.requests, FakeClaude.outcome = [], [bad_request('grammar is too large')] * 2 + [message()]
    assert asyncio.run(chat().respond('hi', context())).error == FIZZLED and len(FakeClaude.requests) == 2


def test_flat_action_keeps_a_reply_without_an_action_and_leaves_bad_json_alone():
    assert json.loads(fri_chat.flat_action(json.dumps(REPLY))) == REPLY
    assert json.loads(fri_chat.flat_action(json.dumps({**REPLY, 'action': {'kind': 'none', 'query': ''}}))) == REPLY
    assert fri_chat.flat_action('not json') == 'not json'


def test_one_info_line_per_claude_attempt_without_content(claude, caplog):
    caplog.set_level(logging.INFO, 'fri.fri_chat')
    FakeClaude.outcome = message(text=json.dumps({**REPLY, 'reply': 'private words'}))
    asyncio.run(chat().respond('hi', context()))
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith('Claude attempt')]
    assert lines == [f'Claude attempt: model={OPUS} stop_reason=end_turn cache_read=100 failover=None']
    assert 'private words' not in caplog.text
