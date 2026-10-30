"""The media-only check reads a whole caption in one pass (fri #234 review)."""
import time

from fri.fri_images import about_media


def test_an_unclosed_tag_run_is_checked_quickly():
    for text in ('<'*40000, '<@'*20000, '<a'*20000):
        started = time.perf_counter()
        about_media(text)
        assert time.perf_counter()-started < 0.05  # Quadratic scans of an unclosed "<" took about half a second.
