import json

import pytest

from fri.privacy import model_json

HIDDEN = {
    '​': 'zero-width space', '‍': 'zero-width joiner', '⁠': 'word joiner', '﻿': 'BOM',
    '‮': 'right-to-left override', '⁦': 'bidi isolate', '­': 'soft hyphen',
    '\U000e0041': 'tag letter A', '\U000e007f': 'cancel tag', ' ': 'line separator',
    ' ': 'paragraph separator', '\x7f': 'delete', '\x85': 'next line', '\ud800': 'lone surrogate',
    '': 'private use', '\U0010ffff': 'noncharacter', '͏': 'combining grapheme joiner',
    'ㅤ': 'hangul filler', '︁': 'variation selector 2', '\U000e0100': 'variation selector 17',
    '\u2800': 'braille blank', '\U0001d159': 'null notehead',
}


@pytest.mark.parametrize('char', HIDDEN, ids=HIDDEN.values())
def test_invisible_characters_stay_escaped(char):
    text = model_json({'message': 'a'+char+'b'})
    assert char not in text and json.dumps(char)[1:-1] in text
    assert json.loads(text) == {'message': 'a'+char+'b'}


@pytest.mark.parametrize('value', ['it’s “fine”', '👍', '👍🏽', '❤️', '한국어', 'café', 'naïve', '¯\\_(ツ)_/¯',
                                   'x y', '日本語', 'Привет', '½ → ∞'])
def test_visible_text_reaches_the_model_as_written(value):
    text = model_json({'message': value})
    assert value.replace('\\', '\\\\') in text
    assert json.loads(text) == {'message': value}


def test_a_run_of_presentation_selectors_cannot_hide_text():
    text = model_json('❤️︎️')
    assert text == '"❤️\\ufe0e\\ufe0f"'
    assert model_json('\U000e0100'*4).count('\\udb40') == 4


@pytest.mark.parametrize('value,expected', [
    ('1\ufe0f\u20e3', '"1\ufe0f\u20e3"'),  # A keycap stays as written.
    ('↔\ufe0f', '"↔\ufe0f"'), ('☺\ufe0e', '"☺\ufe0e"'),
    ('\ufe0f', '"\\ufe0f"'),  # Leading: not after the JSON quote.
    (['❤', '\ufe0f'], '["❤", "\\ufe0f"]'),  # Not after the previous string's emoji.
    ('x\n\ufe0f', '"x\\n\\ufe0f"'),  # Not after the n of an escaped newline.
    ('"\ufe0f', '"\\"\\ufe0f"'),
    ('a\ufe0fb\ufe0ec\ufe0f', '"a\\ufe0fb\\ufe0ec\\ufe0f"'),  # Letters can't carry hidden bits.
    ('1\ufe0f2\ufe0e', '"1\\ufe0f2\\ufe0e"'), ('\u2800\ufe0f', '"\\u2800\\ufe0f"'),
    ('■\ufe0e■\ufe0f★\ufe0f', '"■\\ufe0e■\\ufe0f★\\ufe0f"'),  # Symbols a selector doesn't restyle.
    ({'❤\ufe0f\u200b': 1}, '{"❤\ufe0f\\u200b": 1}'),  # Keys follow the same rules.
])
def test_presentation_selectors_count_only_straight_after_an_emoji_they_restyle(value, expected):
    assert model_json(value) == expected
    assert json.loads(model_json(value)) == value


def test_emoji_reactions_cost_a_third_of_the_characters():
    reactions = ['💩', '🤡', '🗿', '🫠', '🧂', '🍿', '👀', '🧠', '🪦']
    assert len(json.dumps(reactions)) == 144 and len(model_json(reactions)) == 45
    assert json.loads(model_json(reactions)) == reactions


def test_structure_and_quotes_are_unchanged():
    value = {'a': ['"quoted"', 'back\\slash', 'line\nbreak', 1, None, True], 'nested': {'k': 'v'}}
    assert model_json(value) == json.dumps(value)


@pytest.mark.parametrize('value', [
    {3: 'a', 2.5: 'b', True: 'c', None: 'd', 'e': float('nan')},  # json.dumps' key conversion.
    [[[[]]]], ((1, 2), ('x',)), {'a': {'b': {'c': ['d', 1e100, -0.0]}}},
])
def test_traversal_matches_json_dumps(value):
    assert model_json(value) == json.dumps(value)


def test_deep_nesting_works_as_in_json_dumps():
    value = []
    for _ in range(350):
        value = [value]
    assert model_json(value) == json.dumps(value)


@pytest.mark.parametrize('value,error', [
    ({(1, 2): 'x'}, TypeError), ({'k': object()}, TypeError), ({'k': b'bytes'}, TypeError),
])
def test_unsupported_values_raise_as_in_json_dumps(value, error):
    with pytest.raises(error):
        json.dumps(value)
    with pytest.raises(error):
        model_json(value)


def test_a_circular_value_raises_as_in_json_dumps():
    value = []
    value.append(value)
    with pytest.raises(ValueError, match='Circular reference'):
        model_json(value)
