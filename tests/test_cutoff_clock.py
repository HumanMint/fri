"""Which cutoff Fri compares a snowflake with (fri #337 items 4 and 5): the controller's on Discord's clock when it says
one, else its own clock's, as before. A malformed one never holds back less than before."""

import datetime as dt

import discord
import pytest

from fri.fri_memory import before_removal, erased_cutoff


def sent(at):
    return discord.utils.time_snowflake(dt.datetime.fromtimestamp(at, dt.timezone.utc))


NOW = 1_800_000_000.0


@pytest.mark.parametrize('profile,expected', [
    ({'erased_at': NOW, 'erased_at_discord': NOW + 2}, NOW + 2),  # Gene behind Discord: the later one.
    ({'erased_at': NOW, 'erased_at_discord': NOW - 2}, NOW - 2),  # Gene ahead: the earlier one.
    ({'erased_at': NOW}, NOW),  # An older controller: its own clock, as before.
    ({'erased_at': NOW, 'erased_at_discord': 'soon'}, NOW),  # Malformed: no less than before.
    ({'erased_at': NOW, 'erased_at_discord': float('nan')}, NOW),
    ({'erased_at': NOW, 'erased_at_discord': True}, NOW),
    ({'erased_at': NOW, 'erased_at_discord': -1}, NOW),
    ({'erased_at': 0, 'erased_at_discord': 0}, 0),
])
def test_a_members_own_message_is_held_to_the_erasure_on_discords_clock(profile, expected):
    assert erased_cutoff(profile) == expected


@pytest.mark.parametrize('profile,at,held', [
    ({'removed_at': NOW, 'removed_at_discord': NOW + 2}, NOW + 1, True),  # Item 4: before it on Discord's clock.
    ({'removed_at': NOW, 'removed_at_discord': NOW - 2}, NOW - 1, False),  # Item 5: after it.
    ({'removed_at': NOW}, NOW - 1, True),  # An older controller, as before.
    ({'removed_at': NOW}, NOW + 1, False),
    ({'removed_at': NOW, 'removed_at_discord': 'soon'}, NOW + 3, True),  # Malformed: unverifiable, held back.
    ({'removed_at': NOW, 'removed_at_discord': None}, NOW + 3, True),
    ({'removed_at': 0, 'removed_at_discord': 0}, NOW, False),
])
def test_a_post_is_held_to_its_authors_cutoff_on_discords_clock(profile, at, held):
    assert before_removal(sent(at), profile) is held
