"""The archive-recall check reads a whole member message without stalling Fri's event loop (fri #134)."""
import time

import pytest

from fri.fri_history_search import requested

LIMIT = 4000  # Discord's longest message.


@pytest.mark.parametrize('unit', ['what did ', 'what did did ', 'what said ', 'what earlier ', 'find memes ',
                                  'find memes posted ', 'search ', 'find ', 'who ', 'what was that the a b c ',
                                  'what is the song <@1> ', 'what was the max size kev ', 'find clips posted in #a ',
                                  'what was that a b said in <#1> on monday night this ', 'posted a few days '])
def test_a_long_repetitive_message_is_checked_quickly(unit):
    text = (unit*LIMIT)[:LIMIT]
    started = time.perf_counter()
    assert not requested(text)
    assert time.perf_counter()-started < 0.25  # Generous: unbounded scans took seconds.


@pytest.mark.parametrize('text,expected', [
    # fri #239, item 1: after "are", an adjective that ends in "s" isn't the plural thing a name follows.
    ('what are the wireless routers recommended?', False), ('what are the glass bottles recommended?', False),
    ('what are the various cables recommended?', False), ('what are the famous dishes recommended?', False),
    ('what are the songs kev recommended?', True), ('what are the tracks mo recommended?', True),
    ('what are the various songs kev recommended?', True), ('what are the boss fights kev recommended?', True),
    ('what are the songs chris recommended?', True), ('what are the emojis kev sent?', True),
    # Item 2: a name spelled as one is a name, even when it's also a listed property word, as a mention is.
    ('what was the song Price mentioned?', True), ('what was the clip Law sent?', True),
    ('what are the songs Price recommended?', True), ('whats the link Mode dropped', True),
    # Not a property word in lowercase, nor one in a title-cased question.
    ('what was the song price mentioned?', False), ('what is the Max File Size shared on Discord?', False),
    ('what is the file Size shared on discord?', False), ('WHAT WAS THE SONG PRICE MENTIONED?', False),
    # Sol round 1 of #294: real plurals ending in "us" or "ss" are still plurals before a name, as on main.
    ('what are the menus Ross shared?', True), ('what are the emus Gus mentioned?', True),
    ('what are the haikus 芽依 shared?', True), ('what are the bass jo recommended?', True),
    ('what are the menus Ross shared here?', True), ('what are the gurus kev recommended?', True),
    # And the adjectives and modifiers item 1 excludes still are.
    ('what are the stainless pans recommended?', False), ('what are the nervous habits mentioned?', False),
    ('what are the chess openings recommended?', False), ('what are the class notes shared?', False),
    ('what are the bus routes recommended?', False), ('what are the business cards shared?', False),
    ('what are the buses kev recommended?', True),
    # A capitalized property label after a property word, or in a known compound, isn't a speaker (as on main),
    # bare or with "here"; "the song Price" still names one.
    ('what is the file Size shared?', False), ('what is the screen Resolution recommended?', False),
    ('what is the data Rate recommended?', False), ('what is the file Size shared here?', False),
    ('what is the screen Resolution recommended here?', False), ('what is the data Rate recommended here?', False),
    ('what is the frame Rate posted?', False), ('what is the sample Rate recommended?', False),
    ('what was the song Price mentioned here?', True), ('what was the song Price mentioned', True),
])
def test_the_speaker_is_a_name_not_a_compound_or_a_property(text, expected):
    """fri #239, items 1 and 2."""
    assert requested(text) is expected


@pytest.mark.parametrize('ask', ['what did kev recommend?', 'search chat for watched', 'find the clips jo posted',
                                 'what was that song kev mentioned', 'who recommended Minecraft?',
                                 'what was that song <@123> mentioned', 'find the clips jo posted in <#123> this morning'])
def test_a_recall_question_at_the_end_of_a_long_message_still_counts(ask):
    filler = ('what did we ' * LIMIT)[:LIMIT-len(ask)-1]
    started = time.perf_counter()
    assert requested(filler+' '+ask)
    assert time.perf_counter()-started < 0.25


@pytest.mark.parametrize('text', [
    "fri find kev's meme from yesterday", 'fri pull up that screenshot from yesterday',
    'fri show me that clip from last night', 'fri dig up the photo from monday',
    'fri look for that link someone shared in chat', 'fri that screenshot from yesterday?',
    'fri show me that image someone sent', 'fri pull up the file you shared in chat',
])
def test_ordinary_retrieval_phrasing(text):
    assert requested(text)


@pytest.mark.parametrize('text', [
    'fri that screenshot was hilarious yesterday', 'fri show me how to draw',
    'fri find me a good laptop', 'fri I found my keys yesterday', 'fri I could not find my keys yesterday',
    'fri do not pull up that screenshot from yesterday',
    "fri don't find kev's meme from yesterday", 'fri stop digging up chat',
    '> fri pull up that screenshot from yesterday',
    '`fri show me that clip from last night`',
])
def test_ordinary_non_retrieval_phrasing(text):
    assert not requested(text)


def test_shared_reaction_counts_are_bounded_and_never_enumerate_people():
    from types import SimpleNamespace

    from fri.fri_images import details

    def forbidden():
        raise AssertionError('reactor identities must never be read')

    message = SimpleNamespace(reactions=[SimpleNamespace(emoji='😂', count=i, me=False, users=forbidden)
                                         for i in range(8)])
    assert details(message)['reactions'] == [{'emoji': '😂', 'count': i} for i in range(6)]
