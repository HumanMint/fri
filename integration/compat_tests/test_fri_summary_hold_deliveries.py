"""A member summary held back after Fri claimed a delivery that depends on it, and a held summary's echo in a channel's
live view (fri #355, Sol round 1: B1, S1), against the real controller.

B1: the reminder or post was claimed, then the summary it depends on was held before it began sending. The controller
refused it like any conflict, and Fri recorded the delivery as failed for good, so it never went, even once the summary
checked out. The controller now puts it back unsent and answers that it's held (423); Fri records nothing, and the next
pass after the recheck sends it.

S1: Fri's reply that drew on a summary, in a channel's live view, came back from the controller as not said while the
summary was held, so Fri removed it from the view for good. It's left out of requests while held, and kept for after.
"""
import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from gene.api import create_app
from gene.config import Settings

from compat_tests.test_fri_member_profiles import GENERAL, model, run, world
from compat_tests.test_fri_member_summaries import CONTINUATION
from compat_tests.test_fri_summary_echoes import echoed
from compat_tests.test_fri_summary_generations import summarized_from
from compat_tests.test_fri_workspace import discord_harness
from fri.fri_chat import ChatResult
from fri.fri_errors import ControllerRejected


@pytest.fixture
def api(tmp_path):
    """The controller, with each refusal's status, as Fri's own client keeps it."""
    settings = Settings(tmp_path/'api.db', '123', '10', 'runtime', 'worker', 'deploy', workshop_channel_id='456',
                        fri_token='fri')
    client = TestClient(create_app(settings))

    async def call(method, path, body=None):
        result = client.request(method, path, json=body, headers={'Authorization': 'Bearer fri'})
        if result.status_code >= 400:
            raise ControllerRejected(str(result.json()), result.status_code)
        return result.json()
    return client, call


def summary_of_20(store, *, held):
    """Member 20's kept summary, held back until it's read again, or not."""
    now = time.time()
    with store.connection() as db:
        db.execute("INSERT OR IGNORE INTO fri_member_summaries VALUES ('123','20','they planned a raid',0,'[]',"
                   "'[\"public:1\"]',?,?)", (now, now))
        through = db.execute("SELECT through FROM fri_member_summaries WHERE user_id='20'").fetchone()[0]
        db.execute("INSERT INTO fri_member_summary_rechecks(guild_id,user_id,through,checked,held) VALUES "
                   "('123','20',?,0,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET held=excluded.held",
                   (through, int(held)))


def depends(store, table, key, item):
    with store.connection() as db:
        db.execute(f"INSERT INTO {table}({key},guild_id,user_id,generation,revision) VALUES (?,'123','20',0,0)",
                   (item,))


def holding_at_sending(fri, store):
    """The summary held back right as Fri asks to start sending, after its claim."""
    real = fri.api

    async def calling(method, path, body=None):
        if path.endswith('/sending'):
            summary_of_20(store, held=True)
        return await real(method, path, body)
    fri.api = calling
    return lambda: setattr(fri, 'api', real)


def test_b1_a_reminder_claimed_before_its_summary_is_held_is_sent_once_it_checks_out(api, tmp_path, monkeypatch):
    async def scenario():
        from compat_tests.test_fri_social import runtime
        from fri.fri_social import deliver_one
        fri, social, row = runtime(api, tmp_path)
        store = social.store
        summary_of_20(store, held=False)
        depends(store, 'fri_reminder_dependencies', 'reminder_id', row['id'])
        sender = AsyncMock(return_value={'id': '987'})
        monkeypatch.setattr('fri.fri_social.send_once', sender)
        put_back = holding_at_sending(fri, store)
        await deliver_one(fri)
        put_back()
        assert sender.await_count == 0
        with store.connection() as db:
            assert db.execute('SELECT state,lease FROM fri_reminders WHERE id=?', (row['id'],)).fetchone()[:] == (
                'scheduled', '')
        assert not list((tmp_path/'reminder-receipts').glob('*.json'))
        summary_of_20(store, held=False)
        await deliver_one(fri)
        assert sender.await_count == 1
        with store.connection() as db:
            assert db.execute('SELECT state FROM fri_reminders WHERE id=?', (row['id'],)).fetchone()[0] == 'sent'
    asyncio.run(scenario())


def test_b1_a_post_claimed_before_its_drafts_summary_is_held_is_sent_once_it_checks_out(api, tmp_path, monkeypatch):
    async def scenario():
        from fri.fri_workspace import deliver_one
        fri, work, post, _ = discord_harness(api, tmp_path)
        store = work.store
        summary_of_20(store, held=False)
        depends(store, 'fri_work_dependencies', 'work_id', post['work_id'])
        sender = AsyncMock(return_value={'id': '987'})
        monkeypatch.setattr('fri.fri_workspace.send_once', sender)
        monkeypatch.setattr('fri.fri_workspace.capabilities', AsyncMock(return_value={'777': {'one_off_post': True}}))
        put_back = holding_at_sending(fri, store)
        await deliver_one(fri)
        put_back()
        assert sender.await_count == 0
        with store.connection() as db:
            assert db.execute('SELECT state,lease FROM fri_posts WHERE id=?', (post['id'],)).fetchone()[:] == (
                'authorized', '')
        summary_of_20(store, held=False)
        await deliver_one(fri)
        assert sender.await_count == 1
        with store.connection() as db:
            assert db.execute('SELECT state FROM fri_posts WHERE id=?', (post['id'],)).fetchone()[0] == 'sent'
    asyncio.run(scenario())


async def recalls(text, context):
    return ChatResult(status='completed', reply=CONTINUATION)


def test_s1_a_held_summarys_echo_leaves_requests_but_stays_in_the_live_view(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            if 'summary-holds' not in w.fri.capabilities:
                pytest.skip('needs a controller offering summary-holds')
            await summarized_from(monkeypatch, w)
            model(w, recalls)
            assert await echoed(w) == [CONTINUATION]  # In #general, where the summary came from.
            agent = w.fri.agent
            [echo] = [row['id'] for row in agent.recent[GENERAL] if row.get('fri')]
            assert echo in agent.summarized
            [claim] = (await w.fri.api('POST', '/agent/member-summary/recheck', {'fresh': True}))['summaries']
            assert echo not in [row['id'] for row in await agent.context_messages(GENERAL)]
            assert echo in [row['id'] for row in agent.recent[GENERAL]]  # Kept for after.
            await w.fri.api('POST', f"/agent/member-summary/recheck/{claim['id']}", {'status': 'ok'})
            assert echo in [row['id'] for row in await agent.context_messages(GENERAL)]
        finally:
            await w.fri.close()
    run(scenario)
