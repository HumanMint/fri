"""What a channel's withdrawal (fri #372) and a report's own withdrawal reach after their last read (fri #378, #373),
and what fri health says while summaries pause (fri #381).

1. fri #378 item 1: a learning pass read its channel's history, and a channel's withdrawal was accepted while the
   controller checked the batch's authors, its last wait. The withdrawal names no saved message, so nothing marked the
   history it read: it reached the summary model. The pass now stops there, as a failed read does, and its batch is
   read again.
2. fri #373, #378 item 2: a pending report was fetched, then the owner looked up, and the report sent as fetched,
   though its source was withdrawn during that lookup. The owner is found first now, so no wait comes between the
   fetch and the send but the fetch itself; something taken back during that leaves it for the next round.
3. fri #381: a summary pause on a controller missing summary-holds named summary-sources and summary-rechecks.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from gene.api import create_app

from compat_tests.test_api import headers
from compat_tests.test_fri_learning import dm_owner, learning  # noqa: F401 (the fixture)
from compat_tests.test_fri_panel_and_learning_fences import batch
from compat_tests.test_fri_reports import complete
from fri.fri_agent import Agent
from fri.fri_reports import summary

# 1. The learning model.

@pytest.mark.parametrize('fence', ['author-fence', 'each author read'])
def test_a_channel_withdrawn_while_a_passs_authors_are_checked_stops_the_pass(learning, monkeypatch, fence):  # noqa: F811
    """Its last wait before the summary request: Gene's check of the batch's authors, or, from a controller without
    author-fence, each author's read. A channel withdrawal accepted then sends nothing to the model, and the pass
    finishes as a failed read does, so its batch is read again."""
    async def scenario():
        agent, store, _, finishes, asked, signing = batch(learning, monkeypatch)
        if fence == 'author-fence':
            agent.fri.capabilities = frozenset(agent.fri.capabilities) | {'author-fence'}
        api, profile, signed, went = agent.fri.api, agent.profile.side_effect, [], []

        def withdrawn():
            if not went:
                went.append(True)
                agent.channel_withdrawals.taken(())  # A channel's withdrawal accepted, naming no saved message.

        async def checking(method, path, payload=None):
            if path == '/agent/authors/check':
                withdrawn()
            return await api(method, path, payload)
        agent.fri.api = checking
        signing.append(lambda: signed.append(True))

        def reading(channel, user):
            if signed and fence == 'each author read':
                withdrawn()
            return profile(channel, user)
        agent.profile = AsyncMock(side_effect=reading)
        await agent.learn()
        assert went and asked == []
        assert finishes and finishes[-1]['error'] and finishes[-1]['result'] == {}
    asyncio.run(scenario())


def test_a_pass_with_no_withdrawal_meanwhile_sends_as_before(learning, monkeypatch):  # noqa: F811
    async def scenario():
        agent, store, _, finishes, asked, signing = batch(learning, monkeypatch)
        agent.fri.capabilities = frozenset(agent.fri.capabilities) | {'author-fence'}
        await agent.learn()
        assert asked and not finishes[-1]['error']
    asyncio.run(scenario())


# 2. Owner reports.

def reporting(learning, *, owner_cached=True, during=None):  # noqa: F811
    """A pending report, an agent to send it, and the owner, cached or looked up. during(store, report) runs while the
    owner is looked up, or, with owner_cached, right after the report is fetched."""
    _, store, settings, scheduler, report = complete(learning)
    client = TestClient(create_app(settings))
    owner = dm_owner()
    holder = {}

    async def api(method, path, payload=None):
        response = client.request(method, path, headers=headers('fri'), json=payload)
        assert response.status_code == 200, response.text
        if path == '/agent/reports' and owner_cached and during:
            during(store, report, holder['agent'])
        return response.json()

    async def fetch_user(_):
        if during:
            during(store, report, holder['agent'])
        return owner
    fri = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, api=api,
                          get_user=lambda _: owner if owner_cached else None, fetch_user=fetch_user)
    agent = holder['agent'] = Agent(fri)
    return agent, owner, scheduler, report, store


def sources_kept(store):
    """Whether this controller records what a report read (gene #247), which the deployed one may not yet."""
    with store.connection() as db:
        return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fri_learning_sources'")
                    .fetchone())


def read_by(store, report):
    if not sources_kept(store):
        pytest.skip('this controller keeps no record of what a report read (gene #247)')
    with store.connection() as db:
        row = db.execute("SELECT messages FROM fri_learning_sources WHERE kind='report' AND item=?",
                         (report['id'],)).fetchone()
    return json.loads(row[0])


def test_a_report_withdrawn_while_the_owner_is_looked_up_goes_as_the_controller_has_it(learning):  # noqa: F811
    """fri #373's repro: the report's source is withdrawn while Fri looks the owner up. Gene holds it as a breadcrumb
    now (gene #247); the owner gets that, never the summary it was fetched with before."""
    def withdraw(store, report, agent):
        store.withdraw_saved('123', read_by(store, report), deleted=True)

    async def scenario():
        agent, owner, scheduler, report, store = reporting(learning, owner_cached=False, during=withdraw)
        await agent.send_reports()
        now = scheduler.report(report['id'])
        assert summary(now) != summary(report)
        assert owner.send.await_args.args[0] == summary(now)
    asyncio.run(scenario())


@pytest.mark.parametrize('what', ['a channel', 'a message'])
def test_a_report_fetched_while_something_was_taken_back_waits_for_the_next_round(learning, what):  # noqa: F811
    """A channel's withdrawal accepted, or a message's queued here, while the report was fetched: it may not be what
    the controller holds now, so it isn't sent from that fetch, and stays pending for the next round."""
    def taken(store, report, agent):
        if what == 'a channel':
            agent.channel_withdrawals.taken(())
        else:
            agent.saved_turns.generation += 1  # As queueing a withdrawal moves it (see SavedTurns.withdraw).

    async def scenario():
        agent, owner, scheduler, report, store = reporting(learning, during=taken)
        await agent.send_reports()
        owner.send.assert_not_awaited()
        assert [row['id'] for row in scheduler.reports()] == [report['id']]
    asyncio.run(scenario())


def test_a_report_with_nothing_taken_back_goes_as_before(learning):  # noqa: F811
    async def scenario():
        agent, owner, scheduler, report, store = reporting(learning, owner_cached=False)
        await agent.send_reports()
        assert owner.send.await_args.args[0] == summary(report) and not scheduler.reports()
    asyncio.run(scenario())


def test_with_nothing_pending_nobody_is_looked_up(learning):  # noqa: F811
    """The owner isn't in the cache: with no report pending, Fri doesn't look them up from Discord either."""
    async def scenario():
        agent, owner, scheduler, report, store = reporting(learning, owner_cached=False)
        scheduler.delivered(report['id'])
        agent.fri.fetch_user = AsyncMock(side_effect=AssertionError('looked up with nothing to send'))
        await agent.send_reports()
        owner.send.assert_not_awaited()
    asyncio.run(scenario())


# 3. fri health.

@pytest.mark.parametrize('missing', [('summary-holds',), ('summary-sources', 'summary-rechecks')])
def test_fri_health_names_the_capabilities_a_summary_pause_waits_for(monkeypatch, tmp_path, missing):
    """fri #381: a controller offering summary-sources and summary-rechecks but not summary-holds (the 1.0.6 Gene)
    pauses summaries; fri health names summary-holds, and only what's missing."""
    from compat_tests.test_fri_member_profiles import GENERAL, run, world
    from compat_tests.test_fri_member_summaries import settled, summarizer
    from fri.fri_member_summary import build
    from fri.fri_watch import Watch

    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            summarizer(monkeypatch, w)
            for text in ('fri raid tonight?', 'fri which build for it'):
                await w.say(GENERAL, text)
            settled(w, 13*3600)
            w.fri.capabilities = frozenset(w.fri.capabilities)-set(missing)
            assert await build(w.fri.agent) is None and w.fri.agent.summaries_wait_for_gene
            line = next(line for line in (await Watch(w.fri).report()).splitlines() if 'member summaries' in line)
            for name in ('summary-sources', 'summary-rechecks', 'summary-holds'):
                assert (name in line) is (name in missing), (name, line)
        finally:
            await w.fri.close()
    run(scenario)


def test_fri_health_says_summaries_resume_once_the_controller_offers_it_all(monkeypatch, tmp_path):
    """Paused on a controller since upgraded, before the next build looks again: nothing is missing now."""
    from compat_tests.test_fri_member_profiles import run, world
    from fri.fri_watch import Watch

    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            w.fri.capabilities |= {'summary-sources', 'summary-rechecks', 'summary-holds'}  # Upgraded since.
            w.fri.agent.summaries_wait_for_gene = True  # As a build on the older one left it.
            line = next(line for line in (await Watch(w.fri).report()).splitlines() if 'member summaries' in line)
            assert 'next build' in line and 'summary-' not in line
        finally:
            await w.fri.close()
    run(scenario)
