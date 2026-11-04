import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from fri.jpcall.contract import (
    CallBrief,
    CallFinished,
    CallRejected,
    CallState,
    Limits,
    QuestionAsked,
    QuestionClosed,
    StatusChanged,
    UsageLine,
    UsageUpdated,
)
from fri.jpcall.discord_command import AnswerModal, DiscordCalls, register


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('network is forbidden in these tests')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket, 'getaddrinfo', deny)


async def deliver(call, event):
    await call.emit(event)
    await call.flush_events()


class Message:
    def __init__(self, content=None, **kwargs):
        self.content, self.kwargs = content, kwargs
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        self.kwargs.update(kwargs)
        self.content = kwargs.get('content', self.content)
        return self


class Response:
    def __init__(self):
        self.done = False
        self.texts = []
        self.modal = None
        self.deferrals = []

    def is_done(self):
        return self.done

    async def defer(self, **kwargs):
        self.done = True
        self.deferrals.append(kwargs)

    async def send_message(self, text, **kwargs):
        self.done = True
        self.texts.append(text)

    async def edit_message(self, **kwargs):
        self.done = True

    async def send_modal(self, modal):
        self.done, self.modal = True, modal


class Interaction:
    def __init__(self, channel, guild, user_id=10):
        self.guild_id, self.guild = guild.id, guild
        self.channel = channel
        self.user = SimpleNamespace(id=user_id, bot=False,
                                    voice=SimpleNamespace(channel=channel))
        self.response = Response()
        self.followups = []
        self.original = Message()
        self.followup = SimpleNamespace(send=self.followup_send)

    async def edit_original_response(self, **kwargs):
        return await self.original.edit(**kwargs)

    async def followup_send(self, content=None, **kwargs):
        message = Message(content, **kwargs)
        self.followups.append(message)
        return message

    def texts(self):
        return self.response.texts + [m.content or '' for m in self.followups]


class Setup:
    def __init__(self):
        self.guild = SimpleNamespace(id=1, voice_client=None)
        self.channel = MagicMock(spec=discord.VoiceChannel)
        self.channel.id = 2
        self.messages = []
        self.voice = SimpleNamespace(play=MagicMock(), stop=MagicMock(), disconnect=AsyncMock(),
                                     track_speaking=MagicMock(), is_connected=lambda: True,
                                     _connection=SimpleNamespace(dave_protocol_version=0,
                                                                 dave_session=None))

        async def connect(**kwargs):
            self.guild.voice_client = self.voice
            return self.voice

        async def send(content, **kwargs):
            message = Message(content, **kwargs)
            self.messages.append(message)
            return message

        self.channel.connect = AsyncMock(side_effect=connect)
        self.channel.send = AsyncMock(side_effect=send)
        self.session = SimpleNamespace(answer=AsyncMock(return_value=True), hang_up=AsyncMock())
        self.engine = SimpleNamespace(plan=AsyncMock(side_effect=lambda request: CallBrief(
            request.request_id, 'book dinner.', 'AIです。', request.share, request.never)),
            start=AsyncMock(return_value=self.session))
        self.calls = DiscordCalls(None, self.engine, [1], Limits(allowed_user_ids=frozenset({10})))

    def interaction(self, user=10):
        return Interaction(self.channel, self.guild, user)

    async def plan(self):
        interaction = self.interaction()
        await self.calls.invoke(interaction, '+810000000000', 'dinner', 'party of two', 'no fees')
        return self.calls.active[1], interaction

    async def start(self):
        call, interaction = await self.plan()
        await call.brief_view.dial.callback(self.interaction())
        await call.flush_events()
        return call


def test_register_is_guild_only_and_default_deny_all():
    async def run():
        client = discord.Client(intents=discord.Intents.none())
        tree = app_commands.CommandTree(client)
        calls = register(tree, client, object(), [1])
        command = tree.get_command('jpcall', guild=discord.Object(id=1))
        assert command and command.guild_only
        assert command.description == 'ask fri to phone a business'
        assert calls.limits.allowed_user_ids == frozenset()
        assert tree.get_commands() == []
        with pytest.raises(ValueError):
            register(tree, client, object(), [])
        await client.close()
    asyncio.run(run())


def test_authorization_voice_and_one_reservation_per_guild():
    async def run():
        setup = Setup()
        interaction = setup.interaction(11)
        await setup.calls.invoke(interaction, '+81', 'dinner')
        assert 'not available' in interaction.texts()[0]
        setup.engine.plan.assert_not_awaited()
        interaction = setup.interaction()
        interaction.user.voice = None
        await setup.calls.invoke(interaction, '+81', 'dinner')
        assert 'join a voice' in interaction.texts()[0]
        started, release = asyncio.Event(), asyncio.Event()

        async def plan(request):
            started.set()
            await release.wait()
            return CallBrief(request.request_id, 'dinner', 'AIです。', (), ())

        setup.engine.plan.side_effect = plan
        first = asyncio.create_task(setup.calls.invoke(setup.interaction(), '+81', 'dinner'))
        await started.wait()
        second = setup.interaction()
        await setup.calls.invoke(second, '+81', 'dinner')
        assert 'one call' in second.texts()[0]
        assert setup.engine.plan.await_count == 1
        release.set()
        await first
        await setup.calls.close()
    asyncio.run(run())


def test_requester_only_brief_cancel_and_expiry():
    async def run():
        setup = Setup()
        call, interaction = await setup.plan()
        assert call.brief.share == ('party of two',) and call.brief.never == ('no fees',)
        assert not await call.brief_view.interaction_check(setup.interaction(11))
        await call.brief_view.cancel.callback(setup.interaction(11))
        assert 1 in setup.calls.active
        await call.brief_view.cancel.callback(interaction)
        assert not setup.calls.active
        assert all(child.disabled for child in call.brief_view.children)
        setup.engine.start.assert_not_awaited()
        call, _ = await setup.plan()
        await call.brief_view.on_timeout()
        assert not setup.calls.active
    asyncio.run(run())


def test_start_is_send_only_duplicate_clicks_cannot_dial_and_hang_up_cleans():
    async def run():
        setup = Setup()
        call = await setup.start()
        assert call.phase == 'active'
        assert setup.channel.connect.call_args.kwargs['self_deaf'] is True
        assert callable(setup.channel.connect.call_args.kwargs['cls'])
        assert setup.voice.play.call_args.args[0] is call.audio
        assert setup.engine.start.call_args.args == (call.brief, call.request, call.audio, call)
        await call.brief_view.dial.callback(setup.interaction())
        assert setup.engine.start.await_count == 1
        await call.hangup.hang_up.callback(setup.interaction(11))
        assert not setup.session.hang_up.called
        await call.hangup.hang_up.callback(setup.interaction())
        setup.session.hang_up.assert_awaited_once_with('requested')
        setup.voice.disconnect.assert_awaited_once_with(force=True)
        assert call.audio.read() == b'' and not setup.calls.active
        assert all(child.disabled for child in call.hangup.children)
    asyncio.run(run())


def test_requester_moved_and_existing_voice_do_not_dial():
    async def run():
        setup = Setup()
        call, _ = await setup.plan()
        moved = setup.interaction()
        moved.user.voice = None
        await call.brief_view.dial.callback(moved)
        assert call.phase == 'planned'
        setup.guild.voice_client = object()
        await call.brief_view.dial.callback(setup.interaction())
        assert not setup.calls.active
        setup.channel.connect.assert_not_awaited()
        setup.engine.start.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize('stage', ['plan', 'connect', 'play', 'start', 'control_send'])
def test_failures_release_slot_and_allocated_voice(stage):
    async def run():
        setup = Setup()
        if stage == 'plan':
            setup.engine.plan.side_effect = CallRejected('that number is blocked.')
            await setup.calls.invoke(setup.interaction(), '+81', 'dinner')
        else:
            call, _ = await setup.plan()
            if stage == 'connect':
                setup.channel.connect.side_effect = RuntimeError('private details')
            elif stage == 'play':
                setup.voice.play.side_effect = RuntimeError('private details')
            elif stage == 'start':
                setup.engine.start.side_effect = RuntimeError('private details')
            else:
                setup.channel.send.side_effect = discord.HTTPException(
                    SimpleNamespace(status=403, reason='forbidden'), 'private details')
            await call.brief_view.dial.callback(setup.interaction())
            assert call.audio.read() == b''
            if stage != 'connect':
                setup.voice.disconnect.assert_awaited_once_with(force=True)
        assert not setup.calls.active
        assert all('private details' not in (message.content or '') for message in setup.messages)
    asyncio.run(run())


def test_finish_during_start_and_late_price_update_after_disconnect():
    async def run():
        setup = Setup()

        async def start(brief, request, audio, events):
            await events.emit(CallFinished(request.request_id, 'booked.', 60,
                                           (UsageLine('twilio', 'phone', {'minutes': 1}, None),)))
            return setup.session

        setup.engine.start.side_effect = start
        call = await setup.start()
        assert call.phase == 'ended' and not setup.calls.active
        setup.session.hang_up.assert_awaited_once_with('closed_during_start')
        summary = call.cost.message
        assert 'pending' in summary.content
        await deliver(call, UsageUpdated(call.request.request_id,
                                    (UsageLine('twilio', 'phone', {'minutes': 1}, .1, True),), True))
        assert call.cost.message is summary
        assert 'pending' not in summary.content and 'booked.' in summary.content
    asyncio.run(run())


def test_question_membership_modal_first_answer_and_close():
    async def run():
        setup = Setup()
        call = await setup.start()
        await deliver(call, QuestionAsked(call.request.request_id, 'q', 'six?', ('yes', 'no')))
        view = call.questions['q']
        outsider = setup.interaction(11)
        outsider.user.voice = None
        assert not await view.interaction_check(outsider)
        await view.submit(outsider, 'yes')
        setup.session.answer.assert_not_awaited()
        member = setup.interaction(11)  # The group may answer, not just start-call allowlist members.
        await view.free_text(member)
        assert isinstance(member.response.modal, AnswerModal)
        await asyncio.gather(view.submit(setup.interaction(11), 'yes'),
                             view.submit(setup.interaction(12), 'no'))
        setup.session.answer.assert_awaited_once_with('q', 'yes', 11)
        assert view.closed and all(child.disabled for child in view.children)
        await deliver(call, QuestionClosed(call.request.request_id, 'q', 'answered', 'yes'))
        await call.stop('test')
    asyncio.run(run())


def test_membership_rechecked_after_defer_and_terminal_status_closes_question():
    async def run():
        setup = Setup()
        call = await setup.start()
        await deliver(call, QuestionAsked(call.request.request_id, 'q', 'six?'))
        view = call.questions['q']
        member = setup.interaction(11)

        async def defer(**kwargs):
            member.user.voice = None
            member.response.done = True

        member.response.defer = defer
        await view.submit(member, 'yes')
        setup.session.answer.assert_not_awaited()
        await deliver(call, StatusChanged(call.request.request_id, CallState.FAILED, 'busy'))
        assert not setup.calls.active and view.closed
        await deliver(call, CallFinished(call.request.request_id, 'busy', 0))
        assert 'busy' in '\n'.join(m.content or '' for m in setup.messages)
    asyncio.run(run())


def test_event_sink_http_failure_does_not_escape_and_still_disconnects():
    async def run():
        setup = Setup()
        call = await setup.start()
        setup.channel.send.side_effect = discord.HTTPException(
            SimpleNamespace(status=429, reason='rate limit'), 'try later')
        await deliver(call, QuestionAsked(call.request.request_id, 'q', 'six?'))
        await deliver(call, CallFinished(call.request.request_id, 'done.', 10))
        assert not setup.calls.active and call.audio.read() == b''
        setup.voice.disconnect.assert_awaited_once_with(force=True)
    asyncio.run(run())


def test_cancelled_handshake_cleans_captured_voice(monkeypatch):
    async def run():
        setup = Setup()
        call, _ = await setup.plan()
        connected = asyncio.Event()
        monkeypatch.setattr('fri.jpcall.discord_command.CallVoiceClient',
                            lambda client, channel: setup.voice)

        async def connect(**kwargs):
            kwargs['cls'](None, setup.channel)
            connected.set()
            await asyncio.Future()

        setup.channel.connect.side_effect = connect
        operation = asyncio.create_task(call.brief_view.dial.callback(setup.interaction()))
        await connected.wait()
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        setup.voice.disconnect.assert_awaited_once_with(force=True)
        setup.engine.start.assert_not_awaited()
        assert not setup.calls.active
    asyncio.run(run())


def test_shutdown_cancels_inflight_plan_without_resurrecting_brief():
    async def run():
        setup = Setup()
        started = asyncio.Event()

        async def plan(request):
            started.set()
            await asyncio.Future()

        setup.engine.plan.side_effect = plan
        operation = asyncio.create_task(setup.calls.invoke(setup.interaction(), '+81', 'dinner'))
        await started.wait()
        await setup.calls.close()
        assert operation.cancelled()
        assert not setup.calls.active
    asyncio.run(run())


def test_leaving_during_last_discord_write_prevents_engine_start():
    async def run():
        setup = Setup()
        call, _ = await setup.plan()
        interaction = setup.interaction()

        async def send(content, **kwargs):
            interaction.user.voice = None
            return Message(content, **kwargs)

        setup.channel.send.side_effect = send
        await call.brief_view.dial.callback(interaction)
        setup.engine.start.assert_not_awaited()
        setup.voice.disconnect.assert_awaited_once_with(force=True)
        assert not setup.calls.active
    asyncio.run(run())


def test_late_translation_after_finish_updates_original_line():
    from fri.jpcall.contract import Translation, Utterance

    async def run():
        setup = Setup()
        call = await setup.start()
        await deliver(call, Utterance(call.request.request_id, '1', 'callee', 'はい', 0))
        await deliver(call, CallFinished(call.request.request_id, 'done.', 10))
        await deliver(call, Translation(call.request.request_id, '1', 'yes'))
        await call.pager.flush()
        assert 'callee · はい\n↳ yes' in call.pager.pages[0].message.content
        assert not setup.calls.active
    asyncio.run(run())


def test_hangup_starts_while_discord_cleanup_is_waiting():
    async def run():
        setup = Setup()
        call = await setup.start()
        started, release = asyncio.Event(), asyncio.Event()

        async def disconnect(**kwargs):
            started.set()
            await release.wait()

        setup.voice.disconnect.side_effect = disconnect
        closing = asyncio.create_task(call.stop('requested'))
        await started.wait()
        assert setup.session.hang_up.await_count == 1
        release.set()
        await closing
    asyncio.run(run())


def test_new_call_waits_for_phone_hangup_to_finish():
    async def run():
        setup = Setup()
        call = await setup.start()
        hanging_up, release = asyncio.Event(), asyncio.Event()

        async def hang_up(reason):
            hanging_up.set()
            await release.wait()

        setup.session.hang_up.side_effect = hang_up
        closing = asyncio.create_task(call.stop('requested'))
        await hanging_up.wait()
        interaction = setup.interaction()
        await setup.calls.invoke(interaction, '+81', 'another call')
        assert 'one call' in interaction.texts()[0]
        assert setup.engine.plan.await_count == 1
        release.set()
        await closing
        assert not setup.calls.active
    asyncio.run(run())


def test_engine_audio_close_does_not_cancel_terminal_event_path():
    async def run():
        setup = Setup()
        call = await setup.start()
        call.audio.close()
        setup.voice.play.call_args.kwargs['after'](None)
        await asyncio.sleep(0)
        setup.session.hang_up.assert_not_awaited()
        await deliver(call, CallFinished(call.request.request_id, 'done.', 10))
        assert not setup.calls.active
        setup.voice.disconnect.assert_awaited_once_with(force=True)
    asyncio.run(run())


def test_brief_is_public_in_voice_chat_but_errors_stay_private():
    async def run():
        setup = Setup()
        call, interaction = await setup.plan()
        assert call.brief_message in setup.messages
        assert call.brief_message.kwargs['embed'].description.startswith('opener · AIです。')
        assert call.brief_message.kwargs['view'] is call.brief_view
        assert call.brief_message.kwargs['allowed_mentions'].everyone is False
        assert 'ephemeral' not in call.brief_message.kwargs
        assert interaction.original.content == 'brief is in this chat.'
        assert interaction.response.deferrals == [{'ephemeral': True, 'thinking': True}]
        assert not await call.brief_view.interaction_check(setup.interaction(11))
        await setup.calls.close()
        setup.messages.clear()
        setup.engine.plan.side_effect = CallRejected('that number is blocked.')
        interaction = setup.interaction()
        await setup.calls.invoke(interaction, '+81', 'dinner')
        assert not setup.messages
        assert interaction.followups[-1].kwargs['ephemeral'] is True
    asyncio.run(run())


def test_all_controls_and_modals_have_stable_prefixed_ids():
    async def run():
        from fri.jpcall.discord_command import QuestionView

        setup = Setup()
        call = await setup.start()
        event = QuestionAsked(call.request.request_id, 'q' * 200, 'six?', ('yes', 'no'))
        await deliver(call, event)
        view = call.questions[event.question_id]
        duplicate = QuestionView(call, event)
        assert [child.custom_id for child in view.children] == [
            child.custom_id for child in duplicate.children]
        modal = AnswerModal(view)
        other_modal = AnswerModal(duplicate)
        assert modal.custom_id != other_modal.custom_id
        from discord.ui.view import ViewStore
        store = ViewStore(None)
        store.add_view(modal)
        store.add_view(other_modal)
        modal.stop()
        assert store._modals[other_modal.custom_id] is other_modal
        other_modal.stop()
        ids = [child.custom_id for control in (call.brief_view, call.hangup, view, modal)
               for child in control.children] + [modal.custom_id]
        assert all(identifier.startswith('jpcall:') and len(identifier) <= 100 for identifier in ids)
        duplicate.stop()
        modal.stop()
        await call.stop('test')
    asyncio.run(run())


def test_start_validation_rejection_is_private_too():
    async def run():
        setup = Setup()
        call, _ = await setup.plan()
        setup.engine.start.side_effect = CallRejected('daily limit reached.')
        interaction = setup.interaction()
        await call.brief_view.dial.callback(interaction)
        assert interaction.followups[-1].content == 'daily limit reached.'
        assert interaction.followups[-1].kwargs['ephemeral'] is True
        assert all('daily limit' not in (message.content or '') for message in setup.messages)
        assert not setup.calls.active
        setup.voice.disconnect.assert_awaited_once_with(force=True)
    asyncio.run(run())


def test_mls_readiness_precedes_player_and_engine_start():
    async def run():
        setup = Setup()
        call, _ = await setup.plan()
        state = setup.voice._connection
        state.dave_protocol_version = 1
        state.dave_session = SimpleNamespace(ready=False)
        dialing = asyncio.create_task(call.brief_view.dial.callback(setup.interaction()))
        await asyncio.sleep(0)
        setup.voice.play.assert_not_called()
        setup.engine.start.assert_not_awaited()
        state.dave_session.ready = True
        await dialing
        setup.voice.play.assert_called_once()
        setup.engine.start.assert_awaited_once()
        await call.stop('test')
    asyncio.run(run())


def test_all_messages_use_invoking_text_channel_while_voice_and_answers_stay_in_voice():
    from fri.jpcall.contract import Translation, Utterance

    async def run():
        setup = Setup()
        text = MagicMock(spec=discord.TextChannel)
        text.id = 40
        text.send = AsyncMock(side_effect=setup.channel.send.side_effect)
        invocation = setup.interaction()
        invocation.channel = text
        await setup.calls.invoke(invocation, '+81', 'dinner')
        call = setup.calls.active[1]
        assert call.voice_channel is setup.channel and call.text_channel is text
        await call.brief_view.dial.callback(setup.interaction())
        assert setup.channel.connect.await_count == 1
        await deliver(call, Utterance(call.request.request_id, '1', 'callee', 'はい', 0))
        await deliver(call, Translation(call.request.request_id, '1', 'yes'))
        await deliver(call, QuestionAsked(call.request.request_id, 'q', 'six?', ('yes', 'no')))
        question = call.questions['q']
        outsider = setup.interaction(11)
        outsider.user.voice = None
        await question.submit(outsider, 'yes')
        setup.session.answer.assert_not_awaited()
        await question.submit(setup.interaction(12), 'yes')
        setup.session.answer.assert_awaited_once_with('q', 'yes', 12)
        await deliver(call, CallFinished(call.request.request_id, 'done.', 10,
                                     (UsageLine('twilio', 'phone', {'minutes': 1}, None),)))
        summary = call.cost.message
        await deliver(call, UsageUpdated(call.request.request_id,
                                    (UsageLine('twilio', 'phone', {'minutes': 1}, .1, True),), True))
        assert call.cost.message is summary
        assert 'pending' not in summary.content
        assert call.brief_message in setup.messages and question.message in setup.messages
        assert call.pager.pages[0].message in setup.messages and summary in setup.messages
        assert text.send.await_count >= 4
        setup.channel.send.assert_not_awaited()
    asyncio.run(run())


def test_brief_shows_short_opener_and_request_fields_without_a_script_claim():
    async def run():
        setup = Setup()
        setup.engine.plan.side_effect = lambda request: CallBrief(
            request.request_id, 'i will read the whole request aloud.', '안녕하세요.',
            ('rewritten share',), ('rewritten limits',))
        goal = 'Ask for a table.\nThen ask about parking.'
        await setup.calls.invoke(setup.interaction(), '+81', goal, 'Name: Alex', 'No payment')
        call = setup.calls.active[1]
        text = call.brief_message.kwargs['embed'].description
        assert text == ('opener · 안녕하세요.\n\ngoal · ' + goal + '\n\n'
                        'share · Name: Alex\nnever · No payment')
        await setup.calls.close()
    asyncio.run(run())


def test_engine_deadline_does_not_cancel_accepted_final_send_or_duplicate_late_price():
    async def run():
        setup = Setup()
        call = await setup.start()
        accepted, release = asyncio.Event(), asyncio.Event()
        original = setup.channel.send.side_effect
        reports = []

        async def send(content, **kwargs):
            message = await original(content, **kwargs)
            if content.startswith('call ended'):
                reports.append(message)  # Server has accepted the message; its response is delayed.
                accepted.set()
                await release.wait()
            return message

        setup.channel.send.side_effect = send
        # Same deadline pattern as engine 598e1b8._deliver_event, shortened for the test.
        async with asyncio.timeout(.01):
            await call.emit(CallFinished(call.request.request_id, 'done.', 24,
                                         (UsageLine('twilio', 'phone', {'minutes': 1}, None),)))
        await accepted.wait()
        async with asyncio.timeout(.01):
            await call.emit(UsageUpdated(call.request.request_id,
                (UsageLine('twilio', 'phone', {'minutes': 1}, .05, True),), True))
        assert len(reports) == 1
        release.set()
        await call.flush_events()
        assert len(reports) == 1 and call.cost.message is reports[0]
        assert reports[0].content.endswith('cost total: $0.05')
        assert not setup.calls.active
    asyncio.run(run())


@pytest.mark.parametrize('failure', [RuntimeError, asyncio.CancelledError])
def test_transcript_failure_does_not_suppress_final_or_late_price(monkeypatch, caplog, failure):
    async def run():
        setup = Setup()
        call = await setup.start()
        call.pager.close = AsyncMock(side_effect=failure('synthetic-private-details'))
        await call.emit(CallFinished(call.request.request_id, 'done.', 24))
        await call.emit(UsageUpdated(call.request.request_id,
            (UsageLine('twilio', 'phone', {'minutes': 1}, .05, True),), True))
        await call.flush_events()
        assert call.cost.message.content.endswith('cost total: $0.05')
        reports = [message for message in setup.messages
                   if (message.content or '').startswith('call ended')]
        assert reports == [call.cost.message]
        assert 'CallFinished' in caplog.text and failure.__name__ in caplog.text
        assert 'synthetic-private-details' not in caplog.text
        assert not setup.calls.active
    asyncio.run(run())


def test_malformed_event_is_contained_at_admission(caplog):
    async def run():
        setup = Setup()
        call = await setup.start()
        await call.emit(object())
        assert 'admission failed: object AttributeError' in caplog.text
        await call.stop('test')
    asyncio.run(run())


def test_terminal_status_and_finish_produce_only_one_public_end_report():
    async def run():
        setup = Setup()
        call = await setup.start()
        await deliver(call, StatusChanged(call.request.request_id, CallState.IN_PROGRESS))
        await call.emit(StatusChanged(call.request.request_id, CallState.ENDED))
        await call.emit(CallFinished(call.request.request_id, 'done.', 24))
        await call.emit(UsageUpdated(call.request.request_id,
            (UsageLine('twilio', 'phone', {'minutes': 1}, .05, True),), True))
        await setup.calls.close()
        reports = [message for message in setup.messages
                   if (message.content or '').startswith('call ended')]
        assert reports == [call.cost.message]
        assert not any('call · ended' in (message.content or '') for message in setup.messages)
        assert reports[0].content.endswith('cost total: $0.05')
        assert 'minutes' not in reports[0].content and 'phone' not in reports[0].content
    asyncio.run(run())
