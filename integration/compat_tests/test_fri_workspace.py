"""Exercise durable work and publishing across controller/runtime boundaries offline."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import httpx
import pytest
from fastapi.testclient import TestClient
from gene.api import create_app
from gene.config import Settings
from gene.fri_workspace_store import Workspace
from gene.store import Conflict, Store

from fri.fri_chat import ChatResult
from fri.fri_context import ChatSource
from fri.fri_errors import ControllerRejected
from fri.fri_runner import run
from fri.fri_workspace import capabilities, deliver_one, execute, post_component, send_once

SCOPE = {'guild_id': '123', 'user_id': '10', 'channel_id': '456', 'conversation_id': 'private:999'}


def source(offset=0, kind='discord_message', user='10', channel='999', guild=None):
    snowflake = str((int(time.time()*1000)-1420070400000) << 22 | offset)
    return ChatSource(id=snowflake, kind=kind, user_id=user, display_name='HumanMint', channel_id=channel, guild_id=guild)


def saved(work, *, text='exact text\nwith spacing  ', scope=SCOPE, event=None):
    return work.save(scope, event or source(), 0, '', 0, 'Intro', text, 'open')


def prepared(work):
    draft = saved(work)
    return work.prepare(SCOPE, source(1), 0, draft['id'], 1, '777')


def authorize(work, post):
    work.bind(post['id'], '888')
    return work.decide(post['id'], source(2, kind='discord_interaction'), '888', True)


def test_exact_work_survives_restart_chat_clear_and_scope_separation(store):
    work = Workspace(store)
    draft = saved(work)
    store.clear_chats('10')
    work = Workspace(Store(store.path))
    assert work.read(SCOPE, draft['id'])['body'] == 'exact text\nwith spacing  '
    for scope in ({**SCOPE, 'user_id': '11'}, {**SCOPE, 'conversation_id': 'public:999'}, {**SCOPE, 'guild_id': '124'}):
        assert work.read(scope)['items'] == []
        with pytest.raises(KeyError):
            work.read(scope, draft['id'])
    store.personal_control('10', 'disable')
    assert work.read(SCOPE)['items'] == []


def test_save_replay_cas_and_edits_revoke_post(store):
    work, event = Workspace(store), source()
    draft = saved(work, event=event)
    assert saved(work, event=event)['id'] == draft['id']
    with pytest.raises(Conflict):
        saved(work, event=event, text='changed')
    post = work.prepare(SCOPE, source(1), 0, draft['id'], 1, '777')
    authorize(work, post)
    edited = work.save(SCOPE, source(3), 0, draft['id'], 1, 'Intro', 'new version', 'open')
    assert edited['version'] == 2 and work.post(post['id'])['state'] == 'cancelled'
    assert work.claim() is None
    with pytest.raises(Conflict):
        work.save(SCOPE, source(4), 0, draft['id'], 1, 'Intro', 'stale', 'open')


@pytest.mark.parametrize('bad', ['user', 'dm', 'guild', 'message', 'old', 'preview'])
def test_post_requires_exact_fresh_owner_interaction(store, bad):
    work = Workspace(store)
    post = prepared(work)
    work.bind(post['id'], '888')
    event = source(2, kind='discord_interaction')
    edits = {'user': {'user_id': '11'}, 'dm': {'channel_id': '1234'}, 'guild': {'guild_id': '123'},
             'message': {'kind': 'discord_message'}, 'old': {'id': '100000000000000001'}}
    event = event.model_copy(update=edits.get(bad, {}))
    with pytest.raises(ValueError):
        work.decide(post['id'], event, '889' if bad == 'preview' else '888', True)
    assert work.post(post['id'])['state'] == 'pending' and work.claim() is None


def test_post_claim_restart_and_ambiguous_send_never_replay(store):
    work = Workspace(store)
    post = prepared(work)
    authorize(work, post)
    claim = work.claim()
    assert work.claim() is None
    work.sending(post['id'], claim['lease'])
    with pytest.raises(ValueError):
        work.sending(post['id'], claim['lease'])
    work = Workspace(Store(store.path))
    assert work.claim() is None
    with store.connection() as db:
        db.execute('UPDATE fri_posts SET expires=0')
    assert work.post(post['id'])['state'] == 'uncertain'
    assert work.decide(post['id'], source(2, kind='discord_interaction'), '888', True)['state'] == 'uncertain'
    assert work.claim() is None
    # A late real receipt can settle uncertainty without sending again.
    work.finish(post['id'], claim['lease'], 'sent', '987')
    assert work.read(SCOPE, post['work_id'])['deliveries'][0]['message_id'] == '987'


def test_clear_memory_rejects_inflight_grant(store):
    work = Workspace(store)
    post = prepared(work)
    authorize(work, post)
    claim = work.claim()
    store.personal_control('10', 'disable')
    with pytest.raises(ValueError):
        work.sending(post['id'], claim['lease'])
    assert work.claim() is None


def test_disabled_memory_limits_and_credentials(store):
    work = Workspace(store)
    with pytest.raises(ValueError):
        saved(work, text='token=abcd1234567890')
    store.personal_control('10', 'disable')
    with pytest.raises(ValueError):
        saved(work)


@pytest.fixture
def api(tmp_path):
    settings = Settings(tmp_path/'api.db', '123', '10', 'runtime', 'worker', 'deploy',
                        workshop_channel_id='456', fri_token='fri')
    client = TestClient(create_app(settings))
    async def call(method, path, body=None):
        result = client.request(method, path, json=body, headers={'Authorization': 'Bearer fri'})
        if result.status_code >= 400:
            raise ControllerRejected(str(result.json()))
        return result.json()
    return client, call


def test_controller_auth_and_owner_boundaries(api):
    client, _ = api
    body = {**SCOPE, 'source': source().model_dump(), 'revision': 0, 'work_id': '', 'version': 0,
            'title': 'Intro', 'body': 'hello', 'state': 'open'}
    for role in ('runtime', 'worker', 'deploy'):
        assert client.post('/workspace/save', json=body, headers={'Authorization': 'Bearer '+role}).status_code == 401
    body['user_id'] = '20'
    body['source']['user_id'] = '20'
    draft = client.post('/workspace/save', json=body, headers={'Authorization': 'Bearer fri'}).json()
    post = {**body, 'work_id': draft['id'], 'version': 1, 'target_id': '777'}
    for key in ('title', 'body', 'state'):
        post.pop(key)
    assert client.post('/workspace/posts/prepare', json=post, headers={'Authorization': 'Bearer fri'}).status_code == 403
    body['guild_id'] = '999'
    assert client.post('/workspace/save', json=body, headers={'Authorization': 'Bearer fri'}).status_code == 403


def discord_harness(api, tmp_path):
    client, call = api
    work = Workspace(client.app.state.store)
    post = prepared(work)
    authorize(work, post)
    preview = SimpleNamespace(author=SimpleNamespace(id=50), edit=AsyncMock())
    original = SimpleNamespace(author=SimpleNamespace(id=10), edited_at=None)
    dm = SimpleNamespace(fetch_message=AsyncMock(side_effect=lambda mid: preview if mid == 888 else original))
    fri = SimpleNamespace(api=call, data=tmp_path, owner_id=10, guild_id=123, user=SimpleNamespace(id=50),
                          fetch_channel=AsyncMock(return_value=dm), http=SimpleNamespace(request=AsyncMock(return_value={'id': '987'})))
    return fri, work, post, original


@pytest.mark.parametrize('failure', ['', 'permission', 'timeout', 'receipt', 'deleted_source'])
def test_runtime_delivery_failures_do_not_duplicate(api, tmp_path, monkeypatch, failure):
    async def scenario():
        fri, work, post, original = discord_harness(api, tmp_path)
        async def sender(f, p):
            return await f.http.request(json={'content': p['body'], 'allowed_mentions': {'parse': []}, 'enforce_nonce': True, 'nonce': str(int(p['id'][:20],16))})
        monkeypatch.setattr('fri.fri_workspace.send_once', sender)
        monkeypatch.setattr('fri.fri_workspace.capabilities', AsyncMock(return_value={'777': {'one_off_post': failure != 'permission'}}))
        if failure == 'deleted_source':
            original.edited_at = True
        if failure == 'timeout':
            fri.http.request.side_effect = TimeoutError()
        real_api = fri.api
        async def unreliable(method, path, body=None):
            if failure == 'receipt' and path.endswith('/finish'):
                raise OSError('lost acknowledgement')
            return await real_api(method, path, body)
        fri.api = unreliable
        if failure == 'receipt':
            with pytest.raises(OSError):
                await deliver_one(fri)
            assert list((tmp_path/'post-receipts').glob('*.json'))
        else:
            await deliver_one(fri)
        fri.api = real_api
        await deliver_one(fri)
        assert fri.http.request.await_count == (0 if failure in ('permission', 'deleted_source') else 1)
        state = work.post(post['id'])['state']
        assert state == ('failed' if failure in ('permission', 'deleted_source') else 'uncertain' if failure == 'timeout' else 'sent')
        if fri.http.request.await_count:
            payload = fri.http.request.await_args.kwargs['json']
            assert payload['content'] == post['body'] and payload['allowed_mentions'] == {'parse': []}
            assert payload['enforce_nonce'] and len(payload['nonce']) <= 25
    asyncio.run(scenario())


def test_capabilities_fresh_permissions_and_no_ambient_expansion():
    async def scenario():
        # Real discord.py permission resolution, using data fetched by the harness.
        state = Mock()
        state.store_user = lambda data: discord.User(state=state, data=data)
        guild = discord.Guild(state=state, data={'id': '123', 'name': 'ff', 'owner_id': '10',
            'roles': [{'id': '123', 'name': '@everyone', 'permissions': str(discord.Permissions(view_channel=True,
                read_message_history=True, send_messages=True).value)}], 'channels': [], 'members': []})
        channel = discord.TextChannel(state=state, guild=guild, data={'id': '777', 'name': 'announcements', 'type': 5, 'position': 0})
        members = {uid: discord.Member(data={'user': {'id': str(uid), 'username': str(uid), 'discriminator': '0', 'avatar': None}, 'roles': [], 'flags': 0}, guild=guild, state=state) for uid in (10, 50)}
        # Guild methods are slots; fake outer wrapper supplies transport methods, real copied guild handles roles.
        outer = Mock(wraps=guild)
        outer.fetch_member = AsyncMock(side_effect=lambda uid: members[uid])
        outer.fetch_roles = AsyncMock(return_value=guild.roles)
        outer.fetch_channels = AsyncMock(return_value=[channel])
        outer.id, outer.owner_id, outer._roles = 123, 10, guild._roles
        outer.default_role = guild.default_role
        fri = SimpleNamespace(guild_id=123, owner_id=10, user=SimpleNamespace(id=50), get_guild=lambda _: outer,
            fetch_channel=AsyncMock(return_value=channel), agent=SimpleNamespace(config={'enabled': True}, state={'history_channels': []}, allowed=lambda c: False))
        result = await capabilities(fri, '777')
        assert result['777']['one_off_post'] and result['777']['read_history']
        assert not result['777']['ambient_reply'] and not result['777']['configured_read']
        assert outer.fetch_member.await_count == 2 and outer.fetch_roles.await_count == 1
        outer.fetch_roles.return_value = [discord.Role(guild=guild, state=state, data={'id': '123', 'name': '@everyone', 'permissions': '0'})]
        # default role must also reflect refreshed roles (real Guild property does this).
        outer.default_role = outer.fetch_roles.return_value[0]
        result = await capabilities(fri, '777')
        assert not result.get('777', {}).get('one_off_post')
    asyncio.run(scenario())


def test_read_member_history_cannot_be_saved(monkeypatch):
    async def scenario():
        event = Mock(spec=discord.Message)
        event.content, event.author.id, event.webhook_id = 'save this', 10, None
        event.author.display_name, event.id, event.channel.id, event.guild = 'HumanMint', int(source().id), 999, None
        event.channel.fetch_message = AsyncMock(return_value=event)
        actions = SimpleNamespace(event=event, user=10, owner=False, fri=SimpleNamespace(api=AsyncMock(return_value={
            'revision': 0, 'personal_memory': {'enabled': True}})), revision=0, scope=SCOPE, private_evidence=True)
        result = await execute(actions, {'kind': 'work_save'})
        assert 'blocked' in result
        assert actions.fri.api.await_count == 1
    asyncio.run(scenario())


def test_bounded_runner_can_recover_and_prepare_work_without_sending():
    async def scenario():
        kinds = ['work_list', 'work_read', 'channel_capabilities', 'prepare_post']
        outputs = [ChatResult(action={'kind': kind}, status='completed') for kind in kinds]
        fri = SimpleNamespace(chat_once=AsyncMock(side_effect=outputs[1:]))
        actions = SimpleNamespace(execute=AsyncMock(side_effect=[{'items': []}, {'body': 'hello'}, {'channels': []}, {'post_preview': {'id': 'p'}}]))
        result, receipts, _ = await run(fri, actions, 'send my saved intro', {'actions': dict.fromkeys(kinds, {})}, outputs[0])
        assert result.post_preview == {'id': 'p'}
        assert len(receipts) == 4 and actions.execute.await_count == 4
        assert fri.chat_once.await_args.args[1]['action_results'] == receipts[:3]
    asyncio.run(scenario())


def test_post_button_never_accepts_member_or_public_interaction():
    interaction = SimpleNamespace(guild_id=None, user=SimpleNamespace(id=10),
        message=SimpleNamespace(author=SimpleNamespace(id=50)), data={'custom_id': 'fri:post:'+'a'*32+':send'})
    assert post_component(interaction, 50, 10)
    interaction.user.id = 11
    assert not post_component(interaction, 50, 10)
    interaction.user.id, interaction.guild_id = 10, 123
    assert not post_component(interaction, 50, 10)


@pytest.mark.parametrize('response', [500, 502, 504, 429, 307, 'reset'])
def test_single_attempt_transport_never_retries_ambiguous_failures(response):
    async def scenario():
        requests = []
        def handler(request):
            requests.append(request)
            if response == 'reset':
                raise httpx.ReadError('accepted, then connection reset')
            return httpx.Response(response, headers={'Retry-After': '600', 'Location': 'https://example.org'},
                json={'retry_after': 600, 'id': '987'})
        fri = SimpleNamespace(http=SimpleNamespace(token='fake-token'))
        post = {'target_id': '777', 'body': 'hello', 'id': 'a'*32}
        with pytest.raises(httpx.HTTPError):
            await send_once(fri, post, transport=httpx.MockTransport(handler))
        assert len(requests) == 1
        assert str(requests[0].url) == 'https://discord.com/api/v10/channels/777/messages'
    asyncio.run(scenario())


def test_public_saved_work_does_not_launder_ambient_context_or_title(store):
    work = Workspace(store)
    scope = {**SCOPE, 'user_id': '20', 'conversation_id': 'public:456'}
    event = source(user='20', channel='456', guild='123')
    with pytest.raises(ValueError, match='exact text'):
        work.save(scope, event, 0, '', 0, 'Someone else private topic', 'member 30 shared this', 'open', 'save what they said')
    assert work.read(scope)['items'] == []
    own = work.save(scope, event, 0, '', 0, 'member 30 shared this', 'my checklist', 'open', 'save my checklist')
    assert own['title'] == 'my checklist'
    done = work.save(scope, source(5,user='20',channel='456',guild='123'), 0, own['id'], 1,
                    'member 30 private detail', 'my checklist', 'completed', 'mark my checklist done')
    assert done['title'] == 'my checklist' and done['state'] == 'completed'
    store.personal_control('30', 'disable')
    assert work.read(scope, own['id'])['body'] == 'my checklist'


def test_save_idempotency_does_not_retain_surrounding_credentials(store):
    work = Workspace(store)
    event = source()
    request = 'save this draft: hello friends\nMy token=synthetic_secret_12345'
    args = (SCOPE, event, 0, '', 0, 'Intro', 'hello friends', 'open', request)
    item = work.save(*args)
    assert work.save(*args)['id'] == item['id']
    with store.connection() as db:
        payload = db.execute('SELECT payload FROM fri_work_events').fetchone()[0]
        assert len(payload) == 64 and all(c in '0123456789abcdef' for c in payload)
        assert 'synthetic_secret_12345' not in '\n'.join(db.iterdump())
