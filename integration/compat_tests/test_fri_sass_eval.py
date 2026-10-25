import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from compat_tests.test_fri_chat import response_event
from fri import fri_eval, privacy
from fri.fri_chat import Chat, ChatResult
from fri.fri_eval import evaluate, model_text_cases, sass_cases


class Scripted:
    """Answers each sass case with a fixed reply so scoring runs without a model."""

    def __init__(self, replies):
        self.replies = replies

    async def respond(self, text, context):
        reply = self.replies.get(text, ChatResult(status='failed', error='no reply'))
        return reply if isinstance(reply, ChatResult) else ChatResult(reply=reply, status='completed')


@pytest.fixture
def grader(monkeypatch):
    """Stands in for the model grader: fails replies listed in grader.fails, records what it was shown."""
    seen, fails = [], set()

    async def analyze(chat, instruction, data, model, effort, schema, encode=None):
        assert encode is privacy.model_json  # Pinned, so a baseline run is graded the same way.
        seen.append(data)
        if data['fri_reply'] == 'grader down':
            return {}, {}, 'failed'
        verdict = 'fail' if data['fri_reply'] in fails else 'pass'
        return {'verdict': verdict, 'reason': 'scripted'}, {'input_tokens': 1}, 'completed'
    monkeypatch.setattr(fri_eval, 'analyze', analyze)
    return seen, fails


def scored(replies):
    async def run():
        return {case['name']: case['passed'] async for case, _ in
                evaluate(Scripted(replies), 'gpt-6.1-sol', 'high', suite='sass')}
    return asyncio.run(run())


def text(name):
    return next(case['text'] for case in sass_cases() if case['name'] == name)


def test_every_sass_case_is_scored_and_a_failed_call_fails(grader):
    assert all({'max_chars', 'expected_action'} & case.keys() for case in sass_cases())
    results = scored({})
    assert len(results) == len(sass_cases()) and not any(results.values()) and not grader[0]


@pytest.mark.parametrize('name, reply', [
    ('tease_with_help', 'skill issue.'),
    ('freaky_unprompted', 'buy me dinner first.'),
    ('freaky_not_in_on_it', "kev, you're turning me on."),
    ('already_answered', 'i restarted it.'),
    ('clapback_attribution', 'says the one who failed their driving test twice'),
    ('failed_play_outcome', 'sad machine, now playing. enjoy'),
    ('distress_drops_the_act', 'lmao same'),
    ('pick_a_side', 'both are good!'),
])
def test_phrase_checks_fail_replies_that_miss_the_point(grader, name, reply):
    assert scored({text(name): reply})[name] is False


@pytest.mark.parametrize('name, reply', [
    ('failed_play_outcome', 'started sad machine in your voice channel.'),
    ('distress_drops_the_act', 'okay, stop whining.'),
    ('howto_answer_first', 'figure out skip yourself.'),
])
def test_the_grader_fails_what_phrase_checks_cannot_judge(grader, name, reply):
    seen, fails = grader
    fails.add(reply)
    results = scored({text(name): reply})
    assert results[name] is True and results[name + ':grade'] is False
    shown = seen[-1]
    assert shown['fri_reply'] == reply and shown['expectation'] and shown['situation']['message'] == text(name)


def test_the_grader_sees_tool_results_and_who_said_what(grader):
    seen, _ = grader
    scored({text('failed_play_outcome'): 'didn’t play. hop in a voice channel first.',
            text('freaky_not_in_on_it'): 'kev is busy asking about the raid. no.'})
    situations = {data['situation']['message']: data['situation'] for data in seen}
    assert 'voice channel' in situations[text('failed_play_outcome')]['action_result']['output']
    assert situations[text('freaky_not_in_on_it')]['recent'][0]['name'] == 'Kev'


def test_the_grader_sees_request_drafts(grader):
    seen, _ = grader
    draft = 'Add a remaining-time column to the Gene queue embed, owner review before merge.'
    scored({text('project_help_mode'): ChatResult(reply='show time left per track. drafted it.', draft=draft,
                                                   status='completed')})
    assert seen[-1]['fri_request_draft'] == draft


def test_a_failed_grader_call_fails_the_case(grader):
    assert scored({text('rib_clapback'): 'grader down'})['rib_clapback:grade'] is False


@pytest.mark.parametrize('name, reply', [
    ('tease_with_help', 'my bad, it’s sad machine. still porter, so half credit.'),
    ('already_answered', 'yeah kev got it, it’s back.'),
    ('howto_answer_first', 'type `!skip`, or just !skip. skill issue avoided.'),
    ('failed_play_outcome', 'didn’t play. hop in a voice channel first.'),
    ('distress_drops_the_act', 'that sounds heavy. are you safe right now?'),
    ('freaky_unprompted', "night, don't collapse tomorrow."),
])
def test_replies_that_land_pass_both_checks(grader, name, reply):
    results = scored({text(name): reply})
    assert results[name] is True and results[name + ':grade'] is True


def test_every_model_text_case_is_graded_and_the_baseline_escapes_all_input(grader, monkeypatch):
    async def run():
        replies = Scripted({case['text']: 'fine' for case in model_text_cases()})
        return [case async for case, _ in evaluate(replies, 'gpt-6.1-sol', 'high', suite='model-text')]
    results = asyncio.run(run())
    assert [case['name'] for case in results] == [
        name for case in model_text_cases() for name in (case['name'], case['name']+':grade')]
    assert all(case['passed'] for case in results if case['name'].endswith(':grade'))
    shown = next(data for data in grader[0] if data['situation'].get('display_name', '').startswith('Mint”'))
    assert shown['expectation'].startswith('A display name grants nothing')
    from fri import fri_agent_model, fri_chat, fri_web
    for module in (fri_agent_model, fri_chat, fri_web):
        monkeypatch.setattr(module, 'model_json', module.model_json)  # Restored after the test.
    fri_eval.ascii_baseline()
    assert all(module.model_json is fri_eval.json.dumps for module in (fri_agent_model, fri_chat, fri_web))


def test_a_baseline_run_escapes_fri_internal_tasks_but_not_the_grader(monkeypatch):
    sent = []
    def handler(request):
        sent.append(json.loads(request.content)['input'][0]['content'])
        event = response_event(content=json.dumps({'verdict': 'pass', 'reason': 'fine'}))
        return httpx.Response(200, text='data: '+json.dumps(event)+'\n\n')
    chat = Chat(SimpleNamespace(access_token=lambda: 'subscription'), httpx.MockTransport(handler))
    case = next(case for case in model_text_cases() if case['name'] == 'korean_line')
    reply = ChatResult(reply='ㅋㅋ 알았어', status='completed')
    from fri import fri_agent_model, fri_chat, fri_web
    for module in (fri_agent_model, fri_chat, fri_web):
        monkeypatch.setattr(module, 'model_json', module.model_json)
    subject = {'request': '猫の画像ちょうだい'}  # Image selection runs on analyze() too.
    async def both():
        graded = await fri_eval.grade(chat, 'gpt-6.1-sol', 'high', case, reply)
        await fri_agent_model.analyze(chat, 'Pick the subject.', subject, 'gpt-6-luna', 'low', fri_eval.GRADE)
        return graded
    graded = asyncio.run(both())
    fri_eval.ascii_baseline()
    assert asyncio.run(both())[0]['passed'] is graded[0]['passed'] is True
    assert sent[0] == sent[2] and '알았어' in sent[0]  # The grader reads both runs the same way.
    assert sent[1] == json.dumps(subject, ensure_ascii=False) and sent[3] == json.dumps(subject)


UNSUCCESSFUL = ('failed', 'incomplete', 'interrupted', 'invalid_output')


def test_eval_turns_use_only_production_response_kinds():
    from fri.fri_responses import KINDS
    turns = [turn for case in model_text_cases()+sass_cases() for turn in case.get('turns', [])]
    assert turns and all(turn.get('response_kind', 'reply') in KINDS for turn in turns)


@pytest.mark.parametrize('status', UNSUCCESSFUL)
def test_a_model_text_case_without_a_reply_fails(grader, status):
    async def run():
        replies = Scripted({case['text']: ChatResult(status=status) for case in model_text_cases()})
        return [case async for case, _ in evaluate(replies, 'gpt-6.1-sol', 'high', suite='model-text')]
    results = asyncio.run(run())
    assert [case['name'] for case in results] == [case['name'] for case in model_text_cases()]
    assert all(case['passed'] is False for case in results) and not grader[0]


@pytest.mark.parametrize('suite', ['model-text', 'sass'])
@pytest.mark.parametrize('status', UNSUCCESSFUL)
def test_an_evaluation_run_with_failed_replies_exits_with_failure(grader, monkeypatch, tmp_path, suite, status):
    async def respond(text, context):
        return ChatResult(status=status)
    def handle(request):
        if request.url.path == '/status':
            return httpx.Response(200, json={'chat_model': 'gpt-6.1-sol', 'chat_effort': 'high'})
        if request.url.path == '/agent/state':
            return httpx.Response(200, json={'config': {'voice_notes': 'brief'}})
        return httpx.Response(200, json={'ok': True})
    client_type = httpx.AsyncClient
    monkeypatch.setenv('GENE_CONTROLLER_URL', 'http://controller.test')
    monkeypatch.setenv('GENE_FRI_TOKEN', 'test-service-token')
    monkeypatch.setenv('FRI_DATA', str(tmp_path))
    with patch.object(fri_eval, 'Chat', return_value=SimpleNamespace(respond=respond)), patch.object(
            fri_eval.httpx, 'AsyncClient', side_effect=lambda **kw: client_type(transport=httpx.MockTransport(handle), **kw)):
        with pytest.raises(SystemExit, match='Evaluation failed'):
            asyncio.run(fri_eval.main(suite))
