"""An exploration pass takes its members' erasure baseline before it reads anything (#310's round 1): a "forget me" here
while the roster or the target is read never becomes the baseline the pass's model requests are held to."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from gene.api import create_app

from compat_tests.test_api import headers
from compat_tests.test_fri_exploration import archive, by_id, offered, row
from compat_tests.test_fri_learning import learning  # noqa: F401 (the fixture)
from fri.fri_agent import Agent


def explore(learning, monkeypatch, erase_at):  # noqa: F811
    """Two exploration passes over member 11, as test_fri_exploration's: the first reads their history broadly and notes
    a topic, the second asks the planner with it (on a controller that says what its context was read at) or reads
    broadly again. erase_at: (pass, 'roster' or 'target'), when member 11's "forget me" lands here, with Gene not told
    yet. Returns each pass's model requests, as (kind, data)."""
    clock, store, settings, scheduler = archive(learning)
    client = TestClient(create_app(settings))
    capabilities = offered(client)
    old, later = row(clock[0]-120*86400), row(clock[0]-160*86400, text='minecraft redstone was fun')
    channel = SimpleNamespace(id=1, name='memba-chat', guild=SimpleNamespace(id=123, me=object()),
                              permissions_for=lambda _: SimpleNamespace(view_channel=True, read_message_history=True))
    pages, agents, passes, asked = iter([[old], [old, later], []]), [], [], {1: [], 2: []}

    def erase(where):
        if (len(passes), where) == erase_at:
            agents[0].forget(11, erased=True)

    async def api(method, path, payload=None):
        response = client.request(method, path, json=payload, headers=headers('fri'))
        assert response.status_code == 200, response.text
        if path.endswith('/target'):
            erase('target')  # Gene answered; this lands before Fri resumes.
        return response.json()

    async def http(route, params):
        if route.url.endswith('/members'):
            erase('roster')  # While the roster is read.
            return [{'user': {'id': '11', 'username': 'friend', 'bot': False}, 'roles': ['90']}]
        found = [item for item in next(pages, []) if int(params['min_id']) <= int(item['id']) < int(params['max_id'])]
        return {'messages': [[item] for item in found], 'doing_deep_historical_index': False}

    async def sign_in():
        return {}

    async def analyze(chat, instruction, data, model, effort, schema, **kwargs):
        if 'messages' in data:
            asked[len(passes)].append(('summary', data))
            item = data['messages'][0]
            return {'summary': 'Minecraft discussion', 'notes': [], 'observations': [
                {'source_id': item['id'], 'category': 'games', 'topic': 'Minecraft', 'evidence': item['text']}]}, {}, \
                'completed'
        asked[len(passes)].append(('planner', data))
        return {'term': 'minecraft', 'reason': 'follow their Minecraft discussions'}, {}, 'completed'

    monkeypatch.setattr('fri.fri_agent.analyze', analyze)

    async def scenario():
        fri = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, api=api, get_channel=lambda _: channel,
                              get_user=lambda _: SimpleNamespace(send=AsyncMock()), capabilities=capabilities,
                              http=SimpleNamespace(request=by_id(http, [old, later])),
                              chat=SimpleNamespace(headers=sign_in))
        agent = Agent(fri)
        agents.append(agent)
        agent.state = scheduler.state()
        agent.member = AsyncMock(side_effect=lambda user, **_: SimpleNamespace(
            id=user, bot=False, display_name='friend', roles=[SimpleNamespace(id=90)]))
        agent.record = AsyncMock()
        passes.append(1)
        await agent.learn()
        if erase_at[0] == 1:
            return
        clock[0] += 1800
        agent.state = scheduler.state()
        passes.append(2)
        await agent.learn()

    asyncio.run(scenario())
    return asked


def theirs(requests):
    """The requests carrying member 11's words: any planner request (it carries their name and historical evidence),
    and any summary request with a record of theirs."""
    return [kind for kind, data in requests if kind == 'planner' or
            any(record['user_id'] == '11' for record in data['messages'])]


def test_a_first_visits_summary_never_carries_an_erasure_here_taken_in_while_the_target_is_assigned(
        learning, monkeypatch):  # noqa: F811
    """#310's round 1: on a first visit the planner is skipped, so the member's "forget me" here, landing while Gene
    assigned the target and before Gene heard of it, was in the baseline taken before the search: their archived words
    still went to the summary model. The baseline is taken before the roster is read: nothing of theirs is sent."""
    asked = explore(learning, monkeypatch, (1, 'target'))
    assert theirs(asked[1]) == []


@pytest.mark.parametrize('where', ['roster', 'target'])
def test_a_later_visit_never_carries_an_erasure_here_taken_in_while_the_pass_reads_its_member(learning, monkeypatch,  # noqa: F811
                                                                                             where):
    """#310's round 1: on a later visit, the member's "forget me" here while the roster is read was absorbed into the
    baseline taken after it, so the planner got their historical evidence (or, without a planner, the summary their
    archived words). It's absorbed no more, and nothing of theirs is sent in that pass, whatever the controller."""
    asked = explore(learning, monkeypatch, (2, where))
    assert theirs(asked[1])  # The first visit, before they erased, as before.
    assert theirs(asked[2]) == []


def test_a_pass_with_nothing_moved_asks_as_before(learning, monkeypatch):  # noqa: F811
    asked = explore(learning, monkeypatch, (3, 'never'))
    assert theirs(asked[1]) and theirs(asked[2])
