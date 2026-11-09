"""A learning pass sends nothing a member said before their latest memory removal (fri #305), and an exploration's
planner is held to the target's erasures here from before its context was assigned (fri #304)."""

import asyncio
import datetime as dt
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from fastapi.testclient import TestClient
from gene.api import create_app
from gene.fri_memory_store import edit, entries

from compat_tests.test_api import headers
from compat_tests.test_fri_learning import dm_owner, learning  # noqa: F401 (the fixture)
from compat_tests.test_fri_panel_and_learning_fences import batch
from fri.fri_agent import Agent


def remove_a_preference(store, user):
    """The member removes one of their saved preferences through Gene, as My memory does: their revision moves, and
    Gene's removal cutoff is now, so what they said before it is kept nowhere."""
    [note] = entries(store, '123', 'public:1', user)['entries']
    now = dt.datetime.fromtimestamp(time.time(), dt.timezone.utc)  # The learning fixture's clock.
    source = {'kind': 'discord_interaction', 'user_id': user, 'display_name': 'friend', 'channel_id': '1',
              'guild_id': '123', 'id': str(discord.utils.time_snowflake(now))}
    edit(store, '123', 'public:1', user, note['id'], 0, source, 'remove')


def saved_preference(store, user):
    store.save_chat('123', 'public:1', user, 0, 'I like trance', 'noted', 'model', 'high',
                    notes=[{'category': 'music', 'evidence': 'I like trance'}])


READ_OVER = ('removal, while its rows are read', 'memory off and on, while its rows are read')


@pytest.mark.parametrize('moved', ['a removal', 'memory off and on'])
def test_a_learning_pass_never_sends_words_from_before_a_removal_during_its_read(learning, monkeypatch,  # noqa: F811
                                                                                 moved):
    """fri #305: member 11 removes a saved preference through Gene while the pass's history request is out. Their
    message from before that came back with it, was stamped with their revision after the removal, and so passed the
    atomic author check: the summary model got words Gene keeps nowhere now (it refused the derived result only at
    the finish). A record at or before the member's removal cutoff is dropped before the request; member 14's goes.
    fri #321: so is one from before they turned memory off through Gene, and on again, which kept no removal to say
    so (HumanMint/gene#202)."""
    async def scenario():
        agent, store, (first, second), finishes, asked, signing = batch(learning, monkeypatch)
        saved_preference(store, '11')
        channel = agent.fri.get_channel(1)
        logs = channel._state.http.logs_from

        async def reading(*args, **kwargs):
            rows = await logs(*args, **kwargs)
            if not reading.done:  # While the first history request is out; the pass reads it again (fri #335).
                reading.done = True
                if moved == 'a removal':
                    remove_a_preference(store, '11')
                else:
                    store.personal_control('11', 'disable')
                    store.personal_control('11', 'enable')
            return rows
        reading.done = False

        channel._state.http.logs_from = reading
        await agent.learn()
        assert [[record['id'] for record in data['messages']] for data in asked] == [[second]]
        assert [record['id'] for record in finishes[-1]['records']] == [second]

    asyncio.run(scenario())


def explored(learning, monkeypatch, moved):  # noqa: F811
    """Two exploration passes over member 11 in channel 1, as test_fri_exploration's: the first reads their history
    broadly, the second asks the planner with the topics the first noted. moved: what happens meanwhile. Returns the
    planner requests and the summary requests' records."""
    from compat_tests.test_fri_exploration import archive, by_id, offered, row
    clock, store, settings, scheduler = archive(learning)
    client = TestClient(create_app(settings))
    capabilities = offered(client)
    old, later = row(clock[0]-120*86400), row(clock[0]-160*86400, text='minecraft redstone was fun')
    channel = SimpleNamespace(id=1, name='memba-chat', guild=SimpleNamespace(id=123, me=object()),
                              permissions_for=lambda _: SimpleNamespace(view_channel=True, read_message_history=True))
    planned, summarized, pages, agents, passes = [], [], iter([[old], [old, later], []]), [], []
    checked, targets = [], []
    saved_preference(store, '11')

    async def api(method, path, payload=None):
        from fri.fri_errors import ControllerRejected
        response = client.request(method, path, json=payload, headers=headers('fri'))
        if path.endswith('/finish') and response.status_code != 200:
            return {}  # Gene refuses what a removal invalidated; this pass is done either way.
        if response.status_code != 200:
            raise ControllerRejected(response.json().get('detail', ''), response.status_code)
        if path.endswith('/target'):
            targets.append(response.json()['target'])
        if path.endswith('/target') and passes == [1, 2] and moved == 'local, while the target is assigned':
            agents[0].forget(11, erased=True)  # Gene built the target's context; this lands before Fri resumes.
        if path.endswith('/checked'):
            checked.append(path)
        return response.json()

    async def member(user, **_):
        if checked and passes == [1] and moved in READ_OVER and not getattr(member, 'done', 0):
            member.done = 1
            if moved == 'removal, while its rows are read':
                remove_a_preference(store, '11')  # After the search came back and was checked.
            else:  # Memory turned off and on again through Gene, there (fri #321).
                store.personal_control('11', 'disable')
                store.personal_control('11', 'enable')
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

    monkeypatch.setattr('fri.fri_agent.analyze', analyze)

    async def scenario():
        fri = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, api=api, get_channel=lambda _: channel,
                              get_user=lambda _: dm_owner(), capabilities=capabilities,
                              http=SimpleNamespace(request=by_id(http, [old, later])),
                              chat=SimpleNamespace(headers=sign_in))
        agent = Agent(fri)
        agents.append(agent)
        agent.state = scheduler.state()
        agent.member = AsyncMock(side_effect=member)
        agent.record = AsyncMock()
        passes.append(1)
        await agent.learn()
        if moved in READ_OVER:
            return
        clock[0] += 1800
        agent.state = scheduler.state()
        passes.append(2)
        await agent.learn()

    asyncio.run(scenario())
    return planned, summarized, targets


@pytest.mark.parametrize('moved', READ_OVER)
def test_an_archive_search_never_sends_words_from_before_a_removal_while_its_rows_are_read(learning,  # noqa: F811
                                                                                           monkeypatch, moved):
    """fri #305 on the exploration path: the archive search came back and Gene checked it, and member 11 removes a
    saved preference through Gene while Fri reads its rows. Their older message was stamped with their revision after
    the removal and went to the summary model (Gene refused the result only at the finish). It's at or before their
    removal cutoff, so it's dropped first, and with nothing left nothing is asked. A removal while the search is out
    was already safe: Gene drops the planned search, and its check refuses. fri #321: the same with memory turned off
    and on again through Gene there, which kept no removal to say so (HumanMint/gene#202)."""
    _, summarized, _ = explored(learning, monkeypatch, moved)
    assert summarized == []


@pytest.mark.parametrize('moved', ['local, while the target is assigned', 'nothing'])
def test_an_explorations_planner_is_held_to_erasures_here_from_before_its_target_was_assigned(learning,  # noqa: F811
                                                                                              monkeypatch, moved):
    """fri #304, here: the target's "forget me" lands here after Gene built the target's context and before Fri resumes.
    The planner's baseline of their erasures here was taken after that, so the fence passed and the planner got their
    name and historical evidence. It's taken before the target is asked for: the planner isn't asked. With nothing
    moved, it is."""
    planned, _, targets = explored(learning, monkeypatch, moved)
    if moved == 'nothing' and 'revision' in targets[-1]:
        assert len(planned) == 1 and 'historical_topics' in planned[0]
    else:
        assert planned == []
