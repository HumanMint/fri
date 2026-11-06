"""Owner reports and learning passes, after #384 (fri #385).

1. "Forget me" while the last report fetch answered: the controller deleted the report, but forgetting moved nothing
   Fri compared, so the card it fetched went. Privacy controls taken in here count in the delivery mark now.
2. An uncached DM channel: a User's send() opens it first, a wait after the last check. It's opened before the last
   fetch now, and the card goes through it.
3. Details and Summary: a withdrawal while /agent/reports/{id}/view answered showed what was withdrawn. The fetch is
   held to the same mark: read again once if it moved, and not shown if it moves again.
4. A pass aborts only for a channel's withdrawal that may have touched the channel it read, so an unrelated one costs
   it nothing (and its scan reservation, which the controller keeps for a pass that fails, isn't spent for it).
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from gene.api import create_app

from compat_tests.test_api import headers
from compat_tests.test_fri_generation_consumers import read_by, sources_kept
from compat_tests.test_fri_learning import dm_owner, learning  # noqa: F401 (the fixture)
from compat_tests.test_fri_panel_and_learning_fences import batch
from compat_tests.test_fri_reports import complete, event
from fri.fri_agent import Agent
from fri.fri_reports import ReportButton, report_message, summary


def delivering(learning, owner, *, final=None):  # noqa: F811
    """A pending report, an agent to send it to `owner` (cached), and final(store, report, agent) run once the last
    report fetch has answered, before Fri uses its answer."""
    _, store, settings, scheduler, report = complete(learning)
    client = TestClient(create_app(settings))
    holder, fetched = {}, []

    async def api(method, path, payload=None):
        response = client.request(method, path, headers=headers('fri'), json=payload)
        assert response.status_code == 200, response.text
        if path == '/agent/reports':
            fetched.append(path)
            if final and len(fetched) == 2:  # The look for one pending, then the last fetch.
                final(store, report, holder['agent'])
        return response.json()
    fri = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, api=api, get_user=lambda _: owner)
    agent = holder['agent'] = Agent(fri)
    return agent, scheduler, report, store


def forgotten(store, report, agent):
    """Member 11 turns memory off with "forget me" through Fri: taken in here, then the controller erases."""
    agent.forget(11, erased=True)
    store.personal_control('11', 'disable')


# 1. Forget me.

def test_forget_me_as_the_last_report_fetch_answers_sends_nothing(learning):  # noqa: F811
    """The issue's first repro: the controller deleted the report with the erasure, but the card fetched just before
    it went, as forgetting moved nothing Fri compared."""
    async def scenario():
        owner = dm_owner()
        agent, scheduler, report, store = delivering(learning, owner, final=forgotten)
        await agent.send_reports()
        owner.send.assert_not_awaited()
    asyncio.run(scenario())


def test_forget_my_chats_on_the_panel_moves_the_mark_too(monkeypatch):
    """The panel's "Forget my chats" deletes pending learning reports at the controller as well (clear_chats)."""
    from fri import fri_panel

    async def scenario():
        monkeypatch.setattr(fri_panel, 'defer_panel', AsyncMock())
        monkeypatch.setattr(fri_panel, 'reply', AsyncMock())
        agent = Agent(SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456))
        cog = SimpleNamespace(api=AsyncMock(return_value={'ok': True}), agent=agent, guild_id=123)
        before = agent.taken_back_mark()
        await fri_panel.HomeView(cog).forget_chats.callback(SimpleNamespace(guild_id=123, user=SimpleNamespace(id=11)))
        cog.api.assert_awaited_once_with('POST', '/chat/clear', {'guild_id': '123', 'user_id': '11'})
        assert agent.taken_back_mark() != before
    asyncio.run(scenario())


# 2. An uncached DM channel.

class Owner:
    """The owner as discord.User is: with no DM channel cached, send() opens one first (start_private_message), a wait
    of its own. `opening` runs during that wait."""
    def __init__(self, opening):
        self.dm_channel, self.opening, self.sent = None, opening, []

    async def create_dm(self):
        if self.dm_channel is not None:  # As discord.py's: the cached one, with no wait.
            return self.dm_channel
        await self.opening()
        self.dm_channel = SimpleNamespace(send=AsyncMock(side_effect=lambda content, **kwargs: self.sent.append(content)))
        return self.dm_channel

    async def send(self, content, **kwargs):
        channel = self.dm_channel or await self.create_dm()
        return await channel.send(content, **kwargs)


@pytest.mark.parametrize('what', ['its source withdrawn', 'forget me'])
def test_an_owner_whose_dm_isnt_open_gets_the_report_as_it_stands(learning, what):  # noqa: F811
    """The issue's second repro: the DM channel opened inside send(), after the last check, while the report's source
    was withdrawn, or member 11 said forget me. It's opened before the last fetch now: the owner gets the report as the
    controller holds it then (a breadcrumb), or nothing once it's gone."""
    async def scenario():
        holder = {}

        async def opening():
            if what == 'its source withdrawn':
                holder['store'].withdraw_saved('123', read_by(holder['store'], holder['report']), deleted=True)
            else:
                forgotten(holder['store'], holder['report'], holder['agent'])
        owner = Owner(opening)
        agent, scheduler, report, store = delivering(learning, owner)
        holder.update(store=store, report=report, agent=agent)
        await agent.send_reports()
        if what == 'forget me':
            assert owner.sent == []
        else:
            now = scheduler.report(report['id'])
            assert summary(now) != summary(report) and owner.sent == [summary(now)]
    asyncio.run(scenario())


# 3. Details and Summary.

@pytest.mark.parametrize('moves', ['once', 'every time'])
def test_report_details_fetched_while_something_was_taken_back(learning, moves):  # noqa: F811
    """The issue's third repro: the report's source is withdrawn while its view answers. Read again, the owner sees it
    as it is now; moving every time, the DM isn't edited, and they're told to try again."""
    _, store, settings, scheduler, report = complete(learning)
    client = TestClient(create_app(settings))
    mark, views = [0], []

    async def api(method, path, payload=None):
        response = client.request(method, path, headers=headers('fri'), json=payload)
        assert response.status_code == 200, response.text
        views.append(path)
        if mark[0] == 0 and sources_kept(store):  # A controller that keeps no record of it can't show it changed.
            store.withdraw_saved('123', read_by(store, report), deleted=True)
        if mark[0] == 0 or moves == 'every time':
            mark[0] += 1  # Taken back here while it answered.
        return response.json()

    async def scenario():
        fri, interaction = event(report)
        fri.api = api
        fri.agent.taken_back_mark = lambda: mark[0]
        await ReportButton(report['id'], 0).callback(interaction)
        assert len(views) == 2
        if moves == 'once':
            content, _ = report_message(scheduler.report(report['id']), 0)
            assert interaction.edit_original_response.await_args.kwargs['content'] == content
            assert content != report_message(report, 0)[0] or not sources_kept(store)
        else:
            interaction.edit_original_response.assert_not_awaited()
            assert 'try again' in interaction.followup.send.await_args.args[0]
    asyncio.run(scenario())


# 4. A pass and the channels it read.

@pytest.mark.parametrize('withdrawn', ['another channel', 'its own channel', 'a channel not named'])
def test_a_pass_aborts_only_for_a_withdrawal_that_may_touch_its_channel(learning, monkeypatch, withdrawn):  # noqa: F811
    """A channel's withdrawal accepted while a pass's authors are checked: one of another channel can't have taken
    anything it read, so it goes on to the model; one of its own channel, or one that doesn't say which (a thread named
    under a withdrawn channel by a sweep), stops it, as before (fri #378)."""
    async def scenario():
        agent, store, _, finishes, asked, signing = batch(learning, monkeypatch)
        agent.fri.capabilities = frozenset(agent.fri.capabilities) | {'author-fence'}
        api, went = agent.fri.api, []

        async def checking(method, path, payload=None):
            if path == '/agent/authors/check' and not went:
                went.append(True)
                named = {'another channel': ['999'], 'its own channel': ['1'], 'a channel not named': None}[withdrawn]
                agent.channel_withdrawals.taken((), channels=named)
            return await api(method, path, payload)
        agent.fri.api = checking
        await agent.learn()
        assert went and finishes
        if withdrawn == 'another channel':
            assert asked and not finishes[-1]['error']
        else:
            assert asked == [] and finishes[-1]['error']
    asyncio.run(scenario())


@pytest.mark.parametrize('withdrawn', ['another channel', 'its own channel'])
@pytest.mark.parametrize('visit', ['first', 'later'])
def test_an_exploration_stops_only_for_a_withdrawal_that_may_touch_what_it_sends(learning, monkeypatch, visit,  # noqa: F811
                                                                                withdrawn):
    """A first visit sends its channel's messages alone, so only its channel's withdrawal stops it. A later one asks
    the planner with the member's historical topics, which can come from any channel, so every one does."""
    from compat_tests.test_fri_exploration_baseline import explore

    def taking(agent):
        agent.channel_withdrawals.taken((), channels=['999' if withdrawn == 'another channel' else '1'])
    asked = explore(learning, monkeypatch, (1 if visit == 'first' else 2, 'target'), taking)
    if visit == 'first':
        assert [kind for kind, _ in asked[1]] == (['summary'] if withdrawn == 'another channel' else [])
    else:
        assert asked[1] and asked[2] == []


def test_withdrawals_name_the_channels_they_took(monkeypatch, tmp_path):
    """What scopes it: a sweep naming a thread's channel names both, as the controller may take the thread with a
    channel withdrawn before; a deleted channel's withdrawal names it and the threads under it Fri knew. A pass that read
    none of them goes on; one whose scope isn't known, or after a withdrawal that didn't say, doesn't."""
    from compat_tests.test_fri_channel_withdrawals import deleted, landed, needs_channels, queue
    from compat_tests.test_fri_member_profiles import GAMING, GENERAL, THREAD, note, run, world

    async def scenario():
        w = world(monkeypatch, tmp_path)
        needs_channels(w)
        try:
            channels = queue(w)
            await w.say(THREAD, 'fri I like osu', 'cool', note('I like osu'))
            with w.store.connection() as db:
                db.execute('DELETE FROM chat_thread_parents')  # As an older controller saved it: the sweep names it.
            before = channels.generation
            await landed(w)
            assert channels.generation == before+1 and channels.accepted[before+1] == {str(THREAD), str(GENERAL)}
            await deleted(w, GENERAL, (THREAD,))
            named = [channels.accepted[g] for g in range(before+2, channels.generation+1)]
            assert named and set().union(*named) == {str(GENERAL), str(THREAD)}
            assert not channels.touches(before, {str(GAMING)})
            assert channels.touches(before, {str(THREAD)}) and channels.touches(before, None)
            assert not channels.touches(channels.generation, None)  # Nothing since.
            since = channels.generation
            channels.taken(())
            assert channels.touches(since, {str(GAMING)})
        finally:
            await w.fri.close()
    run(scenario)


def test_a_pass_over_a_thread_counts_its_channels_withdrawal(learning, monkeypatch):  # noqa: F811
    """A thread goes with its channel's withdrawal, which may not name it (the controller knows it's under it)."""
    async def scenario():
        agent, store, _, finishes, asked, signing = batch(learning, monkeypatch)
        agent.fri.capabilities = frozenset(agent.fri.capabilities) | {'author-fence'}
        channel = agent.fri.get_channel(1)
        channel.parent_id = 2  # Channel 1 is a thread under 2, another approved channel.
        api, went = agent.fri.api, []

        async def checking(method, path, payload=None):
            if path == '/agent/authors/check' and not went:
                went.append(True)
                agent.channel_withdrawals.taken((), channels=['2'])
            return await api(method, path, payload)
        agent.fri.api = checking
        await agent.learn()
        assert went and asked == [] and finishes[-1]['error']
    asyncio.run(scenario())
