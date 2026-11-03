"""A learning pass's author fence ends on one atomic check of every author (fri #291), and an exploration's planner
request is fenced against its target (fri #292)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from gene.api import create_app

from compat_tests.test_api import headers
from compat_tests.test_fri_learning import learning  # noqa: F401 (the fixture)
from compat_tests.test_fri_panel_and_learning_fences import batch
from fri.fri_agent import Agent


def fenced_batch(learning, monkeypatch):  # noqa: F811
    """batch(), on a controller offering the atomic author check, every controller call it makes recorded."""
    agent, store, ids, finishes, asked, signing = batch(learning, monkeypatch)
    agent.fri.capabilities = frozenset({'author-fence'})
    api, calls = agent.fri.api, []

    async def recording(method, path, payload=None):
        from fri.fri_errors import ControllerRejected
        calls.append(path)
        try:
            return await api(method, path, payload)
        except AssertionError as refused:  # batch()'s client asserts a 200: Gene's 409, as Fri's client raises it.
            if path == '/agent/authors/check':
                raise ControllerRejected(str(refused)) from None
            raise

    agent.fri.api = recording
    return agent, store, ids, finishes, asked, signing, calls


def test_a_learning_pass_never_sends_an_author_who_erased_after_their_own_read(learning, monkeypatch):  # noqa: F811
    """fri #291: the fence read each author's profile concurrently, so member 11's answer could come back, then 11
    erase through Gene while member 14's read was pending, and 11's text still go out on the old answer. Whatever the
    fence reads, its last word before the request is one atomic check of every author it keeps, so an author who
    erased by then is never sent."""
    async def scenario():
        agent, store, (first, second), finishes, asked, signing, calls = fenced_batch(learning, monkeypatch)
        profile, erased = agent.profile.side_effect, []
        signed = []
        signing.append(lambda: signed.append(True))

        def reading(channel, user):
            answer = profile(channel, user)
            if signed and user == 14 and not erased:  # Member 14's read, after 11's came back.
                store.personal_control('11', 'disable')
                erased.append(True)
            return answer
        agent.profile.side_effect = reading
        analyze = __import__('fri.fri_agent', fromlist=['analyze']).analyze

        async def sending(chat, instruction, data, model, effort, schema, **kwargs):
            if erased and any(record['user_id'] == '11' for record in data['messages']):
                raise AssertionError('member 11 was sent after they erased')
            return await analyze(chat, instruction, data, model, effort, schema, **kwargs)
        monkeypatch.setattr('fri.fri_agent.analyze', sending)
        await agent.learn()
        assert finishes and asked

    asyncio.run(scenario())


def test_a_learning_batch_with_nothing_moved_checks_its_authors_in_one_call(learning, monkeypatch):  # noqa: F811
    """fri #291: with nothing moved, the fence is one atomic check of every author, not a read per author, and the
    request is the same."""
    async def scenario():
        agent, store, (first, second), finishes, asked, signing, calls = fenced_batch(learning, monkeypatch)
        signed = []
        signing.append(lambda: signed.append(len(calls)))
        reads = []
        profile = agent.profile.side_effect
        agent.profile.side_effect = lambda channel, user: (reads.append(bool(signed)), profile(channel, user))[1]
        await agent.learn()
        assert [[record['id'] for record in data['messages']] for data in asked] == [[first, second]]
        assert calls[signed[0]:signed[0]+1] == ['/agent/authors/check']  # The fence's one call after the sign-in,
        assert True not in reads  # and no profile read after it.

    asyncio.run(scenario())


@pytest.mark.parametrize('moved', ['gene', 'refused throughout'])
def test_a_refused_atomic_check_drops_who_moved_and_checks_again(learning, monkeypatch, moved):  # noqa: F811
    """fri #291: Gene refuses the check without naming anyone. The fence then finds who moved, drops their records and
    checks the rest atomically again: member 11 erased through Gene during the sign-in, so 14's record alone goes. A
    check refused throughout stops the pass, as any failed read does, so nothing is sent and the batch is read
    again."""
    async def scenario():
        agent, store, (first, second), finishes, asked, signing, calls = fenced_batch(learning, monkeypatch)
        if moved == 'gene':
            signing.append(lambda: store.personal_control('11', 'disable'))
        else:
            api = agent.fri.api

            async def refusing(method, path, payload=None):
                if path == '/agent/authors/check':
                    from fri.fri_errors import ControllerRejected
                    calls.append(path)
                    raise ControllerRejected('Someone in that conversation changed their privacy settings.')
                return await api(method, path, payload)
            agent.fri.api = refusing
        await agent.learn()
        if moved == 'gene':
            assert [[record['id'] for record in data['messages']] for data in asked] == [[second]]
            assert calls.count('/agent/authors/check') == 2
        else:
            assert asked == [] and finishes[-1]['error']

    asyncio.run(scenario())


@pytest.mark.parametrize('moved', ['local', 'gene', 'nothing'])
def test_an_explorations_planner_is_fenced_against_its_target(learning, monkeypatch, moved):  # noqa: F811
    """fri #292: a later exploration asks a planner model with the channel's historical topics, whose notes carry
    evidence excerpts, and that request wasn't fenced: the target erasing during its sign-in, here or through Gene,
    still sent their name and exact evidence. Signed in, the target's Gene revision and erasures here are read again;
    moved, the planner isn't asked and the pass stops for that member, as when their eligibility changes."""
    from compat_tests.test_fri_exploration import archive, row
    clock, store, settings, scheduler = archive(learning)
    client = TestClient(create_app(settings))
    old, later = row(clock[0]-120*86400), row(clock[0]-160*86400, text='minecraft redstone was fun')
    channel = SimpleNamespace(id=1, name='memba-chat', guild=SimpleNamespace(id=123, me=object()),
                              permissions_for=lambda _: SimpleNamespace(view_channel=True, read_message_history=True))
    finishes, roles, pages, agents, passes = [], [], iter([[old], [old, later], []]), [], []
    targets = []

    async def api(method, path, payload=None):
        response = client.request(method, path, json=payload, headers=headers('fri'))
        assert response.status_code == 200, response.text
        if path.endswith('/target'):
            targets.append(response.json()['target'])
        if path.endswith('/finish'):
            finishes.append(payload)
        return response.json()

    async def http(route, params):
        if route.url.endswith('/members'):
            return [{'user': {'id': '11', 'username': 'friend', 'bot': False}, 'roles': ['90']}]
        return {'messages': [[found] for found in next(pages, [])], 'doing_deep_historical_index': False}

    async def sign_in():
        if passes and passes[-1] == 2 and not roles:  # The second pass's first sign-in: the planner's.
            if moved == 'local':
                agents[0].forget(11, erased=True)
            elif moved == 'gene':
                store.personal_control('11', 'disable')
        return {}

    async def analyze(chat, instruction, data, model, effort, schema, **kwargs):
        if 'messages' in data:
            item = data['messages'][0]
            return {'summary': 'Minecraft discussion', 'notes': [], 'observations': [
                {'source_id': item['id'], 'category': 'games', 'topic': 'Minecraft', 'evidence': item['text']}]}, {}, \
                'completed'
        roles.append(data)
        return {'term': 'minecraft', 'reason': 'follow their Minecraft discussions'}, {}, 'completed'

    monkeypatch.setattr('fri.fri_agent.analyze', analyze)

    async def scenario():
        fri = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, api=api, get_channel=lambda _: channel,
                              get_user=lambda _: SimpleNamespace(send=AsyncMock()), http=SimpleNamespace(request=http),
                              chat=SimpleNamespace(headers=sign_in))
        agent = Agent(fri)
        agents.append(agent)
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
    if 'revision' not in targets[-1]:  # Before HumanMint/gene#197: never a planner request (fri #304).
        assert roles == []
    elif moved == 'nothing':
        assert len(roles) == 1 and 'historical_topics' in roles[0]
    else:
        assert roles == [] and finishes[-1]['error']
