"""The media-only check reads a whole caption in one pass (fri #234 review)."""
import time

import pytest

from fri.fri_images import about_media


@pytest.mark.parametrize('text', ['<'*40000, '<@'*20000, '<a'*20000,  # An unclosed tag, retried at every "<".
                                  'how do '+'gifs '*20000, 'what is '+'a gif '*20000],  # Many format words.
                         ids=['unclosed', 'unclosed mentions', 'unclosed tags', 'plurals', 'indefinites'])
def test_a_long_caption_is_checked_in_one_pass(text):
    started = time.perf_counter()
    about_media(text)
    assert time.perf_counter()-started < 0.1  # Quadratic scans took a second or more.
