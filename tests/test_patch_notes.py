import asyncio
import io
import json
import types

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
        if index in self.refused:
            raise RuntimeError('discord refused')
        message = types.SimpleNamespace(id=1000+index, content=content, author=types.SimpleNamespace(id=BOT))
        self.messages.append(message)
        if index in self.lost:
            raise RuntimeError('reply lost')
        return message

    async def history(self, *, limit, after, oldest_first):
        assert oldest_first and after is not None
        for message in self.messages[:limit]:
            yield message


class FakeClient:
    instances = []
    dm = None

    def __init__(self, *, intents):
        self.user = None
        self.closed = False
        FakeClient.instances.append(self)

    async def login(self, token):
        assert token == 'secret-token'
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


@pytest.mark.parametrize('text, reason', [
    ('  \n\n ', 'empty'),
    (paragraph(2001), 'over 2000'),
    ('\n\n'.join(paragraph(1500, t) for t in 'abcde'), 'at most 4'),
])
def test_parts_refuses_notes_it_cant_send_whole(text, reason):
    with pytest.raises(notes.NotesRefused, match=reason):
        notes.parts(text)


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


def test_a_damaged_record_or_a_concurrent_send_is_refused(dm, tmp_path):
    (tmp_path / 'patch-notes.json').write_text('[]')
    with pytest.raises(notes.NotesRefused, match='not a patch-notes record'):
        run('notes', tmp_path)
    (tmp_path / 'patch-notes.json').unlink()
    with notes.locked(tmp_path):
        with pytest.raises(notes.NotesRefused, match='another patch-notes send'):
            run('notes', tmp_path)
    assert dm.sends == []


def test_cli_previews_without_credentials_and_checks_the_version(monkeypatch, capsys):
    monkeypatch.delenv('DISCORD_TOKEN', raising=False)
    monkeypatch.setattr('sys.stdin', types.SimpleNamespace(buffer=io.BytesIO('**fri 0.4.0**\n\nhi'.encode())))
    assert notes.main(['preview', '0.4.0']) == 0
    assert '--- part 1 (17 chars)' in capsys.readouterr().out
    for bad in ('v0.4.0', '0.4', '00.4.0', '0.4.0-rc1'):
        with pytest.raises(SystemExit) as exit:
            notes.main(['send', bad])
        assert exit.value.code == 2


def test_cli_reports_a_refusal_with_exit_code_2(monkeypatch, capsys, tmp_path, dm):
    monkeypatch.setenv('DISCORD_TOKEN', 'secret-token')
    monkeypatch.setenv('GENE_OWNER_ID', str(OWNER))
    monkeypatch.setenv('FRI_DATA', str(tmp_path))
    monkeypatch.setattr('sys.stdin', types.SimpleNamespace(buffer=io.BytesIO(b'  ')))
    assert notes.main(['send', '0.4.0']) == 2
    assert 'refused: the notes are empty' in capsys.readouterr().err
    monkeypatch.setattr('sys.stdin', types.SimpleNamespace(buffer=io.BytesIO(b'**fri 0.4.0**')))
    assert notes.main(['send', '0.4.0']) == 0
    assert dm.sends[0]['content'] == '**fri 0.4.0**'
    assert 'secret-token' not in capsys.readouterr().out
