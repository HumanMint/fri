"""The recall evaluation scores the route a message takes, never the wording: history search offered and chosen
for a recall ask and not for a general question, and a caption on an unseen own gif answered locally or by the
model, as Fri itself would. Scripted stand-ins replace the model; nothing reaches Discord or member history."""
import asyncio
import io
import json
import runpy
import sys
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from fri import fri_eval, fri_recall_eval
from fri.fri_chat import ChatResult
from fri.fri_images import LIMITS
from fri.fri_recall_eval import cases, evaluate

SEARCH = {'kind': 'history_search', 'channel_id': '', 'query': 'song', 'author': '', 'after': '', 'before': '',
          'has': '', 'cursor': ''}
RECALL = [case['name'] for case in cases() if case.get('recall')]
GENERAL = [case['name'] for case in cases() if case.get('recall') is False]


def responder(search=True, reply='a codec squeezes video so it streams. didn’t watch your gif tho'):
    """Searches whenever history search is offered, else replies."""
    seen = []
    async def respond(text, context):
        seen.append((text, context))
        if search and 'history_search' in context['actions']:
            return ChatResult(status='completed', action=SEARCH)
        return ChatResult(status='completed', reply=reply)
    return SimpleNamespace(respond=respond, seen=seen)


def run(chat):
    async def collect():
        return {case['name']: case async for case, _ in evaluate(chat, 'gpt-6.1-sol', 'high', '')}
    return asyncio.run(collect())


def test_each_message_takes_the_route_fri_gives_it():
    chat = responder()
    rows = run(chat)
    assert all(row['passed'] for row in rows.values()), {name: row['evidence'] for name, row in rows.items()}
    assert len(RECALL) == 5 and len(GENERAL) == 2
    assert all(rows[name]['evidence'] == {'offered': True, 'local_notice': False, 'chosen': 'history_search'}
               for name in RECALL)
    assert all(rows[name]['evidence'] == {'offered': False, 'local_notice': False, 'chosen': None} for name in GENERAL)
    assert rows['unseen_gif_short_take']['model_calls'] == 0 and rows['unseen_gif_short_take']['evidence']['local_notice']
    asked = dict(chat.seen)
    assert 'gif broken?' not in asked  # The local notice makes no model call.
    assert asked['what is a video codec?']['own_media_not_inspected'] == {'attachments': ['gif'], 'reason': 'gif'}
    assert asked['what was that song <@123> mentioned?']['actions']['history_search']['default_channel_id'] == '555'


def test_a_model_that_never_searches_or_asks_for_a_resend_fails():
    rows = run(responder(search=False, reply='cant see that one, resend it as a png'))
    assert [name for name, row in rows.items() if not row['passed']] == [*RECALL, 'unseen_gif_general_question']


def test_checks_that_decide_otherwise_fail_the_same_cases(monkeypatch):
    """The suite scores whichever checks are installed, so a build without this change fails where it differs."""
    general = {case['text'] for case in cases() if case.get('recall') is False}
    monkeypatch.setattr('fri.fri_recall_eval.requested', lambda text, real=fri_recall_eval.requested:
                        text in general or real(text))
    monkeypatch.setattr('fri.fri_recall_eval.about_media', lambda text: False)
    rows = run(responder())
    assert [name for name, row in rows.items() if not row['passed']] == [*GENERAL, 'unseen_gif_short_take']


def test_a_recall_run_reports_usage_only_for_model_calls(monkeypatch, tmp_path):
    reports = []
    def handle(request):
        if request.url.path == '/status':
            return httpx.Response(200, json={'chat_model': 'gpt-6.1-sol', 'chat_effort': 'high'})
        if request.url.path == '/agent/state':
            return httpx.Response(200, json={'config': {'voice_notes': ''}})
        reports.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={'ok': True})
    client_type = httpx.AsyncClient
    monkeypatch.setenv('GENE_CONTROLLER_URL', 'http://controller.test')
    monkeypatch.setenv('GENE_FRI_TOKEN', 'test-service-token')
    monkeypatch.setenv('FRI_DATA', str(tmp_path))
    monkeypatch.setattr(fri_eval, 'evaluate', fri_eval.evaluate)  # Put back after the standalone run replaces it.
    for start in (lambda: fri_eval.main('recall'), None):
        printed, reports[:] = io.StringIO(), []
        with patch.object(fri_eval, 'Chat', return_value=responder()), patch.object(
                fri_eval.httpx, 'AsyncClient', side_effect=lambda **kw: client_type(transport=httpx.MockTransport(handle), **kw)):
            with redirect_stdout(printed):
                if start:
                    asyncio.run(start())
                else:  # python -m fri.fri_recall_eval, in a fresh copy as on the command line.
                    monkeypatch.delitem(sys.modules, 'fri.fri_recall_eval')
                    runpy.run_module('fri.fri_recall_eval', run_name='__main__')
        rows = {row['case']: row for row in map(json.loads, printed.getvalue().splitlines())}
        assert list(rows) == [case['name'] for case in cases()] and all(row['passed'] for row in rows.values())
        assert rows['unseen_gif_short_take']['reply'] == LIMITS['gif']
        assert [path for path, _ in reports] == ['/chat/usage/report']*(len(cases())-1)
        assert all(report['role'] == 'evaluation' and report['status'] == 'completed' for _, report in reports)
