"""Which edits count as just made (fri #358 E1): by the edit's own time, against Fri's clock."""
import pytest

from fri.fri_turn_withdrawal import FRESH, fresh

NOW = 1_800_000_000.0


@pytest.mark.parametrize('edited_at, expected', [
    (NOW, True),
    (NOW-FRESH, True),  # Exactly at the window's edge.
    (NOW-FRESH-0.001, False),
    (NOW+FRESH, True),
    (NOW+30*86400, True),  # Ahead of Fri's clock by any amount: a clock behind Discord's.
    (NOW-5*86400, False),  # A pin repeating an edit five days old.
    (None, False),  # No edit time: an update that isn't an edit.
])
def test_an_edit_is_fresh_within_the_window_or_ahead(edited_at, expected):
    assert fresh(edited_at, NOW) is expected


def test_the_window_covers_skew_and_gateway_delay():
    assert FRESH == 15*60
