"""The memory eval: proposals graded by Gene's note rules, note use graded by phrase checks and a rubric judge."""

import asyncio
import io
import json
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from compat_tests.test_fri_chat import response_event
from fri import fri_eval, fri_memory_eval
from fri.fri_chat import SUMMARY_RULE, Chat, ChatResult
from fri.fri_memory_eval import TASTE, cases, evaluate


class Scripted:
    """Answers each case's message with a fixed result, and records the context it was given."""

    def __init__(self, replies):
        self.replies, self.seen = replies, {}

    async def respond(self, text, context):
        self.seen[text] = context
        reply = self.replies.get(text, ChatResult(status='failed', error='no reply'))
        return reply if isinstance(reply, ChatResult) else ChatResult(reply=reply, status='completed')


def judged(replies, verdict='pass'):
    shown = []

    async def judge(data):
        shown.append(data)
        return {'verdict': verdict, 'reason': 'scripted'}, {'input_tokens': 1}, 'completed'

    async def run():
        return [case async for case, _ in evaluate(chat, 'gpt-6.1-sol', 'high', '', judge=judge)]
    chat = Scripted(replies)
    return {case['name']: case for case in asyncio.run(run())}, shown, chat


def text(name):
    return next(case['text'] for case in cases() if case['name'] == name)


PROPOSALS = [case for case in cases() if 'keep' in case]
USES = [case for case in cases() if 'keep' not in case]
CAREFUL = {'banter_skips_notes': 'chaos is generous. that was a war crime.',
           'pick_uses_notes_quietly': 'when the sun hits by slowdive. no notes.',
           'other_members_stay_hidden': "no clue, ask kev. i'm not his diary.",
           'summary_picked_up': 'the minecraft server lives. took long enough.'}


def test_about_half_the_cases_are_graded_without_a_judge():
    names = [case['name'] for case in cases()]
    assert len(names) == len(set(names)) == 9 and len(PROPOSALS) == 5 and len(USES) == 4
    assert [case['keep'] is None for case in PROPOSALS].count(True) == 3  # Three that must keep nothing.
    assert all(case['expect'] for case in USES) and not any('<@' in case['text'] for case in cases())


def test_proposals_go_to_gene_as_proposed_and_only_a_reply_that_didnt_complete_fails():
    proposed = [{'category': 'projects', 'evidence': 'im building a discord bot for my friends rn'}]
    results, shown, chat = judged({text('casual_project_is_kept'): ChatResult(reply='slash commands are a rite of '
                                   'passage.', notes=proposed, status='completed'),
                                   text('joke_is_not_kept'): ChatResult(reply='truly.', status='completed')})
    row = results['casual_project_is_kept']
    assert row == {'name': 'casual_project_is_kept', 'expect': fri_memory_eval.GRADED, 'proposed': proposed,
                   'message': text('casual_project_is_kept'),
                   'keep': {'categories': ['projects'], 'about': ['discord bot']}}
    assert results['joke_is_not_kept']['proposed'] == [] and 'passed' not in results['joke_is_not_kept']
    assert results['denied_nickname_is_not_kept']['passed'] is False  # No reply to grade.
    assert not shown  # Nothing here asks the judge.
    for case in PROPOSALS:  # Memory is on, with nothing saved yet.
        assert chat.seen[case['text']]['personal_memory'] == {'enabled': True, 'notes': []}
        assert 'member_summary' not in chat.seen[case['text']]


@pytest.mark.parametrize('name,reply', [
    ('banter_skips_notes', 'chaos, like your valorant aim.'),
    ('banter_skips_notes', 'i remember you said that last week.'),
    ('pick_uses_notes_quietly', 'since you love radiohead: reckoner.'),
    ('pick_uses_notes_quietly', 'great taste btw, try reckoner.'),
    ('pick_uses_notes_quietly', 'i remember you like slowdive. alison.'),
    ('other_members_stay_hidden', 'kev? probably radiohead.'),
    ('summary_picked_up', 'per my summary, the minecraft server works.'),
    ('summary_picked_up', 'you told me port forwarding was the blocker. nice.'),
    ('summary_picked_up', ''),
])
def test_recitals_note_words_and_empty_replies_fail_before_the_judge(name, reply):
    results, shown, _ = judged({text(name): reply})
    assert results[name]['passed'] is False and results[name]['evidence'] == {'guards_passed': False}
    assert not shown


@pytest.mark.parametrize('result', [
    ChatResult(reply='fine.', notes=[{'category': 'games', 'evidence': 'lol that raid'}], status='completed'),
    ChatResult(reply='fine.', action={'kind': 'web_search', 'query': 'raid'}, status='completed'),
    ChatResult(reply='fine.', draft='Add a raid tracker', status='completed'),
    ChatResult(response_kind='react', reaction='💀', status='completed'),
])
def test_a_use_case_never_saves_acts_drafts_or_only_reacts(result):
    results, shown, _ = judged({text('banter_skips_notes'): result})
    assert results['banter_skips_notes']['passed'] is False and not shown


def test_careful_replies_reach_the_judge_with_the_notes_and_the_summary():
    results, shown, chat = judged({text(name): reply for name, reply in CAREFUL.items()})
    assert all(results[case['name']]['passed'] and results[case['name']+'_judge']['passed'] for case in USES)
    by_message = {data['message']: data for data in shown}
    assert len(shown) == 4 and by_message[text('pick_uses_notes_quietly')]['personal_memory'] == TASTE
    summary = by_message[text('summary_picked_up')]
    assert summary['summary'].startswith('talked about getting a minecraft server') and summary['personal_memory'] == []
    assert by_message[text('other_members_stay_hidden')]['recent'][0]['name'] == 'Kev'
    assert chat.seen[text('summary_picked_up')]['member_summary'] == {'text': summary['summary']}
    assert chat.seen[text('banter_skips_notes')]['personal_memory'] == {'enabled': True, 'notes': TASTE}
    failed, _, _ = judged({text(name): reply for name, reply in CAREFUL.items()}, verdict='fail')
    assert not any(failed[case['name']]['passed'] for case in USES)


def test_the_reply_model_reads_the_notes_and_the_summary():
    sent = []
    reply = {'response_kind': 'reply', 'allow_closing': False, 'reaction': None, 'reply': 'nice.',
             'request_draft': None, 'action': None,
             'notes': [{'category': 'music', 'evidence': 'i really like shoegaze'}]}

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, text='data: '+json.dumps(response_event(content=json.dumps(reply)))+'\n\n')

    async def judge(data):
        return {'verdict': 'pass', 'reason': ''}, {}, 'completed'

    async def run():
        chat = Chat(SimpleNamespace(access_token=lambda: 'plan-token'), httpx.MockTransport(handler))
        return {case['name']: case async for case, _ in evaluate(chat, 'gpt-6.1-sol', 'high', '', judge=judge)}
    results = asyncio.run(run())
    assert results['casual_taste_is_kept']['proposed'] == reply['notes']
    inputs = {json.loads(body['input'][-1]['content'])['message']: (json.loads(body['input'][-1]['content']),
              ' '.join(body['instructions'].split())) for body in sent}
    shown, instructions = inputs[text('summary_picked_up')]
    assert shown['your_summary_of_speaker_untrusted'].startswith('talked about getting a minecraft server')
    assert ' '.join(SUMMARY_RULE.split()) in instructions
    shown, instructions = inputs[text('pick_uses_notes_quietly')]
    assert shown['personal_memory'] == {'enabled': True, 'notes': TASTE}
    assert 'your_summary_of_speaker_untrusted' not in shown and ' '.join(SUMMARY_RULE.split()) not in instructions
    assert 'fav/fave' in instructions and '"im", "ive"' in instructions  # The casual starts Gene keeps.


def test_a_memory_run_prints_what_gene_grades_and_fails_only_judged_cases(monkeypatch, tmp_path):
    async def respond(text, context):
        if text == fri_memory_eval.cases()[0]['text']:
            return ChatResult(reply='ok.', notes=[{'category': 'projects', 'evidence': 'im building a discord bot'}],
                              status='completed')
        return ChatResult(reply=CAREFUL.get(next(case['name'] for case in cases() if case['text'] == text), 'ok.'),
                          status='completed')

    async def analyze(chat, instruction, data, model, effort, schema, encode=None):
        return {'verdict': 'fail' if 'kev' in data['reply'] else 'pass', 'reason': ''}, {}, 'completed'

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
    monkeypatch.setattr(fri_memory_eval, 'analyze', analyze)
    printed = io.StringIO()
    with patch.object(fri_eval, 'Chat', return_value=SimpleNamespace(respond=respond)), patch.object(
            fri_eval.httpx, 'AsyncClient', side_effect=lambda **kw: client_type(transport=httpx.MockTransport(handle), **kw)):
        with redirect_stdout(printed), pytest.raises(SystemExit, match='Evaluation failed: other_members_stay_hidden$'):
            asyncio.run(fri_eval.main('memory'))
    rows = {row['case']: row for row in map(json.loads, printed.getvalue().splitlines())}
    project = rows['casual_project_is_kept']
    assert project['message'] == cases()[0]['text'] and project['status'] == 'completed'
    assert project['proposed'] == [{'category': 'projects', 'evidence': 'im building a discord bot'}]
    assert project['keep'] == cases()[0]['keep'] and 'passed' not in project
    assert rows['joke_is_not_kept']['keep'] is None and rows['joke_is_not_kept']['proposed'] == []
    assert 'keep' not in rows['banter_skips_notes'] and rows['banter_skips_notes']['passed'] is True
