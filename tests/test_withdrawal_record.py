"""The pending withdrawals record is read in pieces (fri #250 round 5): accepted exactly where json.loads accepts it,
read as json.loads reads it, in bounded memory and linear time, whatever its size or nesting."""
import io
import json
import random
import re
import time
import tracemalloc

import pytest

import fri.fri_turn_withdrawal as withdrawal
from fri.fri_turn_withdrawal import MOST, SCOPE, Unreadable, read_record

GOOD = {'guild_id': '123', 'channel_id': '1', 'conversation_id': 'public:1', 'user_id': '20'}
KEYS = ['pending', 'scope', 'revision', 'deleted', 'guild_id', 'channel_id', 'conversation_id', 'user_id', 'junk', '',
        'é', 'k"', '12', '0', '1'*30, 'x'*70, 'scope\x00']
SCALARS = [0, -7, 12, 2**63-1, 2**63, -1, 10**30, -10**30, 1.5, -2e-3, 1e308, 1e-400, True, False, None, '', '123',
           'public:9', 'private:1', 'public:', 'a"b\\cé\n😀 ', 'x'*70, '9'*25, '9'*26, float('nan'), float('inf'),
           float('-inf'), '\ud800', 'ü'*65]


def expected(text):
    """What the record holds by the PR's rules, from json.loads's reading of it; Unreadable where json.loads fails or
    it holds no record of pending withdrawals."""
    try:
        record = json.loads(text)
    except ValueError:
        return Unreadable
    if not isinstance(record, dict) or not isinstance(record.get('pending'), dict):
        return Unreadable
    found = {}
    for i, entry in record['pending'].items():
        if not re.fullmatch(r'[0-9]{1,25}', i):
            continue
        scope = entry.get('scope') if isinstance(entry, dict) else None
        if isinstance(scope, dict) and all(isinstance(scope.get(key), str) and re.fullmatch(pattern, scope[key])
                                           for key, pattern in SCOPE.items()):
            revision = entry.get('revision')
            found[i] = {'scope': {key: scope[key] for key in SCOPE}, 'deleted': entry.get('deleted') is True,
                        'revision': revision if type(revision) is int and 0 <= revision < 2**63 else None}
        else:
            found[i] = None
    return found


def read(text):
    try:
        return read_record(text)
    except Unreadable:
        return Unreadable


def tree(rng, depth=0):
    """A JSON value as a tree that can say a key twice: ('s', scalar), ('a', [values]) or ('o', [(key, value)])."""
    r = rng.random()
    if depth > 5 or r < 0.4:
        return ('s', rng.choice(SCALARS))
    if r < 0.6:
        return ('a', [tree(rng, depth+1) for _ in range(rng.randint(0, 3))])
    if r < 0.63:
        n = rng.randint(17, 400)
        return ('raw', '['*n+']'*n)
    if r < 0.7:  # Wide and flat: what's passed over a window at a time.
        def flat():
            if rng.random() < 0.7:
                return ('s', rng.choice(SCALARS))
            if rng.random() < 0.5:
                return ('a', [('s', rng.choice(SCALARS)) for _ in range(rng.randint(0, 4))])
            return ('o', [(rng.choice(KEYS), ('s', rng.choice(SCALARS))) for _ in range(rng.randint(0, 4))])
        items = [flat() for _ in range(rng.randint(5, 30))]
        return ('a', items) if rng.random() < 0.5 else ('o', [(rng.choice(KEYS), item) for item in items])
    return ('o', [(rng.choice(KEYS), tree(rng, depth+1)) for _ in range(rng.randint(0, 3))])


def scope(rng):
    pairs = [(key, ('s', value)) for key, value in GOOD.items()]
    if rng.random() < 0.3:
        key = rng.choice(list(GOOD))
        pairs.append((key, ('s', rng.choice(SCALARS)) if rng.random() < 0.7 else tree(rng, 3)))
    if rng.random() < 0.2:
        pairs.append((rng.choice(KEYS), tree(rng, 3)))
    rng.shuffle(pairs)
    return ('o', pairs)


def entry(rng):
    r = rng.random()
    if r < 0.15:
        return tree(rng, 2)
    pairs = [('scope', scope(rng) if r < 0.85 else tree(rng, 3)),
             ('revision', ('s', rng.choice([0, 3, -1, 2**63, 1.0, '4', None, True, 10**30]))),
             ('deleted', ('s', rng.choice([True, False, 1, 'true', None])))]
    for _ in range(rng.randint(0, 2)):
        pairs.append((rng.choice(KEYS), tree(rng, 3)))  # Unused fields, or one of these again.
    rng.shuffle(pairs)
    return ('o', pairs)


def record(rng):
    ids = [str(rng.randint(1, 10**19)) for _ in range(rng.randint(0, 5))]
    pending = [(rng.choice(ids) if ids and rng.random() < 0.2 else
                rng.choice([str(rng.randint(1, 10**19)), rng.choice(KEYS)]) if rng.random() < 0.3 else
                str(rng.randint(1, 10**19)), entry(rng)) for _ in range(rng.randint(0, 5))]
    pairs = [('pending', ('o', pending))]
    if rng.random() < 0.3:
        pairs.append((rng.choice(KEYS), tree(rng, 1)))
    if rng.random() < 0.05:
        pairs.append(('pending', tree(rng, 1)))
    rng.shuffle(pairs)
    return ('o', pairs)


def dump(node, rng, ascii=True):
    def space():
        return rng.choice(['', '', '', ' ', '\n  ', '\t', '\r\n'])
    kind, value = node
    if kind == 'raw':
        return value
    if kind == 's':
        return json.dumps(value, ensure_ascii=ascii)
    if kind == 'a':
        return '['+space()+(space()+','+space()).join(dump(item, rng, ascii) for item in value)+space()+']'
    return '{'+space()+(space()+','+space()).join(json.dumps(key, ensure_ascii=ascii)+space()+':'+space() +
                                                  dump(item, rng, ascii) for key, item in value)+space()+'}'


def texts(seed, n):
    rng = random.Random(seed)
    for _ in range(n):
        yield space_around(rng, dump(record(rng), rng, rng.random() < 0.5))


def space_around(rng, text):
    return rng.choice(['', ' ', '\n'])+text+rng.choice(['', ' ', '\n'])


@pytest.mark.parametrize('chunk', [1, 2, 3, 7, 2**20])
def test_records_read_as_json_loads_reads_them(monkeypatch, chunk):
    """Valid records with duplicate keys, escapes, unused and deep fields, NaN and long numbers, in any piece size:
    the same entries as json.loads's reading by the PR's rules, and refused exactly where json.loads refuses."""
    monkeypatch.setattr(withdrawal, 'CHUNK', chunk)
    accepted = 0
    for text in texts(chunk, 1500 if chunk > 3 else 400):
        want, got = expected(text), read(text)
        assert got == want, text
        accepted += want is not Unreadable
    assert accepted > 100


@pytest.mark.parametrize('chunk', [1, 5, 2**20])
def test_whatever_json_loads_refuses_is_refused(monkeypatch, chunk):
    """Valid records with one character changed, dropped or added: acceptance matches json.loads, and so does what's
    read where both accept."""
    monkeypatch.setattr(withdrawal, 'CHUNK', chunk)
    rng, refused = random.Random(11+chunk), 0
    for text in texts(100+chunk, 3000 if chunk > 5 else 800):
        if not text:
            continue
        at = rng.randrange(len(text))
        change = rng.choice(['', '{', '}', '[', ']', ',', ':', '"', '\\', 'x', ' ', '0', '1', '-', '.', 'e', '\x01', 'n'])
        changed = text[:at]+change+text[at+rng.choice([0, 1]):]
        want, got = expected(changed), read(changed)
        assert got == want, changed
        refused += want is Unreadable
    assert refused > 100


SKIPPED_BUT_NOT_JSON = ['['*16+'1 2'+']'*16, '['*17+'1,]'+']'*16, '['*17+']'*16+'}', '{"a" 1}', '{"a":1,}', '[1,]',
                        '[}', '{]', '{1: 2}', "{'a': 1}", '"\\x"', '"\\u12G4"', '"a\x01"', '01', '1.', '1e', '-',
                        '-Infinityx', 'tru', 'nul', 'NaNa', '[1]]', '"abc', '﻿[]', '[1 , ]', '{"a":}',
                        '[' * 100000,
                        # Mid-list, where runs of plain values are passed over a window at a time.
                        '[1,01,2]', '[1,"a\x01",2]', '[1,"\\x",2]', '{"a":1,"b":01,"c":2}', '[1,[1,01],2]',
                        '[1,{"a":01},2]', '[1,[1,],2]', '[1,{"a":1,},2]', '[1,2 3,4]', '{"a":1,"b" 2,"c":3}',
                        '[1,-,2]', '[1,1.,2]', '[1,tru,2]', '[1,[1 2],3]', '[1,{"a" 1},2]', '[1,[1}],2]']


@pytest.mark.parametrize('junk', SKIPPED_BUT_NOT_JSON)
def test_a_value_passed_over_is_still_checked_as_json(junk):
    text = '{"pending": {}, "junk": '+junk+'}'
    with pytest.raises((ValueError, RecursionError)):
        json.loads(text)
    with pytest.raises(Unreadable):
        read_record(text)


def test_deep_nesting_and_huge_numbers_json_loads_gives_up_on_are_read():
    """Past json.loads's own limits, the entries beside them still read."""
    entry = '"12": {"scope": '+json.dumps(GOOD)+', "revision": 3}'
    text = '{"pending": {'+entry+', "junk": '+'['*200000+']'*200000+'}, "n": '+'9'*10000+'}'
    assert read_record(text) == {'12': {'scope': GOOD, 'revision': 3, 'deleted': False}}
    with pytest.raises(Unreadable):
        read_record('{"pending": {}, "junk": '+'['*(withdrawal.DEEPEST+1)+']'*(withdrawal.DEEPEST+1)+'}')


def test_more_entries_than_the_queue_keeps_are_bounded_newest_first():
    entries = {str(10**18+n): {'scope': GOOD, 'revision': 0} for n in range(MOST*3)}
    entries[str(10**18+5)] = 7  # Said again later, after it would have been dropped: dropped all the same.
    found = read_record(json.dumps({'pending': entries}))
    assert len(found) <= 2*MOST
    newest = sorted(found, key=int)[-MOST:]
    assert newest == [str(10**18+n) for n in range(MOST*2, MOST*3)]


def test_what_the_bound_lets_go_is_reported_for_the_queue_to_hold_back():
    """fri #329: the reader keeps at most MOST*2 entries while it reads; what it lets go is counted, with the latest
    time any of it was queued or sent, or that one of it says no time it was queued, for the queue to hold back what
    those name before it accepts what's left (see SavedTurns.pending)."""
    for timed in (True, False):
        entries = {str(10**18+n): {'scope': GOOD, 'revision': 0, **({'queued_at': 1.6e9+n} if timed else {})}
                   for n in range(MOST*3)}
        overflow = {}
        found = read_record(json.dumps({'pending': entries}), overflow)
        gone = [i for i in entries if i not in found]
        assert gone and overflow['dropped'] == len(gone) and overflow['unknown'] is not timed
        assert overflow['latest'] == max(max(withdrawal.sent_at(int(i)), entries[i].get('queued_at', 0)) for i in gone)
    overflow = {}
    read_record(json.dumps({'pending': {'12': {'scope': GOOD, 'revision': 0}}}), overflow)
    assert overflow == {'dropped': 0, 'latest': None, 'unknown': False}


def test_when_each_was_queued_is_read_back_and_nothing_else_counts_as_one():
    """fri #329: when a withdrawal was queued here goes to disk with it and is read back where it's a time, for a hold
    too; anything else there reads as none, which the queue counts as the time it reads it (see SavedTurns.entry)."""
    at = 1.7e9+0.5
    bad = ['1.7e9', None, True, -1, [1], {'a': 1}, float('nan'), float('inf'), 10**30]
    record = {'pending': {'12': {'scope': GOOD, 'revision': 3, 'queued_at': at}, '13': {'hold': True, 'queued_at': at},
                          '14': {'hold': True, 'queued_at': 1700000000},
                          **{str(20+n): {'scope': GOOD, 'revision': 0, 'queued_at': value} for n, value in enumerate(bad)},
                          **{str(40+n): {'hold': True, 'queued_at': value} for n, value in enumerate(bad)}}}
    found = read_record(json.dumps(record))
    assert found['12'] == {'scope': GOOD, 'revision': 3, 'deleted': False, 'queued_at': at}
    assert found['13'] == {'hold': True, 'queued_at': at} and found['14'] == {'hold': True, 'queued_at': 1700000000}
    assert all(found[str(20+n)] == {'scope': GOOD, 'revision': 0, 'deleted': False} for n in range(len(bad)))
    assert all(found[str(40+n)] == {'hold': True} for n in range(len(bad)))


def record_of(tmp_path, size):
    """A valid record of about `size` characters: an entry, a long unused string, whitespace and deep junk."""
    entry = '"12": {"scope": '+json.dumps(GOOD)+', "revision": 3, "deleted": true}'
    deep = 100000
    rest = max(0, size-2*deep-200)
    text = ('{"pending": {'+entry+', "unused": "'+'x'*(rest//2)+'"}, "junk": '+'['*deep+']'*deep +
            ' '*(rest//2)+'}')
    path = tmp_path/'withdrawals.json'
    path.write_text(text)
    return path


def test_a_large_record_is_read_in_bounded_memory(tmp_path):
    path = record_of(tmp_path, 40*2**20)
    tracemalloc.start()
    try:
        with path.open() as stream:
            found = read_record(stream)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert found == {'12': {'scope': GOOD, 'revision': 3, 'deleted': True}}
    assert peak < 12*2**20  # A few pieces of CHUNK characters, against a 40 MB record.


def test_reading_takes_time_in_proportion_to_size(tmp_path):
    def took(size):
        path = record_of(tmp_path, size)
        best = float('inf')
        for _ in range(3):
            began = time.perf_counter()
            with path.open() as stream:
                read_record(stream)
            best = min(best, time.perf_counter()-began)
        return best
    small, large = took(4*2**20), took(32*2**20)
    assert large < small*8*3  # Eight times the size: well under quadratic (64x).


def test_a_stream_and_its_text_read_the_same():
    for text in texts(5, 200):
        assert read(io.StringIO(text).read()) == read(text)
