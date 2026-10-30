"""Isolated recall checks: whether a member's message offers history search and the model uses it, and whether a
caption on their own unseen gif gets the short local notice or the model.

The offer comes from the live check (fri_history_search.requested, as fri_actions.available asks it), and the
model sees fictional context only. A chosen search is recorded, never run. The gif goes where fri_images.decline
sends it: the local notice with no model call, or the model, told what it didn't see. Scores are about the
tool and the route, never the wording. No Discord client, controller writes or member context; the caller
records only anonymous usage, and none for a case that made no model call.

    python -m fri.fri_eval --suite recall
    python -m fri.fri_recall_eval  # The same cases on a build without the recall suite (the without side).
"""

import asyncio
from types import SimpleNamespace

from .fri_chat import ChatResult
from .fri_history_search import HistorySearch, requested
from .fri_images import LIMITS, about_media
from .fri_request_eval import source

CHANNELS = {'555': SimpleNamespace(name='general')}  # The fictional current channel, the only one listed.
UNSEEN_GIF = {'attachments': ['gif'], 'reason': 'gif'}
RESEND = ('resend', 're-send', 'reupload', 're-upload', 'send it again', 'as a png', 'as a jpg')


def offered(text):
    """The history search a public message gets, built as fri_actions.available builds it, or nothing."""
    if not requested(text):
        return {}
    return {'history_search': HistorySearch(SimpleNamespace(channel=555, public=True)).descriptor(CHANNELS)}


def cases():
    def recall(name, text, on):
        return {'name': name, 'text': text, 'recall': on,
                'expect': 'history_search offered and chosen.' if on else 'No history_search offered or chosen.'}
    return [
        recall('mention_speaker', 'what was that song <@123> mentioned?', True),
        recall('place_then_date', 'what was the link nora shared in #general on 10/27?', True),
        recall('named_thread', 'what was the snack lee recommended in the cooking thread?', True),
        recall('plural_after_are', 'what are the tracks mo recommended?', True),
        recall('name_ending_like_a_noun', 'what was the song Felicity mentioned?', True),
        recall('general_place_alone', 'what is the maximum file size shared in a server?', False),
        recall('general_plural_phrase', 'what are the personal details shared in a server?', False),
        {'name': 'unseen_gif_short_take', 'text': 'gif broken?', 'gif': 'local',
         'expect': 'The local notice, with no model call.'},
        {'name': 'unseen_gif_general_question', 'text': 'what is a video codec?', 'gif': 'model',
         'expect': 'The model answers the question, told the gif went unseen. No resend request.'},
    ]


async def evaluate(chat, model, effort, voice_notes):
    for case in cases():
        actions = offered(case['text'])
        local = 'gif' in case and about_media(case['text'])
        evidence = {'offered': 'history_search' in actions, 'local_notice': bool(local)}
        if local:  # fri_images.decline answers this one itself.
            yield ({**case, 'passed': case['gif'] == 'local', 'model_calls': 0, 'evidence': evidence},
                   ChatResult(status='completed', reply=LIMITS['gif']))
            continue
        context = {'model': model, 'effort': effort, 'voice_notes': voice_notes, 'facts': [], 'turns': [],
                   'actions': actions, 'requests_enabled': True, 'personal_memory': {'enabled': True, 'notes': []},
                   'display_name': 'Mint', 'speaker': {'user_id': '101', 'display_name': 'Mint'}, 'source': source()}
        if 'gif' in case:
            context['own_media_not_inspected'] = UNSEEN_GIF
        result = await chat.respond(case['text'], context)
        chosen = (result.action or {}).get('kind')
        evidence['chosen'] = chosen
        if 'gif' in case:
            reply = (result.reply or '').casefold()
            passed = (case['gif'] == 'model' and result.status == 'completed' and result.response_kind == 'reply'
                      and bool(reply) and not result.action and not any(word in reply for word in RESEND))
        else:
            passed = (result.status == 'completed' and evidence['offered'] == case['recall'] and
                      (chosen == 'history_search') == case['recall'])
        yield {**case, 'passed': passed, 'evidence': evidence}, result


if __name__ == '__main__':
    from . import fri_eval

    # Only this suite, through fri_eval's runner, so a build whose fri_eval doesn't list it can still run it.
    fri_eval.evaluate = lambda chat, model, effort, voice_notes='', suite='', **_: evaluate(chat, model, effort,
                                                                                             voice_notes)
    asyncio.run(fri_eval.main('recall'))
