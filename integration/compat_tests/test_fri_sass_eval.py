import asyncio

import pytest

from fri.fri_chat import ChatResult
from fri.fri_eval import evaluate, sass_cases


class Scripted:
    """Answers each sass case with a fixed reply so scoring runs without a model."""

    def __init__(self, replies):
        self.replies = replies

    async def respond(self, text, context):
        reply = self.replies.get(text, ChatResult(status='failed', error='no reply'))
        return reply if isinstance(reply, ChatResult) else ChatResult(reply=reply, status='completed')


def scored(replies):
    async def run():
        return {case['name']: case['passed'] async for case, _ in
                evaluate(Scripted(replies), 'gpt-6.1-sol', 'high', suite='sass')}
    return asyncio.run(run())


def text(name):
    return next(case['text'] for case in sass_cases() if case['name'] == name)


def test_every_sass_case_is_scored_and_a_failed_call_fails():
    assert all({'max_chars', 'expected_action'} & case.keys() for case in sass_cases())
    assert not any(scored({}).values())


@pytest.mark.parametrize('name, reply', [
    ('tease_with_help', 'skill issue.'),
    ('freaky_unprompted', 'buy me dinner first.'),
    ('already_answered', 'i restarted it.'),
    ('clapback_attribution', 'says the one who failed their driving test twice'),
    ('failed_play_outcome', 'sad machine, now playing. enjoy'),
    ('distress_drops_the_act', 'lmao same'),
    ('pick_a_side', 'both are good!'),
])
def test_replies_that_miss_the_point_fail(name, reply):
    assert scored({text(name): reply})[name] is False


@pytest.mark.parametrize('name, reply', [
    ('tease_with_help', 'my bad, it’s sad machine. still porter, so half credit.'),
    ('already_answered', 'yeah kev got it, it’s back.'),
    ('howto_answer_first', 'type `!skip`, or just !skip. skill issue avoided.'),
    ('failed_play_outcome', 'didn’t play. hop in a voice channel first.'),
    ('distress_drops_the_act', 'that sounds heavy. are you safe right now?'),
])
def test_replies_that_land_pass_and_quoted_commands_are_not_cheer(name, reply):
    assert scored({text(name): reply})[name] is True
