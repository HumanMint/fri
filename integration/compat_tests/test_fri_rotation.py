"""Exercise durable member turns across channels, changing rosters and failed searches."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from fastapi.testclient import TestClient
from gene.api import create_app
from gene.fri_agent_store import AgentStore
from gene.store import Store

from compat_tests.test_api import headers
from compat_tests.test_fri_exploration import archive, finish, row
from compat_tests.test_fri_learning import learning as learning
from fri.fri_explore import roster


def members(*users):
    return [{'user_id': str(user), 'name': 'friend '+str(user), 'role_id': '90'} for user in users]


def turn(clock, scheduler, roster, *, error='', term=''):
    claim = scheduler.exploration.target(scheduler.claim()['id'], roster)
    user = claim['target']['user_id']
    plan = scheduler.exploration.prepare(claim['id'], user, term, 'read history')
    item = row(discord.utils.snowflake_time(int(plan['before_id'])).timestamp()-60,
               user=user, channel=claim['channel_id'])
    result = finish(scheduler, claim, [item], error=error)
    scheduler.delivered(claim['id'])
    clock[0] += 1800
    return claim, plan, result


def test_every_member_gets_one_turn_before_repeats_across_channels_and_restarts(learning):
    clock, store, settings, scheduler = archive(learning)
    settings = replace(settings, agent_history_channels='1,2')
    scheduler = AgentStore(store, settings)
    # More members than the old 20-member prompt limit. No partial-roster starvation.
    people = members(*range(11, 36))
    chosen = []
    for _ in people:
        claim, plan, result = turn(clock, scheduler, people, term='movies')
        chosen.append(claim['target']['user_id'])
        assert claim['target']['round'] == 1 and plan['term'] == ''
        assert claim['target']['position'] == len(chosen) and claim['target']['total'] == 25
        assert not result['error']
        scheduler = AgentStore(Store(settings.db), settings)
    assert len(set(chosen)) == len(people)
    claim, _, _ = turn(clock, scheduler, people)
    assert claim['target']['round'] == 2 and claim['target']['user_id'] == chosen[0]
    assert claim['target']['position'] == 1


def test_existing_coverage_is_credited_once_and_new_role_members_join_current_round(learning):
    clock, store, settings, scheduler = archive(learning)
    with store.connection() as db:
        for i in range(2):
            db.execute('INSERT INTO fri_explore_seen VALUES (?,?,?,?)',
                       (str(i), '1', '11', clock[0]-100+i))
    scheduler = AgentStore(Store(settings.db), settings)  # Run additive migration against old coverage.
    first, _, _ = turn(clock, scheduler, members(11, 12, 13))
    assert first['target']['user_id'] == '12' and first['target']['position'] == 2
    second, _, _ = turn(clock, scheduler, members(11, 12, 13, 14))
    assert second['target']['user_id'] == '13' and second['target']['round'] == 1
    # A departing/opted-out member cannot hold up the rest of the roster.
    store.personal_control('14', 'disable')
    third, _, _ = turn(clock, scheduler, members(11, 12, 13, 14))
    assert third['target']['round'] == 2 and third['target']['user_id'] == '11'
    assert third['target']['total'] == 3


def test_failed_pass_does_not_complete_turn_or_advance_query(learning):
    clock, store, settings, scheduler = archive(learning)
    failed, _, result = turn(clock, scheduler, members(11, 12), error='summary unavailable')
    assert result['error']
    with store.connection() as db:
        assert not db.execute('SELECT 1 FROM fri_explore_rotation').fetchone()
        assert not db.execute('SELECT 1 FROM fri_explore_queries').fetchone()
    scheduler = AgentStore(Store(settings.db), settings)
    retry, _, _ = turn(clock, scheduler, members(11, 12))
    assert retry['target'] == failed['target']
    next_member, _, _ = turn(clock, scheduler, members(11, 12))
    assert next_member['target']['user_id'] == '12'


def test_empty_history_counts_as_a_checked_turn_without_claiming_member_knowledge(learning):
    clock, store, settings, scheduler = archive(learning)
    settings = replace(settings, agent_history_channels='1,2')
    scheduler = AgentStore(store, settings)
    people = members(11, 12)
    empty = scheduler.exploration.target(scheduler.claim()['id'], people)
    scheduler.exploration.prepare(empty['id'], '11', '', 'read broadly')
    finish(scheduler, empty, [])
    body = scheduler.reports()[0]['body']
    assert 'checked turn, not a learned preference' in body and 'turn 1/2' in body
    scheduler.delivered(empty['id'])
    clock[0] += 1800
    other, _, _ = turn(clock, scheduler, people)
    assert other['target']['user_id'] == '12'
    retry = scheduler.exploration.target(scheduler.claim()['id'], people)
    assert retry['target']['user_id'] == '11' and retry['channel_id'] == '2'
    assert retry['target']['first_visit']
    scheduler.exploration.prepare(retry['id'], '11', 'watched', 'read broadly')
    finish(scheduler, retry, [])
    scheduler.delivered(retry['id'])
    clock[0] += 1800
    turn(clock, scheduler, people)
    exhausted = scheduler.exploration.target(scheduler.claim()['id'], people)
    plan = scheduler.exploration.prepare(exhausted['id'], '11', '', 'read broadly')
    assert exhausted['target']['exhausted'] and plan['skip']
    finish(scheduler, exhausted, [])
    assert 'No unread history remains' in scheduler.reports()[0]['body']


def test_target_and_scope_are_controller_bound_and_cannot_be_overridden_by_a_plan(learning):
    clock, store, settings, scheduler = archive(learning)
    client = TestClient(create_app(settings))
    claim = scheduler.claim()
    url = '/agent/learning/'+claim['id']+'/target'
    for role in ('runtime', 'worker', 'deploy'):
        assert client.post(url, json={'members': members(11)}, headers=headers(role)).status_code == 401
    for invalid in ([], members(11, 11), [{'user_id': '11', 'role_id': '99', 'name': 'wrong role'}]):
        with pytest.raises(ValueError):
            scheduler.exploration.target(claim['id'], invalid)
    picked = client.post(url, json={'members': members(11, 12)}, headers=headers('fri'))
    assert picked.status_code == 200 and picked.json()['target']['user_id'] == '11'
    for user in ('', '12', '999'):
        with pytest.raises(ValueError, match='assigned'):
            scheduler.exploration.prepare(claim['id'], user, '', 'change member')
    with pytest.raises(ValueError):
        scheduler.exploration.target(claim['id'], members(12))
    store.personal_control('11', 'disable')
    with pytest.raises(ValueError):
        scheduler.exploration.prepare(claim['id'], '11', '', 'revive erased target')
    result = finish(scheduler, claim, [])
    assert result['error']
    with store.connection() as db:
        assert not db.execute('SELECT 1 FROM fri_explore_rotation WHERE user_id="11"').fetchone()


def test_topic_queries_keep_useful_wording_and_fall_back_from_generic_or_exhausted_terms(learning):
    clock, _, _, scheduler = archive(learning)
    people = members(11)
    _, first, _ = turn(clock, scheduler, people, term='watched')
    assert first['term'] == ''  # Establish their wording before applying speculative filters.
    _, specific, _ = turn(clock, scheduler, people, term='watched')
    assert specific['term'] == 'watched'
    _, generic, _ = turn(clock, scheduler, people, term='movies')
    assert generic['term'] == ''
    claim = scheduler.exploration.target(scheduler.claim()['id'], people)
    scheduler.exploration.prepare(claim['id'], '11', 'playing', 'read game conversation')
    finish(scheduler, claim, [])
    scheduler.delivered(claim['id'])
    clock[0] += 1800
    _, fallback, _ = turn(clock, scheduler, people, term='playing')
    assert fallback['term'] == ''


def test_exhausted_history_rechecks_after_one_day_without_skip_turns_delaying_it(learning):
    clock, _, _, scheduler = archive(learning)
    people = members(11)
    claim = scheduler.exploration.target(scheduler.claim()['id'], people)
    scheduler.exploration.prepare(claim['id'], '11', '', 'read broadly')
    finish(scheduler, claim, [])
    scheduler.delivered(claim['id'])
    clock[0] += 1800
    skipped = scheduler.exploration.target(scheduler.claim()['id'], people)
    plan = scheduler.exploration.prepare(skipped['id'], '11', '', 'read broadly')
    assert plan['skip']
    finish(scheduler, skipped, [])
    scheduler.delivered(skipped['id'])
    clock[0] += 86400-1800
    refreshed, plan, result = turn(clock, scheduler, people)
    assert not plan['skip'] and plan['before_id'] == refreshed['until_id']
    assert result['eligible'] == 1


def test_roster_paginates_before_rotation_and_filters_bots_and_other_roles():
    first = [{'user': {'id': str(i), 'username': 'friend'}, 'roles': []} for i in range(1, 1001)]
    first[0]['roles'] = ['90']
    second = [{'user': {'id': '1001', 'username': 'old', 'global_name': 'current'}, 'nick': 'nickname', 'roles': ['90']},
              {'user': {'id': '1002', 'username': 'bot', 'bot': True}, 'roles': ['90']}]
    request = AsyncMock(side_effect=[first, second])
    agent = SimpleNamespace(fri=SimpleNamespace(guild_id=123, http=SimpleNamespace(request=request)))
    assert asyncio.run(roster(agent, '90')) == [members(1)[0] | {'name': 'friend'}, members(1001)[0] | {'name': 'nickname'}]
    assert request.call_args_list[1].kwargs['params']['after'] == 1000
    request.side_effect = [first, first]  # A repeated page is incomplete, never a partial authoritative roster.
    with pytest.raises(ValueError, match='incomplete'):
        asyncio.run(roster(agent, '90'))


def test_unread_channel_is_explored_before_refreshing_exhausted_history(learning):
    clock, store, settings, scheduler = archive(learning)
    settings = replace(settings, agent_history_channels='1,2')
    scheduler = AgentStore(store, settings)
    people = members(11)
    first, _, _ = turn(clock, scheduler, people)
    assert first['channel_id'] == '1'
    clock[0] += 26*3600
    drained = scheduler.exploration.target(scheduler.claim()['id'], people)
    assert drained['channel_id'] == '1'
    scheduler.exploration.prepare(drained['id'], '11', '', 'continue history')
    finish(scheduler, drained, [])
    scheduler.delivered(drained['id'])
    clock[0] += 26*3600
    next_turn = scheduler.exploration.target(scheduler.claim()['id'], people)
    assert next_turn['channel_id'] == '2'


def test_daily_refreshes_eventually_find_new_messages_in_each_exhausted_channel(learning):
    clock, store, settings, scheduler = archive(learning)
    settings = replace(settings, agent_history_channels='1,2')
    scheduler = AgentStore(store, settings)
    history = {'1': [row(clock[0]-1000, channel='1')], '2': [row(clock[0]-900, channel='2')]}
    selected, added = [], None
    for index in range(7):
        claim = scheduler.exploration.target(scheduler.claim()['id'], members(11))
        plan = scheduler.exploration.prepare(claim['id'], '11', '', 'read broadly')
        selected.append(claim['channel_id'])
        page = [r for r in history[claim['channel_id']] if int(r['id']) < int(plan['before_id'])]
        finish(scheduler, claim, page)
        scheduler.delivered(claim['id'])
        clock[0] += 26*3600
        if index == 3:
            added = row(clock[0]-100, channel='2', text='watched a new film')
            history['2'].append(added)
    assert selected == ['1', '1', '2', '2', '1', '1', '2']
    with store.connection() as db:
        assert db.execute('SELECT 1 FROM fri_explore_seen WHERE message_id=?', (added['id'],)).fetchone()
