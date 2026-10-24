"""Exercise reply privacy together with current images, reminders and social erasure."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import httpx
import pytest
from gene.fri_social_store import Social

from compat_tests.test_fri_actions import harness
from compat_tests.test_fri_chat import CONTEXT, response_event
from compat_tests.test_fri_social import DM, PUBLIC, api, confirmed, future, source  # noqa: F401
from fri import feedback as commands
from fri.fri_actions import Actions
from fri.fri_chat import Chat, ChatResult
from fri.fri_social import SocialButton, available, show_preview


async def workflow(monkeypatch, tmp_path, connection, *, private=True, reply=True):
    client, call = connection
    fri, agent, _, _ = harness(monkeypatch, tmp_path)
    fri.api = call
    fri._connection.user = SimpleNamespace(id=99, display_name='Fri', bot=True)
    guild = None if private else SimpleNamespace(id=123, me=fri.user)
    channel = Mock(spec=discord.DMChannel, id=999, history=Mock(return_value=AsyncMock())) if private else SimpleNamespace(
        id=456, guild=guild, permissions_for=lambda _: discord.Permissions.all())
    current = Mock(spec=discord.Message, id=int(source(20, public=not private).id),
        content='fri remind me about that' if private else 'fri help us choose times for that game',
        author=SimpleNamespace(id=10, display_name='Owner', bot=False), guild=guild, channel=channel,
        webhook_id=None, edited_at=None, attachments=[], embeds=[], reference=None,
        reply=AsyncMock(return_value=SimpleNamespace(id=888)))
    target = Mock(spec=discord.Message, id=current.id-(1000 << 22), content='Paddington game tomorrow at seven',
        author=fri.user if private else SimpleNamespace(id=30, display_name='Other member', bot=False),
        guild=guild, channel=channel, webhook_id=None)
    if reply:
        current.reference = SimpleNamespace(message_id=target.id, channel_id=channel.id)
    channel.fetch_message = AsyncMock(side_effect=lambda mid: target if mid == target.id else current)
    return fri, agent, current, client, target


@pytest.mark.parametrize('private', [False, True])
def test_reply_target_cannot_offer_or_create_persistent_reminder_or_plan(api, monkeypatch, tmp_path, private):  # noqa: F811
    async def scenario():
        fri, _, event, client, _ = await workflow(monkeypatch, tmp_path, api, private=private)
        kind = 'reminder_prepare' if private else 'plan_prepare'
        action = ({'kind': kind, 'body': 'Paddington game', 'local_time': future(), 'timezone': 'UTC'} if private else
                  {'kind': kind, 'title': 'Paddington', 'local_times': future()+'\n'+future(2), 'timezone': 'UTC'})
        captured = []
        async def respond(text, context):
            captured.append(context)
            return ChatResult(action=action, status='completed')
        fri.chat_once = AsyncMock(side_effect=respond)
        try:
            with pytest.raises(commands.UserFeedbackCheckFailure, match='not available'):
                await fri.talk(123, event.channel.id, 10, event.content, 'private' if private else 'public', event=event)
            # In the DM case the target is an unrecorded Fri reply: withheld from the model, fail closed.
            from fri.fri import UNAVAILABLE
            target = captured[0]['reply_target'][0]['text']
            assert target == UNAVAILABLE if private else target.startswith('Paddington')
            assert kind not in captured[0]['actions']
            with client.app.state.store.connection() as db:
                assert not db.execute('SELECT 1 FROM fri_reminders').fetchone()
                assert not db.execute('SELECT 1 FROM fri_plans').fetchone()
                assert 'Paddington' not in '\n'.join(db.iterdump())
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_explicit_owned_reminder_cancellation_survives_incidental_reply_context(api, monkeypatch, tmp_path):  # noqa: F811
    async def scenario():
        fri, agent, event, client, _ = await workflow(monkeypatch, tmp_path, api)
        social = Social(client.app.state.store)
        reminder = confirmed(social, 1)
        event.content = 'fri cancel my reminder to update the mods'
        fri.chat_once = AsyncMock(side_effect=[
            ChatResult(action={'kind': 'reminder_cancel', 'reminder_id': reminder['id']}, status='completed'),
            ChatResult(reply='cancelled it', status='completed')])
        try:
            result = await fri.talk(123, 999, 10, event.content, 'private', event=event)
            assert result.reply == 'cancelled it'
            offered = fri.chat_once.await_args_list[0].args[1]['actions']
            assert 'reminder_cancel' in offered and 'reminder_prepare' not in offered
            assert social.reminder_list(DM)['items'][0]['state'] == 'cancelled'
            assert social.reminder_list(DM)['items'][0]['body'] == ''
            with client.app.state.store.connection() as db:
                assert 'Paddington' not in '\n'.join(db.iterdump())
            actions = Actions(agent, 10, 999, False)
            actions.event, actions.private_evidence = event, True
            actions.message = 'fri what do you think of that?'
            event.content = actions.message
            assert set(available(actions)) == {'reminder_list'}
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_explicit_image_and_reminder_workflow_keeps_source_checks_preview_and_confirmation(api, monkeypatch, tmp_path):  # noqa: F811
    async def scenario():
        fri, _, event, client, _ = await workflow(monkeypatch, tmp_path, api, reply=False)
        event.content = 'fri what does this image show? also remind me tomorrow at noon UTC to update the mods'
        event.attachments = [SimpleNamespace(url='https://cdn.discordapp.com/attachments/999/1/test.png',
            content_type='image/png', size=100, width=100, height=100, is_spoiler=lambda: False)]
        loader = AsyncMock(return_value='data:image/png;base64,aGVsbG8=')
        monkeypatch.setattr('fri.fri_images.load_image', loader)
        fri.chat_once = AsyncMock(return_value=ChatResult(status='completed', action={
            'kind': 'reminder_prepare', 'body': 'update the mods', 'local_time': future(), 'timezone': 'UTC'}))
        try:
            result = await fri.talk(123, 999, 10, event.content, 'private', event=event)
            context = fri.chat_once.await_args.args[1]
            assert context['images'] == ['data:image/png;base64,aGVsbG8='] and context['reply_target'] == []
            assert 'reminder_prepare' in context['actions'] and result.social_preview['kind'] == 'reminder'
            assert loader.await_count == 1
            social = Social(client.app.state.store)
            row = social.reminder_list(DM)['items'][0]
            assert row['state'] == 'pending' and social.reminder_claim() is None
            await show_preview(fri, event, result.social_preview)
            assert event.reply.await_args.kwargs['allowed_mentions'].everyone is False
            assert event.reply.await_args.kwargs['embed'].description == 'update the mods'
            button = SocialButton('reminder', row['id'], 'confirm')
            interaction = Mock(spec=discord.Interaction, client=fri, id=int(source(21, interaction=True).id),
                user=SimpleNamespace(id=10, bot=False, display_name='Owner'), guild_id=None, channel_id=999, channel=event.channel,
                message=SimpleNamespace(id=888, author=fri.user), data={'custom_id': button.item.custom_id},
                response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock(),
                followup=SimpleNamespace(send=AsyncMock()))
            await button.callback(interaction)
            assert social.reminder_list(DM)['items'][0]['state'] == 'scheduled'
            interaction.followup.send.assert_not_awaited()
            with client.app.state.store.connection() as db:
                assert 'data:image' not in '\n'.join(db.iterdump())
                assert 'cdn.discordapp.com' not in '\n'.join(db.iterdump())
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('changed', ['target', 'image'])
def test_reply_and_image_guards_both_reject_changes_before_saved_answer(api, monkeypatch, tmp_path, changed):  # noqa: F811
    async def scenario():
        fri, _, event, client, target = await workflow(monkeypatch, tmp_path, api, private=False)
        event.content = 'fri what does this image show compared with that suggestion?'
        event.attachments = [SimpleNamespace(url='https://cdn.discordapp.com/attachments/456/1/test.png',
            content_type='image/png', size=100, width=100, height=100, is_spoiler=lambda: False)]
        monkeypatch.setattr('fri.fri_images.load_image', AsyncMock(return_value='data:image/png;base64,aGVsbG8='))
        async def respond(text, context):
            assert context['reply_target'] and context['images']
            if changed == 'target':
                target.content = 'changed suggestion'
            else:
                event.attachments = []
            return ChatResult(reply='a stale answer about Paddington', status='completed')
        fri.chat_once = AsyncMock(side_effect=respond)
        try:
            with pytest.raises(commands.UserFeedbackCheckFailure, match='conversation changed' if changed == 'target' else 'image'):
                await fri.talk(123, 456, 10, event.content, 'public', event=event)
            assert client.app.state.store.chat_context('123', 'public:456', '10')['turns'] == []
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_text_provenance_and_images_coexist_in_the_same_billed_model_request():
    async def scenario():
        target = [{'source': source(public=True, user='30').model_dump(), 'text': 'a referenced suggestion'}]
        burst = [{'source': source(public=True).model_dump(), 'text': 'fri what does this image show?'}]
        seen = []
        def respond(request):
            payload = json.loads(request.content)
            seen.append(payload)
            content = payload['input'][-1]['content']
            supplied = json.loads(content[0]['text'])
            assert supplied['reply_target'] == target and supplied['message_burst'] == burst
            assert content[1] == {'type': 'input_image', 'image_url': 'data:image/png;base64,aGVsbG8=', 'detail': 'auto'}
            return httpx.Response(200, text='data: '+json.dumps(response_event(content=json.dumps({
                'reply': 'it shows an error', 'request_draft': None})))+'\n\n')
        chat = Chat(SimpleNamespace(access_token=lambda: 'fictional'), httpx.MockTransport(respond))
        result = await chat.respond('fri what does this image show?', {**CONTEXT,
            'reply_target': target, 'message_burst': burst, 'images': ['data:image/png;base64,aGVsbG8=']})
        assert result.status == 'completed' and result.counters['input_tokens'] == 100 and len(seen) == 1
    asyncio.run(scenario())


def test_forget_erases_personal_notes_and_social_rows_without_removing_other_members_work(store):
    social = Social(store)
    store.save_chat('123', 'private:999', '10', 0, 'I like Minecraft', 'noted', 'model', 'high',
                    notes=[{'category': 'games', 'evidence': 'I like Minecraft'}])
    social.reminder_prepare(DM, source(1), 0, 'update mods', future(), 'UTC')
    social.plan_prepare(PUBLIC, source(2, public=True), 0, 'Helldivers', future()+'\n'+future(2),
                        'UTC', 'help us choose times for Helldivers')
    other = {**DM, 'user_id': '11', 'conversation_id': 'private:1000'}
    social.reminder_prepare(other, source(3, user='11', channel='1000'), 0, 'keep this reminder', future(), 'UTC')
    store.personal_control('10', 'disable')
    assert store.chat_context('123', 'private:999', '10')['personal_memory'] == {'enabled': False, 'notes': []}
    with store.connection() as db:
        for table in ('fri_reminders', 'fri_plans', 'fri_plan_votes', 'fri_social_events', 'personal_notes', 'personal_note_removals'):
            assert not db.execute('SELECT 1 FROM '+table+' WHERE user_id=?', ('10',)).fetchone()
    assert social.reminder_list(other)['items'][0]['body'] == 'keep this reminder'
