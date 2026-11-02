"""The media-only check reads a whole caption in one pass (fri #234 review)."""
import time

import pytest

from fri.fri_images import about_media


def best(text, runs=3):
    """The fastest of a few runs, so a busy machine's pauses don't count."""
    times = []
    for _ in range(runs):
        started = time.perf_counter()
        about_media(text)
        times.append(time.perf_counter()-started)
    return min(times)


@pytest.mark.parametrize('build', [lambda n: '<'*n, lambda n: '<@'*(n//2), lambda n: '<a'*(n//2),  # An unclosed tag,
                                   lambda n: 'how do '+'gifs '*(n//2),  # retried at every "<". Many format words.
                                   lambda n: 'what is '+'a gif '*(n//2)],
                         ids=['unclosed', 'unclosed mentions', 'unclosed tags', 'plurals', 'indefinites'])
def test_a_long_caption_is_checked_in_one_pass(build):
    # Scaling, not a fixed ceiling, so a slow runner doesn't fail it: four times the caption costs about four
    # times as long in one pass, and sixteen times as long when every position rescans the rest.
    assert best(build(40000)) < 8*best(build(10000)) + 0.005
