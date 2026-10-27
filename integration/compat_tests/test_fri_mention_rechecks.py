"""What the checks #53 kept still cover, from its Codex reviews: a ping's preceding photo, nearby messages
deleted during the privacy read that follows the Discord reads, images on turns that aren't pings, a ping
edited while its access reads are out, an image withdrawn before a follow-up model call, a follow-up without a grant, and a ping edited or
its author's access revoked during its last check."""
import asyncio
import copy
import datetime as dt
import inspect
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import httpx
import pytest

from compat_tests.test_fri_continuity import arm, origin
from compat_tests.test_fri_mentions import attachment, burst, environment
from fri.fri_chat import Chat, ChatResult


@pytest.mark.parametrize('change', ['delete', 'remove_attachment'])
@pytest.mark.parametrize('tool', [False, True])
def test_preceding_photo_is_checked_after_model(monkeypatch, tmp_path, change, tool):
    async def scenario():
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        photo = message('', age=10, attachments=[attachment(1)])
        question = 'what is the current Python release?'
        event = message('<@99> what does my screenshot say? also '+question)
        event.reply.return_value = SimpleNamespace(id=event.id+1, content='the screenshot contains a medical diagnosis')
        monkeypatch.setattr('fri.fri_images.load_image', AsyncMock(return_value='data:image/png;base64,aGVsbG8='))
        monkeypatch.setattr('fri.fri_actions.lookup', AsyncMock(return_value=({
            'query': question, 'answer': 'Public version info', 'sources': ['https://www.python.org/'],
            'checked_at': dt.datetime.now(dt.timezone.utc).isoformat()}, {}, 'completed')))
        async def fetch(mid):
            if mid not in rows:
                raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'deleted')
            return rows[mid]
        channel.fetch_message.side_effect = fetch
        model_calls = []
        async def respond(_, context):
            assert context['images'] and context['image_event'].id == photo.id
            model_calls.append(context)
            if tool and len(model_calls) == 1:
                return ChatResult(status='completed', action={'kind': 'web_search', 'query': question})
            if change == 'delete':
                del rows[photo.id]
            else:
                changed = copy.copy(photo)
                changed.attachments = []
                changed.edited_at = dt.datetime.now(dt.timezone.utc)
                rows[photo.id] = changed
            return ChatResult(status='completed', reply='the screenshot contains a medical diagnosis')
        fri.chat_once.side_effect = respond
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            replies = [call.args[0] for call in event.reply.await_args_list]
            saved = store.chat_context('123', 'public:1', '20')
            assert len(model_calls) == (2 if tool else 1)
            assert not any('medical diagnosis' in reply for reply in replies)
            assert 'medical diagnosis' not in str(saved)
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('channel_id', [1, 8])
def test_nearby_deleted_during_last_profile_check_stays_withheld(monkeypatch, tmp_path, channel_id):
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=channel_id)
        nearby = message('sensitive third-party detail', user=30, age=20, attachments=[attachment(channel_id)])
        event = message('<@99> what did they say?')
        if channel_id == 1:
            event.reference = SimpleNamespace(message_id=nearby.id, channel_id=channel_id)
        event.reply.return_value = SimpleNamespace(id=event.id+1, content='sensitive third-party detail')
        fri.chat_once.return_value = ChatResult(status='completed', reply='sensitive third-party detail')
        monkeypatch.setattr('fri.fri_images.load_image', AsyncMock(return_value='data:image/png;base64,aGVsbG8='))
        talked = []
        talk, api = fri.talk, fri.api
        async def talking(*args, **kwargs):
            result = await talk(*args, **kwargs)
            talked.append(True)
            return result
        fri.talk = talking
        async def checked(method, path, payload=None):
            result = await api(method, path, payload)
            if talked and path == '/chat/mentions/profiles':
                rows.pop(nearby.id, None)
            return result
        fri.api = checked
        async def fetch(mid):
            if mid not in rows:
                raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'deleted')
            return rows[mid]
        channel.fetch_message.side_effect = fetch
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            assert nearby.id not in rows
            replies = [call.args[0] for call in event.reply.await_args_list]
            assert not any('sensitive third-party detail' in reply for reply in replies)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_nearby_deleted_during_ready_profile_check_never_reaches_provider(monkeypatch, tmp_path):
    import httpx

    from fri.fri_chat import Chat
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path)
        fri.data = tmp_path
        nearby = message('sensitive third-party detail', user=30, age=20)
        event = message('<@99> what did they say?')
        signed_in, sent = [], []
        def access_token():
            signed_in.append(True)
            return 'fictional'
        fri.chat = Chat(SimpleNamespace(access_token=access_token), httpx.MockTransport(
            lambda request: sent.append(request) or httpx.Response(500)))
        del fri.chat_once
        api = fri.api
        async def checked(method, path, payload=None):
            result = await api(method, path, payload)
            if signed_in and path == '/chat/mentions/profiles':
                rows.pop(nearby.id, None)
            return result
        fri.api = checked
        async def fetch(mid):
            if mid not in rows:
                raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'deleted')
            return rows[mid]
        channel.fetch_message.side_effect = fetch
        try:
            await agent.handle(event)
            assert signed_in and nearby.id not in rows
            assert not sent
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_image_ping_edited_during_reply_target_read_cannot_start_tool(monkeypatch, tmp_path):
    import inspect

    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        target = message('ordinary context', user=30, age=20)
        question = 'what is the current Python release?'
        event = message('<@99> '+question, attachments=[attachment(1)])
        event.reference = SimpleNamespace(message_id=target.id, channel_id=channel.id)
        lookup = AsyncMock(return_value=({'query': question, 'answer': 'public information',
            'sources': ['https://www.python.org/'], 'checked_at': dt.datetime.now(dt.timezone.utc).isoformat()}, {}, 'completed'))
        monkeypatch.setattr('fri.fri_actions.lookup', lookup)
        monkeypatch.setattr('fri.fri_images.load_image', AsyncMock(return_value='data:image/png;base64,aGVsbG8='))
        fri.chat_once.side_effect = [ChatResult(status='completed', action={'kind': 'web_search', 'query': question}),
                                    ChatResult(status='completed', reply='Public information')]
        changed = []
        async def fetch(mid):
            names = [frame.function for frame in inspect.stack()]
            # While the tool's conversation fence reads its reply target, after its invitation check.
            if mid == target.id and 'execute' in names and 'reply_target' in names and not changed:
                edited = copy.copy(event)
                edited.content = '<@99> never mind'
                edited.attachments = []
                edited.edited_at = dt.datetime.now(dt.timezone.utc)
                rows[event.id] = edited
                changed.append(True)
            return rows[mid]
        channel.fetch_message.side_effect = fetch
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            assert changed
            lookup.assert_not_awaited()
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('grouped', [False, True])
def test_sending_without_invitation_still_checks_image_after_context_read(monkeypatch, tmp_path, grouped):
    async def scenario():
        fri, agent, store, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        first = message('<@99> hey', age=1) if grouped else None
        question = 'what is the current Python release?'
        event = message(('' if grouped else 'fri ')+'what does my screenshot say? also '+question,
                        attachments=[attachment(1)])
        event.reply.return_value = SimpleNamespace(id=event.id+1, content='the screenshot contains a medical diagnosis')
        monkeypatch.setattr('fri.fri_images.load_image', AsyncMock(return_value='data:image/png;base64,aGVsbG8='))
        monkeypatch.setattr('fri.fri_actions.lookup', AsyncMock(return_value=({'query': question,
            'answer': 'Public version info', 'sources': ['https://www.python.org/'],
            'checked_at': dt.datetime.now(dt.timezone.utc).isoformat()}, {}, 'completed')))
        fri.chat_once.side_effect = [ChatResult(status='completed', action={'kind': 'web_search', 'query': question}),
                                    ChatResult(status='completed', reply='the screenshot contains a medical diagnosis')]
        api, edited = fri.api, []
        async def checked(method, path, payload=None):
            result = await api(method, path, payload)
            # Fri.talk's post-tool context refresh, after its explicit image check.
            if path == '/chat/context' and fri.chat_once.await_count == 2 and not edited:
                changed = copy.copy(event)
                changed.attachments = []
                changed.edited_at = dt.datetime.now(dt.timezone.utc)
                rows[event.id] = changed
                edited.append(True)
            return result
        fri.api = checked
        try:
            if grouped:
                await burst(agent, first, event)
            else:
                with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                    await agent.handle(event)
            assert edited and fri.chat_once.await_count == 2
            replies = [call.args[0] for call in event.reply.await_args_list]
            saved = store.chat_context('123', 'public:1', '20')
            assert not any('medical diagnosis' in reply for reply in replies)
            assert 'medical diagnosis' not in str(saved)
        finally:
            await fri.close()
    asyncio.run(scenario())


REAL_SLEEP = asyncio.sleep



@pytest.mark.parametrize('image', [False, True])
@pytest.mark.parametrize('channel_id', [1, 8])
def test_edit_during_final_member_fetch_is_withheld(monkeypatch, tmp_path, image, channel_id):
    async def scenario():
        fri, agent, store, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=channel_id)
        event = message('<@99> what does my sensitive screenshot say?' if image else '<@99> sensitive member detail',
                        attachments=[attachment(channel_id)] if image else [])
        sensitive = 'the sensitive member detail is a medical diagnosis'
        event.reply.return_value = SimpleNamespace(id=event.id + 1, content=sensitive)
        fri.chat_once.return_value = ChatResult(status='completed', reply=sensitive)
        monkeypatch.setattr('fri.fri_images.load_image', AsyncMock(return_value='data:image/png;base64,aGVsbG8='))
        talked, edited = [], []
        talk = fri.talk

        async def talking(*args, **kwargs):
            result = await talk(*args, **kwargs)
            talked.append(True)
            return result

        fri.talk = talking

        async def fetch_member(user):
            if talked and user == 99 and not edited:
                await REAL_SLEEP(0)
                changed = copy.copy(event)
                changed.content = '<@99> never mind'
                changed.attachments = []
                changed.edited_at = dt.datetime.now(dt.timezone.utc)
                rows[event.id] = changed
                edited.append(True)
            return SimpleNamespace(id=user, bot=user == 99)

        channel.guild.fetch_member.side_effect = fetch_member
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            assert edited
            replies = [call.args[0] for call in event.reply.await_args_list]
            saved = store.chat_context('123', f'public:{channel_id}', '20')
            exposed = (any(sensitive in reply for reply in replies), sensitive in str(saved))
            assert exposed == (False, False), exposed
        finally:
            await fri.close()

    asyncio.run(scenario())



@pytest.mark.parametrize('change', ['delete', 'remove_attachment'])
def test_withdrawn_ping_image_does_not_reach_followup_model(monkeypatch, tmp_path, change):
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        question = 'what is the current Python release?'
        event = message('<@99> what does my screenshot say? also ' + question, attachments=[attachment(1)])
        lookup = AsyncMock(return_value=({'query': question, 'answer': 'Public release info',
            'sources': ['https://www.python.org/'],
            'checked_at': dt.datetime.now(dt.timezone.utc).isoformat()}, {}, 'completed'))
        monkeypatch.setattr('fri.fri_actions.lookup', lookup)
        monkeypatch.setattr('fri.fri_images.load_image', AsyncMock(return_value='data:image/png;base64,aGVsbG8='))
        model_calls, changed, sent = [], [], []
        fri.data = tmp_path

        def provider(request):
            sent.append(json.loads(request.content))
            return httpx.Response(500)

        fri.chat = Chat(SimpleNamespace(access_token=lambda: 'fictional'), httpx.MockTransport(provider))

        async def respond(text, context):
            model_calls.append(bool(context.get('images')))
            if len(model_calls) == 1:
                return ChatResult(status='completed', action={'kind': 'web_search', 'query': question})
            return await type(fri).chat_once(fri, text, context)

        fri.chat_once.side_effect = respond
        api = fri.api

        async def checked(method, path, payload=None):
            result = await api(method, path, payload)
            if (path == '/chat/mentions/profiles' and lookup.await_count and not changed and
                    any(frame.function == '_run' for frame in inspect.stack())):
                if change == 'delete':
                    del rows[event.id]
                else:
                    edited = copy.copy(event)
                    edited.attachments = []
                    edited.edited_at = dt.datetime.now(dt.timezone.utc)
                    rows[event.id] = edited
                changed.append(True)
            return result

        fri.api = checked

        async def fetch(mid):
            if mid not in rows:
                raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'deleted')
            return rows[mid]

        channel.fetch_message.side_effect = fetch
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            assert changed
            images_sent = [part for payload in sent for row in payload['input']
                           if isinstance(row.get('content'), list) for part in row['content']
                           if part.get('type') == 'input_image']
            assert not images_sent, images_sent
            assert model_calls == [True], model_calls
        finally:
            await fri.close()

    asyncio.run(scenario())


SECRET = 'the screenshot contains a medical diagnosis'


def live_fetch(channel, rows):
    async def fetch(mid):
        if mid not in rows:
            raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'deleted')
        return rows[mid]
    channel.fetch_message.side_effect = fetch


@pytest.mark.parametrize('change', ['delete', 'remove_attachment'])
def test_pending_followup_rechecks_preceding_photo(monkeypatch, tmp_path, change):
    async def scenario():
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        question = 'what is the current Python release?'
        original = message(question, age=60)
        body = origin(goal='web', text=question)
        body['source'].update(id=str(original.id), display_name=original.author.display_name)
        arm(store, body, topic=question)
        photo = message('', age=10, attachments=[attachment(1)])
        event = message('<@99> the stable one, and what does my screenshot say?')
        event.reply.return_value = SimpleNamespace(id=event.id + 1, content=SECRET)
        live_fetch(channel, rows)
        monkeypatch.setattr('fri.fri_images.load_image', AsyncMock(return_value='data:image/png;base64,aGVsbG8='))
        async def respond(_, context):
            assert context['images'] and context['image_event'].id == photo.id
            assert set(context['actions']) == {'web_search'}
            if change == 'delete':
                del rows[photo.id]
            else:
                edited = copy.copy(photo)
                edited.attachments = []
                edited.edited_at = dt.datetime.now(dt.timezone.utc)
                rows[photo.id] = edited
            return ChatResult(status='completed', reply=SECRET)
        fri.chat_once.side_effect = respond
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            assert any(path == '/chat/operations/admit' for path, _ in calls)
            assert not any(path == '/chat/mentions/admit' for path, _ in calls)
            replies = [call.args[0] for call in event.reply.await_args_list]
            saved = store.chat_context('123', 'public:1', '20')
            exposed = (any(SECRET in reply for reply in replies), SECRET in str(saved))
            assert exposed == (False, False), exposed
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('channel_id', [1, 8])
def test_member_revoked_while_channel_fetch_is_inflight(monkeypatch, tmp_path, channel_id):
    async def scenario():
        fri, agent, store, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=channel_id)
        event = message('<@99> sensitive medical detail')
        event.reply.return_value = SimpleNamespace(id=event.id + 1, content=SECRET)
        live_fetch(channel, rows)
        fri.chat_once.return_value = ChatResult(status='completed', reply=SECRET)
        state = {'allowed': True, 'changed': False}
        allowed = discord.Permissions.all()
        denied = discord.Permissions.none()
        channel.permissions_for = lambda member: allowed if getattr(member, 'allowed', True) else denied
        async def member(user):
            return SimpleNamespace(id=user, bot=user == 99, allowed=user == 99 or state['allowed'])
        async def fetched_channel(cid):
            if fri.chat_once.await_count and not state['changed']:
                await REAL_SLEEP(0)  # Other independently dispatched reads can finish first.
                state.update(allowed=False, changed=True)
            return channel
        channel.guild.fetch_channel.side_effect = fetched_channel
        channel.guild.fetch_member.side_effect = member
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            assert state['changed']
            replies = [call.args[0] for call in event.reply.await_args_list]
            saved = store.chat_context('123', f'public:{channel_id}', '20')
            exposed = (any(SECRET in reply for reply in replies), SECRET in str(saved))
            assert exposed == (False, False), exposed
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('channel_id', [1, 8])
@pytest.mark.parametrize('image', [False, True])
@pytest.mark.parametrize('workflow', ['plain', 'tool', 'gene_receipt'])
def test_ping_changed_during_post_model_profile_read(monkeypatch, tmp_path, channel_id, image, workflow):
    if workflow == 'gene_receipt' and channel_id == 8:
        pytest.skip('Gene receipts are unavailable outside configured channels')
    async def scenario():
        fri, agent, store, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=channel_id)
        event = message('<@99> what does my sensitive screenshot say?' if image else '<@99> sensitive medical detail',
                        attachments=[attachment(channel_id)] if image else [])
        event.reply.return_value = SimpleNamespace(id=event.id + 1, content=SECRET)
        live_fetch(channel, rows)
        monkeypatch.setattr('fri.fri_images.load_image', AsyncMock(return_value='data:image/png;base64,aGVsbG8='))
        completed, model_calls = [], []
        async def respond(_, context):
            model_calls.append(context)
            if workflow == 'tool' and len(model_calls) == 1:
                return ChatResult(status='completed', action={'kind': 'web_search', 'query': question})
            if workflow == 'gene_receipt':
                assert context['gene_receipts']
            completed.append(True)
            return ChatResult(status='completed', reply=SECRET)
        question = 'what is the current Python release?'
        if workflow == 'tool':
            event.content += ' also ' + question
            monkeypatch.setattr('fri.fri_actions.lookup', AsyncMock(return_value=({'query': question,
                'answer': 'Public release information', 'sources': ['https://www.python.org/'],
                'checked_at': dt.datetime.now(dt.timezone.utc).isoformat()}, {}, 'completed')))
        fri.chat_once.side_effect = respond
        api, changed = fri.api, []
        async def checked(method, path, payload=None):
            if workflow == 'gene_receipt' and path == '/controls/recent':
                return {'receipts': [{'id': 'r1', 'channel_id': '1', 'action': 'pause', 'argument': '',
                    'state': 'finished', 'at': time.time(), 'grounded': True,
                    'result': {'done': True, 'output': 'Paused'}}]}
            result = await api(method, path, payload)
            if path == '/chat/mentions/profiles' and completed and not changed:
                edited = copy.copy(event)
                edited.content = '<@99> never mind'
                edited.attachments = []
                edited.edited_at = dt.datetime.now(dt.timezone.utc)
                rows[event.id] = edited
                changed.append(True)
            return result
        fri.api = checked
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            assert changed
            replies = [call.args[0] for call in event.reply.await_args_list]
            saved = store.chat_context('123', f'public:{channel_id}', '20')
            exposed = (any(SECRET in reply for reply in replies), SECRET in str(saved))
            assert exposed == (False, False), exposed
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('workflow', ['plain', 'tool', 'gene_receipt', 'pending'])
@pytest.mark.parametrize('change', ['edit', 'access'])
def test_ping_changed_during_post_model_photo_fetch(monkeypatch, tmp_path, workflow, change):
    async def scenario():
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        question = 'what is the current Python release?'
        if workflow == 'pending':
            original = message(question, age=60)
            body = origin(goal='web', text=question)
            body['source'].update(id=str(original.id), display_name=original.author.display_name)
            arm(store, body, topic=question)
        photo = message('', age=10, attachments=[attachment(1)])
        event = message('<@99> read my sensitive screenshot' + (
            ' also ' + question if workflow == 'tool' else
            ', the stable one' if workflow == 'pending' else ''))
        secret = 'the screenshot contains a private medical diagnosis'
        event.reply.return_value = SimpleNamespace(id=event.id + 1, content=secret)
        monkeypatch.setattr('fri.fri_images.load_image', AsyncMock(return_value='data:image/png;base64,aGVsbG8='))
        monkeypatch.setattr('fri.fri_actions.lookup', AsyncMock(return_value=({
            'query': question, 'answer': 'Public release information', 'sources': ['https://www.python.org/'],
            'checked_at': dt.datetime.now(dt.timezone.utc).isoformat()}, {}, 'completed')))
        completed, edits = [], []

        async def respond(_, context):
            assert context['images'] and context['image_event'].id == photo.id
            if workflow == 'tool' and not context.get('action_result'):
                return ChatResult(status='completed', action={'kind': 'web_search', 'query': question})
            if workflow == 'gene_receipt':
                assert context['gene_receipts']
            completed.append(True)
            return ChatResult(status='completed', reply=secret)

        fri.chat_once.side_effect = respond
        api = fri.api

        async def checked(method, path, payload=None):
            if workflow == 'gene_receipt' and path == '/controls/recent':
                return {'receipts': [{'id': 'r1', 'channel_id': '1', 'action': 'pause', 'argument': '',
                    'state': 'finished', 'at': time.time(), 'grounded': True,
                    'result': {'done': True, 'output': 'Paused'}}]}
            return await api(method, path, payload)

        fri.api = checked

        async def fetch(mid):
            if mid == photo.id and completed and not edits:
                if change == 'edit':
                    changed = copy.copy(event)
                    changed.content = '<@99> never mind, do not disclose that image'
                    changed.edited_at = dt.datetime.now(dt.timezone.utc)
                    rows[event.id] = changed
                else:
                    channel.permissions_for = lambda member: (
                        discord.Permissions.none() if member.id == 20 else discord.Permissions.all())
                edits.append(True)
            return rows[mid]

        channel.fetch_message.side_effect = fetch
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            assert edits
            replies = [call.args[0] for call in event.reply.await_args_list]
            saved = store.chat_context('123', 'public:1', '20')
            exposed = (any(secret in reply for reply in replies), secret in str(saved))
            assert exposed == (False, False), {'exposed': exposed, 'replies': replies}
        finally:
            await fri.close()
    asyncio.run(scenario())
