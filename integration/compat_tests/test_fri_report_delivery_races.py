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
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from gene.api import create_app

from compat_tests.test_api import headers
from compat_tests.test_fri_generation_consumers import read_by, sources_kept
from compat_tests.test_fri_learning import learning  # noqa: F401 (the fixture)
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
        owner = SimpleNamespace(send=AsyncMock())
        agent, scheduler, report, store = delivering(learning, owner, final=forgotten)
        await agent.send_reports()
        owner.send.assert_not_awaited()
    asyncio.run(scenario())


# 2. An uncached DM channel.

class Owner:
    """The owner as discord.User is: with no DM channel cached, send() opens one first (start_private_message), a wait
    of its own. `opening` runs during that wait."""
    def __init__(self, opening):
        self.dm_channel, self.opening, self.sent = None, opening, []

    async def create_dm(self):
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


def test_the_withdrawal_log_names_what_send_and_a_sweep_took(learning, monkeypatch):  # noqa: F811
    """What scopes it: a withdrawal Fri sent names its channel and threads; a sweep's naming threads names them."""
    agent, *_ = batch(learning, monkeypatch)
    channels = agent.channel_withdrawals
    before = channels.generation
    channels.taken((), channels=['5', '70'])
    assert not channels.touches(before, {'6'}) and channels.touches(before, {'70'}) and channels.touches(before, None)
    json.dumps(list(channels.accepted))  # Plain values only.
