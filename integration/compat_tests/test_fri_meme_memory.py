"""Fri remembers, for a few days, what it reacted to in a meme channel and what it saw there."""

import asyncio
import datetime as dt
import json
from types import SimpleNamespace

import aiohttp
import discord
import pytest

from compat_tests.test_fri_learning import learning as learning  # noqa: F401
from compat_tests.test_fri_looking import picture, read_harness
from compat_tests.test_fri_memes import attachment
from compat_tests.test_fri_memes import harness as harness  # noqa: F401
from fri.fri_actions import Actions
from fri.fri_chat import INSTRUCTIONS
from fri.fri_memes import INSTRUCTION, NOTE, Memes, carried, reacted


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


def test_a_note_shows_only_while_the_author_and_the_post_are_as_they_were():
    edited = dt.datetime(2026, 10, 25, tzinfo=dt.timezone.utc)
    now = __import__('time').time()
    notes = {5: {'author': '30', 'revision': 2, 'emoji': '🗿', 'note': 'gigachad at a desk', 'edited': None, 'at': now,
                 'media': []},
             6: {'author': '30', 'revision': 2, 'emoji': '💀', 'note': 'a cat', 'edited': edited.isoformat(), 'at': now,
                 'media': []}}
    post = SimpleNamespace(id=5, author=SimpleNamespace(id=30), edited_at=None)
    assert reacted(notes, post, {'revision': 2}) == {'fri_reacted': {'emoji': '🗿', 'saw': 'gigachad at a desk'}}
    assert reacted(notes, SimpleNamespace(id=6, author=SimpleNamespace(id=30), edited_at=edited), {'revision': 2})
    assert not reacted(notes, post, {'revision': 3})  # They changed their privacy since.
    assert not reacted(notes, SimpleNamespace(id=5, author=SimpleNamespace(id=30), edited_at=edited), {'revision': 2})
    assert not reacted(notes, SimpleNamespace(id=5, author=SimpleNamespace(id=31), edited_at=None), {'revision': 2})
    assert not reacted(notes, SimpleNamespace(id=7, author=SimpleNamespace(id=30), edited_at=None), {'revision': 2})
    assert not reacted(notes, SimpleNamespace(id=5, author=SimpleNamespace(id=30), edited_at=None,  # Its image changed.
                                              attachments=[SimpleNamespace(url='https://cdn.test/new.png')]), {'revision': 2})


def test_a_renewed_cdn_signature_keeps_a_meme_note():
    """Discord renews an attachment link's signature on refetch; the same image keeps its note."""
    now = __import__('time').time()
    url = 'https://cdn.discordapp.com/attachments/1/2/meme.png?ex=1&is=2&hm=aa'
    post = SimpleNamespace(id=5, author=SimpleNamespace(id=30), edited_at=None,
                           attachments=[SimpleNamespace(id=2, url=url)])
    notes = {5: {'author': '30', 'revision': 2, 'emoji': '🗿', 'note': 'a cat', 'edited': None, 'at': now,
                 'media': carried(post)}}
    renewed = SimpleNamespace(**{**vars(post), 'attachments': [SimpleNamespace(id=2, url=url.replace('hm=aa', 'hm=bb'))]})
    assert reacted(notes, renewed, {'revision': 2})
    replaced = SimpleNamespace(**{**vars(post), 'attachments': [SimpleNamespace(id=3, url=url)]})
    assert not reacted(notes, replaced, {'revision': 2})


def test_a_read_shows_fris_own_reaction_and_what_it_saw(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channel, rows, post = read_harness(monkeypatch, tmp_path)
        meme = post('', age=600, attachments=[picture(1, 'gigachad.png')])
        other = post('lol', user=40, age=300)
        agent.ensure_memes = lambda: SimpleNamespace(notes=lambda: {meme.id: {
            'author': '30', 'revision': 0, 'emoji': '🗿', 'note': 'gigachad at a desk', 'edited': None,
            'at': __import__('time').time(), 'media': carried(meme)}})
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
    assert "kept in memory for up to\na few days" in INSTRUCTION and "isn't posted or retained" not in INSTRUCTION
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


def test_an_opt_out_through_gene_drops_the_note_even_with_meme_checks_off(harness):  # noqa: F811
    async def scenario():
        h = harness
        h.add(attachments=[attachment()])
        await h.agent.memes.check()
        assert h.agent.memes.notes()
        h.scheduler.update({'meme_enabled': False}, 2, '10')
        h.agent.state = h.scheduler.state()
        h.store.personal_control('11', 'disable')  # Not through Fri, so no local forget().
        await h.agent.memes.verify(now=h.clock[0]+601)  # What each heartbeat runs, every ten minutes.
        assert h.agent.memes.notes() == {}
    asyncio.run(scenario())


def test_a_note_on_a_deleted_or_edited_post_goes_at_the_next_check_even_with_meme_checks_off(harness):  # noqa: F811
    """A read can't return a deleted post, so nothing else would notice it: the ten-minute check refetches."""
    import discord

    async def scenario():
        h = harness
        deleted, edited, kept, unread = (h.add(mid=mid, attachments=[attachment()]) for mid in (100, 101, 102, 103))
        for post in (deleted, edited, kept, unread):
            h.agent.memes.remember(post, '🗿', 'a desk', 0)
        h.scheduler.update({'meme_enabled': False}, 2, '10')
        h.agent.state = h.scheduler.state()
        h.rows.remove(deleted)
        edited.edited_at = dt.datetime.fromtimestamp(h.clock[0]+1, dt.timezone.utc)

        async def fetch(mid):
            if mid == unread.id:
                raise discord.HTTPException(SimpleNamespace(status=503, reason='down'), 'down')
            if not any(m.id == mid for m in h.rows):
                raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'Unknown Message')
            return next(m for m in h.rows if m.id == mid)
        h.channel.fetch_message.side_effect = fetch
        await h.agent.memes.verify(now=h.clock[0]+601)
        # Deleted or edited: dropped. Discord unreachable for now: kept for next time. Unchanged: kept.
        assert set(h.agent.memes.notes()) == {kept.id, unread.id}
    asyncio.run(scenario())


@pytest.mark.parametrize('error', [
    aiohttp.ClientConnectionError('connection closed'), aiohttp.ServerDisconnectedError(), TimeoutError('timed out'),
    ConnectionResetError(54, 'connection reset'), discord.RateLimited(40),
    discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'Missing Access'),
    discord.HTTPException(SimpleNamespace(status=503, reason='down'), 'down'),
])
def test_however_a_refetch_fails_every_notes_privacy_check_and_the_other_reads_still_run(tmp_path, error):
    """An opted-out author's note goes at the next check whatever Discord does, and one post Discord can't
    show keeps only its own note: the next post is still read."""
    import time

    async def scenario():
        async def profile(channel, user):
            return {'enabled': user not in (11, 12), 'revision': 0}  # 11 and 12 opted out outside Fri.
        notes = ((1, '11'), (2, '12'), (3, '13'), (4, '14'))
        read = []

        async def fetch(mid):
            read.append(mid)
            if mid == 4:
                raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'Unknown Message')
            raise error
        fri = SimpleNamespace(data=tmp_path, get_partial_messageable=lambda _: SimpleNamespace(fetch_message=fetch))
        memes = Memes(SimpleNamespace(fri=fri, profile=profile))
        now = time.time()
        for mid, author in notes:
            memes.noted[mid] = {'at': now, 'channel': 3, 'author': author, 'revision': 0, 'emoji': '🗿',
                                'note': 'a desk', 'edited': None, 'media': []}
        await memes.verify(now=now+601)
        # Opted out: dropped, read or not. Unreadable for now: kept for next time. Deleted: dropped.
        assert set(memes.noted) == {3} and 4 in read
    asyncio.run(scenario())


def test_a_note_that_expires_during_a_read_is_not_shown():
    import time

    post = SimpleNamespace(id=5, author=SimpleNamespace(id=30), edited_at=None)
    note = {'author': '30', 'revision': 2, 'emoji': '🗿', 'note': 'a desk', 'edited': None,
            'at': time.time()-3*86400-1}
    stale = []
    assert reacted({5: note}, post, {'revision': 2}, stale) == {} and stale == [5]


def test_notes_never_reach_the_state_file_so_a_restart_or_rollback_keeps_none(harness):  # noqa: F811
    async def scenario():
        h = harness
        h.agent.run.return_value = ({'reaction': '🗿', 'confidence': 'high', 'observation': 'gigachad at a desk'},
                                    {}, 'completed')
        m = h.add(attachments=[attachment()])
        await h.agent.memes.check()
        assert h.agent.memes.notes()[m.id]['note'] == 'gigachad at a desk'
        entry = json.loads(h.agent.memes.path.read_text())['entries'][str(m.id)]
        assert 'gigachad' not in h.agent.memes.path.read_text() and not {'author', 'note', 'emoji'} & entry.keys()
        assert Memes(h.agent).notes() == {}  # A restarted Fri, or an older one, finds nothing to keep or erase.
    asyncio.run(scenario())


def test_the_heartbeat_expires_notes_even_when_gene_is_down(monkeypatch, tmp_path):
    import time

    from fri.fri_agent import Agent
    from fri.fri_errors import ControllerUnavailable

    async def scenario():
        async def down(*_, **__):
            raise ControllerUnavailable('down')
        agent = Agent(SimpleNamespace(data=tmp_path, api=down, guild_id=123, owner_id=10, workshop_id=456))
        agent.ensure_memes().noted[5] = {'at': time.time()-3*86400-1, 'channel': 3, 'author': '11', 'revision': 0,
                                         'emoji': '🗿', 'note': 'a desk', 'edited': None}
        await agent.heartbeat.coro(agent)
        assert agent.heartbeat_status == 'deferred' and agent.memes.noted == {}
    asyncio.run(scenario())


def test_a_refetch_held_in_a_rate_limit_never_holds_the_heartbeat_or_a_later_opt_out(monkeypatch, tmp_path):
    """Discord's rate-limit wait can last minutes: every stage still runs, reminders included, while the read
    waits uncancelled, no second pass starts beside it, and an opt-out meanwhile still goes at its check."""
    import time
    from unittest.mock import AsyncMock

    from fri import fri_memes
    from fri.fri_agent import Agent
    monkeypatch.setattr(fri_memes, 'REFETCH_WAIT', 0.05)

    async def scenario():
        released, read = asyncio.Event(), []

        def post(mid, author):
            return SimpleNamespace(id=mid, channel=SimpleNamespace(id=3), author=SimpleNamespace(id=author),
                                   edited_at=None, attachments=[], embeds=[])

        async def fetch(mid):
            read.append(mid)
            await released.wait()
            return post(mid, 11 if mid == 100 else 12)
        fri = SimpleNamespace(data=tmp_path, guild_id=123, owner_id=10, workshop_id=456, capabilities=set(),
                              get_partial_messageable=lambda _: SimpleNamespace(fetch_message=fetch))
        agent = Agent(fri)
        fri.api = AsyncMock(return_value=agent.state)
        agent.state['config']['meme_enabled'] = False
        allowed = {11: True, 12: True}

        async def profile(channel, user):
            return {'enabled': allowed[user], 'revision': 0}
        agent.profile = profile
        agent.stage = AsyncMock(return_value=True)
        memes = agent.ensure_memes()
        for mid, author in ((100, 11), (101, 12)):
            memes.remember(post(mid, author), '🗿', 'a desk', 0)
        await asyncio.wait_for(agent.heartbeat.coro(agent), 1)
        assert agent.stage.await_count == 11 and read == [100]  # Every stage ran; the read still waits.
        allowed[12] = False  # Opted out outside Fri while Discord still holds the read.
        await asyncio.wait_for(memes.verify(now=time.time()+601), 1)
        assert set(memes.noted) == {100} and read == [100]  # Gone at its check, and no second pass.
        released.set()
        await asyncio.sleep(0.01)
        # Never cancelled: the read finishes, the unchanged post keeps its note, the opted-out one stays gone.
        assert read == [100] and set(memes.noted) == {100}
    asyncio.run(scenario())


def test_one_channel_gene_refuses_never_stops_the_other_notes_privacy_check(tmp_path):
    import time

    from fri.fri_errors import ControllerRejected, ControllerUnavailable

    async def scenario():
        async def profile(channel, user):
            if channel == 3:
                raise ControllerRejected('channel out of scope', status=403)  # A meme channel removed.
            if channel == 4:
                raise ControllerUnavailable('down')
            return {'enabled': user != 12, 'revision': 0}
        notes = ((1, 3, '11'), (2, 1, '12'), (3, 4, '13'), (4, 1, '14'))
        posts = {mid: SimpleNamespace(id=mid, author=SimpleNamespace(id=int(author)), edited_at=None)
                 for mid, _, author in notes}  # Each post still there, unchanged.

        async def fetch(mid):
            return posts[mid]
        fri = SimpleNamespace(data=tmp_path, get_partial_messageable=lambda _: SimpleNamespace(fetch_message=fetch))
        memes = Memes(SimpleNamespace(fri=fri, profile=profile))
        now = time.time()
        for mid, channel, author in notes:
            memes.noted[mid] = {'at': now, 'channel': channel, 'author': author, 'revision': 0, 'emoji': '🗿',
                                'note': 'a desk', 'edited': None, 'media': []}
        await memes.verify(now=now+601)
        # Refused for good: dropped. Opted out: dropped. Unreachable for now: kept for next time. Fine: kept.
        assert set(memes.noted) == {3, 4}
    asyncio.run(scenario())


def test_a_note_on_a_meme_whose_image_is_gone_stays_out(monkeypatch, tmp_path):
    """An embed can be suppressed without an edit time: what Fri saw in it doesn't reach the model after."""
    import copy

    import discord

    from compat_tests.test_fri_member_profiles import DM
    from compat_tests.test_fri_read_fences import read_world
    seen = 'a handwritten phone number in the old image'

    async def scenario():
        w, posts, rows, sent, _, _ = read_world(monkeypatch, tmp_path, ['https://example.test/meme.png'])
        post = posts[0]
        post.embeds = [discord.Embed.from_dict({'type': 'image', 'image': {
            'proxy_url': 'https://media.discordapp.net/external/abc/https/example.test/meme.png'}})]
        w.fri.agent.ensure_memes().remember(post, '🗿', seen, 0)
        changed = copy.copy(post)
        changed.embeds = []  # Same text and edit time; the image is gone.
        rows[post.id] = changed
        text = 'fri what is in general'
        try:
            await w.fri.talk(123, DM, 20, text, 'private', event=w.message(DM, text))
            assert len(sent) == 2 and seen not in json.dumps(sent[-1])
            assert post.id not in w.fri.agent.ensure_memes().notes()
        finally:
            await w.fri.close()
    asyncio.run(scenario())
