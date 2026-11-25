"""fri_eval's provider choice and per-case timing, offline."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from fri import fri_eval
from fri.fri_chat import ChatResult


def test_claude_stays_an_alias_of_provider_claude():
    assert fri_eval.arguments(['--claude']).provider == 'claude'
    assert fri_eval.arguments(['--provider', 'chatgpt', '--suite', 'sass']).provider == 'chatgpt'
    assert fri_eval.arguments([]).provider is None


@pytest.mark.parametrize('status,flag,expected', [
    ({}, None, None),  # An older controller: the plan first, Claude on a limit, as before.
    ({'provider': 'claude', 'claude_chat_model': 'claude-sonnet-5-5'}, None, 'claude'),
    ({'provider': 'claude', 'claude_chat_model': ''}, 'chatgpt', 'chatgpt')])
def test_each_line_names_its_provider_and_time(monkeypatch, tmp_path, capsys, status, flag, expected):
    posted = []
    chat = SimpleNamespace(force_provider=None, claude_chat_model='')

    async def respond(text, context):
        return ChatResult(status='completed', reply='sure.', provider='claude', elapsed_ms=1234, cost_micros=7,
                          model='claude-opus-5-5')
    chat.respond = respond

    def controller(request):
        if request.url.path == '/status':
            return httpx.Response(200, json={'chat_model': 'gpt-6.1-sol', 'chat_effort': 'high', **status})
        if request.url.path == '/agent/state':
            return httpx.Response(200, json={'config': {'voice_notes': ''}})
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={'ok': True})
    client_type = httpx.AsyncClient
    monkeypatch.setenv('GENE_CONTROLLER_URL', 'http://fictional-controller.test')
    monkeypatch.setenv('GENE_FRI_TOKEN', 'fictional-token')
    monkeypatch.setenv('FRI_DATA', str(tmp_path))
    monkeypatch.setattr(fri_eval, 'claude_key', lambda: 'fictional-key')
    with patch.object(fri_eval, 'Chat', return_value=chat), patch.object(
            fri_eval.httpx, 'AsyncClient', side_effect=lambda **kw: client_type(transport=httpx.MockTransport(controller), **kw)):
        try:
            asyncio.run(fri_eval.main('pronouns', provider=flag))
        except SystemExit:
            pass  # A scripted reply may miss a case; only the line's fields matter here.
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows and all(row['provider'] == 'claude' and row['elapsed_ms'] == 1234 for row in rows)
    assert chat.force_provider == expected and chat.claude_chat_model == status.get('claude_chat_model', '')
    assert all(('provider' in report) == bool(status) for report in posted)
    if status:
        assert posted[0]['cost_micros'] == 7
