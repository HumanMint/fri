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
from fri.fri import FIRST_HERE, FIRST_MEMORY, FIRST_PROFILE, NO_MENTIONS
from fri.fri_agent import Agent
from fri.fri_chat import Chat, ChatResult
from fri.fri_errors import ControllerRejected
from fri.fri_memory import describe_profile, note_place, server_wide
from fri.fri_memory_panel import SHOWN, MemoryView, show

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
        item.permissions_for = lambda who: (discord.Permissions(view_channel=readable[cid]) if who is everyone
                                            else discord.Permissions.all())
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

    async def say(cid, text, reply='bold of you', notes=(), user=20, age=0):
        fri.chat_once.return_value = ChatResult(status='completed', reply=reply, notes=list(notes))
        event = message(cid, text, user, age)
        result = await fri.talk(123, cid, user, text, 'private' if cid == DM else 'public', f'member {user}', event=event)
        sent = SimpleNamespace(id=event.id+1, content=result.reply, edit=AsyncMock(), reply=AsyncMock())
        saved = await fri.record_turn(result, sent) if result.turn else None
        return result, saved, sent
    return SimpleNamespace(fri=fri, store=store, channels=channels, readable=readable, calls=calls, say=say,
                           client=client)


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
                assert notes == [{'category': 'games', 'evidence': 'I like Minecraft', 'scope': 'server'}]
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


def test_a_message_from_before_a_removal_stays_out_of_channel_chatter(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        with store.connection() as db:
            save(db, '123', 'server', '20', ('games', 'I like Minecraft'), time.time())
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
    def channel(view=True, nsfw=False):
        return SimpleNamespace(guild=guild, is_nsfw=lambda: nsfw,
                               permissions_for=lambda who: discord.Permissions(view_channel=view and who is everyone))
    assert server_wide(channel())
    assert not server_wide(channel(view=False)) and not server_wide(channel(nsfw=True))
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
