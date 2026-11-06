"""An exploration's planner request is held to the revision Gene read its context at (fri #304, HumanMint/gene#197).
A controller that doesn't say gets no planner request: the pass reads the member's history broadly instead."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from gene.api import create_app

from compat_tests.test_api import headers
from compat_tests.test_fri_learning import dm_owner, learning  # noqa: F401 (the fixture)
from compat_tests.test_fri_learning_cutoffs import remove_a_preference, saved_preference
from fri.fri_agent import Agent
from fri.fri_errors import ControllerRejected


def planned_pass(learning, monkeypatch, moved):  # noqa: F811
    """Two exploration passes over member 11, as test_fri_exploration's: the first reads their history broadly and notes
    a topic, the second may ask the planner with it. moved: what happens while the second pass's target is assigned.
    Returns the planner requests, the second pass's target and the plan it asked Gene for."""
    from compat_tests.test_fri_exploration import archive, by_id, offered, row
    clock, store, settings, scheduler = archive(learning)
    client = TestClient(create_app(settings))
    capabilities = offered(client)
    old, later = row(clock[0]-120*86400), row(clock[0]-160*86400, text='minecraft redstone was fun')
    channel = SimpleNamespace(id=1, name='memba-chat', guild=SimpleNamespace(id=123, me=object()),
                              permissions_for=lambda _: SimpleNamespace(view_channel=True, read_message_history=True))
    planned, targets, plans, pages, passes = [], [], [], iter([[old], [old, later], []]), []
    saved_preference(store, '11')

    async def api(method, path, payload=None):
        response = client.request(method, path, json=payload, headers=headers('fri'))
        if path.endswith('/finish') and response.status_code != 200:
            return {}  # Gene refuses what a removal invalidated; this pass is done either way.
        if response.status_code != 200:
            raise ControllerRejected(response.json().get('detail', ''), response.status_code)
        answer = response.json()
        if path.endswith('/target') and passes == [1, 2]:
            targets.append(answer['target'])
            if moved == 'removal through Gene':
                remove_a_preference(store, '11')  # Gene built the target's context; this lands before Fri resumes.
        if path.endswith('/plan') and passes == [1, 2]:
            plans.append(payload)
        return answer

    async def http(route, params):
        if route.url.endswith('/members'):
            return [{'user': {'id': '11', 'username': 'friend', 'bot': False}, 'roles': ['90']}]
        return {'messages': [[found] for found in next(pages, [])], 'doing_deep_historical_index': False}

    async def sign_in():
        return {}

    async def analyze(chat, instruction, data, model, effort, schema, **kwargs):
        if 'messages' in data:
            item = data['messages'][0]
            return {'summary': 'Minecraft discussion', 'notes': [], 'observations': [
                {'source_id': item['id'], 'category': 'games', 'topic': 'Minecraft', 'evidence': item['text']}]}, {}, \
                'completed'
        planned.append(data)
        return {'term': 'minecraft', 'reason': 'follow their Minecraft discussions'}, {}, 'completed'

    monkeypatch.setattr('fri.fri_agent.analyze', analyze)

    async def scenario():
        fri = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, api=api, get_channel=lambda _: channel,
                              get_user=lambda _: dm_owner(), capabilities=capabilities,
                              http=SimpleNamespace(request=by_id(http, [old, later])),
                              chat=SimpleNamespace(headers=sign_in))
        agent = Agent(fri)
        agent.state = scheduler.state()
        agent.member = AsyncMock(side_effect=lambda user, **_: SimpleNamespace(
            id=user, bot=False, display_name='friend', roles=[SimpleNamespace(id=90)]))
        agent.record = AsyncMock()
        passes.append(1)
        first = await agent.learn()
        assert first['historical_notes'] == 1  # The topics a later planner request carries.
        clock[0] += 1800
        agent.state = scheduler.state()
        passes.append(2)
        await agent.learn()

    asyncio.run(scenario())
    [target] = targets
    return planned, target, plans


def test_a_planner_is_never_asked_with_a_context_a_removal_through_gene_made_stale(learning, monkeypatch):  # noqa: F811
    """fri #304: Gene built the target's context (historical topics with their evidence, previous searches, their
    name), then the member removed a saved preference through Gene before Fri resumed. That cleared the exploration
    state and moved their revision, and Fri's own read after the answer saw only the new one, so the fence passed and
    the planner got the old context. Gene refused the plan afterwards. The target's revision now comes from the same
    transaction as its context, and the planner is held to that: nothing is asked. A controller that doesn't say gets
    no planner request at all."""
    planned, _, _ = planned_pass(learning, monkeypatch, 'removal through Gene')
    assert planned == []


@pytest.mark.parametrize('moved', ['nothing'])
def test_a_planner_is_asked_only_when_the_controller_says_what_its_context_was_read_at(learning, monkeypatch,  # noqa: F811
                                                                                       moved):
    """With nothing moved, a controller that returns the target's revision gets the planner request as before. One that
    doesn't (before HumanMint/gene#197) can't say what the context is current for, so the planner isn't asked: the
    pass reads the member's history broadly, as a first visit does."""
    planned, target, plans = planned_pass(learning, monkeypatch, moved)
    if 'revision' in target:
        assert len(planned) == 1 and 'historical_topics' in planned[0] and plans[0]['term'] == 'minecraft'
    else:
        assert planned == [] and plans[0]['term'] == ''
