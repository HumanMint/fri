"""A /fri panel turn held to its author's erasures like a DM's (fri #288), and a learning pass's summary request held to
every record author's (fri #289)."""

import asyncio
import datetime as dt
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import httpx
import pytest
from fastapi.testclient import TestClient
from gene.api import create_app
from gene.fri_agent_store import AgentStore

from compat_tests.test_api import headers
from compat_tests.test_fri_edit_reanswer import model
from compat_tests.test_fri_learning import HistoryChannel, learning  # noqa: F401 (the fixture)
from compat_tests.test_fri_mentions import environment
from fri import fri as runtime
from fri.fri_agent import Agent
from fri.fri_chat import ChatResult
from fri.fri_errors import UNREACHABLE, ControllerUnavailable
from fri.fri_notices import notice_text
from fri.panels import PrivatePanels

QUESTION = 'what is the current Python release?'
LEARNING_WITHDRAWALS = 'learning-withdrawals'  # Gene says which messages a pass holds it knows were withdrawn.


def panel(fri):
    """Member 20's /fri chat, submitted in the workshop: a real interaction's shape, its panel edits recorded."""
    fri.private_panels = PrivatePanels()
    interaction = Mock(spec=discord.Interaction)
    interaction.id = discord.utils.time_snowflake(dt.datetime.now(dt.timezone.utc))
    interaction.user = SimpleNamespace(id=20, display_name='member 20')
    interaction.guild_id, interaction.channel_id = 123, 456
    interaction.response = SimpleNamespace(is_done=lambda: True)
    interaction.message = SimpleNamespace(id=1234, flags=SimpleNamespace(ephemeral=True))
    interaction.edit_original_response = AsyncMock(return_value=SimpleNamespace(id=1234))
    interaction.delete_original_response = AsyncMock()
    return interaction


def shown(interaction):
    """What the panel showed last: its text, or its card's."""
    kwargs = interaction.edit_original_response.await_args.kwargs
    return kwargs['content'] or ' '.join(embed.description or '' for embed in kwargs['embeds'])


def skipped():
    """The panel's line for a turn that stopped (looked up here, after what each test checks first)."""
    return notice_text(runtime.PANEL_SKIPPED)


def erase(agent, store, moved):
    if moved == 'local':
        agent.forget(20, erased=True)  # As receive() takes in their "forget me", before Gene has it.
    elif moved == 'gene':
        store.personal_control('20', 'disable')
    elif moved == 'someone else':
        agent.forget(30, erased=True)
        store.personal_control('30', 'disable')
    elif moved == 'web lookups off':
        agent.state['web_search_enabled'] = False


@pytest.mark.parametrize('moved', ['local', 'gene', 'web lookups off', 'someone else'])
def test_a_panel_turns_research_is_held_to_its_authors_erasures(monkeypatch, tmp_path, moved):
    """fri #288: a /fri panel turn carried no erasure baseline, so its research request was checked against neither
    the author's erasures here nor Gene's revision: a "forget me" or an erasure through Gene during the research
    sign-in still sent the question. The panel now takes their baseline as it comes in, as a DM does: nothing is asked,
    nothing recorded, and the panel says it skipped that rather than staying on "Thinking". So it does when a switch
    goes off there (fri #287). Someone else's erasure changes nothing."""
    async def scenario():
        fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
        interaction = panel(fri)
        chosen = []

        async def respond(text, context, ready):
            await ready()
            if not chosen:
                chosen.append(sorted(context.get('actions', {})))
                return ChatResult(status='completed', action={'kind': 'web_search', 'query': QUESTION})
            return ChatResult(status='completed', reply='python 3 something')

        model(fri, tmp_path, respond)
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, text='')

        async def signing():
            erase(agent, store, moved)
            return {'authorization': 'Bearer research'}

        fri.chat.headers, fri.chat.transport = signing, httpx.MockTransport(handle)
        api, saved = fri.api, []

        async def recording(method, path, payload=None):
            if path == '/chat/turn':
                saved.append(payload)
            return await api(method, path, payload)

        fri.api = recording
        try:
            await fri.chat_interaction(interaction, QUESTION)
            assert chosen and 'web_search' in chosen[0]
            if moved == 'someone else':
                assert len(requests) == 1 and QUESTION in requests[0].content.decode() and saved
                return
            assert requests == [] and not saved
            assert shown(interaction) == skipped()
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('when', ['during the sign-in', 'while the model answers'])
@pytest.mark.parametrize('moved', ['local', 'gene', 'someone else'])
def test_a_panel_reply_is_held_to_its_authors_erasures_like_a_dms(monkeypatch, tmp_path, moved, when):
    """fri #288: the panel's own reply. Erased during its sign-in, the model is asked nothing; erased while it answers,
    the reply isn't shown or recorded, as a DM's isn't sent: Gene's revision and the local count are read last before
    the panel shows it. An erasure through Gene was caught on main already, by Gene's turn check, which still says
    so; a "forget me" here wasn't. Someone else's erasure changes nothing."""
    async def scenario():
        fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
        interaction = panel(fri)
        asked = []

        async def respond(text, context, ready):
            if when == 'during the sign-in':
                erase(agent, store, moved)
            await ready()
            asked.append(text)
            if when == 'while the model answers':
                erase(agent, store, moved)
            return ChatResult(status='completed', reply='lol same')

        model(fri, tmp_path, respond)
        api, saved = fri.api, []

        async def recording(method, path, payload=None):
            if path == '/chat/turn':
                saved.append(payload)
            return await api(method, path, payload)

        fri.api = recording
        try:
            await fri.chat_interaction(interaction, 'how was your day')
            if moved == 'someone else':
                assert asked and saved and 'lol same' in shown(interaction)
                return
            assert asked == ([] if when == 'during the sign-in' else ['how was your day'])
            assert not saved and 'lol same' not in shown(interaction)
            if moved == 'local':  # Gene doesn't know of it yet: the panel's own checks stop it.
                assert shown(interaction) == skipped()
            # Through Gene, Gene's turn check stops it first and says so, as on main.
        finally:
            await fri.close()
    asyncio.run(scenario())


SAID = 'member thirty swears by the synthetic-quartz-banjo'


def drawn_turn(monkeypatch, tmp_path, erase_at=None):
    """Member 20's panel turn, its reply drawing on a saved reply attributed to member 30 (fri #290's round 1). Returns
    the controller calls in order, how many came before the reply was shown (None if it wasn't), and what the panel
    showed. erase_at: the call during which member 30 erases, through Gene."""
    async def scenario():
        fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
        interaction = panel(fri)
        store.save_chat('123', 'private:456', '20', 0, 'what did they say', SAID, 'model', 'high',
                        provenance=[{'user_id': '30', 'revision': 0}])

        async def respond(text, context, ready):
            await ready()
            return ChatResult(status='completed', reply='they said '+SAID)

        model(fri, tmp_path, respond)
        api, calls, display = fri.api, [], []

        async def watching(method, path, payload=None):
            calls.append(path)
            if len(calls)-1 == erase_at:
                store.personal_control('30', 'disable')
            return await api(method, path, payload)

        fri.api = watching
        edit = interaction.edit_original_response.side_effect

        async def showing(**kwargs):
            if any('synthetic-quartz-banjo' in (embed.description or '') for embed in kwargs.get('embeds') or []):
                display.append(len(calls))
            return SimpleNamespace(id=1234) if edit is None else await edit(**kwargs)

        interaction.edit_original_response.side_effect = showing
        try:
            await fri.chat_interaction(interaction, 'remind me what they said')
            return calls, display[0] if display else None, shown(interaction)
        finally:
            await fri.close()
    return asyncio.run(scenario())


def test_nothing_waits_between_a_panel_turns_last_check_and_what_it_shows(monkeypatch, tmp_path):
    """fri #290's round 1: the panel's own last read of the author's revision came after talk()'s check of the whole
    turn, so it reopened that fence while checking only the requester: member 30 erasing during that read still saw
    their words a reply drew on shown (Gene refused the save only afterwards). Gene's check of the whole turn, which
    holds the requester's revision too, is the panel's last wait now: what follows it before the reply is shown reads
    nothing. Member 30 erasing at any call after it would show their words, so there must be none."""
    (tmp_path/'first').mkdir()
    calls, shown_after, _ = drawn_turn(monkeypatch, tmp_path/'first')
    assert shown_after is not None and '/chat/turn/check' in calls[:shown_after]
    last = max(n for n, path in enumerate(calls[:shown_after]) if path == '/chat/turn/check')
    late = calls[last+1:shown_after]
    if late:  # Erasing then shows what main's turn would have stopped.
        (tmp_path/'second').mkdir()
        _, shown_again, text = drawn_turn(monkeypatch, tmp_path/'second', erase_at=last+1)
        assert 'synthetic-quartz-banjo' not in text, late
    assert late == []


def test_a_panel_reply_whose_last_check_cant_reach_the_controller_gets_the_outage_text(monkeypatch, tmp_path):
    """fri #288: the controller unreachable at the turn's last check, Gene's check of the whole turn, isn't an erasure:
    the reply isn't shown or recorded, and the panel says the controller is down, as for any of its reads."""
    async def scenario():
        fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
        interaction = panel(fri)
        answered = []

        async def respond(text, context, ready):
            await ready()
            answered.append(True)
            return ChatResult(status='completed', reply='lol same')

        model(fri, tmp_path, respond)
        api, saved = fri.api, []

        async def down(method, path, payload=None):
            if answered and path == '/chat/turn/check':
                raise ControllerUnavailable(UNREACHABLE)
            if path == '/chat/turn':
                saved.append(payload)
            return await api(method, path, payload)

        fri.api = down
        try:
            await fri.chat_interaction(interaction, 'how was your day')
            assert answered and not saved and 'lol same' not in shown(interaction)
            assert shown(interaction) == notice_text(UNREACHABLE)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_memory_control_from_the_panel_still_hears_how_it_went(monkeypatch, tmp_path):
    """Guard: "forget me" from the panel is the member's own erasure, not one that stops their turn: it says it's done."""
    async def scenario():
        fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
        interaction = panel(fri)
        try:
            await fri.chat_interaction(interaction, 'forget me')
            assert 'erased our saved chats' in shown(interaction)
        finally:
            await fri.close()
    asyncio.run(scenario())


def batch(learning, monkeypatch, older=False):  # noqa: F811 (the fixture's value)
    """Members 11 and 14 each with a public message in channel 1, a learning pass over them, the real fenced model call
    (Agent.run) with the summary request captured, and what the pass finished with. A pass sends only once Gene says
    which of its messages it knows were withdrawn (learning-withdrawals, fri #335), so this needs a controller that
    can; `older`: as one that can't, whatever this one offers."""
    clock, store, settings, scheduler = learning
    settings = replace(settings, agent_history_channels='1')
    scheduler = AgentStore(store, settings)
    channel = HistoryChannel()
    first, second = channel.add(clock[0]-100, user=11), channel.add(clock[0]-90, user=14, text='I like chess')
    client = TestClient(create_app(settings))
    offered = set(client.get('/fri/contract', headers=headers('fri')).json()['capabilities']) & {LEARNING_WITHDRAWALS}
    if not offered and not older:
        pytest.skip('needs a controller offering '+LEARNING_WITHDRAWALS)
    finishes, asked, signing = [], [], []

    async def api(method, path, payload=None):
        response = client.request(method, path, json=payload, headers=headers('fri'))
        assert response.status_code == 200, response.text
        if path.endswith('/finish'):
            finishes.append(payload)
        return response.json()

    fri = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, api=api, capabilities=frozenset(
                              () if older else offered),
                          get_channel=lambda _: channel, get_user=lambda _: SimpleNamespace(send=AsyncMock()))
    agent = Agent(fri)
    agent.state = scheduler.state()
    agent.member = AsyncMock(return_value=SimpleNamespace(roles=[SimpleNamespace(id=90)]))
    agent.profile = AsyncMock(side_effect=lambda channel, user: scheduler.context(str(channel), str(user)))
    agent.record = AsyncMock()

    async def sign_in():
        for hook in signing:
            hook()
        return {}

    fri.chat = SimpleNamespace(headers=sign_in)

    async def analyze(chat, instruction, data, model, effort, schema, **kwargs):
        asked.append(data)
        return {'summary': 'Music chat.', 'notes': []}, {'input_tokens': 20, 'output_tokens': 5}, 'completed'

    monkeypatch.setattr('fri.fri_agent.analyze', analyze)
    return agent, store, (str(first), str(second)), finishes, asked, signing


@pytest.mark.parametrize('moved', ['local', 'gene', 'local, during the read', 'both', 'controller down', 'someone else',
                                   'nothing'])
def test_a_learning_passs_summary_request_carries_no_record_whose_author_erased(learning, monkeypatch, moved):  # noqa: F811
    """fri #289: a learning pass sent the batch of members' recent public messages to the summary model with no
    erasure check, so a member who erased during its sign-in, here or through Gene, still had their text sent (Gene's
    finish refused it only afterwards). Signed in, every record's author is read again: Gene's revision, then their
    erasures here since the record was read (a "forget me" during that read counts). A record whose author moved is
    dropped before anything is sent, and the rest go; with none left, nothing is. The controller unreachable there is
    the pass's existing failure. With nothing moved, the request is main's."""
    async def scenario():
        agent, store, (first, second), finishes, asked, signing = batch(learning, monkeypatch)
        profile = agent.profile.side_effect

        def move():
            if moved in ('local', 'both'):
                agent.forget(11, erased=True)
            if moved in ('gene', 'both'):
                store.personal_control('11', 'disable')
            if moved == 'both':
                agent.forget(14, erased=True)
            if moved == 'someone else':
                agent.forget(30, erased=True)
                store.personal_control('30', 'disable')
            if moved == 'controller down':
                agent.profile.side_effect = ControllerUnavailable(UNREACHABLE)

        if moved == 'local, during the read':
            def reading(channel, user):
                if user == 11 and not agent.forgotten.get(11):
                    agent.forget(11, erased=True)  # Taken in while its record's privacy is read, once.
                return profile(channel, user)
            agent.profile.side_effect = reading
        else:
            signing.append(move)
        await agent.learn()
        ids = [[record['id'] for record in data['messages']] for data in asked]
        if moved in ('someone else', 'nothing'):
            assert ids == [[first, second]] and set(asked[0]) == {'messages'}
            assert set(asked[0]['messages'][0]) == {'id', 'user_id', 'role_id', 'revision', 'text', 'at'}
            assert not finishes[-1]['error']
        elif moved == 'controller down':
            assert asked == [] and finishes[-1]['error']
        elif moved == 'both':
            # Nothing left to send is an empty batch, not a failed summary: no usage, and its scans count as read
            # (fri #290's round 1).
            assert asked == [] and not finishes[-1]['error'] and finishes[-1]['records'] == []
            assert finishes[-1]['counters'] == {'input_tokens': 0, 'output_tokens': 0, 'reasoning_output_tokens': 0}
            assert finishes[-1]['scanned'] == 2
        else:
            assert ids == [[second]]  # Their text never reached the model; the other member's did.
            assert [record['id'] for record in finishes[-1]['records']] == [second]

    asyncio.run(scenario())


@pytest.mark.parametrize('moved', ['during the history read', 'between two of their records'])
def test_a_learning_pass_holds_every_record_to_one_baseline_taken_before_it_reads(learning, monkeypatch, moved):  # noqa: F811
    """fri #290's round 1: each record took its author's erasures here as it was read, after the history was. A "forget
    me" during the history read was every record's baseline then, and one between two of a member's records dropped
    the first and sent the second. One snapshot, taken before the history is read, holds the whole batch."""
    async def scenario():
        agent, store, (first, second), finishes, asked, signing = batch(learning, monkeypatch)
        channel = agent.fri.get_channel(1)
        third = str(channel.add(learning[0][0]-80, user=11, text='i play chess on weekends'))
        if moved == 'during the history read':
            logs = channel._state.http.logs_from

            async def reading(*args, **kwargs):
                agent.forget(11, erased=True)
                return await logs(*args, **kwargs)
            channel._state.http.logs_from = reading
        else:
            profile = agent.profile.side_effect

            def between(channel_id, user):
                if user == 14 and not agent.forgotten.get(11):
                    agent.forget(11, erased=True)  # After member 11's first record, before their second.
                return profile(channel_id, user)
            agent.profile.side_effect = between
        await agent.learn()
        assert [[record['id'] for record in data['messages']] for data in asked] == [[second]], (first, third)
        assert [record['id'] for record in finishes[-1]['records']] == [second]

    asyncio.run(scenario())


@pytest.mark.parametrize('moved', ['local', 'gene', 'local, during the search', 'nothing'])
def test_an_exploration_passs_summary_request_carries_no_record_whose_author_erased(learning, monkeypatch, moved):  # noqa: F811
    """fri #289, the learning pass's other mode: an exploration of a member's archived messages sends them to the summary
    model the same way, and was checked the same way, not at all. It's held to the same fence (see Agent.unmoved): the
    member erasing during its sign-in, here or through Gene, or during the archive search (one baseline, taken before
    it, fri #290's round 1), leaves nothing to send, so nothing is: an empty batch, not a failed one."""
    from compat_tests.test_fri_exploration import archive, by_id, offered, row
    clock, store, settings, scheduler = archive(learning)
    client = TestClient(create_app(settings))
    capabilities = offered(client)
    old = row(clock[0]-120*86400)
    channel = SimpleNamespace(id=1, name='memba-chat', guild=SimpleNamespace(id=123, me=object()),
                              permissions_for=lambda _: SimpleNamespace(view_channel=True, read_message_history=True))
    finishes, asked, pages, agents = [], [], iter([[old]]), []

    async def api(method, path, payload=None):
        response = client.request(method, path, json=payload, headers=headers('fri'))
        assert response.status_code == 200, response.text
        if path.endswith('/finish'):
            finishes.append(payload)
        return response.json()

    async def http(route, params):
        if route.url.endswith('/members'):
            return [{'user': {'id': '11', 'username': 'friend', 'bot': False}, 'roles': ['90']}]
        if moved == 'local, during the search' and not agents[0].forgotten.get(11):
            agents[0].forget(11, erased=True)
        return {'messages': [[found] for found in next(pages, [])], 'doing_deep_historical_index': False}

    async def sign_in():
        if moved == 'local':
            agents[0].forget(11, erased=True)
        elif moved == 'gene':
            store.personal_control('11', 'disable')
        return {}

    async def analyze(chat, instruction, data, model, effort, schema, **kwargs):
        asked.append(data)
        return {'summary': 'Minecraft discussion', 'notes': [], 'observations': []}, {}, 'completed'

    monkeypatch.setattr('fri.fri_agent.analyze', analyze)

    async def scenario():
        fri = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, api=api, get_channel=lambda _: channel,
                              get_user=lambda _: SimpleNamespace(send=AsyncMock()), capabilities=capabilities,
                              http=SimpleNamespace(request=by_id(http, [old])), chat=SimpleNamespace(headers=sign_in))
        agent = Agent(fri)
        agents.append(agent)
        agent.state = scheduler.state()
        agent.member = AsyncMock(side_effect=lambda user, **_: SimpleNamespace(
            id=user, bot=False, display_name='friend', roles=[SimpleNamespace(id=90)]))
        agent.record = AsyncMock()
        await agent.learn()

    asyncio.run(scenario())
    assert finishes and 'scanned_ids' in finishes[-1]  # An exploration's finish.
    if moved == 'nothing':
        assert [[record['id'] for record in data['messages']] for data in asked] == [[old['id']]]
    else:
        assert asked == [] and finishes[-1]['records'] == [] and not finishes[-1]['error']
        assert finishes[-1]['scanned_ids'] == [old['id']] and not any(finishes[-1]['counters'].values())
