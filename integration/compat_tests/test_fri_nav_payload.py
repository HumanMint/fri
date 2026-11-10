"""Synthetic ordinary model request bytes captured on main 3d32067, before runtime edits."""
import asyncio
import datetime as dt
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from compat_tests.test_fri_chat import CONTEXT, response_event
from compat_tests.test_fri_history_search import setup
from fri import fri_social
from fri.fri_chat import Chat
from fri.fri_conversation import evidence

# SHA-256 of the complete HTTP request body, not a projection of selected fields.
MAIN_PAYLOADS = {
    'True:fri hi there': '73b8c018d85e11a85d38e072410827f189c155813f85658ab2b4303981968bf5',
    'True:fri how are you?': '1f512e890b2f80c1db48ff149999eae77de108052ec1145d0d4f7f2d7d3bd7f3',
    'False:fri hi there': '9e8546c74ae5a941a4a56148bffd86ad10bebfd9e3eb50d0a731a6d9285b2119',
    'False:fri find me a good laptop': 'af2b10a63c264ec44f436aecffefbc61e44e3820fc8628bde852b5da4b92d538',
}


async def payload(monkeypatch, tmp_path, public, text):
    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 10, 12, tzinfo=dt.timezone.utc)

    monkeypatch.setattr(fri_social, 'dt', SimpleNamespace(datetime=Clock, timezone=dt.timezone))
    fri, agent, actions, _, _, _ = setup(monkeypatch, tmp_path, public=public, text=text)
    captured = []

    async def transport(request):
        captured.append(request.content)
        return httpx.Response(200, text='data: '+json.dumps(response_event())+'\n\n')

    chat = Chat(SimpleNamespace(access_token=lambda: 'fictional'), httpx.MockTransport(transport))
    try:
        context = {**CONTEXT, **await evidence(agent, actions.event, []),
                   'actions': await actions.available()}
        assert not context['reply_target'] and 'history_search' not in context['actions']
        await chat.respond(text, context)
        return captured[0]
    finally:
        await fri.close()


@pytest.mark.parametrize('public,text', [(True, 'fri hi there'), (True, 'fri how are you?'),
                                         (False, 'fri hi there'), (False, 'fri find me a good laptop')])
def test_ordinary_model_payload_bytes_match_main(monkeypatch, tmp_path, public, text):
    digest = hashlib.sha256(asyncio.run(payload(monkeypatch, tmp_path, public, text))).hexdigest()
    assert digest == MAIN_PAYLOADS[f'{public}:{text}']
