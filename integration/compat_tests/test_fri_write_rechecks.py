"""A request taken back during a write's last wait writes nothing: saves, posts, reminders, plans (fri #142)."""
import asyncio
import copy
import datetime as dt
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from fastapi.testclient import TestClient
from gene.api import create_app
from gene.config import Settings
from gene.fri_social_store import Social
from gene.fri_workspace_store import Workspace

from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_unsend import model, no_debounce
from fri.fri_actions import Actions
from fri.fri_agent import Agent
from fri.fri_chat import ChatResult
from fri.fri_context import ChatSource
from fri.fri_errors import ControllerRejected
from fri.fri_social import execute as social_execute
from fri.fri_workspace import execute as workspace_execute

MARKER = 'synthetic-private-marker'  # Synthetic words a withdrawal must keep from going anywhere.
DM = {'guild_id': '123', 'user_id': '10', 'channel_id': '456', 'conversation_id': 'private:999'}
PUBLIC = {**DM, 'conversation_id': 'public:456'}
CHANGED = 'Your original request changed. Send a fresh message.'  # As when its fetch shows the change.


@pytest.mark.parametrize('change', ['edit', 'delete', ''])
def test_a_save_taken_back_during_its_last_check_keeps_nothing(monkeypatch, tmp_path, change):
    """'fri save <words>', edited to 'never mind' or deleted while its save checks Gene's context (after
    the request's own fetch): nothing is saved. Left alone, it saves."""
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        trigger = message('fri save ' + MARKER)
        trigger.reply.return_value = SimpleNamespace(id=trigger.id+100, content='okay', edit=AsyncMock())
        asked = []
        async def respond(context, ready):
            await ready()
            if not asked:
                asked.append(True)
                return ChatResult(status='completed', action={'kind': 'work_save', 'work_id': '', 'version': 0,
                    'title': MARKER, 'body': MARKER, 'state': 'open'})
            return ChatResult(status='completed', reply='okay')
        model(fri, tmp_path, respond)
        api, changed = fri.api, []
        async def taken_back_meanwhile(method, path, payload=None):
            result = await api(method, path, payload)
            if path == '/chat/context' and asked and change and not changed:
                changed.append(True)
                if change == 'edit':
                    current = copy.copy(trigger)
                    current.content, current.edited_at = 'never mind', discord.utils.utcnow()
                    rows[trigger.id] = current
                    await fri.on_raw_message_edit(SimpleNamespace(channel_id=1, message=current))
                else:
                    rows.pop(trigger.id)
                    await fri.on_raw_message_delete(SimpleNamespace(channel_id=1, message_id=trigger.id))
            return result
        fri.api = taken_back_meanwhile
        try:
            await agent.handle(trigger)
            scope = {'guild_id': '123', 'user_id': '20', 'channel_id': '1', 'conversation_id': 'public:1'}
            kept = await api('POST', '/workspace/list', scope)
            assert bool(changed) == bool(change)
            if change:
                assert not kept['items'] and MARKER not in str(kept)
            else:
                assert len(kept['items']) == 1
                saved = await api('POST', '/workspace/read/'+kept['items'][0]['id'], scope)
                assert saved['body'] == MARKER
        finally:
            await fri.close()
    asyncio.run(scenario())


def snowflake(offset=0):
    return (int(time.time()*1000)-1420070400000) << 22 | offset


@pytest.mark.parametrize('change', ['edit', 'delete', ''])
@pytest.mark.parametrize('kind', ['prepare_post', 'reminder_prepare', 'reminder_cancel', 'plan_prepare'])
def test_other_writes_taken_back_during_their_last_wait_write_nothing(monkeypatch, tmp_path, kind, change):
    """The same for a post preview, a reminder preview or cancel, and a game-night card: the request edited
    or deleted during the last wait before its write (fresh channel access for a post, Gene's context for
    the rest) writes nothing. Left alone, each still goes through."""
    async def scenario():
        settings = Settings(tmp_path/'api.db', '123', '10', 'runtime', 'worker', 'deploy',
                            workshop_channel_id='456', fri_token='fri')
        client = TestClient(create_app(settings))
        store = client.app.state.store
        calls = []
        async def call(method, path, body=None):
            calls.append(path)
            result = client.request(method, path, json=body, headers={'Authorization': 'Bearer fri'})
            if result.status_code >= 400:
                raise ControllerRejected(str(result.json()))
            result = result.json()
            if path == '/chat/context' and kind != 'prepare_post':
                take_back()  # The last wait before the write: Gene's context.
            return result
        fri = SimpleNamespace(api=call, data=tmp_path, guild_id=123, workshop_id=456, owner_id=10,
                              user=SimpleNamespace(id=50), busy=set())
        agent = fri.agent = Agent(fri)
        agent.state = {'config': {'enabled': True, 'support_dms': True}, 'history_channels': []}
        agent.member = AsyncMock(return_value=SimpleNamespace(id=10))
        public = kind == 'plan_prepare'
        text = {'prepare_post': 'post my intro to announcements', 'reminder_prepare': 'remind me tomorrow to update the mods',
                'reminder_cancel': 'cancel my reminder', 'plan_prepare': 'help us choose times for Helldivers'}[kind]
        guild = SimpleNamespace(id=123, me=fri.user) if public else None
        event = Mock(spec=discord.Message, id=snowflake(), content=text, guild=guild, webhook_id=None,
                     author=SimpleNamespace(id=10, display_name='Member', bot=False))
        event.channel = (Mock(spec=discord.TextChannel, id=456, parent_id=None, guild=guild) if public else
                         Mock(spec=discord.DMChannel, id=999))
        event.channel.fetch_message = AsyncMock(return_value=event)
        # Taken in as handle() takes a message, and still being handled.
        agent.arrived[event.id] = time.time()
        agent.handling[event.id] += 1

        def take_back():
            if not change or calls.count('/chat/context') > 1:
                return
            if change == 'edit':
                agent.edit(event.channel.id, SimpleNamespace(id=event.id, content='never mind',
                    edited_at=discord.utils.utcnow(), author=SimpleNamespace(id=10, bot=False)))
                assert str(event.id) in agent.withdrawn
            else:
                agent.unsend(event.channel.id, [event.id])
                assert event.id in agent.unsent

        async def capabilities(_, target):
            take_back()  # The last wait before a post preview: fresh channel access.
            return {target: {'one_off_post': True}}
        monkeypatch.setattr('fri.fri_workspace.capabilities', capabilities)
        actions = Actions(agent, 10, 456 if public else 999, public)
        actions.event, actions.message, actions.revision = event, text, 0
        actions.scope = PUBLIC if public else DM
        source = ChatSource(id=str(snowflake(1)), kind='discord_message', user_id='10', display_name='Member',
                            channel_id='999')
        if kind == 'prepare_post':
            work = Workspace(store).save(DM, source, 0, '', 0, 'Intro', 'exact text', 'open')
            action = {'kind': kind, 'work_id': work['id'], 'version': work['version'], 'channel_id': '777'}
            write = '/workspace/posts/prepare'
        elif kind == 'reminder_cancel':
            reminder = Social(store).reminder_prepare(DM, source, 0, 'update the mods', future(), 'UTC')
            action = {'kind': kind, 'reminder_id': reminder['id']}
            write = '/social/reminders/'+reminder['id']+'/decide'
        elif kind == 'reminder_prepare':
            action = {'kind': kind, 'body': 'update the mods', 'local_time': future(), 'timezone': 'UTC'}
            write = '/social/reminders/prepare'
        else:
            action = {'kind': kind, 'title': 'Helldivers', 'local_times': future()+'\n'+future(2), 'timezone': 'UTC'}
            write = '/social/plans/prepare'
        execute = workspace_execute if kind == 'prepare_post' else social_execute
        result = await execute(actions, action)
        if change:
            assert result == {'blocked': CHANGED}
            assert write not in calls
            if kind == 'reminder_cancel':
                assert Social(store).reminder_list(DM)['items'][0]['state'] == 'pending'
        else:
            assert 'blocked' not in result and write in calls
            if kind == 'reminder_cancel':
                assert result['state'] == 'cancelled'
    asyncio.run(scenario())


def future(days=1):
    return dt.datetime.fromtimestamp(time.time()+days*86400, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M')
