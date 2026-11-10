"""Synthetic banter/help sample: none asks to retrieve past server posts."""
import pytest

from fri.fri_history_search import requested

NON_RETRIEVAL = [
    'fri show me a workout for monday',
    'fri show me how to mute someone in this chat',
    'fri show me a recipe for tomorrow',
    'fri show me a plan for next week',
    'fri show me how to pin a message in this channel',
    'fri show me how to upload images in chat',
    'fri show me how to set roles on this server',
    'fri show me how to make a thread in this channel',
    'fri show me a funny meme',
    'fri show me a screenshot',
    'fri show me the weather',
    'fri show me the weather in this chat',
    'fri show me a nice picture for monday',
    'fri show me an example for last week of the schedule',
    'fri show me how to write a bot for this server',
    'fri show me how to read logs',
    'fri show me the steps to invite someone in this chat',
    'fri show me how to fix my microphone from yesterday',
    'fri show me how to edit the screenshot from yesterday',
    'fri show me how to improve my workout from monday',
    'fri can you help with tomorrow\'s meeting?',
    'fri could you help me with the screenshot from yesterday?',
    'fri help me mute someone in this chat',
    'fri how do I invite a friend to this server?',
    'fri how do I upload a screenshot in this channel?',
    'fri can we play tomorrow?',
    'fri show me the steps to edit the screenshot from yesterday',
    'fri I will send that screenshot tomorrow',
    'fri that screenshot from yesterday was hilarious',
    'fri that meme from last night still makes me laugh',
    'fri that link from monday is broken',
    'fri the photo from yesterday looks great',
    'fri everyone was laughing yesterday',
    'fri I am tired from last night',
    'fri I found my keys yesterday',
    'fri I could not find my phone yesterday',
    'fri I need a new laptop for monday',
    'fri recommend a movie for tomorrow',
    'fri what should I cook for monday?',
    'fri what do you think of this chat?',
    'fri explain how permissions work in this channel',
    'fri explain why screenshots look blurry in chat',
    'fri write a poem about yesterday',
    'fri draw a meme about monday',
    'fri make me a workout for tomorrow',
    'fri tell me a joke about this server',
    'fri show me the weather from yesterday',
    'fri show me the workout from monday',
    'fri please show me a tutorial in this chat',
    'fri please show me the rules for tomorrow',
]


def test_sample_has_fifty_messages():
    assert len(NON_RETRIEVAL) == len(set(NON_RETRIEVAL)) == 50


@pytest.mark.parametrize('text', NON_RETRIEVAL)
def test_banter_and_help_do_not_admit_history(text):
    assert not requested(text)
