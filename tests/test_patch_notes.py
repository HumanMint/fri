import asyncio
import datetime as dt
import io
import json
import types

import aiohttp
import discord
import pytest

from fri import fri_patch_notes as notes

OWNER = 4242
BOT = 99


class FakeDM:
    id = 777

    def __init__(self):
        self.messages = []
        self.sends = []
        self.lost = set()  # Sends Discord takes but whose reply never arrives.
        self.refused = set()  # Sends that fail before Discord takes them.

    async def send(self, content, *, nonce, allowed_mentions, suppress_embeds):
        self.sends.append({'content': content, 'nonce': nonce, 'mentions': allowed_mentions,
                           'suppress_embeds': suppress_embeds})
        index = len(self.sends)
        if index in self.refused or len(content.encode('utf-16-le')) > 4000:
            raise RuntimeError('discord refused')
        message = said(1000+index, content, BOT)
        self.messages.append(message)
        if index in self.lost:
            raise RuntimeError('reply lost')
        return message

    async def history(self, *, limit, after, oldest_first):
        assert oldest_first and after is not None
        for message in [m for m in self.messages if m.created_at > after][:limit]:
            yield message


def said(id, content, author):
    return types.SimpleNamespace(id=id, content=content, author=types.SimpleNamespace(id=author),
                                 created_at=dt.datetime.now(dt.timezone.utc))


class FakeClient:
    instances = []
    dm = None
    login_error = None

    def __init__(self, *, intents):
        self.user = None
        self.closed = False
        FakeClient.instances.append(self)

    async def login(self, token):
        assert token == 'secret-token'
        if FakeClient.login_error:
            raise FakeClient.login_error
        self.user = types.SimpleNamespace(id=BOT)

    async def fetch_user(self, user_id):
        assert user_id == OWNER, 'patch notes only ever go to the owner'
        async def create_dm():
            return FakeClient.dm
        return types.SimpleNamespace(id=user_id, create_dm=create_dm)

    async def close(self):
        self.closed = True


@pytest.fixture
def dm(monkeypatch):
    FakeClient.instances = []
    FakeClient.dm = FakeDM()
    FakeClient.login_error = None
    monkeypatch.setattr(notes.discord, 'Client', FakeClient)
    return FakeClient.dm


def run(text, tmp_path, version='0.4.0'):
    lines = []
    asyncio.run(notes.send(version, text, token='secret-token', owner_id=OWNER, data=tmp_path, out=lines.append))
    return lines


def paragraph(size, tag='x'):
    return (tag * size)[:size]


def test_parts_packs_whole_paragraphs_into_discord_sized_messages():
    text = '\n\n'.join([paragraph(900, 'a'), paragraph(900, 'b'), paragraph(900, 'c')])
    chunks = notes.parts(text)
    assert [len(c) for c in chunks] == [1802, 900]
    assert chunks[0] == paragraph(900, 'a') + '\n\n' + paragraph(900, 'b')
    assert notes.parts('**fri 0.4.0**\r\nline\r\n\r\n- next') == ['**fri 0.4.0**\nline\n\n- next']


def test_parts_count_length_the_way_discord_does():
    """Discord counts UTF-16 units, so an emoji is 2: two paragraphs that fit as Python text don't fit one message."""
    text = paragraph(1000, 'a') + '\n\n' + '\U0001f3ae' * 600
    assert notes.parts(text) == [paragraph(1000, 'a'), '\U0001f3ae' * 600]
    assert notes.parts('\U0001f3ae' * 1000) == ['\U0001f3ae' * 1000]


@pytest.mark.parametrize('text, reason', [
    ('  \n\n ', 'empty'),
    (paragraph(2001), 'over 2000'),
    ('\U0001f3ae' * 1001, 'over 2000'),
    ('\n\n'.join(paragraph(1500, t) for t in 'abcde'), 'at most 4'),
], ids=['empty', 'long', 'emoji', 'five-parts'])
def test_parts_refuses_notes_it_cant_send_whole(text, reason):
    with pytest.raises(notes.NotesRefused, match=reason):
        notes.parts(text)


def test_emoji_heavy_notes_are_sent_in_parts_discord_accepts(dm, tmp_path):
    run(paragraph(1000, 'a') + '\n\n' + '\U0001f3ae' * 600, tmp_path)
    assert [s['content'] for s in dm.sends] == [paragraph(1000, 'a'), '\U0001f3ae' * 600]


def test_send_dms_the_owner_once_without_mentions_or_embeds(dm, tmp_path):
    text = paragraph(1500, 'a') + '\n\n' + paragraph(1500, 'b')
    lines = run(text, tmp_path)
    assert [s['content'] for s in dm.sends] == [paragraph(1500, 'a'), paragraph(1500, 'b')]
    assert all(s['mentions'] is notes.NO_MENTIONS and s['suppress_embeds'] for s in dm.sends)
    assert len({s['nonce'] for s in dm.sends}) == 2
    assert lines[-1].startswith('part 2/2: sent as message 1002')
    record = json.loads((tmp_path / 'patch-notes.json').read_text())['versions']['0.4.0']
    assert [p['message_id'] for p in record['parts']] == ['1001', '1002']
    assert FakeClient.instances[-1].closed
    assert run(text, tmp_path) == ['0.4.0: all 2 parts were already sent']
    assert len(dm.sends) == 2 and len(FakeClient.instances) == 1  # No login for a finished version.


def test_the_same_version_with_other_text_is_refused_before_login(dm, tmp_path):
    run('first notes', tmp_path)
    with pytest.raises(notes.NotesRefused, match='different notes'):
        run('second notes', tmp_path)
    assert len(dm.sends) == 1 and len(FakeClient.instances) == 1
    run('second notes', tmp_path, version='0.4.1')
    assert len(dm.sends) == 2


def test_a_rerun_finishes_a_partial_send_without_repeating_parts(dm, tmp_path):
    text = paragraph(1500, 'a') + '\n\n' + paragraph(1500, 'b')
    dm.refused.add(2)
    with pytest.raises(RuntimeError, match='refused'):
        run(text, tmp_path)
    assert FakeClient.instances[-1].closed
    lines = run(text, tmp_path)
    assert lines[0] == 'part 1/2: already sent'
    assert [s['content'] for s in dm.sends] == [paragraph(1500, 'a')] + [paragraph(1500, 'b')] * 2
    assert [m.content for m in dm.messages] == [paragraph(1500, 'a'), paragraph(1500, 'b')]
    assert dm.sends[1]['nonce'] == dm.sends[2]['nonce']  # Discord dedupes a retry inside its window too.


def test_a_send_that_landed_but_was_never_confirmed_is_found_not_resent(dm, tmp_path):
    text = paragraph(1500, 'a') + '\n\n' + paragraph(1500, 'b')
    dm.lost.add(2)
    with pytest.raises(RuntimeError, match='lost'):
        run(text, tmp_path)
    run(text, tmp_path)
    assert len(dm.sends) == 2
    assert [m.content for m in dm.messages] == [paragraph(1500, 'a'), paragraph(1500, 'b')]
    record = json.loads((tmp_path / 'patch-notes.json').read_text())['versions']['0.4.0']
    assert [p['message_id'] for p in record['parts']] == ['1001', '1002']


def test_an_identical_part_from_an_earlier_release_is_not_mistaken_for_this_one(dm, tmp_path):
    run('**fri 0.4.0**\n\n**next**\n- same plan', tmp_path)
    run('**fri 0.4.1**\n\n**next**\n- same plan', tmp_path, version='0.4.1')
    assert [s['content'] for s in dm.sends][-1].startswith('**fri 0.4.1**')
    assert len(dm.messages) == 2


def test_recovery_looks_past_the_first_page_of_history(dm, tmp_path):
    text = paragraph(1500, 'a') + '\n\n' + paragraph(1500, 'b')
    dm.lost.add(2)
    with pytest.raises(RuntimeError, match='lost'):
        run(text, tmp_path)
    dm.messages[1:1] = [said(500+i, 'hm', OWNER) for i in range(150)]
    run(text, tmp_path)
    assert len(dm.sends) == 2
    record = json.loads((tmp_path / 'patch-notes.json').read_text())['versions']['0.4.0']
    assert [p['message_id'] for p in record['parts']] == ['1001', '1002']


@pytest.mark.parametrize('earlier', [None, paragraph(1500, 'a') + '\n\n' + paragraph(1500, 's')],
                         ids=['own-first-part', 'earlier-release'])
def test_a_part_never_delivered_is_not_confirmed_by_another_parts_message(dm, tmp_path, earlier):
    """Identical text is no proof: a part refused before Discord took it is sent on the rerun."""
    if earlier:
        run(earlier, tmp_path, version='0.3.9')
    before = len(dm.messages)
    text = paragraph(1500, 's') + '\n\n' + paragraph(1500, 's')
    dm.refused.add(len(dm.sends) + 2)
    with pytest.raises(RuntimeError, match='refused'):
        run(text, tmp_path)
    run(text, tmp_path)
    mine = dm.messages[before:]
    assert [m.content for m in mine] == [paragraph(1500, 's')] * 2
    record = json.loads((tmp_path / 'patch-notes.json').read_text())['versions']['0.4.0']
    assert [p['message_id'] for p in record['parts']] == [str(m.id) for m in mine]


def test_recovery_searches_from_now_when_the_attempt_time_is_ahead(dm, tmp_path):
    text = paragraph(1500, 'a')
    dm.lost.add(1)
    with pytest.raises(RuntimeError, match='lost'):
        run(text, tmp_path)
    path = tmp_path / 'patch-notes.json'
    record = json.loads(path.read_text())
    record['versions']['0.4.0']['parts'][0]['attempted_at'] += 3600  # A clock that stepped back since.
    path.write_text(json.dumps(record))
    run(text, tmp_path)
    assert len(dm.sends) == 1


def test_two_unfinished_releases_with_the_same_text_are_refused_not_guessed(dm, tmp_path):
    """0.3.9's part never reached Discord and 0.4.0's identical part did: the DM can't say whose it is."""
    text = paragraph(1500, 's')
    dm.refused.add(1)
    with pytest.raises(RuntimeError, match='refused'):
        run(text, tmp_path, version='0.3.9')
    dm.lost.add(2)
    with pytest.raises(RuntimeError, match='lost'):
        run(text, tmp_path)
    for version in ('0.3.9', '0.4.0'):
        with pytest.raises(notes.NotesRefused, match='never confirmed'):
            run(text, tmp_path, version=version)
    assert len(dm.sends) == 2 and len(FakeClient.instances) == 2  # Both refused before login.


def test_a_reply_naming_a_message_already_on_record_is_refused(dm, tmp_path, monkeypatch):
    run(paragraph(10, 'a'), tmp_path, version='0.3.9')
    earlier = dm.messages[0]
    async def same(*args, **kwargs):
        return earlier
    monkeypatch.setattr(dm, 'send', same)
    with pytest.raises(notes.NotesRefused, match='already gives another part'):
        run(paragraph(10, 'b'), tmp_path)
    notes.load(tmp_path / 'patch-notes.json')  # Still a sound record.


def test_a_part_hash_that_disagrees_with_the_notes_is_refused(dm, tmp_path):
    text = paragraph(1500, 'a')
    dm.lost.add(1)
    with pytest.raises(RuntimeError, match='lost'):
        run(text, tmp_path)
    path = tmp_path / 'patch-notes.json'
    record = json.loads(path.read_text())
    record['versions']['0.4.0']['parts'][0]['sha256'] = '0' * 64
    path.write_text(json.dumps(record))
    with pytest.raises(notes.NotesRefused, match='not a patch-notes record'):
        run(text, tmp_path)
    assert len(dm.sends) == 1


def test_a_damaged_record_or_a_concurrent_send_is_refused(dm, tmp_path):
    (tmp_path / 'patch-notes.json').write_text('[]')
    with pytest.raises(notes.NotesRefused, match='not a patch-notes record'):
        run('notes', tmp_path)
    (tmp_path / 'patch-notes.json').unlink()
    with notes.locked(tmp_path):
        with pytest.raises(notes.NotesRefused, match='another patch-notes send'):
            run('notes', tmp_path)
    assert dm.sends == []


H = '0' * 64
T = 1790000000  # A real past attempt time.
TRIED = {'attempted_at': T, 'sha256': H}
SENT = {**TRIED, 'message_id': '5', 'channel_id': '777'}


def record(*parts, more=None):
    return json.dumps({'versions': {'0.3.0': {'sha256': H, 'parts': list(parts)}, **(more or {})}})


@pytest.mark.parametrize('record', [
    pytest.param('{"versions": {"0.4.0": {}}}', id='empty-entry'),
    pytest.param('{"versions": {"0.4.0": null}}', id='null-entry'),
    pytest.param('{"versions": {"0.4.0": 7}}', id='number-entry'),
    pytest.param('{"versions": {"0.4.0": {"sha256": "x", "parts": [{}]}}}', id='bad-hash'),
    pytest.param('{"versions": "x"}', id='versions-string'),
    pytest.param('{"versions": {', id='truncated'),
    pytest.param(b'\xff', id='not-utf8'),
    pytest.param('[' * 100000 + ']' * 100000, id='nested'),
    pytest.param('{"versions": {"0.3.0": {"sha256": "' + '0' * 64 + '", "parts": [{}]}}, "versions": {}}', id='duplicate-key'),
    pytest.param(record(), id='no-parts'),
    pytest.param(record({**SENT, 'message_id': 5}), id='number-id'),
    pytest.param(record({**SENT, 'message_id': '0'}), id='zero-id'),
    pytest.param(record({'message_id': '0'}), id='lone-zero-id'),
    pytest.param(record({'channel_id': '777'}), id='lone-channel'),
    pytest.param(record({'message_id': '5', 'channel_id': '777'}), id='sent-never-tried'),
    pytest.param(record({'attempted_at': T}), id='tried-no-hash'),
    pytest.param(record({**TRIED, 'sha256': 'x'}), id='tried-bad-hash'),
    pytest.param(record({**TRIED, 'attempted_at': 1e100}), id='far-future'),
    pytest.param(record({**TRIED, 'attempted_at': float('nan')}), id='nan'),
    pytest.param(record({**TRIED, 'attempted_at': -1}), id='negative'),
    pytest.param(record({**TRIED, 'attempted_at': 0}), id='zero-time'),
    pytest.param(record({**TRIED, 'attempted_at': 1e-9}), id='epoch'),
    pytest.param(record({**TRIED, 'attempted_at': True}), id='bool-time'),
    pytest.param(record({}, SENT), id='sent-after-unsent'),
    pytest.param(record(TRIED, SENT), id='sent-after-tried'),
    pytest.param(record(TRIED, TRIED), id='two-tried'),
    pytest.param(record(SENT, more={'0.3.1': {'sha256': H, 'parts': [SENT]}}), id='one-message-two-parts'),
])
def test_a_damaged_entry_is_refused_with_exit_2_before_login(monkeypatch, capsys, tmp_path, dm, record):
    """Truncated or nested JSON, a malformed entry or part, or a state send() never writes, anywhere in the record."""
    path = tmp_path / 'patch-notes.json'
    path.write_bytes(record if isinstance(record, bytes) else record.encode())
    monkeypatch.setenv('DISCORD_TOKEN', 'secret-token')
    monkeypatch.setenv('GENE_OWNER_ID', str(OWNER))
    monkeypatch.setenv('FRI_DATA', str(tmp_path))
    monkeypatch.setattr('sys.stdin', types.SimpleNamespace(buffer=io.BytesIO(b'notes')))
    assert notes.main(['send', '0.4.0']) == 2
    assert 'not a patch-notes record' in capsys.readouterr().err
    assert dm.sends == [] and path.read_bytes() == (record if isinstance(record, bytes) else record.encode())


def test_cli_previews_without_credentials_and_checks_the_version(monkeypatch, capsys):
    monkeypatch.delenv('DISCORD_TOKEN', raising=False)
    monkeypatch.setattr('sys.stdin', types.SimpleNamespace(buffer=io.BytesIO('**fri 0.4.0**\n\nhi'.encode())))
    assert notes.main(['preview', '0.4.0']) == 0
    assert '--- part 1 (17 chars)' in capsys.readouterr().out
    for bad in ('v0.4.0', '0.4', '00.4.0', '0.4.0-rc1'):
        with pytest.raises(SystemExit) as exit:
            notes.main(['send', bad])
        assert exit.value.code == 2


def test_cli_reports_a_discord_or_network_failure_with_exit_1_and_closes_the_client(monkeypatch, capsys, tmp_path, dm):
    monkeypatch.setenv('DISCORD_TOKEN', 'secret-token')
    monkeypatch.setenv('GENE_OWNER_ID', str(OWNER))
    monkeypatch.setenv('FRI_DATA', str(tmp_path))
    def cli():
        monkeypatch.setattr('sys.stdin', types.SimpleNamespace(buffer=io.BytesIO(b'notes')))
        return notes.main(['send', '0.4.0'])
    async def forbidden(*args, **kwargs):
        raise discord.Forbidden(types.SimpleNamespace(status=403, reason='Forbidden'),
                                {'code': 50007, 'message': 'Cannot send messages to this user'})
    monkeypatch.setattr(dm, 'send', forbidden)
    assert cli() == 1
    assert 'failed: Forbidden: 403 Forbidden (error code: 50007)' in capsys.readouterr().err
    FakeClient.login_error = discord.LoginFailure('Improper token has been passed.')
    assert cli() == 1
    FakeClient.login_error = aiohttp.ClientConnectionError('cannot connect')
    assert cli() == 1
    assert 'failed: ClientConnectionError: cannot connect' in capsys.readouterr().err
    FakeClient.login_error = json.JSONDecodeError('Expecting value', '{', 1)  # A malformed Discord reply.
    assert cli() == 1
    assert len(FakeClient.instances) == 4 and all(c.closed for c in FakeClient.instances)


@pytest.mark.parametrize('token, owner', [(None, str(OWNER)), ('secret-token', None), ('secret-token', 'me')])
def test_cli_refuses_a_send_without_its_settings(monkeypatch, capsys, tmp_path, dm, token, owner):
    for key, value in (('DISCORD_TOKEN', token), ('GENE_OWNER_ID', owner)):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    monkeypatch.setenv('FRI_DATA', str(tmp_path))
    monkeypatch.setattr('sys.stdin', types.SimpleNamespace(buffer=io.BytesIO(b'notes')))
    assert notes.main(['send', '0.4.0']) == 2
    assert 'refused: DISCORD_TOKEN and GENE_OWNER_ID' in capsys.readouterr().err
    assert FakeClient.instances == []


def test_cli_reports_a_refusal_with_exit_code_2(monkeypatch, capsys, tmp_path, dm):
    monkeypatch.setenv('DISCORD_TOKEN', 'secret-token')
    monkeypatch.setenv('GENE_OWNER_ID', str(OWNER))
    monkeypatch.setenv('FRI_DATA', str(tmp_path))
    monkeypatch.setattr('sys.stdin', types.SimpleNamespace(buffer=io.BytesIO(b'  ')))
    assert notes.main(['send', '0.4.0']) == 2
    assert 'refused: the notes are empty' in capsys.readouterr().err
    monkeypatch.setattr('sys.stdin', types.SimpleNamespace(buffer=io.BytesIO(b'\xff\xfe notes')))
    assert notes.main(['preview', '0.4.0']) == 2
    assert 'refused: the notes are not UTF-8' in capsys.readouterr().err
    monkeypatch.setattr('sys.stdin', types.SimpleNamespace(buffer=io.BytesIO(b'**fri 0.4.0**')))
    assert notes.main(['send', '0.4.0']) == 0
    assert dm.sends[0]['content'] == '**fri 0.4.0**'
    assert 'secret-token' not in capsys.readouterr().out
