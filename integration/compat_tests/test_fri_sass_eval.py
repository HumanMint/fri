import asyncio

import pytest

from fri import fri_eval
from fri.fri_chat import ChatResult
from fri.fri_eval import evaluate, sass_cases


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

    async def analyze(chat, instruction, data, model, effort, schema):
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
    ('test_ping', 'yeah, your message came through and i can reply.'),
    ('test_ping_after_failure', 'lol try it and see'),
    ('you_there_in_distress', 'unfortunately.'),
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


def test_the_grader_sees_what_each_action_does(grader):
    seen, _ = grader
    scored({text('howto_answer_first'): 'ping me with “skip this song” and i’ll send it to gene. usual dj/vote checks still apply.'})
    assert 'DJ/vote checks' in seen[-1]['situation']['available_actions']['gene_command']['commands']['skip']


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
    ('distress_drops_the_act', 'i’m sorry things feel this bleak. are you thinking about hurting yourself or ending your life?'),
    ('howto_answer_first', 'ping me with “skip this song” and i’ll send it to gene. usual dj/vote checks still apply.'),
    ('freaky_unprompted', "night, don't collapse tomorrow."),
    ('test_ping', 'unfortunately.'),
    ('test_ping_after_failure', 'can’t tell yet, gene still isn’t answering. try play again in a sec.'),
    ('you_there_in_distress', 'yeah, i’m here. are you safe right now?'),
])
def test_replies_that_land_pass_both_checks(grader, name, reply):
    results = scored({text(name): reply})
    assert results[name] is True and results[name + ':grade'] is True
