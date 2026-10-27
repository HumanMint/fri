"""Member profiles end to end: Fri decides the audience, the real controller keeps and serves it."""

import asyncio
import datetime as dt
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import httpx
import pytest
from fastapi.testclient import TestClient
from gene.api import create_app
from gene.config import Settings
from gene.fri_agent_store import AgentStore
from gene.fri_memory_store import edit, entries, save

from compat_tests.test_api import headers
from compat_tests.test_fri_chat import CONTEXT, response_event
from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_runtime import configured
from fri import feedback as commands
from fri.fri import FIRST_HERE, FIRST_MEMORY, FIRST_PROFILE, NO_MENTIONS, Fri
from fri.fri_agent import Agent
from fri.fri_chat import Chat, ChatResult
from fri.fri_context import discord_source
from fri.fri_errors import ControllerRejected
from fri.fri_memory import describe_profile, note_place, profiled, server_wide
from fri.fri_memory_panel import SHOWN, EditMemory, MemoryView, show
from fri.subscription import SubscriptionError

GENERAL, GAMING, STAFF, SPICY, DM, THREAD, SECRET = 1, 2, 5, 6, 900, 70, 71


def world(monkeypatch, tmp_path):
    settings = Settings(tmp_path/'profiles.db', '123', '10', 'runtime-'+'x'*32, 'worker-'+'x'*32,
                        'deploy-'+'x'*32, fri_token='fri-'+'x'*32, workshop_channel_id='456',
                        agent_channels='1,2,5,6')
    client = TestClient(create_app(settings))
    store = client.app.state.store
    AgentStore(store, settings).update({'enabled': True}, 0, '10')
    fri = configured(monkeypatch)
    fri.data = tmp_path
    fri._connection.user = SimpleNamespace(id=99)
    fri.capabilities = frozenset(client.get('/fri/contract', headers=headers('fri')).json()['capabilities'])
    agent = fri.agent = Agent(fri)
    agent.state = AgentStore(store, settings).state()
    agent.record = AsyncMock()
    everyone = SimpleNamespace(id=123, name='@everyone')
    guild = SimpleNamespace(id=123, me=fri.user, default_role=everyone)
    readable = {GENERAL: True, GAMING: True, STAFF: False, SPICY: True}

    @asynccontextmanager
    async def typing():
        yield

    async def history(**kwargs):
        return
        yield

    def text_channel(cid, name, nsfw=False):
        item = SimpleNamespace(id=cid, guild=guild, parent_id=None, name=name, typing=typing, history=history)
        item.is_nsfw = lambda: nsfw
        item.permissions_for = lambda who: (discord.Permissions(view_channel=readable[cid], read_message_history=True)
                                            if who is everyone else discord.Permissions.all())
        item.overwrites = {}
        return item

    channels = {GENERAL: text_channel(GENERAL, 'general'), GAMING: text_channel(GAMING, 'gaming'),
                STAFF: text_channel(STAFF, 'staff'), SPICY: text_channel(SPICY, 'spicy', nsfw=True)}
    for cid, kind in ((THREAD, discord.ChannelType.public_thread), (SECRET, discord.ChannelType.private_thread)):
        thread = Mock(spec=discord.Thread, id=cid, guild=guild, parent_id=GENERAL, type=kind, typing=typing,
                      history=history)
        thread.name, thread.parent = 'thread-'+str(cid), channels[GENERAL]  # Mock(parent=...) means something else.
        thread.is_nsfw = lambda: False
        thread.permissions_for = lambda who: discord.Permissions.all()
        channels[cid] = thread
    channels[DM] = SimpleNamespace(id=DM, guild=None, name='dm', typing=typing, history=history)
    guild.fetch_member = AsyncMock(side_effect=lambda user: SimpleNamespace(id=user, bot=user == 99))
    guild.fetch_channel = AsyncMock(side_effect=lambda cid: channels[cid])
    guild.fetch_roles = AsyncMock(return_value=[])
    fri.get_channel = lambda cid: channels.get(cid)
    fri.get_guild = lambda gid: guild if gid == 123 else None
    calls = []

    async def api(method, path, payload=None):
        calls.append((path, payload))
        if path == '/agent/admit':
            return {'allowed': True}  # Admission budgets are covered elsewhere.
        response = client.request(method, path, headers=headers('fri'), json=payload)
        if not response.is_success:
            raise ControllerRejected(response.text)
        return response.json()
    fri.api = api
    fri.chat_once = AsyncMock(return_value=ChatResult(status='completed', reply='bold of you'))
    ids = iter(range(10**6))

    def message(cid, text, user=20, age=0):
        mid = discord.utils.time_snowflake(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=age)) + next(ids)
        channel = channels[cid]
        return Mock(spec=discord.Message, id=mid, content=text, channel=channel, guild=channel.guild,
                    author=SimpleNamespace(id=user, display_name=f'member {user}', bot=False), webhook_id=None,
                    mentions=[], reference=None, attachments=[], embeds=[], edited_at=None)

    async def say(cid, text, reply='bold of you', notes=(), user=20, age=0, event=None):
        fri.chat_once.return_value = ChatResult(status='completed', reply=reply, notes=list(notes))
        event = event or message(cid, text, user, age)
        result = await fri.talk(123, cid, user, text, 'private' if cid == DM else 'public', f'member {user}', event=event)
        sent = SimpleNamespace(id=event.id+1, content=result.reply, edit=AsyncMock(), reply=AsyncMock())
        saved = await fri.record_turn(result, sent) if result.turn else None
        return result, saved, sent
    return SimpleNamespace(fri=fri, store=store, channels=channels, readable=readable, calls=calls, say=say,
                           client=client, message=message)


def model(w, respond):
    """Fri's own model call, with the model itself replaced."""
    async def call(text, context):
        return await Fri.chat_once(w.fri, text, context)

    async def signed_in(text, context, ready=None):
        if ready:  # Fri's last checks, as Chat.respond runs them once signed in.
            await ready()
        return await respond(text, context)
    w.fri.chat = SimpleNamespace(respond=signed_in, headers=AsyncMock(return_value={'Authorization': 'Bearer test'}))
    w.fri.chat_once = AsyncMock(side_effect=call)


def model_context(w):
    return w.fri.chat_once.await_args.args[1]


def turns(w):
    return [body for path, body in w.calls if path == '/chat/turn']


def note(text, category='games'):
    return [{'category': category, 'evidence': text}]


def run(scenario):
    asyncio.run(scenario())


def test_a_preference_said_in_general_follows_the_member_to_gaming_and_dms(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            _, saved, sent = await w.say(GENERAL, 'fri I like Minecraft', 'cubes, groundbreaking', note('I like Minecraft'))
            assert turns(w)[-1]['audience'] == 'server'
            assert saved == {'ok': True, 'first_memory': True, 'memory_scope': 'server'}
            sent.edit.assert_awaited_once_with(content='cubes, groundbreaking'+FIRST_PROFILE)
            for where in (GAMING, THREAD, DM):
                await w.say(where, 'fri what should i play tonight')
                notes = model_context(w)['personal_memory']['notes']
                assert notes == [{'category': 'games', 'evidence': 'I like Minecraft', 'scope': 'server',
                                  'provenance': []}]
            # What they said in #general shows up elsewhere, labelled and clipped for the model.
            elsewhere = model_context(w)['elsewhere']
            assert [row['channel'] for row in elsewhere][-2:] == ['#gaming', '#thread-70']
            await w.say(GAMING, 'fri thoughts?')
            rows = model_context(w)['elsewhere']
            # Newest last; the DM exchange in between never shows up here.
            assert [(row['channel'], row['input']) for row in rows] == [
                ('#general', 'fri I like Minecraft'), ('#thread-70', 'fri what should i play tonight')]
            assert rows[-1]['minutes_ago'] == 0 and 'reply' not in rows[-1]  # Only their own words travel.
            assert all(body.get('profiles') for path, body in w.calls if path == '/chat/context')
        finally:
            await w.fri.close()
    run(scenario)


@pytest.mark.parametrize('where', [DM, STAFF, SECRET, SPICY])
def test_what_is_said_privately_or_in_restricted_places_stays_there(monkeypatch, tmp_path, where):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            _, saved, sent = await w.say(where, 'fri I like private jazz', 'noted, sicko', note('I like private jazz', 'music'))
            assert turns(w)[-1]['audience'] == 'conversation'
            assert saved['memory_scope'] == 'conversation'
            sent.edit.assert_awaited_once_with(content='noted, sicko'+FIRST_HERE)
            await w.say(GENERAL, 'fri recommend me something')
            context = model_context(w)
            assert context['personal_memory']['notes'] == []
            assert context['elsewhere'] == []
            assert 'jazz' not in json.dumps(context, default=str)
            await w.say(where, 'fri again')
            assert [n['evidence'] for n in model_context(w)['personal_memory']['notes']] == ['I like private jazz']
        finally:
            await w.fri.close()
    run(scenario)


def test_what_a_member_is_working_on_stays_in_the_channel_they_said_it(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            project = "I'm building a Factorio mod"
            _, saved, sent = await w.say(GENERAL, 'fri '+project, 'another belt person', note(project, 'projects'))
            assert turns(w)[-1]['audience'] == 'server'  # Everyone can read #general, and still it stays.
            assert saved == {'ok': True, 'first_memory': True, 'memory_scope': 'conversation'}
            sent.edit.assert_awaited_once_with(content='another belt person'+FIRST_HERE)
            assert 'working on' in FIRST_HERE
            await w.say(GENERAL, 'fri what should i do tonight')
            assert model_context(w)['personal_memory']['notes'] == [
                {'category': 'projects', 'evidence': project, 'scope': 'here', 'provenance': []}]
            for where in (GAMING, THREAD, DM):
                await w.say(where, 'fri what should i do tonight')
                assert model_context(w)['personal_memory']['notes'] == []
            # A preference in the same message follows them; the project beside it doesn't.
            text = 'fri I like Minecraft and I main Mercy'
            _, saved, sent = await w.say(GAMING, text, 'sure', note('I like Minecraft')+note('I main Mercy', 'projects'))
            assert saved == {'ok': True, 'first_memory': True, 'memory_scope': 'server'}
            sent.edit.assert_awaited_once_with(content='sure'+FIRST_PROFILE)
            assert 'working on stays where you said it' in FIRST_PROFILE
            await w.say(GENERAL, 'fri hi')
            assert [(n['scope'], n['evidence']) for n in model_context(w)['personal_memory']['notes']] == [
                ('server', 'I like Minecraft'), ('here', project)]
            overview, _, _ = await w.say(DM, 'fri what do you remember about me')
            assert ('**across the server**\n- I like Minecraft\n**only in #general**\n- I\'m building a Factorio mod\n'
                    '**only in #gaming**\n- I main Mercy') in overview.reply
            assert 'the rest stays where you said it, what you’re working on included' in overview.reply
            public, _, _ = await w.say(THREAD, 'what do you remember about me fri')
            assert 'Mercy' not in public.reply and 'Factorio' not in public.reply
            assert 'except what you’re working on' in public.reply
        finally:
            await w.fri.close()
    run(scenario)


def test_a_channel_that_becomes_restricted_stops_feeding_elsewhere(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await w.say(GAMING, 'fri my build keeps crashing on load')
            await w.say(GENERAL, 'fri anyway')
            assert [row['channel'] for row in model_context(w)['elsewhere']] == ['#gaming']
            w.readable[GAMING] = False
            await w.say(GENERAL, 'fri still there?')
            assert model_context(w)['elsewhere'] == []
            assert turns(w)[-1]['audience'] == 'server'  # #general itself is still readable by everyone.
        finally:
            await w.fri.close()
    run(scenario)


def test_elsewhere_carries_only_the_members_words_and_goes_with_its_sources(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        source = {'kind': 'discord_message', 'id': str(discord.utils.time_snowflake(dt.datetime.now(dt.timezone.utc))),
                  'user_id': '20', 'display_name': 'friend', 'channel_id': str(GAMING), 'guild_id': '123'}
        w.store.save_chat('123', 'public:2', '20', 0, 'what did ash say', 'ash said denver', 'm', 'high',
                          source=source, provenance=[{'user_id': '30', 'revision': 0}], audience='server')
        try:
            await w.say(GENERAL, 'fri where was that again')
            assert [row['input'] for row in model_context(w)['elsewhere']] == ['what did ash say']
            assert 'denver' not in json.dumps(model_context(w), default=str)
            # The member's words can repeat ash's, so this reply depends on ash too.
            assert {'user_id': '30', 'revision': 0} in turns(w)[-1]['provenance']
            w.store.personal_control('30', 'disable')  # Ash's privacy change reaches both replies.
            await w.say(GENERAL, 'fri and now?')
            assert model_context(w)['elsewhere'] == []
            assert [t['input'] for t in w.store.chat_context('123', 'public:1', '20')['turns']] == ['fri and now?']
        finally:
            await w.fri.close()
    run(scenario)


def test_a_reply_drawing_on_a_note_from_a_restricted_time_stays_in_its_channel(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            w.readable[GENERAL] = False
            await w.say(GENERAL, 'fri I like private jazz', 'noted', note('I like private jazz', 'music'))
            w.readable[GENERAL] = True  # Opened to everyone later; Fri's reply there uses the note.
            await w.say(GENERAL, 'fri what music do i like', 'you like private jazz')
            assert turns(w)[-1]['audience'] == 'server'
            await w.say(GAMING, 'fri recommend me something')
            context = model_context(w)
            assert [row['input'] for row in context['elsewhere']] == ['fri what music do i like']
            assert 'private jazz' not in json.dumps(context, default=str)
        finally:
            await w.fri.close()
    run(scenario)


def test_a_removed_preference_stays_gone_from_queued_messages_and_other_excerpts(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await w.say(GENERAL, 'fri I like Minecraft', 'cubes', note('I like Minecraft'))
            with w.store.connection() as db:  # Background learning kept a longer excerpt in #gaming.
                save(db, '123', 'public:2', '20', ('hobbies', 'I like Minecraft modpacks'), time.time())
            listed = entries(w.store, '123', 'private:900', '20')
            profile = next(row for row in listed['entries'] if row['scope'] == 'server')
            edit(w.store, '123', 'private:900', '20', profile['id'], listed['revision'], dm_control(), 'remove')
            # Sent before the removal, answered after it: Fri replies but keeps nothing.
            result, saved, _ = await w.say(GENERAL, 'fri I like Minecraft modpacks', 'ok',
                                           note('I like Minecraft modpacks', 'hobbies'), age=3)
            assert result.reply == 'ok' and saved == {'ok': True, 'first_memory': False, 'kept': False}
            await w.say(GAMING, 'fri what should i play')
            assert 'Minecraft' not in json.dumps(model_context(w), default=str)
            assert entries(w.store, '123', 'private:900', '20')['entries'] == []
        finally:
            await w.fri.close()
    run(scenario)


def dm_control():
    return {'kind': 'discord_interaction', 'id': str(discord.utils.time_snowflake(dt.datetime.now(dt.timezone.utc))),
            'user_id': '20', 'display_name': 'friend', 'channel_id': '900', 'guild_id': None}


@pytest.mark.parametrize('failure', [None, TimeoutError, ControllerRejected])
def test_a_message_from_before_a_removal_stays_out_of_channel_chatter(monkeypatch, tmp_path, failure):
    async def scenario():
        fri, agent, store, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        controller = fri.api

        async def api(method, path, payload=None):
            if failure and path == '/chat/turn':  # The save times out or is refused: nothing says it was kept.
                raise failure('save failed')
            return await controller(method, path, payload)
        fri.api = api
        with store.connection() as db:
            save(db, '123', 'server', '20', ('games', 'I like Minecraft'), time.time(), provenance=[])
        early = message('fri I like Minecraft modpacks', age=3)  # Waiting while the member edits their memory.
        removed = entries(store, '123', 'private:900', '20')['entries'][0]
        edit(store, '123', 'private:900', '20', removed['id'], 0, dm_control(), 'remove')
        fri.chat_once.return_value = ChatResult(status='completed', reply='Minecraft modpacks, endless')
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(early)
                early.reply.assert_awaited_once()  # Answered, then kept nowhere: not the message, not the reply.
                assert list(agent.recent[1]) == []
                await agent.handle(message('fri what is everyone on about', user=30))
            assert 'Minecraft' not in json.dumps(fri.chat_once.await_args.args[1], default=str)
        finally:
            await fri.close()
    run(scenario)


def test_a_channel_opened_mid_reply_keeps_what_was_said_while_closed(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        controller = w.fri.api

        async def api(method, path, payload=None):
            if path == '/chat/context':
                w.readable[GENERAL] = True  # Opened to everyone while Fri reads its context.
            return await controller(method, path, payload)
        w.fri.api = api
        w.readable[GENERAL] = False
        try:
            _, saved, _ = await w.say(GENERAL, 'fri I like private jazz', 'noted', note('I like private jazz', 'music'))
            assert saved['memory_scope'] == 'conversation'
            await w.say(GAMING, 'fri recommend me something')
            assert 'private jazz' not in json.dumps(model_context(w), default=str)
        finally:
            await w.fri.close()
    run(scenario)


def test_what_fri_heard_on_arrival_outlasts_busy_channels(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            w.readable[GENERAL] = False
            waiting = w.message(GENERAL, 'fri I like private jazz')
            w.fri.hear(waiting)  # Arrives while #general is closed...
            for _ in range(3000):
                w.fri.hear(w.message(GAMING, 'chatter', user=40))
            w.readable[GENERAL] = True  # ...and is answered after it opens, behind a lot of chatter.
            _, saved, _ = await w.say(GENERAL, waiting.content, 'noted', note('I like private jazz', 'music'),
                                      event=waiting)
            assert saved['memory_scope'] == 'conversation'
        finally:
            await w.fri.close()
    run(scenario)


def test_a_channel_closed_while_fri_gathers_context_drops_out_of_elsewhere(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await w.say(GENERAL, 'fri compare these two lenses for me')
            seen = []

            async def respond(text, context):
                seen.append([row['input'] for row in context['elsewhere']])
                return ChatResult(status='completed', reply='sure')
            model(w, respond)
            gather = w.fri.resolve_references

            async def close(scope, context):
                w.readable[GENERAL] = False  # Closed after elsewhere was read, before the model call.
                return await gather(scope, context)
            w.fri.resolve_references = close
            await w.say(GAMING, 'fri recommend me something')
            assert seen == [[]]
        finally:
            await w.fri.close()
    run(scenario)


def test_a_channel_closed_while_fri_signs_in_drops_out_of_elsewhere(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await w.say(GENERAL, 'fri I like mellow jazz')
            sent = []

            def handler(request):
                sent.append(json.loads(json.loads(request.content)['input'][-1]['content']))
                return httpx.Response(200, text='data: '+json.dumps(response_event(content=json.dumps(
                    {'reply': 'sure', 'request_draft': None})))+'\n\n')

            def access_token():
                w.readable[GENERAL] = False  # Closed while Fri signs in, after it gathered context.
                return 'test'
            w.fri.chat = Chat(SimpleNamespace(access_token=access_token), httpx.MockTransport(handler))

            async def call(text, context):
                return await Fri.chat_once(w.fri, text, context)
            w.fri.chat_once = AsyncMock(side_effect=call)
            await w.say(GAMING, 'fri recommend me something')
            assert sent and sent[-1]['with_you_elsewhere_untrusted'] == []
        finally:
            await w.fri.close()
    run(scenario)


def test_a_sign_in_failure_on_a_cross_channel_turn_is_reported(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await w.say(GENERAL, 'fri I like mellow jazz')

            def access_token():
                raise SubscriptionError('Reconnect the subscription.')
            w.fri.chat = Chat(SimpleNamespace(access_token=access_token), httpx.MockTransport(
                lambda request: pytest.fail('nothing is sent without signing in')))

            async def call(text, context):
                return await Fri.chat_once(w.fri, text, context)
            w.fri.chat_once = AsyncMock(side_effect=call)
            with pytest.raises(commands.UserFeedbackCheckFailure, match='Reconnect the subscription'):
                await w.say(GAMING, 'fri recommend me something')
        finally:
            await w.fri.close()
    run(scenario)


def test_a_channel_closed_while_fri_replies_keeps_the_note_there(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            async def respond(text, context):
                w.readable[GENERAL] = False  # Closed while the model writes.
                return ChatResult(status='completed', reply='noted', notes=note('I like jazz', 'music'))
            model(w, respond)
            _, saved, _ = await w.say(GENERAL, 'fri I like jazz')
            assert saved['memory_scope'] == 'conversation' and turns(w)[-1]['audience'] == 'conversation'
            await w.say(GAMING, 'fri recommend me something')
            assert 'jazz' not in json.dumps(model_context(w), default=str)
        finally:
            await w.fri.close()
    run(scenario)


def test_a_first_profile_notice_that_does_not_fit_is_sent_on_its_own(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            long = 'y'*1900
            _, saved, sent = await w.say(GENERAL, 'fri I like Minecraft', long, note('I like Minecraft'))
            assert saved['first_memory'] and len(long+FIRST_PROFILE) > 2000
            sent.edit.assert_not_awaited()
            sent.reply.assert_awaited_once_with(FIRST_PROFILE.strip(), allowed_mentions=NO_MENTIONS)
        finally:
            await w.fri.close()
    run(scenario)


def test_memory_overview_lists_everything_in_dms_and_only_whats_visible_in_public(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await w.say(GENERAL, 'fri I like Minecraft', notes=note('I like Minecraft'))
            await w.say(STAFF, 'fri I like restricted chess', notes=note('I like restricted chess'))
            await w.say(DM, 'fri I like private_jazz', notes=note('I like private_jazz', 'music'))
            dm, _, _ = await w.say(DM, 'fri what do you remember about me')
            assert dm.memory_scope['conversation_id'] == 'private:900'
            assert dm.reply.startswith('memory’s on. everything i keep about you:')
            assert ('**across the server**\n- I like Minecraft\n**only here**\n- I like private\\_jazz\n'
                    '**only in #staff**\n- I like restricted chess') in dm.reply
            public, _, _ = await w.say(GAMING, 'what do you remember about me fri')
            assert '- I like Minecraft' in public.reply and 'jazz' not in public.reply and 'chess' not in public.reply
            assert 'ask in DMs or /fri for the full list' in public.reply
            staff, _, _ = await w.say(STAFF, 'what do you remember about me')
            assert '**only in this channel**\n- I like restricted chess' in staff.reply and 'jazz' not in staff.reply
            await w.say(GAMING, 'forget me')
            dm, _, _ = await w.say(DM, 'what do you remember about me')
            assert dm.reply.startswith('memory’s off')
            listed = [body for path, body in w.calls if path == '/chat/memory/notes']
            assert listed and all(body.get('profiles') for body in listed)
            assert w.store.chat_context('123', 'public:1', '20')['elsewhere'] == []
        finally:
            await w.fri.close()
    run(scenario)


def test_an_older_controller_or_fri_keeps_todays_per_conversation_memory(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        await w.say(GAMING, 'fri I like Terraria', 'ok', note('I like Terraria'))  # A profile note, before.
        w.fri.capabilities = w.fri.capabilities - {'member-profiles'}
        try:
            _, saved, sent = await w.say(GENERAL, 'fri I like Minecraft', 'ok', note('I like Minecraft'))
            assert 'audience' not in turns(w)[-1]
            assert saved['memory_scope'] == 'conversation'
            sent.edit.assert_awaited_once_with(content='ok'+FIRST_MEMORY)  # The notice the older controller allows.
            await w.say(GAMING, 'fri hi')
            # Without asking for profiles, only this conversation's notes come back, as before.
            assert model_context(w)['personal_memory']['notes'] == [] and model_context(w)['elsewhere'] == []
            overview, _, _ = await w.say(GENERAL, 'what do you remember about me')
            assert overview.reply.startswith('Memory is on.') and 'Terraria' not in overview.reply
            assert not any('profiles' in body for path, body in w.calls[-3:] if path == '/chat/context')
        finally:
            await w.fri.close()
    run(scenario)


def test_server_wide_needs_everyone_to_read_a_safe_for_work_channel():
    everyone = SimpleNamespace(name='@everyone')
    guild = SimpleNamespace(default_role=everyone)
    def channel(view=True, nsfw=False, history=True, overwrites=None):
        return SimpleNamespace(guild=guild, is_nsfw=lambda: nsfw, overwrites=overwrites or {},
                               permissions_for=lambda who: discord.Permissions(
                                   view_channel=view and who is everyone, read_message_history=history))
    assert server_wide(channel())
    assert not server_wide(channel(view=False)) and not server_wide(channel(nsfw=True))
    assert not server_wide(channel(history=False))  # Later readers couldn't scroll back to it.
    # One role or one member kept out (fri #45): the default role alone doesn't prove everyone reads it.
    for deny in (discord.PermissionOverwrite(view_channel=False), discord.PermissionOverwrite(read_message_history=False)):
        assert not server_wide(channel(overwrites={'role 50': deny}))
    assert server_wide(channel(overwrites={'role 50': discord.PermissionOverwrite(send_messages=False)}))
    without = channel()
    del without.overwrites
    assert not server_wide(without)
    assert not server_wide(SimpleNamespace(guild=None)) and not server_wide(None)
    assert not server_wide(SimpleNamespace(guild=guild))  # Unknown channel kinds count as restricted.
    for kind, expected in ((discord.ChannelType.public_thread, True), (discord.ChannelType.news_thread, True),
                           (discord.ChannelType.private_thread, False)):
        thread = Mock(spec=discord.Thread, guild=guild, type=kind)
        thread.parent = channel()
        assert server_wide(thread) is expected
    thread.type, thread.parent = discord.ChannelType.public_thread, None  # Parent not cached.
    assert not server_wide(thread)


def test_places_and_overview_fit_discord():
    channels = {5: SimpleNamespace(name='st*ff')}
    assert note_place(channels.get, 'server', 'private:9') == 'across the server'
    assert note_place(channels.get, 'private:9', 'private:9') == 'only here'
    assert note_place(channels.get, 'here', 'public:5', public=True) == 'only in this channel'
    assert note_place(channels.get, 'public:5', 'private:9') == 'only in #st\\*ff'
    assert note_place(channels.get, 'public:8', 'private:9') == 'only in a channel i can’t see anymore'
    assert note_place(channels.get, 'private:3', 'private:9') == 'only in another private chat'
    groups = [('across the server', ['I like '+'x'*170]*15), ('only here', ['I like '+'y'*170]*10)]
    text = describe_profile(True, groups, private=True)
    assert len(text) <= 1950 and text.count('**') == 2 and 'more in My memory' in text
    assert describe_profile(True, [], private=False).startswith('memory’s on. nothing kept that this channel')


def test_emoji_heavy_memory_fits_discord_in_utf16_units(monkeypatch, tmp_path):
    def units(text):
        return len(text.encode('utf-16-le'))//2
    emoji = 'I like '+'🎮'*86
    assert units(describe_profile(True, [('across the server', [emoji]*15), ('only here', [emoji]*10)], True)) <= 2000
    async def scenario():
        w = world(monkeypatch, tmp_path)
        entries = [{'id': i, 'category': 'games', 'evidence': emoji, 'at': time.time(), 'expires_at': time.time()+86400,
                    'scope': 'server', 'origin': None} for i in range(25)]
        state = {'enabled': True, 'revision': 0, 'entries': entries}
        scope = {'guild_id': '123', 'user_id': '20', 'channel_id': '456', 'conversation_id': 'private:900'}
        interaction = Mock(spec=discord.Interaction, guild_id=None, channel_id=DM, user=SimpleNamespace(id=20))
        shown = AsyncMock()
        monkeypatch.setattr('fri.fri_panel.reply', shown)
        monkeypatch.setattr('fri.fri_memory_panel.allowed', AsyncMock(return_value=True))
        try:
            await show(w.fri, interaction, scope, state=state)
            assert units(shown.await_args.kwargs['embed'].description) <= 4096
            options = MemoryView(w.fri, scope, state).children[0].options
            assert all(units(o.label) <= 100 and units(o.description) <= 100 for o in options)
            assert not any('\ud83c' in o.label for o in options)  # No half of an emoji left behind.
        finally:
            await w.fri.close()
    run(scenario)


def test_panel_labels_notes_and_stays_inside_discords_limits(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        entries = [{'id': i, 'category': 'games', 'evidence': 'I like '+'z'*170, 'at': time.time(),
                    'expires_at': time.time()+86400, 'scope': 'server' if i < 15 else 'public:5', 'origin': None}
                   for i in range(30)]
        state = {'enabled': True, 'revision': 0, 'entries': entries}
        scope = {'guild_id': '123', 'user_id': '20', 'channel_id': '456', 'conversation_id': 'private:900'}
        interaction = Mock(spec=discord.Interaction, guild_id=None, channel_id=DM, user=SimpleNamespace(id=20))
        shown = AsyncMock()
        monkeypatch.setattr('fri.fri_panel.reply', shown)
        try:
            view = MemoryView(w.fri, scope, state)
            assert len(view.children[0].options) == SHOWN
            assert view.children[0].options[-1].description == 'games · only in #staff'
            monkeypatch.setattr('fri.fri_memory_panel.allowed', AsyncMock(return_value=True))
            await show(w.fri, interaction, scope, state=state)
            body = shown.await_args.kwargs['embed'].description
            assert len(body) <= 4096 and 'games · across the server' in body and 'more. Remove some' in body
        finally:
            await w.fri.close()
    run(scenario)


@pytest.mark.parametrize('where', [DM, GENERAL])
def test_an_edited_profile_note_keeps_following_the_member(monkeypatch, tmp_path, where):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        shown = AsyncMock()
        monkeypatch.setattr('fri.fri_panel.reply', shown)
        monkeypatch.setattr('fri.fri_memory_panel.allowed', AsyncMock(return_value=True))
        try:
            await w.say(GENERAL, 'fri I like chess', 'noted', note('I like chess'))
            # My memory in DMs, or opened from "what do you remember about me?" in #general.
            scope = profiled(w.fri, {'guild_id': '123', 'user_id': '20', 'channel_id': '456' if where == DM else '1',
                                     'conversation_id': 'private:900' if where == DM else 'public:1'})
            state = await w.fri.api('POST', '/chat/memory/notes', scope)
            assert [row['scope'] for row in state['entries']] == ['server']
            modal = EditMemory(MemoryView(w.fri, scope, state, selected=state['entries'][0]['id']))
            modal.preference._value = 'I prefer quiet conversations'
            await modal.on_submit(Mock(spec=discord.Interaction, id=int(dm_control()['id']),
                                       guild_id=None if where == DM else 123, channel_id=where,
                                       user=SimpleNamespace(id=20, display_name='friend')))
            body = shown.await_args.kwargs['embed'].description
            assert 'Updated that preference.' in body and 'across the server' in body
            await w.say(GAMING, 'fri recommend me something')
            assert model_context(w)['personal_memory']['notes'] == [
                {'category': 'games', 'evidence': 'I prefer quiet conversations', 'scope': 'server', 'provenance': []}]
        finally:
            await w.fri.close()
    run(scenario)


def test_the_member_can_share_what_they_are_working_on_from_my_memory(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        shown = AsyncMock()
        monkeypatch.setattr('fri.fri_panel.reply', shown)
        monkeypatch.setattr('fri.fri_memory_panel.allowed', AsyncMock(return_value=True))
        try:
            project = "I'm working on a pixel art game"
            await w.say(GENERAL, 'fri '+project, 'noted', note(project, 'projects'))
            scope = profiled(w.fri, {'guild_id': '123', 'user_id': '20', 'channel_id': '456',
                                     'conversation_id': 'private:900'})
            interaction = Mock(spec=discord.Interaction, guild_id=None, channel_id=DM, user=SimpleNamespace(id=20))
            await show(w.fri, interaction, scope)
            body = shown.await_args.kwargs['embed'].description
            assert 'projects · only in #general: I\'m working on a pixel art game' in body
            assert 'what you’re working on always starts there' in body
            state = await w.fri.api('POST', '/chat/memory/notes', scope)
            view = MemoryView(w.fri, scope, state, selected=state['entries'][0]['id'])
            assert view.children[0].options[0].description == 'projects · only in #general'
            assert EditMemory(view).preference.label == 'What you’re doing, e.g. I’m working on a mod'
            # The member's own Share server-wide press, as My memory sends it from their DMs.
            shared = await w.fri.api('POST', f"/chat/memory/notes/{state['entries'][0]['id']}", {
                **scope, 'source': dm_control(), 'revision': state['revision'], 'action': 'share'})
            assert [(row['scope'], row['category']) for row in shared['entries']] == [('server', 'projects')]
            await show(w.fri, interaction, scope, state=shared)
            assert 'projects · across the server: I\'m working on a pixel art game' in shown.await_args.kwargs[
                'embed'].description
            for where in (GAMING, THREAD, DM):
                await w.say(where, 'fri what should i do tonight')
                assert model_context(w)['personal_memory']['notes'] == [
                    {'category': 'projects', 'evidence': project, 'scope': 'server', 'provenance': []}]
        finally:
            await w.fri.close()
    run(scenario)


def test_an_older_fri_without_projects_still_lists_and_uses_one(monkeypatch, tmp_path):
    """A Fri released before projects notes, against a controller that keeps them: nothing on the way in checks."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        monkeypatch.setattr('fri.fri_memory.CATEGORIES', ('nickname', 'games', 'music', 'hobbies', 'conversation'))
        shown = AsyncMock()
        monkeypatch.setattr('fri.fri_panel.reply', shown)
        monkeypatch.setattr('fri.fri_memory_panel.allowed', AsyncMock(return_value=True))
        try:
            project = 'I main Mercy'
            w.store.save_chat('123', 'public:1', '20', 0, project, 'ok', 'model', 'high', notes=note(project, 'projects'),
                              source=discord_source(w.message(GENERAL, project)).model_dump(), provenance=[],
                              audience='server')
            await w.say(GENERAL, 'fri hi')
            assert [n['category'] for n in model_context(w)['personal_memory']['notes']] == ['projects']
            here, _, _ = await w.say(GENERAL, 'what do you remember about me')
            dm, _, _ = await w.say(DM, 'what do you remember about me')
            assert project in here.reply and '**only in #general**\n- '+project in dm.reply
            scope = profiled(w.fri, {'guild_id': '123', 'user_id': '20', 'channel_id': '456',
                                     'conversation_id': 'private:900'})
            await show(w.fri, Mock(spec=discord.Interaction, guild_id=None, channel_id=DM, user=SimpleNamespace(id=20)),
                       scope)
            assert 'projects · only in #general: '+project in shown.await_args.kwargs['embed'].description
            state = await w.fri.api('POST', '/chat/memory/notes', scope)
            assert MemoryView(w.fri, scope, state).children[0].options[0].label == project
        finally:
            await w.fri.close()
    run(scenario)


ASH = {'user_id': '30', 'revision': 0}


def quoting_note(w):
    """A preference saved from a message quoting ash, whose exchange has since expired."""
    w.store.save_chat('123', 'public:1', '20', 0, 'ash said: I like Minecraft modpacks', 'noted', 'model', 'high',
                      notes=note('I like Minecraft modpacks'), audience='server', provenance=[ASH],
                      source=discord_source(w.message(GENERAL, 'ash said: I like Minecraft modpacks')).model_dump())
    with w.store.connection() as db:
        db.execute('DELETE FROM chat_turns')


def test_a_reply_built_on_a_quoting_note_goes_when_they_forget(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            quoting_note(w)
            _, saved, _ = await w.say(GAMING, 'fri what games do i like', 'minecraft modpacks, endless')
            assert saved['ok'] and turns(w)[-1]['provenance'] == [ASH]
            w.store.erase_user('30')
            with w.store.connection() as db:
                assert db.execute("SELECT count(*) FROM chat_turns WHERE user_id='20'").fetchone()[0] == 0
            await w.say(THREAD, 'fri recommend me a game')
            assert 'Minecraft' not in json.dumps(model_context(w), default=str)
        finally:
            await w.fri.close()
    run(scenario)


def test_a_reply_built_on_a_quoting_note_is_withheld_if_they_forget_meanwhile(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            quoting_note(w)

            async def respond(text, context):
                w.store.erase_user('30')  # Forgets while Fri writes the reply.
                return ChatResult(status='completed', reply='minecraft modpacks, endless')
            model(w, respond)
            with pytest.raises(commands.UserFeedbackCheckFailure):
                await w.say(GAMING, 'fri what games do i like')
            with w.store.connection() as db:
                assert db.execute("SELECT count(*) FROM chat_turns WHERE user_id='20'").fetchone()[0] == 0
        finally:
            await w.fri.close()
    run(scenario)


def test_the_model_never_sees_whose_words_a_note_drew_on():
    seen = []
    def handler(request):
        seen.append(json.loads(json.loads(request.content)['input'][-1]['content']))
        return httpx.Response(200, text='data: '+json.dumps(response_event())+'\n\n')
    chat = Chat(SimpleNamespace(access_token=lambda: 'test'), httpx.MockTransport(handler))
    memory = {'enabled': True, 'notes': [{'category': 'games', 'evidence': 'I like chess', 'scope': 'server',
                                          'provenance': [ASH]}]}
    asyncio.run(chat.respond('hey', {**CONTEXT, 'personal_memory': memory}))
    assert seen[0]['personal_memory'] == {'enabled': True, 'notes': [
        {'category': 'games', 'evidence': 'I like chess', 'scope': 'server'}]}


def test_the_model_sees_elsewhere_rows_clipped_without_ids():
    seen = []
    def handler(request):
        seen.append(json.loads(json.loads(request.content)['input'][-1]['content']))
        return httpx.Response(200, text='data: '+json.dumps(response_event())+'\n\n')
    chat = Chat(SimpleNamespace(access_token=lambda: 'test'), httpx.MockTransport(handler))
    rows = [{'conversation_id': 'public:2', 'channel': '#gaming', 'minutes_ago': 40, 'input': 'a'*500,
             'at': time.time()}]
    asyncio.run(chat.respond('hey', {**CONTEXT, 'elsewhere': rows}))
    assert seen[0]['with_you_elsewhere_untrusted'] == [{'channel': '#gaming', 'minutes_ago': 40, 'message': 'a'*300}]
    asyncio.run(chat.respond('hey', CONTEXT))
    assert seen[1]['with_you_elsewhere_untrusted'] == []


@pytest.mark.parametrize('kept', ['profile note', 'elsewhere'])
def test_words_of_someone_who_forgets_while_fri_signs_in_never_reach_the_model(monkeypatch, tmp_path, kept):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            if kept == 'profile note':
                quoting_note(w)
            else:  # The member's own words in #general, said while ash's were in view.
                text = 'ash and i are into minecraft modpacks'
                w.store.save_chat('123', 'public:1', '20', 0, text, 'nice', 'model', 'high', audience='server',
                                  provenance=[ASH], source=discord_source(w.message(GENERAL, text)).model_dump())
            sent = []

            def access_token():
                w.store.erase_user('30')  # Forgets while Fri signs in, after it gathered context.
                return 'test'
            w.fri.chat = Chat(SimpleNamespace(access_token=access_token), httpx.MockTransport(
                lambda request: sent.append(request) or httpx.Response(500)))

            async def call(text, context):
                return await Fri.chat_once(w.fri, text, context)
            w.fri.chat_once = AsyncMock(side_effect=call)
            with pytest.raises(commands.UserFeedbackCheckFailure):
                await w.say(GAMING, 'fri what games do i like')
            assert sent == []  # Refused before the request, not only before delivery.
        finally:
            await w.fri.close()
    run(scenario)


def test_a_channel_closed_while_fri_thinks_keeps_the_reply_unsaid(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await w.say(GENERAL, 'fri my concert is friday at the blue room')
            saved = len(turns(w))

            closing = [True]

            async def respond(text, context):
                assert [row['channel'] for row in context['elsewhere']] == ['#general']
                if closing.pop() if closing else False:
                    w.readable[GENERAL] = False  # Restricted while the model writes the reply.
                return ChatResult(status='completed', reply='your concert is friday at the blue room')
            model(w, respond)
            with pytest.raises(commands.UserFeedbackCheckFailure, match='went private'):
                await w.say(GAMING, 'fri what are my plans')
            assert len(turns(w)) == saved and not w.fri.agent.recent[GAMING]  # Not posted, saved or echoed.
            w.readable[GENERAL] = True
            await w.say(GAMING, 'fri what are my plans')  # Open again, so it's said.
            assert len(turns(w)) == saved+1
        finally:
            await w.fri.close()
    run(scenario)


def test_a_dm_note_is_shared_and_pulled_back_only_on_the_members_press(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        shown = AsyncMock()
        monkeypatch.setattr('fri.fri_panel.reply', shown)
        monkeypatch.setattr('fri.fri_memory_panel.allowed', AsyncMock(return_value=True))
        w.fri.agent.forget = Mock()
        press = lambda: Mock(spec=discord.Interaction, id=int(dm_control()['id']), guild_id=None, channel_id=DM,  # noqa: E731
                             user=SimpleNamespace(id=20, display_name='friend'))
        try:
            await w.say(DM, 'fri I like jazz', 'noted', note('I like jazz', 'music'))
            await w.say(GAMING, 'fri recommend me something')
            assert model_context(w)['personal_memory']['notes'] == []
            scope = profiled(w.fri, {'guild_id': '123', 'user_id': '20', 'channel_id': '456',
                                     'conversation_id': 'private:900'})
            state = await w.fri.api('POST', '/chat/memory/notes', scope)
            view = MemoryView(w.fri, scope, state, selected=state['entries'][0]['id'])
            assert [item.label for item in view.children[1:]] == ['Remove selected', 'Edit selected', 'Share server-wide']
            await view.children[3].callback(press())  # The first press only asks.
            assert 'Share it server-wide?' in shown.await_args.kwargs['embed'].description
            confirm = shown.await_args.kwargs['view']
            assert confirm.children[3].label == 'Yes, share server-wide'
            await w.say(GAMING, 'fri recommend me something')
            assert model_context(w)['personal_memory']['notes'] == []
            await confirm.children[3].callback(press())
            assert 'Shared that preference across the server.' in shown.await_args.kwargs['embed'].description
            await w.say(GAMING, 'fri recommend me something')
            assert [row['evidence'] for row in model_context(w)['personal_memory']['notes']] == ['I like jazz']

            state = await w.fri.api('POST', '/chat/memory/notes', scope)
            assert [row['scope'] for row in state['entries']] == ['server']
            view = MemoryView(w.fri, scope, state, selected=state['entries'][0]['id'])
            assert view.children[3].label == 'Keep here only'
            await view.children[3].callback(press())
            assert 'stays here now' in shown.await_args.kwargs['embed'].description
            w.fri.agent.forget.assert_called_once_with(20)
            await w.say(GAMING, 'fri recommend me something')
            assert model_context(w)['personal_memory']['notes'] == []
            await w.say(DM, 'fri recommend me something')
            assert [(row['scope'], row['evidence']) for row in model_context(w)['personal_memory']['notes']] == [
                ('here', 'I like jazz')]
        finally:
            await w.fri.close()
    run(scenario)


@pytest.mark.parametrize('where,capable', [(DM, False), (GENERAL, True)])
def test_no_move_buttons_in_public_or_against_an_older_controller(monkeypatch, tmp_path, where, capable):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        if not capable:
            w.fri.capabilities = w.fri.capabilities - {'memory-moves'}
        try:
            await w.say(GENERAL, 'fri I like chess', 'noted', note('I like chess'))
            scope = profiled(w.fri, {'guild_id': '123', 'user_id': '20', 'channel_id': '456' if where == DM else '1',
                                     'conversation_id': 'private:900' if where == DM else 'public:1'})
            state = await w.fri.api('POST', '/chat/memory/notes', scope)
            view = MemoryView(w.fri, scope, state, selected=state['entries'][0]['id'])
            assert [item.label for item in view.children[1:]] == ['Remove selected', 'Edit selected']
        finally:
            await w.fri.close()
    run(scenario)


def test_a_projects_note_is_shared_with_the_share_button(monkeypatch, tmp_path):
    """The member can still share a projects note server-wide with the two-press Share button."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        shown = AsyncMock()
        monkeypatch.setattr('fri.fri_panel.reply', shown)
        monkeypatch.setattr('fri.fri_memory_panel.allowed', AsyncMock(return_value=True))
        w.fri.agent.forget = Mock()
        press = lambda: Mock(spec=discord.Interaction, id=int(dm_control()['id']), guild_id=None, channel_id=DM,  # noqa: E731
                             user=SimpleNamespace(id=20, display_name='friend'))
        try:
            project = "I'm working on a pixel art game"
            await w.say(GENERAL, 'fri '+project, 'noted', note(project, 'projects'))
            scope = profiled(w.fri, {'guild_id': '123', 'user_id': '20', 'channel_id': '456',
                                     'conversation_id': 'private:900'})
            state = await w.fri.api('POST', '/chat/memory/notes', scope)
            view = MemoryView(w.fri, scope, state, selected=state['entries'][0]['id'])
            assert view.children[3].label == 'Share server-wide'
            await view.children[3].callback(press())
            await shown.await_args.kwargs['view'].children[3].callback(press())  # Yes, share server-wide.
            assert 'Shared that preference across the server.' in shown.await_args.kwargs['embed'].description
            for where in (GAMING, THREAD, DM):
                await w.say(where, 'fri what should i do tonight')
                assert model_context(w)['personal_memory']['notes'] == [
                    {'category': 'projects', 'evidence': project, 'scope': 'server', 'provenance': []}]
        finally:
            await w.fri.close()
    run(scenario)


def test_a_profile_note_said_where_not_everyone_reads_now_stays_out_of_public(monkeypatch, tmp_path):
    # fri #98: a note already in the profile from a channel that no longer counts as server-wide.
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await w.say(GENERAL, 'fri I like private jazz', 'noted', note('I like private jazz', 'music'))
            await w.say(GAMING, 'fri I like chess', 'noted', note('I like chess'))
            w.readable[GENERAL] = False
            await w.say(GAMING, 'fri what should i play')
            notes = model_context(w)['personal_memory']['notes']
            assert [row['evidence'] for row in notes] == ['I like chess']
            sent = json.dumps(model_context(w), default=str)
            assert 'private jazz' not in sent
            overview, _, _ = await w.say(GAMING, 'fri what do you remember about me')
            assert 'private jazz' not in overview.reply and 'chess' in overview.reply
            await w.say(DM, 'fri what should i listen to')  # Their own DM still has all of it.
            assert {row['evidence'] for row in model_context(w)['personal_memory']['notes']} == {
                'I like private jazz', 'I like chess'}
            w.readable[GENERAL] = True
            await w.say(GAMING, 'fri what should i play')
            assert {row['evidence'] for row in model_context(w)['personal_memory']['notes']} == {
                'I like private jazz', 'I like chess'}
        finally:
            await w.fri.close()
    run(scenario)


@pytest.mark.parametrize('origins', [None, ['public:1', None], ['public:1'], 'junk'])
def test_placing_notes_fails_closed_on_a_mismatch(origins):
    from fri.fri_memory import place_notes
    notes = [{'scope': 'server', 'evidence': 'a'}, {'scope': 'here', 'evidence': 'b'}]
    channels = {1: None}  # Gone, or never cached: can't tell, so restricted.
    context = place_notes(channels.get, {'personal_memory': {'notes': notes}, 'profile_origins': origins}, 'public:2')
    assert [row['evidence'] for row in context['personal_memory']['notes']] == (['a', 'b'] if origins is None else ['b'])
    private = place_notes(channels.get, {'personal_memory': {'notes': list(notes)}, 'profile_origins': origins},
                          'private:9')
    assert len(private['personal_memory']['notes']) == 2 and 'profile_origins' not in private


def test_a_note_the_member_shared_keeps_showing_after_its_channel_closes(monkeypatch, tmp_path):
    """Shared from My memory by their own press: its origin is that DM, so a closed channel doesn't hide it."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            w.readable[GENERAL] = False  # Said where not everyone can read: it stays there.
            await w.say(GENERAL, 'fri I like private jazz', 'noted', note('I like private jazz', 'music'))
            await w.say(GAMING, 'fri what should i play')
            assert model_context(w)['personal_memory']['notes'] == []
            scope = profiled(w.fri, {'guild_id': '123', 'user_id': '20', 'channel_id': '456',
                                     'conversation_id': 'private:900'})
            state = await w.fri.api('POST', '/chat/memory/notes', scope)
            await w.fri.api('POST', f"/chat/memory/notes/{state['entries'][0]['id']}", {
                **scope, 'source': dm_control(), 'revision': state['revision'], 'action': 'share'})
            await w.say(GAMING, 'fri what should i play')
            assert [row['evidence'] for row in model_context(w)['personal_memory']['notes']] == ['I like private jazz']
        finally:
            await w.fri.close()
    run(scenario)
