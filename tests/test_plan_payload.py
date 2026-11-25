"""The plan's request body is byte-for-byte what it was before the provider seam (fri PR 3 of the Claude-native plan).

tests/fixtures/plan_payload.json holds the bodies main sent for these contexts, made by running bodies() at main's
acf1b27, with INSTRUCTIONS and the unchanged reply SCHEMA stood in for by markers so a prompt edit doesn't churn it.
Regenerate it only for a deliberate change to the plan's request: `python tests/test_plan_payload.py > ...`."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx

from fri import fri_chat
from fri.fri_chat import Chat

IMAGE = 'data:image/png;base64,iVBORw0KGgo='
BASE = {'model': 'gpt-6.1-sol', 'effort': 'high', 'facts': [{'text': 'gene plays music'}],
        'source': {'kind': 'discord_message', 'id': '9', 'channel_id': '42'},
        'turns': [{'input': 'yo', 'reply': 'sup', 'source': {'kind': 'legacy_unverified'}},
                  {'input': 'thumbs', 'response_kind': 'reaction', 'reaction': '👍'}],
        'speaker': {'user_id': '7', 'display_name': 'Sam'}, 'display_name': 'Sam'}
CASES = {
    'plain': {},
    'rich': {'images': [IMAGE], 'member_summary': {'text': 'talked about synths'}, 'voice_notes': 'be dry',
             'message_link_cards': [{'title': 'x'}], 'own_media_not_inspected': [{'kind': 'video'}],
             'actions': {'web_search': {}}, 'requester_is_owner': True},
    'beside': {'own_media_not_inspected': [{'kind': 'video'}], 'image_sources': ['x'], 'force_text_reply': True},
    'no_channel': {'source': None},
}


def bodies(extra=None):
    out = {}
    for name, case in CASES.items():
        calls = []

        def handle(request):
            calls.append(json.loads(request.content))
            return httpx.Response(400, text='')
        chat = Chat(SimpleNamespace(access_token=lambda: 'fictional-token'), httpx.MockTransport(handle))
        asyncio.run(chat.respond('hello there', {**BASE, **case, **(extra or {})}))
        body = calls[0]
        assert body['instructions'].startswith(fri_chat.INSTRUCTIONS)
        body['instructions'] = '<INSTRUCTIONS>' + body['instructions'][len(fri_chat.INSTRUCTIONS):]
        if body['text']['format']['schema'] == fri_chat.SCHEMA:
            body['text']['format']['schema'] = '<SCHEMA>'
        out[name] = body
    return out


def test_the_plan_request_is_as_it_was_on_main():
    expected = json.loads((Path(__file__).parent / 'fixtures' / 'plan_payload.json').read_text())
    assert bodies() == expected


def test_a_controller_naming_the_plan_sends_the_same_request():
    expected = json.loads((Path(__file__).parent / 'fixtures' / 'plan_payload.json').read_text())
    assert bodies({'provider': 'chatgpt', 'model_provider': 'chatgpt', 'claude_chat_model': ''}) == expected


if __name__ == '__main__':
    print(json.dumps(bodies(), indent=1, ensure_ascii=False, sort_keys=True))
