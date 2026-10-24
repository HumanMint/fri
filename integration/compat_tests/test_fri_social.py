"""Offline complete reminder and group-plan workflows, including failure fences."""

import asyncio
import datetime as dt
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import httpx
import pytest
from fastapi.testclient import TestClient
from gene.api import create_app
from gene.config import Settings
from gene.fri_social_store import Social, local_timestamp
from gene.store import Store

from compat_tests.test_fri_actions import harness
from fri.fri_chat import ChatResult
from fri.fri_context import ChatSource
from fri.fri_errors import ControllerRejected
from fri.fri_social import SocialButton, component, deliver_one, execute, render, show_preview
from fri.fri_workspace import send_once

DM = {'guild_id': '123', 'user_id': '10', 'channel_id': '456', 'conversation_id': 'private:999'}
PUBLIC = {**DM, 'conversation_id': 'public:456'}


def source(offset=0, *, interaction=False, public=False, user='10', channel=None):
    return ChatSource(id=str(((int(time.time()*1000)-1420070400000) << 22) | offset),
        kind='discord_interaction' if interaction else 'discord_message', user_id=user,
        display_name='Test member', channel_id=channel or ('456' if public else '999'),
        guild_id='123' if public else None)


def future(days=1):
    return dt.datetime.fromtimestamp(time.time()+days*86400, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M')


def reminder(social, offset=0):
    return social.reminder_prepare(DM, source(offset), 0, 'update the mods', future(), 'UTC')


def confirmed(social, offset=0):
    row = reminder(social, offset)
    social.bind('reminder', row['id'], DM, '888')
    return social.reminder_decide(row['id'], DM, source(offset+1, interaction=True), 0, 'confirm', '888')


def plan(social, offset=0):
    return social.plan_prepare(PUBLIC, source(offset, public=True), 0, 'Helldivers', future()+'\n'+future(2), 'UTC', 'help us choose times for Helldivers')


def published(social):
    row = plan(social)
    social.bind('plan', row['id'], PUBLIC, '777')
    return social.plan_decide(row['id'], PUBLIC, source(1, interaction=True, public=True), 0, 'publish', '777')


def due(store, item):
    with store.connection() as db:
        db.execute('UPDATE fri_reminders SET due=? WHERE id=?', (time.time()-1, item))


def test_times_reject_dst_gaps_folds_missing_zone_and_implicit_offsets():
    now = dt.datetime(2026, 10, 20, tzinfo=dt.timezone.utc).timestamp()
    with pytest.raises(ValueError, match='ambiguous'):
        local_timestamp('2026-11-01T01:30', 'America/Los_Angeles', now=now)
    with pytest.raises(ValueError, match='does not exist'):
        local_timestamp('2026-03-08T02:30', 'America/Los_Angeles', now=dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc).timestamp())
    assert local_timestamp('2026-11-01T09:30', 'UTC', now=now) == dt.datetime(2026, 11, 1, 9, 30, tzinfo=dt.timezone.utc).timestamp()
    for value, zone in [('2026-10-25T07:00+01:00', 'UTC'), ('2026-10-25T07:00', 'PST'), ('2026-10-25T07:00:01', 'UTC')]:
        with pytest.raises(ValueError):
            local_timestamp(value, zone, now=now)


def test_reminder_requires_bound_exact_member_dm_confirmation_and_survives_restart(store):
    social = Social(store)
    row = reminder(social)
    assert social.reminder_claim() is None and row['state'] == 'pending'
    social.bind('reminder', row['id'], DM, '888')
    for scope, event, preview in [({**DM, 'user_id': '11'}, source(1, interaction=True, user='11'), '888'),
                                  ({**DM, 'conversation_id': 'private:111'}, source(2, interaction=True, channel='111'), '888'),
                                  (DM, source(3), '888'), (DM, source(4, interaction=True), '889')]:
        with pytest.raises((ValueError, KeyError)):
            social.reminder_decide(row['id'], scope, event, 0, 'confirm', preview)
    result = social.reminder_decide(row['id'], DM, source(5, interaction=True), 0, 'confirm', '888')
    assert result['state'] == 'scheduled'
    social = Social(Store(store.path))
    assert social.reminder_list(DM)['items'][0]['state'] == 'scheduled'
    assert social.reminder_list({**DM, 'user_id': '11'})['items'] == []
    due(store, row['id'])
    claim = social.reminder_claim()
    assert claim['id'] == row['id'] and social.reminder_claim() is None
    social.reminder_sending(row['id'], claim['lease'])
    with pytest.raises(ValueError):
        social.reminder_sending(row['id'], claim['lease'])
    with store.connection() as db:
        db.execute('UPDATE fri_reminders SET lease_until=0')
    assert Social(Store(store.path)).reminder_claim() is None
    assert social.reminder_list(DM)['items'][0]['state'] == 'uncertain'
    social.reminder_finish(row['id'], claim['lease'], 'sent', '1234')
    assert social.reminder_list(DM)['items'][0]['state'] == 'sent'


def test_reminder_cancellation_expiry_quota_and_erasure(store):
    social = Social(store)
    first = confirmed(social)
    due(store, first['id'])
    claim = social.reminder_claim()
    assert claim
    cancelled = social.reminder_decide(first['id'], DM, source(3), 0, 'cancel')
    assert cancelled['state'] == 'cancelled' and not cancelled['body']
    with pytest.raises(ValueError):
        social.reminder_sending(first['id'], claim['lease'])
    for offset in range(4, 9):
        reminder(social, offset)
    with pytest.raises(ValueError, match='five'):
        reminder(social, 9)
    with store.connection() as db:
        db.execute("UPDATE fri_reminders SET created=0 WHERE state='pending'")
    assert {r['state'] for r in social.reminder_list(DM)['items']} == {'expired', 'cancelled'}
    fresh = confirmed(social, 20)
    due(store, fresh['id'])
    social.reminder_claim()
    store.personal_control('10', 'disable')
    assert social.reminder_claim() is None and not social.reminder_list(DM)['items']
    with pytest.raises(ValueError, match='Memory'):
        reminder(social, 30)


def test_group_plan_each_member_votes_once_organizer_finalizes_then_explicit_rsvp(store):
    social = Social(store)
    row = published(social)
    member = {**PUBLIC, 'user_id': '11'}
    event = source(2, public=True, interaction=True, user='11')
    vote = social.plan_decide(row['id'], member, event, 0, 'vote0', '777')
    assert vote['available'] == [1, 0]
    assert social.plan_decide(row['id'], member, event, 0, 'vote0', '777')['available'] == [1, 0]
    with pytest.raises(ValueError, match='organizer'):
        social.plan_decide(row['id'], member, source(3, public=True, interaction=True, user='11'), 0, 'final0', '777')
    finalized = social.plan_decide(row['id'], PUBLIC, source(4, public=True, interaction=True), 0, 'final0', '777')
    assert finalized['state'] == 'finalized' and finalized['selected'] == 0
    assert finalized['rsvps'] == {'going': 0, 'maybe': 0, 'cannot': 0}
    result = social.plan_decide(row['id'], member, source(5, public=True, interaction=True, user='11'), 0, 'going', '777')
    assert result['rsvps']['going'] == 1
    result = social.plan_decide(row['id'], member, source(6, public=True, interaction=True, user='11'), 0, 'maybe', '777')
    assert result['rsvps'] == {'going': 0, 'maybe': 1, 'cannot': 0}
    social = Social(Store(store.path))
    with store.connection() as db:
        assert social.plan_row(db, row['id'], PUBLIC)['rsvps']['maybe'] == 1
    store.personal_control('11', 'disable')
    with store.connection() as db:
        assert social.plan_row(db, row['id'], PUBLIC)['available'] == [0, 0]
    store.personal_control('10', 'disable')
    with store.connection() as db, pytest.raises(KeyError):
        social.plan_row(db, row['id'], PUBLIC)


@pytest.mark.parametrize('failure', ['other_author', 'other_channel', 'other_guild', 'other_card', 'old', 'message'])
def test_plan_interactions_are_bound_to_event_channel_card_and_identity(store, failure):
    social = Social(store)
    row = published(social)
    scope, event, preview = PUBLIC.copy(), source(3, public=True, interaction=True), '777'
    if failure == 'other_author':
        scope['user_id'] = '11'
    elif failure == 'other_channel':
        scope['conversation_id'] = 'public:1234'
        event = event.model_copy(update={'channel_id': '1234'})
    elif failure == 'other_guild':
        scope['guild_id'] = '124'
        event = event.model_copy(update={'guild_id': '124'})
    elif failure == 'other_card':
        preview = '778'
    elif failure == 'old':
        event = event.model_copy(update={'id': '100000000000000001'})
    else:
        event = event.model_copy(update={'kind': 'discord_message'})
    with pytest.raises((ValueError, KeyError)):
        social.plan_decide(row['id'], scope, event, 0, 'vote0', preview)


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


def test_api_service_and_scope_boundaries(api):
    client, _ = api
    body = {**DM, 'source': source().model_dump(), 'revision': 0, 'body': 'update mods', 'local_time': future(), 'timezone': 'UTC'}
    for role in ('runtime', 'worker', 'deploy'):
        assert client.post('/social/reminders/prepare', json=body, headers={'Authorization': 'Bearer '+role}).status_code == 401
    for edits in ({'guild_id': '124'}, {'conversation_id': 'public:999'}, {'channel_id': '1234'}):
        assert client.post('/social/reminders/prepare', json={**body, **edits}, headers={'Authorization': 'Bearer fri'}).status_code == 403
    assert client.post('/social/reminders/prepare', json=body, headers={'Authorization': 'Bearer fri'}).status_code == 200


def runtime(api, tmp_path):
    client, call = api
    social = Social(client.app.state.store)
    row = confirmed(social)
    due(client.app.state.store, row['id'])
    preview = SimpleNamespace(author=SimpleNamespace(id=50), edit=AsyncMock())
    dm = Mock(spec=discord.DMChannel, recipient=SimpleNamespace(id=10), fetch_message=AsyncMock(return_value=preview))
    fri = SimpleNamespace(api=call, data=tmp_path, guild_id=123, user=SimpleNamespace(id=50),
        agent=SimpleNamespace(config={'enabled': True, 'support_dms': True}, member=AsyncMock(return_value=SimpleNamespace(id=10))),
        fetch_channel=AsyncMock(return_value=dm))
    return fri, social, row


@pytest.mark.parametrize('failure', ['', 'timeout', 'departed', 'wrong_dm', 'receipt', 'erase_receipt'])
def test_delivery_attempts_once_restarts_and_does_not_resurrect_erased_reminders(api, tmp_path, monkeypatch, failure):
    async def scenario():
        fri, social, row = runtime(api, tmp_path)
        sender = AsyncMock(return_value={'id': '987'})
        monkeypatch.setattr('fri.fri_social.send_once', sender)
        if failure == 'timeout':
            sender.side_effect = TimeoutError()
        if failure == 'departed':
            fri.agent.member.return_value = None
        if failure == 'wrong_dm':
            fri.fetch_channel.return_value.recipient.id = 11
        real_api = fri.api
        async def flaky(method, path, body=None):
            if failure in ('receipt', 'erase_receipt') and path.endswith('/finish'):
                raise OSError('receipt unavailable')
            return await real_api(method, path, body)
        fri.api = flaky
        if failure in ('receipt', 'erase_receipt'):
            with pytest.raises(OSError):
                await deliver_one(fri)
            files = list((tmp_path/'reminder-receipts').glob('*.json'))
            assert len(files) == 1 and 'update the mods' not in files[0].read_text()
            if failure == 'erase_receipt':
                social.store.personal_control('10', 'disable')
        else:
            await deliver_one(fri)
        fri.api = real_api
        await deliver_one(fri)
        assert sender.await_count == (0 if failure in ('departed', 'wrong_dm') else 1)
        items = social.reminder_list(DM)['items']
        if failure == 'erase_receipt':
            assert items == []
        else:
            assert items[0]['state'] == ('failed' if failure in ('departed', 'wrong_dm') else 'uncertain' if failure == 'timeout' else 'sent')
        assert not list((tmp_path/'reminder-receipts').glob('*.json'))
        if sender.await_count:
            payload = sender.await_args.args[1]
            assert payload['target_id'] == '999' and payload['body'].startswith('Reminder\nScheduled for')
            assert sender.await_args.kwargs['embed']['description'] == 'update the mods'
    asyncio.run(scenario())


def test_tools_prepare_preview_then_explicit_bound_button_schedules_without_model(api, tmp_path):
    async def scenario():
        client, call = api
        event = source()
        message = Mock(spec=discord.Message, id=int(event.id), content='remind me tomorrow to update the mods',
            author=SimpleNamespace(id=10, display_name='Member'), guild=None, webhook_id=None)
        message.channel = Mock(spec=discord.DMChannel, id=999, fetch_message=AsyncMock(return_value=message))
        message.reply = AsyncMock(return_value=SimpleNamespace(id=888))
        agent = SimpleNamespace(config={'enabled': True, 'support_dms': True}, member=AsyncMock(return_value=SimpleNamespace(id=10)))
        fri = SimpleNamespace(api=call, data=tmp_path, agent=agent, guild_id=123, workshop_id=456, user=SimpleNamespace(id=50))
        actions = SimpleNamespace(event=message, public=False, guild=123, user=10, agent=agent, fri=fri,
            scope=DM, revision=0, private_evidence=False, message=message.content)
        action = {'kind': 'reminder_prepare', 'body': 'update the mods', 'local_time': future(), 'timezone': 'UTC'}
        result = await execute(actions, action)
        await show_preview(fri, message, result['social_preview'])
        social = Social(client.app.state.store)
        row = social.reminder_list(DM)['items'][0]
        assert row['state'] == 'pending' and social.reminder_claim() is None
        button = SocialButton('reminder', row['id'], 'confirm')
        interaction = Mock(spec=discord.Interaction, client=fri, id=int(source(2, interaction=True).id),
            user=SimpleNamespace(id=10, bot=False, display_name='Member'), guild_id=None, channel_id=999,
            channel=message.channel, message=SimpleNamespace(id=888, author=fri.user),
            data={'custom_id': button.item.custom_id}, response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(), followup=SimpleNamespace(send=AsyncMock()))
        await button.callback(interaction)
        assert social.reminder_list(DM)['items'][0]['state'] == 'scheduled'
        assert interaction.edit_original_response.await_count == 1
        assert not interaction.followup.send.await_count
        assert component(interaction, 50)
        interaction.message.author.id = 51
        assert not component(interaction, 50)
        actions.private_evidence = True
        assert 'blocked' in await execute(actions, action)
    asyncio.run(scenario())


def test_cards_use_bounded_persistent_buttons_and_disabled_pings(store):
    social = Social(store)
    row = published(social)
    row['title'] = '@everyone **game**'
    text, view = render('plan', row)
    assert '@\u200beveryone' in text and len(view.children) == 5
    for child in view.children:
        assert child.is_persistent()
    assert all(len(child.item.custom_id) < 100 for child in view.children)
    with store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM fri_plan_votes').fetchone()[0] == 0


@pytest.mark.parametrize('failure', ['', 'edit', 'delete', 'expired', 'privacy', 'user', 'channel', 'cancel'])
def test_reminder_short_clarification_through_real_fri_talk(api, tmp_path, monkeypatch, failure):
    async def scenario():
        client, call = api
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        fri.api = call
        event = source()
        original = Mock(spec=discord.Message, id=int(event.id), content='remind me tomorrow at 7 to update the mods',
            author=SimpleNamespace(id=10, display_name='Member'), guild=None, webhook_id=None,
            attachments=[], embeds=[], edited_at=None)
        dm = Mock(spec=discord.DMChannel, id=999, history=Mock(return_value=AsyncMock()))
        original.channel = dm
        answer = Mock(spec=discord.Message, id=int(source(3).id), content='7 pm',
            author=original.author, guild=None, webhook_id=None, channel=dm, attachments=[], embeds=[], edited_at=None)
        if failure == 'expired':
            original.id -= 301000 << 22
        dm.fetch_message = AsyncMock(side_effect=lambda mid: original if mid == original.id else answer)
        action = {'kind': 'reminder_prepare', 'body': 'update the mods', 'local_time': future(), 'timezone': 'UTC'}
        fri.chat_once = AsyncMock(side_effect=[ChatResult(reply='7 am or 7 pm?', status='completed'),
            ChatResult(action=action, status='completed'), ChatResult(reply='Ask again with the reminder details.', status='completed')])
        try:
            first = await fri.talk(123, 999, 10, original.content, 'private', event=original)
            await fri.record_turn(first)  # The delivered question arms the clarification.
            assert first.reply == '7 am or 7 pm?'
            if failure == 'edit':
                original.content = 'changed request'
            elif failure == 'delete':
                async def fetch(mid):
                    if mid == original.id:
                        raise discord.NotFound(SimpleNamespace(status=404, reason='deleted'), 'deleted')
                    return answer
                dm.fetch_message.side_effect = fetch
            elif failure == 'privacy':
                client.app.state.store.personal_control('10', 'disable')
            elif failure == 'user':
                answer.author = SimpleNamespace(id=11, display_name='Other member')
            elif failure == 'channel':
                dm = Mock(spec=discord.DMChannel, id=1000, fetch_message=AsyncMock(return_value=answer),
                          history=Mock(return_value=AsyncMock()))
                answer.channel = dm
            elif failure == 'cancel':
                answer.content = 'cancel'
            result = await fri.talk(123, dm.id, answer.author.id, answer.content, 'private', event=answer)
            rows = Social(client.app.state.store).reminder_list(DM)['items']
            if not failure:
                assert result.social_preview['kind'] == 'reminder'
                assert rows[0]['state'] == 'pending'
                assert Social(client.app.state.store).reminder_claim() is None
            else:
                assert result.social_preview is None and rows == []
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_game_night_tool_publish_availability_and_rsvp_buttons_use_real_controller(api, tmp_path):
    async def scenario():
        client, call = api
        guild = SimpleNamespace(id=123, me=object())
        channel = Mock(spec=discord.TextChannel, id=456, parent_id=None, guild=guild,
            permissions_for=lambda _: SimpleNamespace(view_channel=True, read_message_history=True, send_messages=True))
        message = Mock(spec=discord.Message, id=int(source(public=True).id),
            content='help us choose times for Helldivers', author=SimpleNamespace(id=10, display_name='Organizer'),
            guild=guild, channel=channel, webhook_id=None)
        channel.fetch_message = AsyncMock(return_value=message)
        message.reply = AsyncMock(return_value=SimpleNamespace(id=777))
        agent = SimpleNamespace(config={'enabled': True, 'support_dms': True}, allowed=lambda _: True,
            member=AsyncMock(return_value=SimpleNamespace(id=10)))
        fri = SimpleNamespace(api=call, data=tmp_path, guild_id=123, workshop_id=456, agent=agent, user=SimpleNamespace(id=50))
        actions = SimpleNamespace(event=message, public=True, guild=123, user=10, agent=agent, fri=fri,
            scope=PUBLIC, revision=0, private_evidence=False, message=message.content)
        result = await execute(actions, {'kind': 'plan_prepare', 'title': 'Helldivers',
            'local_times': future()+'\n'+future(2), 'timezone': 'UTC'})
        await show_preview(fri, message, result['social_preview'])
        item = result['social_preview']['item']['id']
        async def click(decision, user, offset):
            button = SocialButton('plan', item, decision)
            interaction = Mock(spec=discord.Interaction, client=fri, id=int(source(offset, interaction=True, public=True).id),
                user=SimpleNamespace(id=user, bot=False, display_name='Member'), guild_id=123, guild=guild,
                channel_id=456, channel=channel, message=SimpleNamespace(id=777, author=fri.user),
                data={'custom_id': button.item.custom_id}, response=SimpleNamespace(defer=AsyncMock()),
                edit_original_response=AsyncMock(), followup=SimpleNamespace(send=AsyncMock()))
            await button.callback(interaction)
            return interaction
        assert (await click('publish', 11, 1)).edit_original_response.await_count == 0
        assert (await click('publish', 10, 2)).edit_original_response.await_count == 1
        assert (await click('vote1', 11, 3)).edit_original_response.await_count == 1
        assert (await click('final1', 10, 4)).edit_original_response.await_count == 1
        assert (await click('going', 11, 5)).edit_original_response.await_count == 1
        with client.app.state.store.connection() as db:
            row = Social(client.app.state.store).plan_row(db, item, PUBLIC)
        assert row['state'] == 'finalized' and row['selected'] == 1 and row['rsvps']['going'] == 1
        assert message.reply.await_count == 1
        assert message.reply.await_args.kwargs['allowed_mentions'].everyone is False
        assert (await click('cancel', 10, 6)).edit_original_response.await_count == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('body', ['*'*1000, '😀'*1000, '`'*1000], ids=['markdown', 'utf16', 'backticks'])
def test_full_reminder_body_fits_confirm_preview_and_single_attempt_delivery(api, tmp_path, monkeypatch, body):
    async def scenario():
        client, call = api
        social = Social(client.app.state.store)
        row = social.reminder_prepare(DM, source(), 0, body, future(), 'UTC')
        expected = discord.utils.escape_markdown(discord.utils.escape_mentions(body))
        def valid_layout(content, embed):
            assert len((content or '').encode('utf-16-le'))//2 <= 2000
            assert embed and embed.description == expected
            assert len(embed.description.encode('utf-16-le'))//2 <= 4096
        async def reply(content=None, **kwargs):
            valid_layout(content, kwargs.get('embed'))
            assert not kwargs.get('suppress_embeds', False)
            assert kwargs['view'].children[0].item.label == 'Confirm'
            return SimpleNamespace(id=888)
        posted = SimpleNamespace(author=SimpleNamespace(id=50), edit=AsyncMock())
        dm = Mock(spec=discord.DMChannel, id=999, recipient=SimpleNamespace(id=10), fetch_message=AsyncMock(return_value=posted))
        event = Mock(spec=discord.Message, author=SimpleNamespace(id=10), guild=None, channel=dm, reply=AsyncMock(side_effect=reply))
        fri = SimpleNamespace(api=call, data=tmp_path, guild_id=123, workshop_id=456, user=SimpleNamespace(id=50),
            agent=SimpleNamespace(config={'enabled': True, 'support_dms': True}, member=AsyncMock(return_value=SimpleNamespace(id=10))),
            fetch_channel=AsyncMock(return_value=dm), http=SimpleNamespace(token='synthetic-test-token'))
        await show_preview(fri, event, {'kind': 'reminder', 'item': row})
        social.reminder_decide(row['id'], DM, source(2, interaction=True), 0, 'confirm', '888')
        due(client.app.state.store, row['id'])
        requests = []
        def transport(request):
            requests.append(request)
            payload = json.loads(request.content)
            valid_layout(payload['content'], discord.Embed.from_dict(payload['embeds'][0]))
            assert not payload.get('flags', 0) & 4
            assert payload['allowed_mentions'] == {'parse': []}
            return httpx.Response(200, json={'id': '987'})
        async def sender(f, post, **kwargs):
            return await send_once(f, post, transport=httpx.MockTransport(transport), **kwargs)
        monkeypatch.setattr('fri.fri_social.send_once', sender)
        await deliver_one(fri)
        await deliver_one(fri)
        assert len(requests) == 1
        assert social.reminder_list(DM)['items'][0]['state'] == 'sent'
        valid_layout(posted.edit.await_args.kwargs['content'], posted.edit.await_args.kwargs['embed'])
    asyncio.run(scenario())
