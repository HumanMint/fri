"""A turn's gates after its last wait, in the requests a tool makes of a model of its own and in a DM's last steps
(fri #273, #275, #276): web research, an image search's looks, a DM image reply's last check and a DM reaction."""

import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import httpx
import pytest

from compat_tests.test_fri_continuations import saved
from compat_tests.test_fri_edit_reanswer import model
from compat_tests.test_fri_fence_boundaries import place_of
from compat_tests.test_fri_image_share import QUERY, setup
from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_timing import no_debounce
from fri.fri_actions import Actions
from fri.fri_chat import ChatResult
from fri.fri_errors import UNREACHABLE, ControllerUnavailable

QUESTION = 'what is the current Python release?'


def researching(fri, agent, store, erasure):
    """The research model, on its own sign-in and transport. Returns its requests. `erasure` lands where the issue
    has it: through Gene while the tool step reads Discord, or here, during the research sign-in."""
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, text='')  # Nothing completed: the lookup says so.

    async def headers():
        if erasure == 'local, during the sign-in':
            agent.forget(20, erased=True)  # As receive() takes in their "forget me", before Gene has it.
        return {'authorization': 'Bearer research'}

    fri.chat.headers, fri.chat.transport = headers, httpx.MockTransport(handle)
    return requests


@pytest.mark.parametrize('place', ['server', 'dm'])
@pytest.mark.parametrize('erasure', ['gene, during the tool reads', 'local, during the sign-in', 'controller down',
                                     'dms off, during the tool reads', 'someone else'])
def test_web_research_is_never_asked_after_the_turns_fence_moved(monkeypatch, tmp_path, erasure, place):
    """fri #273: the conversation model chose web research, and the member erased through Gene while the tool step
    read Discord, or said "forget me" during the research model's sign-in: the research model still got their
    question. Signed in, the step reads the turn's fence (Gene's revision, the local count and a DM's switch) last
    before the request: nothing is asked, nothing goes out or is saved. The controller unreachable there is the turn's
    outage notice, not an uncertain tool result. Another member's erasure changes nothing: the question is asked."""
    async def scenario():
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        channel, message, prefix = place_of(fri, agent, rows, channel, message, place)
        if place == 'server' and erasure.startswith('dms off'):
            pytest.skip('a channel turn has no DM switch')
        trigger = message(prefix+QUESTION)
        chosen = []

        async def respond(text, context, ready):
            await ready()
            if not chosen:
                chosen.append(sorted(context.get('actions', {})))
                return ChatResult(status='completed', action={'kind': 'web_search', 'query': QUESTION})
            return ChatResult(status='completed', reply='python 3 something')

        model(fri, tmp_path, respond)
        requests = researching(fri, agent, store, erasure)
        channels, moved = Actions.channels, []

        async def reading(self, *args, **kwargs):
            result = await channels(self, *args, **kwargs)
            if chosen and not moved:  # The tool step's own access read, once the model chose it.
                moved.append(True)
                if erasure.startswith('gene'):
                    store.personal_control('20', 'disable')
                elif erasure.startswith('dms off'):
                    agent.config['support_dms'] = False
                elif erasure == 'someone else':
                    agent.forget(30, erased=True)
                    store.personal_control('30', 'disable')
            return result

        monkeypatch.setattr(Actions, 'channels', reading)
        api = fri.api

        async def unreachable(method, path, payload=None):
            if erasure == 'controller down' and moved and path == '/agent/context':
                raise ControllerUnavailable(UNREACHABLE)
            return await api(method, path, payload)

        fri.api = unreachable
        try:
            await agent.handle(trigger)
            assert chosen and 'web_search' in chosen[0] and moved
            outcomes = agent.ledger.snapshot()
            assert outcomes['in_flight'] == 0
            if erasure == 'someone else':
                assert len(requests) == 1 and QUESTION in requests[0].content.decode()
                return
            assert requests == []  # Their question never reached the research model.
            assert not saved(calls)
            if erasure == 'controller down':
                assert outcomes['outage'] == 1 and UNREACHABLE in str(trigger.reply.await_args)
            else:
                trigger.reply.assert_not_awaited()
        finally:
            await fri.close()
    asyncio.run(scenario())


def dm_share(monkeypatch, tmp_path):
    """test_fri_image_share's DM: a prepared image's last check, in a DM with DMs on."""
    fri, agent, event, _ = setup(monkeypatch, tmp_path)
    event.guild = None
    event.channel = Mock(spec=discord.DMChannel, id=999, fetch_message=AsyncMock(return_value=event))
    a = Actions(agent, 10, 999, False)
    a.event, a.message, a.media_allowed = event, QUERY, True
    a.scope = {'guild_id': '123', 'user_id': '10', 'channel_id': '456', 'conversation_id': 'private:999'}
    a.image_share.source_text, a.image_share.source_edited = QUERY, None
    agent.config['support_dms'] = True
    return fri, agent, event, a


@pytest.mark.parametrize('switch', ['support_dms', 'enabled', 'web_search_enabled'])
@pytest.mark.parametrize('during', ['membership', 'message fetch', 'context read', 'access check'])
def test_a_dm_image_replys_last_check_reads_its_switches_after_its_waits(monkeypatch, tmp_path, during, switch):
    """fri #275: a DM reply carrying a shared image checked the DM switch before its reads (membership, the message,
    the controller's context, access) and not after, so with DMs going off during one it still went out. The same
    held for Fri being switched off and web lookups. Each is read again after the last of them."""
    async def scenario():
        fri, agent, event, a = dm_share(monkeypatch, tmp_path)

        def off():
            if switch == 'web_search_enabled':
                agent.state['web_search_enabled'] = False
            else:
                agent.config[switch] = False

        fetch, api, valid = event.channel.fetch_message, fri.api, a.valid

        async def membership(*args, **kwargs):
            off()
            return SimpleNamespace(id=10)

        async def fetched(*args, **kwargs):
            off()
            return await fetch(*args, **kwargs)

        async def context(method, path, body=None):
            if path == '/chat/context':
                off()
            return await api(method, path, body)

        async def checked():
            result = await valid()
            off()
            return result

        if during == 'membership':
            agent.member = membership
        elif during == 'message fetch':
            event.channel.fetch_message = fetched
        elif during == 'context read':
            fri.api = context
        else:
            a.valid = checked
        try:
            assert await a.image_share.current() is False
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_dm_image_replys_last_check_passes_with_everything_on(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, event, a = dm_share(monkeypatch, tmp_path)
        try:
            assert await a.image_share.current() is True
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('switch', ['support_dms', 'enabled', 'none'])
def test_a_dm_reaction_reads_its_switches_after_its_last_privacy_read(monkeypatch, tmp_path, switch):
    """fri #276: the model chose a reaction in a DM, and DMs went off during the reaction's last privacy read: it was
    still added (the later DM check only stopped the save). So it was with Fri switched off. Both are read again after
    that read, before the reaction. With nothing changed it's added as before."""
    async def scenario():
        fri, agent, store, _, _, _, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        channel, message, _ = place_of(fri, agent, rows, None, None, 'dm')
        trigger = message('lol nice')
        trigger.add_reaction, trigger.reactions = AsyncMock(), []
        composed = []

        async def respond(text, context, ready):
            await ready()
            composed.append(True)
            return ChatResult(status='completed', response_kind='react', reaction='👍')

        model(fri, tmp_path, respond)
        api, reads = fri.api, []

        async def privacy(method, path, payload=None):
            result = await api(method, path, payload)
            if composed and path == '/agent/context':
                reads.append(path)
                if len(reads) == 2 and switch != 'none':  # The reaction's own last read, after the send check's.
                    agent.config[switch] = False
            return result

        fri.api = privacy
        try:
            await agent.handle(trigger)
            assert composed and len(reads) == 2
            assert trigger.add_reaction.await_count == (switch == 'none')
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_web_research_still_runs_with_nothing_changed(monkeypatch, tmp_path):
    """Guard: the fence after sign-in reads nothing it shouldn't change, and the question is asked once."""
    async def scenario():
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        trigger = message('fri '+QUESTION)
        chosen = []

        async def respond(text, context, ready):
            await ready()
            if not chosen:
                chosen.append(True)
                return ChatResult(status='completed', action={'kind': 'web_search', 'query': QUESTION})
            return ChatResult(status='completed', reply='python 3 something')

        model(fri, tmp_path, respond)
        requests = researching(fri, agent, store, 'none')
        try:
            await agent.handle(trigger)
            assert len(requests) == 1 and requests[0].headers['authorization'] == 'Bearer research'
            assert dt.datetime  # The research request carries the sign-in it was checked after.
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('moved', ['local', 'gene', 'dms off', 'nothing'])
def test_an_image_searchs_own_model_requests_are_fenced_after_sign_in(monkeypatch, tmp_path, moved):
    """The sweep, the same shape as fri #273: an image search asks a model of its own, with the member's request, to
    plan the search and to check each image. Each now goes through the agent's fenced call (Agent.fenced): signed in,
    it reads the turn's fence (the local count, Gene's revision, a DM's switch) last, and moved by then, nothing is
    asked and the turn stops. With nothing changed the plan is asked as before."""
    from fri.fri_errors import AuthorErased, GateClosed
    async def scenario():
        fri, agent, event, a = dm_share(monkeypatch, tmp_path)
        forgotten = agent.forgotten.get(10, 0)
        a.turn_context = {'fence': {
            'erased': lambda read=False: agent.erasure(10, forgotten, 0 if read else None),
            'dms': lambda: agent.config['support_dms']}}
        if moved == 'gene':  # Their revision as Gene has it now: they erased through it.
            agent.profile = AsyncMock(return_value={'enabled': True, 'revision': 1, 'summary': ''})

        async def headers():
            if moved == 'local':
                agent.forget(10, erased=True)
            elif moved == 'dms off':
                agent.config['support_dms'] = False
            return {}

        fri.chat = SimpleNamespace(headers=headers)
        asked = []

        async def analyze(chat, instruction, data, model, effort, schema, **kwargs):
            asked.append(data)
            return {}, {}, 'failed'  # Nothing planned: the search stops there either way.

        monkeypatch.setattr('fri.fri_agent.analyze', analyze)
        a.image_share.source_text, a.image_share.source_edited = QUERY, None
        try:
            if moved == 'nothing':
                await a.image_share.find(QUERY)
                assert asked == [{'request': QUERY}]
            else:
                with pytest.raises(GateClosed if moved == 'dms off' else AuthorErased):
                    await a.image_share.find(QUERY)
                assert asked == []
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('switch', ['support_dms', 'enabled', 'none'])
def test_a_dm_low_battery_reaction_reads_its_switches_after_its_last_privacy_read(monkeypatch, tmp_path, switch):
    """The sweep, the same shape as fri #276: a cooldown's low battery in a DM is added after its last privacy read,
    which came after the DM and Fri switches were read. Both are read again after it."""
    async def scenario():
        fri, agent, store, _, _, _, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        _, message, _ = place_of(fri, agent, rows, None, None, 'dm')
        trigger = message('you there')
        trigger.add_reaction = AsyncMock()
        since = (agent.forgotten.get(20, 0), store.chat_context('123', 'private:555', '20')['revision'])
        api = fri.api

        async def privacy(method, path, payload=None):
            result = await api(method, path, payload)
            if path == '/agent/context' and switch != 'none':
                agent.config[switch] = False
            return result

        fri.api = privacy
        try:
            assert await agent.recharging(trigger, True, since) is (switch == 'none')
            assert trigger.add_reaction.await_count == (switch == 'none')
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_dm_tool_step_reads_the_dm_switch_after_its_reads(monkeypatch, tmp_path):
    """The sweep, at a tool step that makes no model request of its own: a DM draft is saved after DMs went off during
    the step's reads, which it checked before them. It's read again after them, with the local count, before the tool
    acts: nothing is saved, nothing goes out."""
    async def scenario():
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        channel, message, _ = place_of(fri, agent, rows, channel, message, 'dm')
        trigger = message('save that as a draft: raid notes, bring snacks')
        chosen = []

        async def respond(text, context, ready):
            await ready()
            if not chosen:
                chosen.append(sorted(context.get('actions', {})))
                return ChatResult(status='completed', action={'kind': 'work_save', 'work_id': '', 'version': 0,
                                                              'title': 'raid notes', 'body': 'bring snacks',
                                                              'state': 'open'})
            return ChatResult(status='completed', reply='saved')

        model(fri, tmp_path, respond)
        channels, moved = Actions.channels, []

        async def reading(self, *args, **kwargs):
            result = await channels(self, *args, **kwargs)
            if chosen and not moved:
                moved.append(True)
                agent.config['support_dms'] = False
            return result

        monkeypatch.setattr(Actions, 'channels', reading)
        try:
            await agent.handle(trigger)
            assert chosen and 'work_save' in chosen[0] and moved
            assert not any(path == '/workspace/save' for path, _ in calls)
            trigger.reply.assert_not_awaited()
        finally:
            await fri.close()
    asyncio.run(scenario())
