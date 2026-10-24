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


def test_emoji_reactions_cost_a_third_of_the_characters():
    reactions = ['💩', '🤡', '🗿', '🫠', '🧂', '🍿', '👀', '🧠', '🪦']
    assert len(json.dumps(reactions)) == 144 and len(model_json(reactions)) == 45
    assert json.loads(model_json(reactions)) == reactions


def test_structure_and_quotes_are_unchanged():
    value = {'a': ['"quoted"', 'back\\slash', 'line\nbreak', 1, None, True], 'nested': {'k': 'v'}}
    assert model_json(value) == json.dumps(value)
