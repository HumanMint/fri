"""A kept member summary is rechecked against what it drew on, rebuilt or not (fri #349).

A rebuild reads a summary's messages again from Discord (fri #333), but needs new exchanges. So a summary whose
exchanges aged out was never checked again: one of its messages deleted where Fri never heard of it kept reaching
other channels until the summary expired, 30 days on. Fri now claims a few kept summaries at a time from a controller
offering summary-rechecks, reads their messages again (no model call), and says how it went: one gone, or edited
after its exchange was saved, is withdrawn as its event would have withdrawn it, and the summary goes; one Discord
can't vouch for now is held back until it can. The first claim after Fri starts holds every summary back until it's
rechecked. Without that controller, Fri uses no summary at all.
"""
import asyncio
import datetime as dt
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from compat_tests.test_fri_heartbeat import STATE
from compat_tests.test_fri_member_profiles import GAMING, GENERAL, model_context, run, world
from compat_tests.test_fri_member_summaries import settled, summarizer
from fri.fri_agent import Agent
from fri.fri_member_summary import build

SAID = 'they planned the lavender bicycle thing'  # The kept summary, naming what one message said.
WORDS = ('fri raid tonight?', 'fri which build for it', 'fri the lavender bicycle plan stays between us')
RECHECKS = 'summary-rechecks'


def rechecks(w):
    if RECHECKS not in w.fri.capabilities:
        pytest.skip('needs a controller offering '+RECHECKS)


async def recheck(agent):
    """A recheck pass, as the heartbeat runs one (nothing, before fri #349)."""
    import fri.fri_member_summary as summaries
    passing = getattr(summaries, 'recheck', None)
    result = await passing(agent) if passing else None
    await asyncio.gather(*agent.saved_turns.tasks)
    return result


async def kept(monkeypatch, w):
    """Member 20's three exchanges in #general, summarized; their messages."""
    rechecks(w)
    events = []
    for text in WORDS:
        event = w.message(GENERAL, text)
        await w.say(GENERAL, text, event=event)
        events.append(event)
    settled(w)
    summarizer(monkeypatch, w, reply=SAID)
    assert await build(w.fri.agent) == {'kept': True}
    return events


def aged(w):
    """A week and more on: its exchanges pruned as any context read prunes them, so nothing rebuilds it."""
    with w.store.connection() as db:
        db.execute('UPDATE chat_turns SET at=at-?', (8*86400,))
    assert not w.store.chat_context('123', f'public:{GENERAL}', '20')['turns']


def gene_later(monkeypatch, seconds):
    """The controller's summary clock `seconds` on, and nothing else's."""
    import gene.fri_member_summary as summaries
    monkeypatch.setattr(summaries, 'time', SimpleNamespace(time=lambda: time.time()+seconds,
                                                           monotonic=time.monotonic))


async def asked(w, where=GAMING):
    """What Fri's model gets of member 20's summary in another channel."""
    await w.say(where, 'fri what were we on about')
    return model_context(w).get('member_summary')


def recorded(w):
    with w.store.connection() as db:
        return json.loads(db.execute('SELECT messages FROM fri_member_summary_sources').fetchone()[0])


def down(w, channel=GENERAL):
    """Discord fails reading that channel's messages; returns what undoes it."""
    item = w.fri.get_channel(channel)
    real = item.fetch_message

    async def failing(mid):
        raise discord.HTTPException(SimpleNamespace(status=503, reason='Service Unavailable'), 'unavailable')
    item.fetch_message = failing
    return lambda: setattr(item, 'fetch_message', real)


@pytest.mark.parametrize('when', ['after a restart', 'a day later'])
def test_the_repro_a_message_deleted_unheard_after_its_exchanges_aged_out_takes_the_summary(monkeypatch, tmp_path,
                                                                                            when):
    """The issue: a summary carrying the lavender message, its exchanges past a week and pruned, the message deleted in
    Discord with no event reaching Fri. Nothing rebuilds it, so it reached the next request in another channel until
    it expired. A recheck reads the message again, withdraws it, and the summary goes."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            events = await kept(monkeypatch, w)
            agent = w.fri.agent
            aged(w)
            del w.posted[events[2].id]  # In Discord alone.
            assert await build(agent) is None  # Nothing new: no claim, no check.
            if when == 'a day later':
                agent.summaries_rechecked = True  # This process rechecked them already.
                gene_later(monkeypatch, 13*3600)
            await recheck(agent)
            assert await asked(w) is None
        finally:
            await w.fri.close()
    run(scenario)


@pytest.mark.parametrize('edit', ['after its exchange was saved', 'before it was saved'])
def test_an_edit_takes_the_summary_only_once_it_came_after_the_save(monkeypatch, tmp_path, edit):
    """As a build reads one (Sol round 1 of #348, B3): an edit before the save may be what was summarized, so it stays."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            events = await kept(monkeypatch, w)
            aged(w)
            saved_at = next(m['saved_at'] for m in recorded(w) if m['id'] == str(events[2].id))
            message = w.posted[events[2].id]
            message.content = 'never mind'
            message.edited_at = (dt.datetime.now(dt.timezone.utc) if edit.startswith('after') else
                                 dt.datetime.fromtimestamp(saved_at-60, dt.timezone.utc))
            await recheck(w.fri.agent)
            summary = await asked(w)
            assert (summary is None) if edit.startswith('after') else summary['text'] == SAID
        finally:
            await w.fri.close()
    run(scenario)


def test_one_discord_cant_vouch_for_is_held_back_until_it_can(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await kept(monkeypatch, w)
            agent = w.fri.agent
            undo = down(w)
            await recheck(agent)
            assert await asked(w) is None
            undo()
            gene_later(monkeypatch, 601)  # Past the claim it answered.
            await recheck(agent)
            assert (await asked(w))['text'] == SAID
        finally:
            await w.fri.close()
    run(scenario)


def test_one_whose_message_is_still_being_withdrawn_is_held_back(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            events = await kept(monkeypatch, w)
            agent = w.fri.agent
            agent.saved_turns.marked[str(events[0].id)] = time.time()  # Taken back within the hour.
            await recheck(agent)
            with w.store.connection() as db:
                assert db.execute('SELECT held FROM fri_member_summary_rechecks').fetchone()[0] == 1
        finally:
            await w.fri.close()
    run(scenario)


def test_fris_first_pass_after_it_starts_holds_every_summary_back_until_rechecked(monkeypatch, tmp_path):
    """Deletions Fri never heard of happen while it's down: what it kept before is held back until read again."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await kept(monkeypatch, w)
            agent = w.fri.agent
            assert (await asked(w))['text'] == SAID  # Built and checked: shown.
            await w.fri.api('POST', '/agent/member-summary/recheck', {'fresh': True})  # A pass cut off at its claim.
            assert await asked(w) is None
            agent.summaries_rechecked = False  # Started again.
            gene_later(monkeypatch, 601)
            await recheck(agent)
            assert (await asked(w))['text'] == SAID
        finally:
            await w.fri.close()
    run(scenario)


def test_a_summary_whose_messages_check_out_reaches_the_model_as_before_and_no_model_is_called(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await kept(monkeypatch, w)
            agent = w.fri.agent
            before = await asked(w)

            async def never(*args, **kwargs):
                raise AssertionError('no model call in a recheck')
            monkeypatch.setattr('fri.fri_member_summary.analyze', never)
            requests = len(w.requests) if hasattr(w, 'requests') else None
            await recheck(agent)
            assert requests is None or len(w.requests) == requests
            assert await asked(w) == before
        finally:
            await w.fri.close()
    run(scenario)


def test_without_rechecks_no_summary_reaches_the_model_and_builds_wait(monkeypatch, tmp_path):
    """A controller that can't recheck them (an older one): any summary might hold a message deleted unheard, so none is
    used, and none is built; fri health says so."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await kept(monkeypatch, w)
            agent = w.fri.agent
            w.fri.capabilities = w.fri.capabilities - {RECHECKS}
            assert await asked(w) is None
            for text in ('fri raid went fine', 'fri next one friday'):
                await w.say(GENERAL, text)
            settled(w)
            with w.store.connection() as db:
                db.execute('DELETE FROM fri_member_summary_calls')
            assert await build(agent) is None
            assert any('summar' in line and RECHECKS in line for line in (await w.fri.watcher.report()).splitlines())
        finally:
            await w.fri.close()
    run(scenario)


def test_rechecks_run_as_their_own_stage_whether_or_not_learning_is_on(monkeypatch, tmp_path):
    passes = AsyncMock()
    monkeypatch.setattr('fri.fri_member_summary.recheck', passes, raising=False)
    monkeypatch.setattr('fri.fri_member_summary.build', AsyncMock())
    for name in ('fri.fri_workspace.deliver_one', 'fri.fri_social.deliver_one', 'fri.fri_fixes.check'):
        monkeypatch.setattr(name, AsyncMock())

    async def api(method, path, payload=None):
        return {'/agent/state': STATE, '/agent/reports': []}[path]
    fri = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, api=AsyncMock(side_effect=api),
                          get_user=lambda _: None, wait_until_ready=AsyncMock(), capabilities=frozenset(
                              {'member-summaries'}), data=tmp_path)
    agent = Agent(fri)
    agent.learn, agent.attend, agent.check_memes = AsyncMock(), AsyncMock(), AsyncMock()
    asyncio.run(agent.heartbeat.coro(agent))
    passes.assert_not_awaited()
    fri.capabilities = frozenset({'member-summaries', RECHECKS})
    monkeypatch.setitem(STATE, 'config', {**STATE['config'], 'learning_enabled': False})
    asyncio.run(agent.heartbeat.coro(agent))
    passes.assert_awaited_once_with(agent)
    assert agent.stages['summary_rechecks']['error'] is None
