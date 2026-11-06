"""Owner reports while a privacy change is on its way, or the withdrawal queue holds (fri #390).

1. A privacy change can commit in the controller while its answer is still on its way back, and the controller deletes
   every pending report with it. Fri moved the report mark only once that answer came, so a report read meanwhile was
   sent, or shown by Details or Summary. Reports now wait while a change is on its way, and the mark moves as one
   starts and as it finishes: "Forget my chats", "forget me" and memory back on, and a note's or the summary's edit.
2. A channel deleted while a report was read, its withdrawal not yet taken by the controller: every saved context is
   held back (SavedTurns.closed), but the mark moves only once the controller takes it. Reports wait for that too.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from gene.api import create_app

from compat_tests.test_api import headers
from compat_tests.test_fri_learning import dm_owner, learning  # noqa: F401 (the fixture)
from compat_tests.test_fri_reports import complete, event
from fri.fri_agent import Agent
from fri.fri_errors import ControllerRejected
from fri.fri_reports import ReportButton

REPORT = {'id': 'a'*32, 'body': 'Learning pass a'}  # A pending report as the controller lists it (no card).


class Open:
    """A privacy change's POST: it commits in the controller (`commit`), and its answer stays on its way until
    released."""

    def __init__(self, commit):
        self.commit, self.committed, self.released = commit, asyncio.Event(), asyncio.Event()

    async def api(self, method, path, payload=None):
        self.commit()
        self.committed.set()
        await self.released.wait()
        return {'ok': True}


def forget_my_chats(monkeypatch, agent, opened):
    """Member 11 presses the panel's "Forget my chats", its POST going through `opened`. Returns its task."""
    from fri import fri_panel
    monkeypatch.setattr(fri_panel, 'defer_panel', AsyncMock())
    monkeypatch.setattr(fri_panel, 'reply', AsyncMock())
    cog = SimpleNamespace(api=opened.api, agent=agent, guild_id=123)
    return asyncio.create_task(fri_panel.HomeView(cog).forget_chats.callback(
        SimpleNamespace(guild_id=123, user=SimpleNamespace(id=11))))


def controller(learning, path, nth, during):  # noqa: F811
    """A pending report, and an API onto the controller holding it: during(store) awaited once the `nth` call to `path`
    has read its answer, before Fri gets it."""
    _, store, settings, scheduler, report = complete(learning)
    client = TestClient(create_app(settings))
    calls = []
    path = path.format(report=report['id'])

    async def api(method, called, payload=None):
        response = client.request(method, called, headers=headers('fri'), json=payload)
        if called == path:
            calls.append(called)
            if len(calls) == nth:
                await during(store)
        if response.status_code >= 400:
            raise ControllerRejected(response.text, response.status_code)
        return response.json()
    return api, store, scheduler, report


# 1. A privacy change on its way.

def test_forget_my_chats_committing_while_the_last_report_fetch_answers_holds_the_report(learning, monkeypatch):  # noqa: F811
    """The issue's repro: the last report fetch read the report; "Forget my chats" then commits in the controller,
    which deletes it, and its answer is still on its way when the report's comes back. Nothing is sent."""
    async def scenario():
        holder, tasks = {}, []

        async def during(store):
            holder['open'] = Open(lambda: store.clear_chats('11'))
            tasks.append(forget_my_chats(monkeypatch, holder['agent'], holder['open']))
            await holder['open'].committed.wait()
        api, store, scheduler, report = controller(learning, '/agent/reports', 2, during)  # The last fetch.
        owner = dm_owner()
        agent = holder['agent'] = Agent(SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, api=api,
                                                        get_user=lambda _: owner))
        await agent.send_reports()
        owner.send.assert_not_awaited()
        holder['open'].released.set()
        await tasks[0]
        assert not scheduler.reports()
    asyncio.run(scenario())


def test_forget_my_chats_committing_while_details_load_shows_nothing_of_it(learning, monkeypatch):  # noqa: F811
    """The same for Details: its view read the report, then "Forget my chats" commits, its answer still on its way
    when the view's comes back. The DM isn't edited with what the controller deleted."""
    async def scenario():
        holder, tasks = {}, []

        async def during(store):
            holder['open'] = Open(lambda: store.clear_chats('11'))
            tasks.append(forget_my_chats(monkeypatch, holder['agent'], holder['open']))
            await holder['open'].committed.wait()
        api, store, scheduler, report = controller(learning, '/agent/reports/{report}/view', 1, during)
        fri, interaction = event(report)
        fri.api = api
        agent = holder['agent'] = fri.agent = Agent(SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456))
        agent.member = AsyncMock(return_value=object())
        await ReportButton(report['id'], 0).callback(interaction)
        interaction.edit_original_response.assert_not_awaited()
        assert interaction.followup.send.await_args.kwargs['ephemeral']
        holder['open'].released.set()
        await tasks[0]
    asyncio.run(scenario())


def delivered_during(fri, agent, path):
    """fri.api with a pending report for its owner, and the agent's report delivery run, as another task's would,
    while the POST to `path` is on its way. Returns the owner, whose DM shows what was sent, and the POSTs made."""
    owner, posted = dm_owner(), []
    fri.get_user = lambda _: owner
    real = fri.api

    async def api(method, called, payload=None):
        if called == '/agent/reports':
            return [REPORT]
        if called.endswith('/delivered'):
            return {}
        if method == 'POST' and called == path:
            posted.append(called)
            await agent.send_reports()
        return await real(method, called, payload)
    fri.api = api
    return owner, posted


def test_a_memory_control_on_its_way_holds_reports(monkeypatch, tmp_path):
    """Member 20's "forget me" in a chat channel: the mark moved only once its answer came, so a report read entirely
    while the controller erased was sent."""
    async def scenario():
        from compat_tests.test_fri_member_profiles import GENERAL, world
        w = world(monkeypatch, tmp_path)
        try:
            w.fri.owner_id = 10
            owner, posted = delivered_during(w.fri, w.fri.agent, '/chat/memory/control')
            await w.say(GENERAL, 'fri forget me')
            assert posted
            owner.send.assert_not_awaited()
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('edit', ['remove', 'keep', 'share', 'replace', 'summary edit', 'summary clear'])
def test_a_note_or_summary_edit_on_its_way_holds_reports(monkeypatch, tmp_path, edit):
    """My memory's edits: the controller deletes pending reports with each, so none is sent while one is on its way."""
    async def scenario():
        from compat_tests.test_fri_actions import harness
        from fri.fri_memory_panel import MemoryView, summarized
        fri, agent, channels, _ = harness(monkeypatch, tmp_path)
        fri.owner_id = 10
        state = {'enabled': True, 'revision': 2, 'entries': [
            {'id': 7, 'category': 'games', 'evidence': 'I like chess', 'scope': 'server' if edit == 'keep' else '1',
             'expires_at': 4102444800}]}
        scope = {'guild_id': '123', 'user_id': '20', 'channel_id': '1', 'conversation_id': 'public:1'}
        interaction = SimpleNamespace(id=1, guild_id=123, channel_id=1, user=SimpleNamespace(id=20),
                                      response=SimpleNamespace(send_modal=AsyncMock()))
        path = '/chat/memory/summary' if edit.startswith('summary') else '/chat/memory/notes/7'
        fri.api = AsyncMock(return_value={**state, 'revision': 3})
        owner, posted = delivered_during(fri, agent, path)
        try:
            with patch('fri.fri_memory_panel.show', new=AsyncMock()), \
                    patch('fri.fri_memory_panel.discord_source', return_value=SimpleNamespace(model_dump=dict)):
                view = MemoryView(fri, scope, state, selected=7)
                view.interaction_check = AsyncMock(return_value=True)
                if edit == 'remove':
                    await view.remove(interaction)
                elif edit in ('keep', 'share'):
                    await view.move(interaction, edit, 'moved')
                elif edit == 'replace':
                    await view.replace(interaction)
                    modal = interaction.response.send_modal.await_args.args[0]
                    modal.preference._value = 'I prefer Terraria'
                    modal.interaction_check = AsyncMock(return_value=True)
                    await modal.on_submit(interaction)
                else:
                    await summarized(view, interaction, 'edit' if edit == 'summary edit' else 'clear', 'raids', 'ok')
            assert posted == [path]
            owner.send.assert_not_awaited()
        finally:
            await fri.close()
    asyncio.run(scenario())


# 2. A channel deleted, its withdrawal on its way.

def test_a_channel_deleted_during_the_last_report_fetch_holds_the_report(learning):  # noqa: F811
    """The issue's repro: the report's channel is deleted while the last fetch answers, and its withdrawal waits to
    be sent. Every saved context is held back, so the report is too, until the controller has taken it."""
    async def scenario():
        holder = {}

        async def during(store):
            holder['agent'].channel_withdrawals.withdraw(123, 1, send=False)  # Queued, not sent yet.
        api, store, scheduler, report = controller(learning, '/agent/reports', 2, during)
        owner = dm_owner()
        agent = holder['agent'] = Agent(SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, api=api,
                                                        get_user=lambda _: owner))
        await agent.send_reports()
        assert agent.saved_turns.closed()
        owner.send.assert_not_awaited()
        assert scheduler.reports()  # Still pending, for once the controller has taken the channel.
    asyncio.run(scenario())


def test_a_channel_deleted_while_details_load_shows_nothing(learning):  # noqa: F811
    """The same for Details: the DM isn't edited while the channel's withdrawal waits."""
    async def scenario():
        holder = {}

        async def during(store):
            holder['agent'].channel_withdrawals.withdraw(123, 1, send=False)
        api, store, scheduler, report = controller(learning, '/agent/reports/{report}/view', 1, during)
        fri, interaction = event(report)
        fri.api = api
        agent = holder['agent'] = fri.agent = Agent(SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456))
        agent.member = AsyncMock(return_value=object())
        await ReportButton(report['id'], 0).callback(interaction)
        interaction.edit_original_response.assert_not_awaited()
        assert 'try again' in interaction.followup.send.await_args.args[0]
    asyncio.run(scenario())
