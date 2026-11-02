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


@pytest.mark.parametrize('ask', ['what did kev recommend?', 'search chat for watched', 'find the clips jo posted',
                                 'what was that song kev mentioned', 'who recommended Minecraft?',
                                 'what was that song <@123> mentioned', 'find the clips jo posted in <#123> this morning'])
def test_a_recall_question_at_the_end_of_a_long_message_still_counts(ask):
    filler = ('what did we ' * LIMIT)[:LIMIT-len(ask)-1]
    started = time.perf_counter()
    assert requested(filler+' '+ask)
    assert time.perf_counter()-started < 0.25
