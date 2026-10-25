"""Fri remembers, for a few days, what it reacted to in a meme channel and what it saw there."""

import asyncio
import datetime as dt
import json
from types import SimpleNamespace

from compat_tests.test_fri_learning import learning as learning  # noqa: F401
from compat_tests.test_fri_looking import picture, read_harness
from compat_tests.test_fri_memes import attachment
from compat_tests.test_fri_memes import harness as harness  # noqa: F401
from fri.fri_actions import Actions
from fri.fri_chat import INSTRUCTIONS
from fri.fri_memes import INSTRUCTION, NOTE, Memes, reacted


def test_a_reaction_keeps_what_fri_saw_with_whose_post_it_was(harness):  # noqa: F811
    async def scenario():
        h = harness
        h.agent.run.return_value = ({'reaction': '🗿', 'confidence': 'high', 'observation': 'gigachad at a desk '*30},
                                    {}, 'completed')
        m = h.add(attachments=[attachment()])
        await h.agent.memes.check()
        m.add_reaction.assert_awaited_once_with('🗿')
        note = h.agent.memes.notes()[m.id]
        assert note['author'] == '11' and note['revision'] == 0 and note['emoji'] == '🗿' and note['edited'] is None
        assert len(note['note']) <= NOTE and note['note'].startswith('gigachad at a desk')
        assert 'a dry joke' not in h.agent.memes.path.read_text()  # Never the caption itself.
    asyncio.run(scenario())


def test_staying_quiet_keeps_nothing(harness):  # noqa: F811
    async def scenario():
        h = harness
        h.agent.run.return_value = ({'reaction': 'quiet', 'confidence': 'high', 'observation': 'a cat'}, {}, 'completed')
        h.add(attachments=[attachment()])
        await h.agent.memes.check()
        assert h.agent.memes.notes() == {} and 'a cat' not in h.agent.memes.path.read_text()
    asyncio.run(scenario())


def test_forgetting_a_member_drops_what_fri_saw_in_their_memes(harness):  # noqa: F811
    async def scenario():
        h = harness
        h.add(mid=100, attachments=[attachment()])
        await h.agent.memes.check()
        h.clock[0] += 3601  # The next check, past the hourly reaction gap.
        theirs = h.add(mid=101, attachments=[attachment()], at=h.clock[0]-5)
        theirs.author = SimpleNamespace(id=12, bot=False)
        with h.agent.memes.state() as state:
            state['next_check'] = 0
        await h.agent.memes.check()
        assert set(h.agent.memes.notes()) == {100, 101}
        h.agent.forget(11, erased=True)
        assert set(h.agent.memes.notes()) == {101}
        entry = json.loads(h.agent.memes.path.read_text())['entries']['100']
        assert entry['status'] == 'reacted' and not {'author', 'note', 'emoji'} & entry.keys()  # Never rechecked.
    asyncio.run(scenario())


def test_forgetting_without_any_meme_state_writes_nothing(tmp_path):
    agent = SimpleNamespace(fri=SimpleNamespace(data=tmp_path/'fri'))
    Memes(agent).forget(11)
    assert not (tmp_path/'fri').exists()


def test_a_note_shows_only_while_the_author_and_the_post_are_as_they_were():
    edited = dt.datetime(2026, 10, 25, tzinfo=dt.timezone.utc)
    notes = {5: {'author': '30', 'revision': 2, 'emoji': '🗿', 'note': 'gigachad at a desk', 'edited': None},
             6: {'author': '30', 'revision': 2, 'emoji': '💀', 'note': 'a cat', 'edited': edited.isoformat()}}
    post = SimpleNamespace(id=5, author=SimpleNamespace(id=30), edited_at=None)
    assert reacted(notes, post, {'revision': 2}) == {'fri_reacted': {'emoji': '🗿', 'saw': 'gigachad at a desk'}}
    assert reacted(notes, SimpleNamespace(id=6, author=SimpleNamespace(id=30), edited_at=edited), {'revision': 2})
    assert not reacted(notes, post, {'revision': 3})  # They changed their privacy since.
    assert not reacted(notes, SimpleNamespace(id=5, author=SimpleNamespace(id=30), edited_at=edited), {'revision': 2})
    assert not reacted(notes, SimpleNamespace(id=5, author=SimpleNamespace(id=31), edited_at=None), {'revision': 2})
    assert not reacted(notes, SimpleNamespace(id=7, author=SimpleNamespace(id=30), edited_at=None), {'revision': 2})


def test_a_read_shows_fris_own_reaction_and_what_it_saw(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channel, rows, post = read_harness(monkeypatch, tmp_path)
        meme = post('', age=600, attachments=[picture(1, 'gigachad.png')])
        other = post('lol', user=40, age=300)
        agent.ensure_memes = lambda: SimpleNamespace(notes=lambda: {meme.id: {
            'author': '30', 'revision': 0, 'emoji': '🗿', 'note': 'gigachad at a desk', 'edited': None}})
        actions = Actions(agent, 20, 999, False)
        actions.event = SimpleNamespace(id=1, channel=SimpleNamespace(id=999))
        try:
            first, second = (await actions.execute({'kind': 'read_recent', 'channel_id': '1', 'window': 'recent'}))['messages']
            assert first['fri_reacted'] == {'emoji': '🗿', 'saw': 'gigachad at a desk'} and 'fri_reacted' not in second
            assert other.id != meme.id
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_the_models_are_told_the_truth_about_what_is_kept():
    assert "it's kept for a few days so" in INSTRUCTION and "isn't posted or retained" not in INSTRUCTION
    assert 'fri_reacted' in INSTRUCTIONS


def test_a_note_expires_after_three_days_even_with_meme_checks_off(harness):  # noqa: F811
    async def scenario():
        h = harness
        post = h.add(attachments=[attachment()])
        await h.agent.memes.check()
        assert post.id in h.agent.memes.notes()
        h.scheduler.update({'meme_enabled': False}, 2, '10')
        h.agent.state = h.scheduler.state()
        h.clock[0] += 4*86400
        assert h.agent.memes.notes() == {}  # Hidden at once,
        h.agent.memes.expire()  # and gone from disk on the next heartbeat, which expires notes every tick.
        assert 'A joke' not in h.agent.memes.path.read_text()
    asyncio.run(scenario())


def test_a_note_on_an_edited_post_or_a_changed_author_is_dropped_when_read(harness, monkeypatch, tmp_path):  # noqa: F811
    async def scenario():
        h = harness
        post = h.add(attachments=[attachment()])
        await h.agent.memes.check()
        stale = []
        post.edited_at = dt.datetime.fromtimestamp(h.clock[0]+1, dt.timezone.utc)
        assert reacted(h.agent.memes.notes(), post, await h.agent.profile(3, 11), stale) == {} and stale == [post.id]
        h.agent.memes.drop(stale)
        assert h.agent.memes.notes() == {} and 'A joke' not in h.agent.memes.path.read_text()
    asyncio.run(scenario())


def test_an_erasure_while_the_reaction_sends_is_not_written_back(harness):  # noqa: F811
    async def scenario():
        h = harness
        post = h.add(attachments=[attachment()])

        async def erase(_emoji):  # They erase while Discord takes the reaction.
            h.store.personal_control('11', 'disable')
            h.agent.forget(11, erased=True)
        post.add_reaction.side_effect = erase
        await h.agent.memes.check()
        post.add_reaction.assert_awaited_once()
        assert h.agent.memes.notes() == {} and 'A joke' not in h.agent.memes.path.read_text()
    asyncio.run(scenario())


def test_forgetting_still_clears_everything_else_when_the_meme_state_is_broken(tmp_path):
    from fri.fri_agent import Agent

    for broken in ('[]', 'null', '"broken"', '{not json'):
        agent = Agent(SimpleNamespace(data=tmp_path))
        agent.pending[(3, 11)] = {'request': 'something'}
        agent.recent[3].append({'user_id': '11', 'text': 'something'})
        (tmp_path/'meme-attention.json').write_text(broken)
        agent.forget(11, erased=True)
        assert (3, 11) not in agent.pending and not list(agent.recent[3])


def test_a_note_missing_a_field_is_never_shown(tmp_path):
    agent = SimpleNamespace(fri=SimpleNamespace(data=tmp_path))
    memes = Memes(agent)
    with memes.state() as state:
        state['entries']['100'] = {'at': __import__('time').time(), 'status': 'reacted', 'channel': '3',
                                   'author': '11', 'revision': 0, 'note': 'A joke', 'edited': None}
    assert memes.notes() == {}
