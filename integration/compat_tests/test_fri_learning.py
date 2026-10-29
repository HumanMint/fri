"""Exercise the real Discord iterator, controller API, and persisted learning scheduler offline."""

import asyncio
import datetime as dt
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from fastapi.testclient import TestClient
from gene.api import create_app
from gene.config import Settings
from gene.fri_agent_store import AgentStore, snowflake_floor
from gene.store import Store

from compat_tests.test_api import headers
from fri.fri_agent import Agent


@pytest.fixture
def learning(tmp_path, monkeypatch):
    clock = [1792450800.0]
    monkeypatch.setattr('gene.fri_agent_store.time.time', lambda: clock[0])
    settings = Settings(tmp_path/'learning.db', '123', '10', 'runtime-'+'x'*32,
        'worker-'+'x'*32, 'deploy-'+'x'*32, workshop_channel_id='456',
        fri_token='fri-'+'x'*32, agent_channels='1,2', agent_history_channels='1,2', agent_role_id='90')
    store = Store(settings.db)
    store.update_chat_models(['gpt-6-luna', 'gpt-6.1-sol'])
    agent = AgentStore(store, settings)
    agent.update({'enabled': True}, 0, 'owner')
    return clock, store, settings, agent


def value(store, key):
    with store.connection() as db:
        return AgentStore.get(db, key)


class HistoryChannel(discord.abc.Messageable):
    """Use discord.py's pagination/boundary logic against an offline Discord HTTP fixture."""

    def __init__(self):
        self.id, self.guild = 1, SimpleNamespace(id=123)
        self.rows, self.reads = [], []
        self._state = SimpleNamespace(http=SimpleNamespace(logs_from=self.logs),
            create_message=lambda channel, data: data['message'])

    async def _get_channel(self):
        return self

    async def logs(self, channel, limit, before=None, after=None, around=None):
        self.reads.append({'limit': limit, 'before': before, 'after': after})
        rows = [row for row in self.rows if (before is None or row.id < int(before))
                and (after is None or row.id > int(after))]
        rows = sorted(rows, key=lambda row: row.id, reverse=after is None)[:limit]
        return [{'id': str(row.id), 'message': row} for row in sorted(rows, key=lambda row: row.id, reverse=True)]

    def add(self, at, user=11, bot=False, text='I love trance'):
        row = SimpleNamespace(id=int(snowflake_floor(at))+1,
            author=SimpleNamespace(id=user, bot=bot), webhook_id=None, content=text,
            created_at=dt.datetime.fromtimestamp(at, dt.timezone.utc))
        self.rows.append(row)
        return row.id


def test_initial_scan_then_new_messages_drain_without_skips_after_restart(learning):
    clock, store, settings, scheduler = learning
    settings = replace(settings, agent_history_channels='1')
    scheduler = AgentStore(store, settings)
    scheduler.update({'history_batch': 5}, 1, 'owner')
    channel = HistoryChannel()
    old = channel.add(clock[0]-100)
    store.personal_control('13', 'disable')
    owner = SimpleNamespace(send=AsyncMock())
    client = TestClient(create_app(settings))
    requests, summaries = [], []

    async def api(method, path, payload=None):
        response = client.request(method, path, json=payload, headers=headers('fri'))
        assert response.status_code == 200, response.text
        requests.append((path, payload))
        return response.json()

    async def model(role, instructions, context, schema, model, effort):
        assert (role, model, effort) == ('summary', 'gpt-6-luna', 'medium')
        summaries.append(context['messages'])
        return {'summary': 'Music chat.', 'notes': []}, {'input_tokens': 20, 'output_tokens': 5}, 'completed'

    async def scenario():
        fri = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, api=api,
            get_channel=lambda _: channel, get_user=lambda _: owner)
        def runtime():
            agent = Agent(fri)
            agent.state = scheduler.state()
            agent.member = AsyncMock(side_effect=lambda user: SimpleNamespace(
                roles=[SimpleNamespace(id=90)] if user != 12 else []))
            agent.profile = AsyncMock(side_effect=lambda channel, user: scheduler.context(str(channel), str(user)))
            agent.run = model
            return agent

        agent = runtime()
        first = await agent.learn()
        assert first['scanned'] == first['eligible'] == 1
        assert summaries[0][0]['id'] == str(old)
        assert value(store, 'budget')['used'] == 1
        assert value(store, 'cursors')['1']['done_at'] == clock[0]
        # New messages include non-admins, bots and opted-out members. They move
        # the scan cursor but never enter a model's summary/memory candidates.
        new = [channel.add(clock[0]+i+1, user=user, bot=bot) for i, (user, bot) in enumerate(
            [(11, False), (12, False), (11, True), (13, False), (11, False), (11, False), (11, False)])]
        clock[0] += 1800
        agent = runtime()  # The next runtime uses only persisted cursors.
        second = await agent.learn()
        assert (second['scanned'], second['eligible']) == (5, 2)
        assert [r['id'] for r in summaries[1]] == [str(new[0]), str(new[4])]
        assert value(store, 'cursors')['1']['after_id'] == str(new[4])
        clock[0] += 1800
        third = await agent.learn()
        assert (third['scanned'], third['eligible']) == (2, 2)
        assert [r['id'] for r in summaries[2]] == [str(new[5]), str(new[6])]
        assert value(store, 'cursors')['1']['after_id'] == str(int(snowflake_floor(clock[0]))-1)
        # A message in the exact snapshot millisecond must remain eligible.
        boundary = channel.add(clock[0])
        clock[0] += 1800
        assert (await agent.learn())['scanned'] == 1
        assert summaries[-1][0]['id'] == str(boundary)
        clock[0] += 1800
        empty = await agent.learn()
        assert empty['scanned'] == 0
        assert len(summaries) == 4  # Empty checks spend no model tokens.
        assert value(store, 'budget')['used'] == 9
        assert owner.send.await_count == 5 and not scheduler.reports()
        # The mobile DM is concise; full usage remains available under Details.
        assert 'no new notes' in owner.send.await_args.args[0]
        assert 'Tokens:' not in owner.send.await_args.args[0]
        assert 'input 0, output 0, reasoning 0' in scheduler.report(empty['report_id'])['body']
        assert owner.send.await_args.kwargs['view'].is_persistent()
        assert channel.reads[0]['after'] is None  # Backward initial scan.
        assert all(read['after'] for read in channel.reads[1:])
        finishes = [body for path, body in requests if path.endswith('/finish')]
        assert all(not body['error'] for body in finishes)
    asyncio.run(scenario())


def test_legacy_cursor_resumes_from_enablement_and_failed_summary_retries_same_slice(learning):
    clock, store, settings, scheduler = learning
    started = clock[0]-7200
    original = {'1': {'before': '100', 'done_at': clock[0]-3600}}
    settings = replace(settings, agent_history_channels='1')
    scheduler = AgentStore(store, settings)
    with store.connection() as db:
        scheduler.put(db, 'started', started)
        scheduler.put(db, 'cursors', original)
    claim = scheduler.claim()
    assert claim['mode'] == 'recent' and claim['before'] is None
    assert claim['after_id'] == str(int(snowflake_floor(started))-1)
    # This arrived during the original scan, before the old channel was marked done.
    source_id = str(int(snowflake_floor(started+600))+1)
    records = [{'id': source_id, 'user_id': '11', 'role_id': '90', 'revision': 0,
                'text': 'I love trance', 'at': started+600}]
    result = {'summary': 'Music chat', 'notes': [
        {'source_id': source_id, 'category': 'music', 'evidence': 'I love trance'}]}
    scheduler.finish(claim['id'], records, result, 1, source_id, True, {},
        error='Summary failed', newest_id=source_id)
    assert value(store, 'cursors') == original
    assert not store.chat_context('123', 'public:1', '11')['personal_memory']['notes']
    assert value(store, 'budget')['used'] == 30
    clock[0] += 1800
    assert scheduler.claim() is None  # Pending owner report still blocks learning.
    scheduler.delivered(claim['id'])
    restarted = AgentStore(Store(settings.db), settings)
    retry = restarted.claim()
    assert retry['after_id'] == claim['after_id']
    with pytest.raises(ValueError, match='cursor'):
        restarted.finish(retry['id'], [], {}, 1, source_id, True, {}, newest_id=retry['until_id'])
    assert value(store, 'cursors') == original
    saved = restarted.finish(retry['id'], records, result, 1, source_id, False, {}, newest_id=source_id)
    assert saved['saved'] == 1
    assert value(store, 'budget')['used'] == 31
    assert value(store, 'cursors')['1']['after_id'] == str(int(retry['until_id'])-1)


def test_full_day_schedule_paces_budget_survives_restart_and_expires(learning):
    clock, store, settings, scheduler = learning
    original_config = scheduler.state()['config']
    started = clock[0]-12*3600
    cursors = {c: {'before': '100', 'done_at': started+100,
        'after_id': snowflake_floor(started)} for c in ('1', '2')}
    with store.connection() as db:
        scheduler.put(db, 'started', started)
        scheduler.put(db, 'budget', {'day': 0, 'used': 120})
        scheduler.put(db, 'cursors', cursors)
    until = clock[0]+86400
    state = scheduler.start_learning_window(30, 24, 1, 'maintainer:codex')
    assert state['version'] == 2 and state['started'] == started
    assert state['config'] == original_config and value(store, 'cursors') == cursors
    assert value(store, 'budget') == {'day': 0, 'used': 120}
    assert state['learning_schedule']['window']['until'] == until
    with pytest.raises(ValueError, match='fresh preview'):
        scheduler.start_learning_window(30, 24, 1, 'maintainer:codex')
    counts = {0: 120, 1: 0}
    for index in range(48):
        scheduler = AgentStore(Store(settings.db), settings)
        claim = scheduler.claim()
        assert claim and 0 < claim['limit'] <= 30
        assert claim['channel_id'] == ('1' if index % 2 == 0 else '2')
        assert scheduler.state()['next_learning'] == clock[0]+1800
        counts[claim['budget_day']] += claim['limit']
        assert counts[claim['budget_day']] <= 360
        low = str(int(claim['after_id'])+1)
        high = str(int(claim['after_id'])+claim['limit'])
        # A mistaken done=True cannot skip unseen messages after a full batch.
        scheduler.finish(claim['id'], [], {}, claim['limit'], low, True, {}, newest_id=high)
        assert value(store, 'cursors')[claim['channel_id']]['after_id'] == high
        scheduler.delivered(claim['id'])
        clock[0] += 1800
    assert clock[0] == until
    assert scheduler.state()['learning_schedule'] == {'minutes': 360, 'window': None}
    assert scheduler.claim()
    assert scheduler.state()['next_learning'] == until+360*60
    with store.connection() as db:
        audit = db.execute('SELECT actor,action FROM audit WHERE action="fri_learning_window"').fetchall()
        assert len(audit) == 1 and audit[0]['actor'] == 'maintainer:codex'


def test_owner_schedule_change_or_pause_takes_precedence_over_window(learning):
    _, _, _, scheduler = learning
    scheduler.start_learning_window(30, 24, 1, 'maintainer:codex')
    scheduler.update({'learning_first_minutes': 60}, 2, '10')
    assert scheduler.state()['learning_schedule'] == {'minutes': 60, 'window': None}
    scheduler.start_learning_window(30, 24, 3, 'maintainer:codex')
    scheduler.update({'learning_enabled': False}, 4, '10')
    assert scheduler.claim() is None
    with pytest.raises(ValueError, match='Enable learning'):
        scheduler.start_learning_window(30, 24, 5, 'maintainer:codex')


def test_window_expiry_does_not_leave_a_long_temporary_deadline(learning):
    clock, store, _, scheduler = learning
    with store.connection() as db:
        scheduler.put(db, 'started', clock[0]-2*86400)
    scheduler.update({'learning_later_minutes': 60}, 1, '10')
    scheduler.start_learning_window(360, 1, 2, 'maintainer:codex')
    claim = scheduler.claim()
    scheduler.finish(claim['id'], [], {}, 0, None, True, {})
    scheduler.delivered(claim['id'])
    clock[0] += 3600
    assert scheduler.state()['learning_schedule'] == {'minutes': 60, 'window': None}
    assert scheduler.claim()  # Expiry cannot leave a five-hour stale delay.
    assert scheduler.state()['next_learning'] == clock[0]+3600


def test_old_claim_refund_cannot_credit_another_budget_day(learning):
    _, store, _, scheduler = learning
    claim = scheduler.claim()
    with store.connection() as db:
        scheduler.put(db, 'budget', {'day': 1, 'used': 40})
    scheduler.finish(claim['id'], [], {}, 0, None, True, {})
    assert value(store, 'budget') == {'day': 1, 'used': 40}


@pytest.mark.parametrize('failure', [False, True])
def test_heartbeat_records_actual_tick_outcome(learning, failure, monkeypatch, tmp_path):
    monkeypatch.setattr('fri.fri_workspace.deliver_one', AsyncMock())
    monkeypatch.setattr('fri.fri_social.deliver_one', AsyncMock())
    monkeypatch.setattr('fri.fri_fixes.check', AsyncMock())
    clock, _, _, scheduler = learning
    async def scenario():
        fri = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, data=tmp_path,
            api=AsyncMock(return_value=scheduler.state()))
        agent = Agent(fri)
        agent.send_reports = AsyncMock()
        agent.learn = AsyncMock(side_effect=ValueError('unavailable') if failure else None)
        agent.attend = AsyncMock()
        await agent.heartbeat.coro(agent)
        assert agent.heartbeat_started == agent.heartbeat_finished == clock[0]
        assert agent.heartbeat_status == ('deferred' if failure else 'ok')
        assert agent.stages['learning']['error'] == ('ValueError' if failure else None)
        agent.attend.assert_awaited_once()  # A failed stage no longer skips the ones after it.
    asyncio.run(scenario())


def test_learning_keeps_what_a_member_is_working_on_in_its_channel(learning):
    clock, store, settings, scheduler = learning
    settings = replace(settings, agent_history_channels='1')
    scheduler = AgentStore(store, settings)
    channel = HistoryChannel()
    channel.add(clock[0]-100, text="I'm building a Factorio mod")
    client = TestClient(create_app(settings))
    asked = []

    async def api(method, path, payload=None):
        response = client.request(method, path, json=payload, headers=headers('fri'))
        assert response.status_code == 200, response.text
        return response.json()

    async def model(role, instructions, context, schema, model, effort):
        asked.append((instructions, schema))
        return {'summary': 'Modding chat.', 'notes': [{'source_id': context['messages'][0]['id'], 'category': 'projects',
                                                       'evidence': "I'm building a Factorio mod"}]}, {}, 'completed'

    async def scenario():
        fri = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, api=api,
            get_channel=lambda _: channel, get_user=lambda _: SimpleNamespace(send=AsyncMock()))
        agent = Agent(fri)
        agent.state = scheduler.state()
        agent.member = AsyncMock(return_value=SimpleNamespace(roles=[SimpleNamespace(id=90)]))
        agent.profile = AsyncMock(side_effect=lambda channel, user: scheduler.context(str(channel), str(user)))
        agent.run = model
        assert (await agent.learn())['saved'] == 1

    asyncio.run(scenario())
    instructions, schema = asked[0]
    assert "projects (what they are doing) only and always with I'm working on" in instructions
    assert 'projects' in schema['properties']['notes']['items']['properties']['category']['enum']
    assert [(n['scope'], n['category'], n['evidence']) for n in store.chat_context(
        '123', 'public:1', '11', True)['personal_memory']['notes']] == [('here', 'projects', "I'm building a Factorio mod")]
    assert store.chat_context('123', 'public:2', '11', True)['personal_memory']['notes'] == []
