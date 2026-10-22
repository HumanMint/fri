import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from gene.api import create_app
from gene.fri_agent_store import AgentStore, snowflake_floor
from gene.store import Store

from compat_tests.test_api import headers
from compat_tests.test_fri_learning import learning as learning
from fri import feedback as commands
from fri.fri import Fri
from fri.fri_agent import Agent
from fri.fri_explore import search


def archive(learning):
    clock, store, settings, _ = learning
    settings = replace(settings, agent_history_channels='1')
    scheduler = AgentStore(store, settings)
    scheduler.update({'exploration_enabled': True, 'exploration_days': 365}, 1, '10')
    return clock, store, settings, scheduler


def assigned(scheduler, claim):
    return scheduler.exploration.target(claim['id'], [{'user_id': '11', 'name': 'friend', 'role_id': '90'}])


def row(at, user='11', text='played minecraft with everyone', channel='1', bot=False):
    return {'id': str(int(snowflake_floor(at))+1), 'channel_id': channel,
            'author': {'id': user, 'bot': bot}, 'content': text}


def raw_record(item):
    import discord
    return {'id': item['id'], 'user_id': item['author']['id'], 'display_name': 'friend',
            'role_id': '90', 'revision': 0, 'text': item['content'],
            'at': discord.utils.snowflake_time(int(item['id'])).timestamp()}


def note(item, **changes):
    return {'source_id': item['id'], 'category': 'games', 'topic': 'Minecraft',
            'evidence': item['content'], **changes}


def finish(scheduler, claim, items, observations=(), **changes):
    return scheduler.finish(claim['id'], [raw_record(r) for r in items],
        {'summary': 'Minecraft discussion', 'observations': list(observations)}, len(items),
        min((r['id'] for r in items), default=None), True, {},
        scanned_ids=[r['id'] for r in items], **changes)


def test_real_learning_harness_explores_old_messages_and_deduplicates_cross_query(learning):
    clock, store, settings, scheduler = archive(learning)
    client = TestClient(create_app(settings))
    old = row(clock[0]-120*86400)
    new = row(clock[0]-160*86400, text='minecraft redstone was fun')
    store.personal_control('13', 'disable')
    owner = SimpleNamespace(send=AsyncMock())
    channel = SimpleNamespace(id=1, name='memba-chat', guild=SimpleNamespace(id=123, me=object()),
        permissions_for=lambda _: SimpleNamespace(view_channel=True, read_message_history=True))
    plans = iter([{'member_id': '11', 'term': 'minecraft', 'reason': 'follow their Minecraft discussions'},
                  {'member_id': '11', 'term': 'minecraft', 'reason': 'continue the unchecked older results'}])
    pages = iter([[old], [old, new], []])
    model_inputs, searches = [], []

    async def api(method, path, payload=None):
        response = client.request(method, path, json=payload, headers=headers('fri'))
        assert response.status_code == 200, response.text
        return response.json()

    async def http(route, params):
        if route.url.endswith('/members'):
            return [{'user': {'id': str(user), 'username': 'friend', 'bot': user == 99},
                     'roles': [] if user == 12 else ['90']} for user in (11, 12, 13, 99)]
        assert route.method == 'GET' and route.url.endswith('/guilds/123/messages/search')
        assert params['channel_id'] == '1' and params['include_nsfw'] == 'false'
        assert params['author_type'] == 'user' and params['limit'] <= 25
        assert int(params['min_id']) == int(snowflake_floor(clock[0]-365*86400))
        searches.append(params.copy())
        return {'messages': [[r] for r in next(pages)], 'doing_deep_historical_index': False}

    async def infer(role, instruction, data, schema, model, effort):
        assert (model, effort) == ('gpt-6-luna', 'medium')
        model_inputs.append((role, data))
        if role == 'exploration_plan':
            return next(plans), {'input_tokens': 50, 'output_tokens': 20}, 'completed'
        raise AssertionError('Unexpected model task')

    async def scenario():
        fri = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, api=api,
            get_channel=lambda _: channel, get_user=lambda _: owner, http=SimpleNamespace(request=http))
        def runtime():
            agent = Agent(fri)
            agent.state = scheduler.state()
            agent.member = AsyncMock(side_effect=lambda user, **_: SimpleNamespace(id=user, bot=False,
                display_name='current nickname', roles=[] if user == 12 else [SimpleNamespace(id=90)]))
            agent.run = infer
            return agent
        # Summary mocks consume the real normalized records, not Discord response objects.
        async def summarize(role, instruction, data, schema, model, effort):
            if role == 'summary':
                model_inputs.append((role, data))
                assert [r['user_id'] for r in data['messages']] == ['11']
                item = data['messages'][0]
                return {'summary': 'Minecraft discussion', 'notes': [], 'observations': [
                    {'source_id': item['id'], 'category': 'games', 'topic': 'Minecraft', 'evidence': item['text']}]}, {
                        'input_tokens': 70, 'output_tokens': 10}, 'completed'
            return await infer(role, instruction, data, schema, model, effort)
        agent = runtime()
        agent.run = summarize
        first = await agent.learn()
        assert first['scanned'] == 1 and first['eligible'] == first['historical_notes'] == 1
        history = scheduler.context('1', '11')['historical_topics']
        assert history[0]['source_id'] == old['id'] and history[0]['source_date']
        assert not scheduler.context('1', '12')['historical_topics']
        assert not scheduler.context('1', '11', guild='other')['historical_topics']
        assert not scheduler.context('2', '11')['historical_topics']
        assert not store.chat_context('123', 'public:1', '11')['personal_memory']['notes']
        assert not scheduler.context('1', '11')['summary']  # Old topics cannot masquerade as current chat.
        assert 'historical_topics' not in await agent.extra_context(1, 11, False)
        clock[0] += 1800
        agent = runtime()  # Restart and overlap a different search with a previously read message.
        agent.run = summarize
        second = await agent.learn()
        assert second['scanned'] == 2 and second['eligible'] == second['historical_notes'] == 1
        assert model_inputs[-1][1]['messages'][0]['id'] == new['id']
        assert searches[1]['author_id'] == '11' and searches[1]['content'] == 'minecraft'
        clock[0] += 1800
        third = await agent.learn()
        assert third['scanned'] == 0
        assert searches[2]['max_id'] == new['id']
        assert len([x for x in model_inputs if x[0] == 'summary']) == 2
        assert owner.send.await_count == 3 and not scheduler.reports()
        with store.connection() as db:
            assert db.execute('SELECT count(*) FROM fri_explore_seen').fetchone()[0] == 2
            assert db.execute('SELECT done FROM fri_explore_queries WHERE term="minecraft"').fetchone()[0] == 1
            assert db.execute('SELECT count(*) FROM fri_explore_notes').fetchone()[0] == 2
    asyncio.run(scenario())


def test_plans_are_bound_to_claim_members_channels_and_privacy(learning):
    _, store, settings, scheduler = archive(learning)
    claim = assigned(scheduler, scheduler.claim())
    client = TestClient(create_app(settings))
    payload = {'member_id': '11', 'term': 'minecraft', 'reason': 'learn a shared game'}
    for role in ('runtime', 'worker', 'deploy'):
        assert client.post('/agent/learning/'+claim['id']+'/plan', headers=headers(role), json=payload).status_code == 401
    for member, term in [('999', ''), ('', 'password'), ('', 'https://example.com'), ('', 'a'*81)]:
        with pytest.raises(ValueError):
            scheduler.exploration.prepare(claim['id'], member, term, 'read history')
    plan = scheduler.exploration.prepare(claim['id'], **payload)
    assert plan['before_id'] == claim['until_id']
    with pytest.raises(ValueError, match='already'):
        scheduler.exploration.prepare(claim['id'], **payload)
    store.personal_control('11', 'disable')
    with pytest.raises(ValueError):
        scheduler.exploration.prepare(claim['id'], **payload)


def test_failed_inference_does_not_check_off_history_and_erasure_blocks_inflight_notes(learning):
    clock, store, settings, scheduler = archive(learning)
    item = row(clock[0]-100*86400)
    claim = assigned(scheduler, scheduler.claim())
    scheduler.exploration.prepare(claim['id'], '11', 'minecraft', 'discover who discusses the game')
    finish(scheduler, claim, [item], [note(item)], error='Summary failed')
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM fri_explore_queries').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM fri_explore_notes').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM fri_explore_seen').fetchone()[0] == 0
    scheduler.delivered(claim['id'])
    clock[0] += 1800
    scheduler = AgentStore(Store(settings.db), settings)
    claim = assigned(scheduler, scheduler.claim())
    scheduler.exploration.prepare(claim['id'], '11', 'minecraft', 'try the unread archive')
    assert finish(scheduler, claim, [item], [note(item)])['historical_notes'] == 1
    scheduler.delivered(claim['id'])
    clock[0] += 1800
    claim = assigned(scheduler, scheduler.claim())
    scheduler.exploration.prepare(claim['id'], '11', '', 'look at another conversation')
    store.personal_control('11', 'disable')
    result = finish(scheduler, claim, [item], [note(item)])
    assert result['error'] and result['historical_notes'] == 0
    assert 'Minecraft' not in scheduler.reports()[0]['body']
    assert not scheduler.context('1', '11')['historical_topics']
    assert not scheduler.overview()['historical_notes']
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM fri_explore_seen').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM fri_explore_members').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM fri_explore_queries').fetchone()[0] == 0


@pytest.mark.parametrize('bad', ['future', 'channel', 'member', 'indexing'])
def test_discord_search_rejects_unready_or_out_of_scope_results(learning, bad):
    clock, _, _, scheduler = archive(learning)
    claim = assigned(scheduler, scheduler.claim())
    item = row(clock[0]-100, user='11')
    plan = {'member_id': '11', 'term': 'minecraft', 'before_id': claim['until_id']}
    if bad == 'future':
        item['id'] = snowflake_floor(clock[0]+60)
    elif bad == 'channel':
        item['channel_id'] = 'private'
    elif bad == 'member':
        item['author']['id'] = '12'
    agent = SimpleNamespace(fri=SimpleNamespace(guild_id=123, http=SimpleNamespace(request=AsyncMock(return_value={
        'messages': [[item]], 'doing_deep_historical_index': bad == 'indexing'}))))
    with pytest.raises(ValueError):
        asyncio.run(search(agent, claim, plan))


def test_source_forgery_and_sensitive_candidates_do_not_become_member_notes(learning):
    clock, _, _, scheduler = archive(learning)
    item = row(clock[0]-100*86400)
    claim = assigned(scheduler, scheduler.claim())
    scheduler.exploration.prepare(claim['id'], '11', '', 'discover past conversations')
    findings = [note(item, source_id='999'), note(item, evidence='made up quote'),
                note(item, topic='health diagnosis'), note(item)]
    assert finish(scheduler, claim, [item], findings)['historical_notes'] == 1
    assert scheduler.context('1', '11')['historical_topics'][0]['evidence'] == item['content']
    # A short nonempty search page remains unfinished, unlike ordinary history pagination.
    scheduler.delivered(claim['id'])
    clock[0] += 1800
    next_claim = assigned(scheduler, scheduler.claim())
    assert scheduler.exploration.prepare(next_claim['id'], '11', '', 'continue older history')['before_id'] == item['id']


def test_planner_and_summary_usage_outbox_delivers_through_controller(learning, tmp_path):
    _, _, settings, scheduler = archive(learning)
    client = TestClient(create_app(settings))
    async def scenario():
        async def api(method, path, payload=None):
            response = client.request(method, path, json=payload, headers=headers('fri'))
            if response.status_code != 200:
                raise commands.UserFeedbackCheckFailure('Usage rejected')
            return response.json()
        fri = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, api=api,
            data=tmp_path/'runtime', usage_lock=asyncio.Lock())
        fri.flush_usage = lambda: Fri.flush_usage(fri)
        agent = Agent(fri)
        for role in ('exploration_plan', 'summary'):
            await agent.record(role, 'gpt-6-luna', 'medium', {'input_tokens': 100, 'output_tokens': 20}, 'completed')
        assert not list((fri.data/'usage').glob('*.json'))
        assert {r['role'] for r in scheduler.usage()} == {'exploration_plan', 'summary'}
    asyncio.run(scenario())
