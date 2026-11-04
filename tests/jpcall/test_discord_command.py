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

    def is_done(self):
        return self.done

    async def defer(self, **kwargs):
        self.done = True

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
        self.voice = SimpleNamespace(play=MagicMock(), stop=MagicMock(), disconnect=AsyncMock())

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
        return call


def test_register_is_guild_only_and_default_deny_all():
    async def run():
        client = discord.Client(intents=discord.Intents.none())
        tree = app_commands.CommandTree(client)
        calls = register(tree, client, object(), [1])
        command = tree.get_command('jpcall', guild=discord.Object(id=1))
        assert command and command.guild_only
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
        summary = call.cost.messages[0]
        assert 'pending' in summary.content
        await call.emit(UsageUpdated(call.request.request_id,
                                    (UsageLine('twilio', 'phone', {'minutes': 1}, .1, True),), True))
        assert call.cost.messages[0] is summary
        assert 'pending' not in summary.content and 'booked.' in summary.content
    asyncio.run(run())


def test_question_membership_modal_first_answer_and_close():
    async def run():
        setup = Setup()
        call = await setup.start()
        await call.emit(QuestionAsked(call.request.request_id, 'q', 'six?', ('yes', 'no')))
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
        await call.emit(QuestionClosed(call.request.request_id, 'q', 'answered', 'yes'))
        await call.stop('test')
    asyncio.run(run())


def test_membership_rechecked_after_defer_and_terminal_status_closes_question():
    async def run():
        setup = Setup()
        call = await setup.start()
        await call.emit(QuestionAsked(call.request.request_id, 'q', 'six?'))
        view = call.questions['q']
        member = setup.interaction(11)

        async def defer(**kwargs):
            member.user.voice = None
            member.response.done = True

        member.response.defer = defer
        await view.submit(member, 'yes')
        setup.session.answer.assert_not_awaited()
        await call.emit(StatusChanged(call.request.request_id, CallState.FAILED, 'busy'))
        assert not setup.calls.active and view.closed
        assert 'busy' in '\n'.join(m.content or '' for m in setup.messages)
    asyncio.run(run())


def test_event_sink_http_failure_does_not_escape_and_still_disconnects():
    async def run():
        setup = Setup()
        call = await setup.start()
        setup.channel.send.side_effect = discord.HTTPException(
            SimpleNamespace(status=429, reason='rate limit'), 'try later')
        await call.emit(QuestionAsked(call.request.request_id, 'q', 'six?'))
        await call.emit(CallFinished(call.request.request_id, 'done.', 10))
        assert not setup.calls.active and call.audio.read() == b''
        setup.voice.disconnect.assert_awaited_once_with(force=True)
    asyncio.run(run())


def test_cancelled_handshake_cleans_captured_voice(monkeypatch):
    async def run():
        setup = Setup()
        call, _ = await setup.plan()
        connected = asyncio.Event()
        monkeypatch.setattr(discord, 'VoiceClient', lambda client, channel: setup.voice)

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
        await call.emit(Utterance(call.request.request_id, '1', 'callee', 'はい', 0))
        await call.emit(CallFinished(call.request.request_id, 'done.', 10))
        await call.emit(Translation(call.request.request_id, '1', 'yes'))
        await call.pager.flush()
        assert 'callee · ja: はい\nen: yes' in call.pager.pages[0].message.content
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
        await call.emit(CallFinished(call.request.request_id, 'done.', 10))
        assert not setup.calls.active
        setup.voice.disconnect.assert_awaited_once_with(force=True)
    asyncio.run(run())
