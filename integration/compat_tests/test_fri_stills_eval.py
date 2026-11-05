"""The stills evaluation scores the route a still beside a video link's card takes through Fri's own code, then the
reply: the still in view, the video never claimed watched. Scripted stand-ins replace the model; nothing reaches
Discord, the network or member history."""
import asyncio
import io
import json
import runpy
import sys
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from fri import fri_eval, fri_meme_media
from fri.fri_chat import BESIDE_RULE, ChatResult
from fri.fri_stills_eval import cases, evaluate

FLIPS = ['own_still_short_take', 'own_still_card_unfurls_late', 'own_still_with_a_question',
         'others_still_beside_a_video', 'others_video_unwatched']  # What a build without N2a routes otherwise.


def responder(reply='red circle, clean. 144 btw. kev’s square too. didn’t watch the video tho, rain sounds by the title'):
    seen = []
    async def respond(text, context):
        seen.append((text, context))
        return ChatResult(status='completed', reply=reply)
    return SimpleNamespace(respond=respond, seen=seen)


def run(chat, offline=False):
    async def collect():
        return {case['name']: case async for case, _ in evaluate(chat, 'gpt-6.1-sol', 'high', '', offline=offline)}
    return asyncio.run(collect())


def test_each_case_takes_its_route_and_a_fitting_reply_passes():
    chat = responder()
    rows = run(chat)
    assert all(row['passed'] for row in rows.values()), {name: row['evidence'] for name, row in rows.items()}
    asked = {text: context for text, context in chat.seen}
    own = asked['thoughts? https://youtu.be/rain']
    assert own['own_media_not_inspected'] == {'attachments': ['linked video'], 'reason': 'video'}
    assert own['images'] and own['message_link_cards'][0]['site'] == 'YouTube'
    others = asked['what shape did kev post?']
    assert [entry.get('still') for entry in others['inspected_images']] == [None, 'link preview']
    assert others['invited_context'][0]['attachments'] == ['image', 'linked video']


def test_a_reply_that_claims_it_watched_or_misses_the_still_fails():
    rows = run(responder('watched it, great video. 10/10'))
    assert sorted(name for name, row in rows.items() if not row['passed']) == sorted(
        [case['name'] for case in cases()])


def test_routes_without_a_model_pass_here_and_flip_on_a_build_without_n2a(monkeypatch):
    assert all(row['passed'] for row in run(None, offline=True).values())
    real = fri_meme_media.sources
    monkeypatch.setattr(fri_meme_media, 'sources', lambda message, most=2, *, gifs=False, cards=False:
                        real(message, most, gifs=gifs))  # Before N2a: a video card voids the post.
    monkeypatch.setattr('fri.fri_images.sources', fri_meme_media.sources)
    rows = run(None, offline=True)
    assert [name for name, row in rows.items() if not row['passed']] == FLIPS


def test_the_rule_beside_a_still_goes_to_the_model_with_the_speakers_own_still():
    async def scenario():
        seen = []
        def handler(request):
            seen.append(json.loads(request.content))
            from compat_tests.test_fri_chat import response_event
            return httpx.Response(200, text='data: '+json.dumps(response_event(content=json.dumps(
                {'reply': 'red circle. didn’t watch the video tho', 'request_draft': None})))+'\n\n')
        from fri.fri_chat import Chat
        chat = Chat(SimpleNamespace(access_token=lambda: 'test'), httpx.MockTransport(handler))
        rows = {case['name']: case async for case, _ in evaluate(chat, 'gpt-6.1-sol', 'high', '')}
        assert rows['own_still_short_take']['passed']
        assert any(BESIDE_RULE in body['instructions'] for body in seen)
    asyncio.run(scenario())


def test_a_stills_run_reports_usage_for_each_model_call_and_the_offline_run_needs_none(monkeypatch, tmp_path):
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
    for argv in (None, ['fri_stills_eval'], ['fri_stills_eval', '--offline']):
        printed, reports[:] = io.StringIO(), []
        with patch.object(fri_eval, 'Chat', return_value=responder()), patch.object(
                fri_eval.httpx, 'AsyncClient', side_effect=lambda **kw: client_type(transport=httpx.MockTransport(handle), **kw)):
            with redirect_stdout(printed):
                if argv is None:
                    asyncio.run(fri_eval.main('stills'))
                else:  # python -m fri.fri_stills_eval, in a fresh copy as on the command line.
                    monkeypatch.setattr(sys, 'argv', argv)
                    monkeypatch.delitem(sys.modules, 'fri.fri_stills_eval', raising=False)
                    runpy.run_module('fri.fri_stills_eval', run_name='__main__')
        rows = {row['case']: row for row in map(json.loads, printed.getvalue().splitlines())}
        assert list(rows) == [case['name'] for case in cases()] and all(row['passed'] for row in rows.values())
        calls = 0 if argv and '--offline' in argv else len(cases())
        assert [path for path, _ in reports] == ['/chat/usage/report']*calls
