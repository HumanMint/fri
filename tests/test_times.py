import datetime as dt

import pytest

from fri.fri_times import ASK, render, said_at

NOW = dt.datetime(2026, 10, 27, 12, 0, tzinfo=dt.timezone.utc)


def epoch(value):
    return int(dt.datetime.fromisoformat(value).timestamp())


def test_full_and_relative_forms_render_the_same_instant():
    text = "that's gonna be [[time 2026-10-27T20:00 America/New_York]] which is [[reltime 2026-10-27T20:00 America/New_York]]"
    assert render(text, NOW) == "that's gonna be <t:1793145600:F> which is <t:1793145600:R>"
    assert 1793145600 == epoch('2026-10-28T00:00:00+00:00')


@pytest.mark.parametrize('wall,zone,utc', [
    # New York: daylight time until 2026-11-01 02:00, then standard time.
    ('2026-10-31T20:00', 'America/New_York', '2026-11-01T00:00:00+00:00'),
    ('2026-11-02T20:00', 'America/New_York', '2026-11-03T01:00:00+00:00'),
    # Berlin: summer time ended 2026-10-25, a week before New York's.
    ('2026-10-24T18:00', 'Europe/Berlin', '2026-10-24T16:00:00+00:00'),
    ('2026-10-26T18:00', 'Europe/Berlin', '2026-10-26T17:00:00+00:00'),
    # Spring forward, 2026-03-08 in New York.
    ('2026-03-07T20:00', 'America/New_York', '2026-03-08T01:00:00+00:00'),
    ('2026-03-09T20:00', 'America/New_York', '2026-03-10T00:00:00+00:00'),
    # Seoul has no daylight time.
    ('2026-10-28T21:00', 'Asia/Seoul', '2026-10-28T12:00:00+00:00'),
])
def test_daylight_saving_is_applied_on_each_side_of_a_change(wall, zone, utc):
    assert render(f'[[time {wall} {zone}]]', NOW) == f'<t:{epoch(utc)}:F>'


@pytest.mark.parametrize('zone,utc', [
    ('EST', '2026-11-01T00:00:00+00:00'),  # Eastern time, as people mean it, still daylight here.
    ('ET', '2026-11-01T00:00:00+00:00'), ('pdt', '2026-11-01T03:00:00+00:00'),
    ('KST', '2026-10-31T11:00:00+00:00'), ('CET', '2026-10-31T19:00:00+00:00'),
    ('UTC', '2026-10-31T20:00:00+00:00'), ('GMT', '2026-10-31T20:00:00+00:00'),
    ('UTC+5:30', '2026-10-31T14:30:00+00:00'), ('UTC-03', '2026-10-31T23:00:00+00:00'),
    ('GMT+0900', '2026-10-31T11:00:00+00:00'), ('america/new_york', '2026-11-01T00:00:00+00:00'),
])
def test_common_abbreviations_offsets_and_any_case_resolve(zone, utc):
    assert render(f'[[time 2026-10-31T20:00 {zone}]]', NOW) == f'<t:{epoch(utc)}:F>'


def test_several_placeholders_render_in_place():
    text = ('raid is [[time 2026-10-28T21:00 Asia/Seoul]] ([[reltime 2026-10-28T21:00 Asia/Seoul]]), '
            'scrims [[TIME 2026-10-29T18:00 Europe/Berlin]]\nbring snacks 🍕')
    raid, scrims = epoch('2026-10-28T12:00:00+00:00'), epoch('2026-10-29T17:00:00+00:00')
    assert render(text, NOW) == f'raid is <t:{raid}:F> (<t:{raid}:R>), scrims <t:{scrims}:F>\nbring snacks 🍕'


BROKEN = [
    '[[time 2026-02-30T20:00 UTC]]',  # No such date.
    '[[time 2026-10-27T24:00 UTC]]',
    '[[time 2026-10-27T20:00 Mars/Olympus_Mons]]',
    '[[time 2026-10-27T20:00 CST]]',  # Central or China: the model's call, never the renderer's.
    '[[time 2026-10-27T20:00 Etc/GMT+5]]',  # UTC-5, despite the name.
    '[[time 2026-10-27T20:00 right/America/New_York]]',
    '[[time 2026-10-27T20:00 UTC+15]]',
    '[[time 2027-12-02T20:00 UTC]]', '[[time 2025-09-21T20:00 UTC]]',  # Over 400 days from when it was said.
    '[[time 0001-01-01T00:00 Asia/Seoul]]', '[[time 9999-12-31T23:59 UTC-12]]',
    '[[time 2026-03-08T02:30 America/New_York]]',  # Skipped by spring forward.
    '[[time 2026-11-01T01:30 America/New_York]]',  # Happens twice at fall back.
    '[[time 2026-10-27T20:00]]', '[[time tomorrow 8pm EST]]', '[[time 2026-10-27 20:00 UTC]]',
    '[[ time 2026-10-27T20:00 UTC ]]', '[[time 2026-10-27T20:00 UTC', '[[reltime]]',
    '[[timestamp 2026-10-27T20:00 UTC]]', '[[time 2026-10-27T20:00 America/New_York]]x[[time 2026-02-30T20:00 UTC]]',
]


@pytest.mark.parametrize('placeholder', BROKEN)
def test_a_broken_placeholder_turns_the_reply_into_a_short_ask(placeholder):
    assert render(f"that's {placeholder} for you", NOW) == ASK


def test_the_ask_is_in_fris_voice():
    assert ASK == ASK.lower() and '!' not in ASK and len(ASK) < 80 and 'timezone' in ASK


@pytest.mark.parametrize('text', ['', 'just text', 'matrix [[1, 2], [3, 4]] and [[wiki link]]', '<t:1793145600:F> already',
                                  'time 2026-10-27T20:00 America/New_York', 'café 😭 [time]', ' trailing space ',
                                  'tabs\tand\r\nnewlines'])
def test_text_without_a_placeholder_is_unchanged(text):
    assert render(text, NOW) is text


@pytest.mark.parametrize('text', [*BROKEN, '[[time 2026-10-27T20:00 UTC]]', '[[reltime 2026-10-30T09:15 Asia/Tokyo]]'])
def test_no_placeholder_ever_survives(text):
    assert '[[' not in render(text, NOW)


def test_the_range_is_measured_from_when_it_was_said():
    said = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)
    assert render('[[time 2030-01-02T09:00 UTC]]', said) == '<t:1893574800:F>'
    assert render('[[time 2030-01-02T09:00 UTC]]', NOW) == ASK
    tomorrow = dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=1)
    assert render(f'[[time {tomorrow:%Y-%m-%dT%H:%M} UTC]]').startswith('<t:')  # Now, by default.


def test_relative_times_resolve_against_the_current_messages_source():
    assert said_at({'source': {'at': '2026-11-12T18:00:00+00:00'}}) == dt.datetime(2026, 11, 12, 18, tzinfo=dt.timezone.utc)
    for context in ({}, {'source': None}, {'source': {'at': 'soon'}}, {'source': {'at': '2026-11-12T18:00:00'}},
                    {'source': {'at': 1793145600}}):
        assert abs(said_at(context)-dt.datetime.now(dt.timezone.utc)) < dt.timedelta(seconds=5)
