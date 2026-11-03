"""A learning or exploration pass never sends a message deleted or edited while it holds it (fri #325): its summary
request drops the record, and its planner the historical topic that came from it. When Fri can't tell, the pass stops
and keeps its progress."""

import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from gene.api import create_app

from compat_tests.test_api import headers
from compat_tests.test_fri_learning import learning  # noqa: F401 (the fixture)
from compat_tests.test_fri_panel_and_learning_fences import batch
from fri.fri_agent import Agent
from fri.fri_errors import ControllerRejected
from fri.fri_turn_withdrawal import SavedTurns


def edited(message_id, user):
    """Discord's edit of a member's message, as on_raw_message_edit hands it to the agent."""
    return SimpleNamespace(id=message_id, author=SimpleNamespace(id=user, bot=False), content='never mind',
                           edited_at=dt.datetime.now(dt.timezone.utc), channel=SimpleNamespace(id=1))


@pytest.mark.parametrize('when', ['while the history is read', 'during the sign-in'])
@pytest.mark.parametrize('change', ['deleted', 'edited'])
def test_a_learning_pass_never_sends_a_message_taken_back_while_it_holds_it(learning, monkeypatch, when,  # noqa: F811
                                                                             change):
    """Member 11's message is deleted or edited while the pass holds it: while Discord answers the history request, or
    while the summary request signs in. The request carried its words anyway, and Gene refused the result only at the
    finish. It's dropped right before the request, from the finish too; member 14's goes, and the pass completes."""
    async def scenario():
        agent, store, (first, second), finishes, asked, signing = batch(learning, monkeypatch)

        def take_back():
            if change == 'deleted':
                agent.unsend(1, [int(first)])  # Discord's delete, as on_raw_message_delete hands it over.
            else:
                agent.edit(1, edited(int(first), 11))

        if when == 'during the sign-in':
            signing.append(take_back)
        else:
            channel = agent.fri.get_channel(1)
            logs = channel._state.http.logs_from

            async def reading(*args, **kwargs):
                rows = await logs(*args, **kwargs)
                take_back()  # Before the rows reach Fri.
                return rows

            channel._state.http.logs_from = reading
        await agent.learn()
        assert [[record['id'] for record in data['messages']] for data in asked] == [[second]]
        assert [record['id'] for record in finishes[-1]['records']] == [second] and not finishes[-1]['error']
        assert not agent.passes  # Nothing kept once it's done.

    asyncio.run(scenario())


@pytest.mark.parametrize('why', ['the withdrawal record is unreadable', 'too much was taken back to keep'])
def test_a_learning_pass_that_cant_tell_stops_and_keeps_its_progress(learning, monkeypatch, tmp_path, why):  # noqa: F811
    """When Fri can't tell what was taken back, nothing is sent and the pass stops as a failed one does, so its batch
    is read again: the withdrawal queue's record can't be read, or more was deleted meanwhile than a pass keeps."""
    import fri.fri_agent as fri_agent

    async def scenario():
        agent, store, (first, second), finishes, asked, signing = batch(learning, monkeypatch)
        if why == 'the withdrawal record is unreadable':
            agent.fri.data = tmp_path
            (tmp_path/'withdrawals.json').write_text('{"pending": {')
            agent.saved_turns = SavedTurns(agent)
            assert agent.saved_turns.closed()
        else:
            monkeypatch.setattr(fri_agent, 'PASS_WATCH', 2, raising=False)
            signing.append(lambda: agent.unsend(7, [101, 102, 103]))  # Anyone's, anywhere.
        await agent.learn()
        assert asked == [] and finishes[-1]['error'] and not agent.passes

    asyncio.run(scenario())


def explorer(learning, moved):  # noqa: F811
    """Two exploration passes over member 11 in channel 1, as test_fri_exploration's: the first reads their history
    broadly and notes a topic from their message, the second asks the planner with it. moved: what's deleted, and when.
    Returns the planner requests, the summary requests' records and the first message's ID."""
    from compat_tests.test_fri_exploration import archive, row
    clock, store, settings, scheduler = archive(learning)
    client = TestClient(create_app(settings))
    old, later = row(clock[0]-120*86400), row(clock[0]-160*86400, text='minecraft redstone was fun')
    channel = SimpleNamespace(id=1, name='memba-chat', guild=SimpleNamespace(id=123, me=object()),
                              permissions_for=lambda _: SimpleNamespace(view_channel=True, read_message_history=True))
    planned, summarized, pages, agents, passes, checked = [], [], iter([[old], [old, later], []]), [], [], []

    async def api(method, path, payload=None):
        response = client.request(method, path, json=payload, headers=headers('fri'))
        if path.endswith('/finish') and response.status_code != 200:
            return {}
        if response.status_code != 200:
            raise ControllerRejected(response.json().get('detail', ''), response.status_code)
        if path.endswith('/checked'):
            checked.append(path)
        if path.endswith('/target') and passes == [1, 2] and moved == 'its topic, while the target is assigned':
            agents[0].unsend(1, [int(old['id'])])  # Its source deleted after Gene read the topic, before Fri resumes.
        return response.json()

    async def member(user, **_):
        if checked and passes == [1] and moved == 'its row, while the rows are read' and not getattr(member, 'done', 0):
            member.done = 1
            agents[0].unsend(1, [int(old['id'])])
        return SimpleNamespace(id=user, bot=False, display_name='friend', roles=[SimpleNamespace(id=90)])

    async def http(route, params):
        if route.url.endswith('/members'):
            return [{'user': {'id': '11', 'username': 'friend', 'bot': False}, 'roles': ['90']}]
        return {'messages': [[found] for found in next(pages, [])], 'doing_deep_historical_index': False}

    async def sign_in():
        return {}

    async def analyze(chat, instruction, data, model, effort, schema, **kwargs):
        if 'messages' in data:
            summarized.append([record['id'] for record in data['messages']])
            item = data['messages'][0]
            return {'summary': 'Minecraft discussion', 'notes': [], 'observations': [
                {'source_id': item['id'], 'category': 'games', 'topic': 'Minecraft', 'evidence': item['text']}]}, {}, \
                'completed'
        planned.append(data)
        return {'term': 'minecraft', 'reason': 'follow their Minecraft discussions'}, {}, 'completed'

    import fri.fri_agent
    original = fri.fri_agent.analyze
    fri.fri_agent.analyze = analyze

    async def scenario():
        fri_runtime = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, api=api, get_channel=lambda _: channel,
                                      get_user=lambda _: SimpleNamespace(send=AsyncMock()),
                                      http=SimpleNamespace(request=http), chat=SimpleNamespace(headers=sign_in))
        agent = Agent(fri_runtime)
        agents.append(agent)
        agent.state = scheduler.state()
        agent.member = AsyncMock(side_effect=member)
        agent.record = AsyncMock()
        passes.append(1)
        await agent.learn()
        if moved == 'its row, while the rows are read':
            return
        clock[0] += 1800
        agent.state = scheduler.state()
        passes.append(2)
        await agent.learn()

    try:
        asyncio.run(scenario())
    finally:
        fri.fri_agent.analyze = original
    return planned, summarized, old['id']


def test_an_archive_search_never_sends_a_row_deleted_while_its_rows_are_read(learning):  # noqa: F811
    """The same on the exploration path: member 11's archived message is deleted while Fri reads the search's rows, and
    with nothing left, nothing is asked."""
    _, summarized, _ = explorer(learning, 'its row, while the rows are read')
    assert summarized == []


@pytest.mark.parametrize('moved', ['its topic, while the target is assigned', 'nothing'])
def test_a_planner_never_gets_a_topic_whose_message_was_deleted_meanwhile(learning, moved):  # noqa: F811
    """The planner's context carries historical topics with evidence from the member's messages. One's message is
    deleted after Gene read the topic and before Fri asks: that topic is left out of the request. With nothing
    deleted, it's there as before (on a controller that says what the context was read at; see fri #304)."""
    planned, _, source = explorer(learning, moved)
    if not planned:  # A controller before HumanMint/gene#197 asks no planner at all.
        return
    topics = [topic['source_id'] for topic in planned[0]['historical_topics']]
    assert (source in topics) is (moved == 'nothing')
