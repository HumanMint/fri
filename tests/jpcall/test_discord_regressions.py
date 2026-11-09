"""Synthetic regressions for connecting, disclosure, delayed writes and late billing."""

import asyncio
import gc
import time
import weakref
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from test_discord_command import Message, Setup, deliver
from test_discord_transcript import Messages

from fri.jpcall.contract import (
    CallFinished,
    QuestionAsked,
    Translation,
    UsageLine,
    UsageUpdated,
    Utterance,
)
from fri.jpcall.discord_command import DiscordCall
from fri.jpcall.discord_cost import CostSummary
from fri.jpcall.discord_transcript import TranscriptPager


def test_stop_is_visible_during_voice_handshake_and_never_dials():
    async def run():
        setup = Setup()
        call, _ = await setup.plan()
        connecting = asyncio.Event()

        async def connect(**kwargs):
            connecting.set()
            await asyncio.Future()

        setup.channel.connect.side_effect = connect
        dialing = asyncio.create_task(call.brief_view.dial.callback(setup.interaction()))
        await connecting.wait()
        assert call.brief_message.kwargs['view'] is call.hangup
        assert call.hangup.children[0].label == 'stop'
        await call.hangup.hang_up.callback(setup.interaction())
        assert dialing.cancelled() and call.phase == 'ended'
        assert call.audio.closed and not setup.calls.active
        setup.engine.start.assert_not_awaited()
    asyncio.run(run())


def test_stop_preserves_intent_and_reservation_until_pending_session_returns():
    async def run():
        setup = Setup()
        call, _ = await setup.plan()
        started, release = asyncio.Event(), asyncio.Event()

        async def start(*args):
            started.set()
            await release.wait()
            return setup.session

        setup.engine.start.side_effect = start
        dialing = asyncio.create_task(call.brief_view.dial.callback(setup.interaction()))
        await started.wait()
        stopping = asyncio.create_task(call.hangup.hang_up.callback(setup.interaction()))
        while call.phase != 'ended':
            await asyncio.sleep(0)
        assert call.audio.closed and setup.calls.active[1] is call
        await setup.calls.invoke(setup.interaction(), '+81300000000', 'another')
        assert setup.engine.plan.await_count == 1
        release.set()
        await asyncio.gather(dialing, stopping)
        setup.session.hang_up.assert_awaited_once_with('requested')
        assert not setup.calls.active
    asyncio.run(run())


def test_requester_authority_is_rechecked_after_acknowledgement():
    async def run():
        setup = Setup()
        call = await setup.start()
        await deliver(call, QuestionAsked(call.request.request_id, 'q', 'may i share the name?', consent_generation=1))
        view = call.questions['q']
        other = setup.interaction(11)
        assert not await view.interaction_check(other)
        await view.free_text(other)
        assert other.response.modal is None
        await view.submit(other, 'yes')
        setup.session.answer.assert_not_awaited()
        requester = setup.interaction()

        async def defer(**kwargs):
            requester.user.id = 11
            requester.response.done = True

        requester.response.defer = defer
        await view.submit(requester, 'yes')
        setup.session.answer.assert_not_awaited()
        await call.stop('test')
    asyncio.run(run())


def test_absolute_expiry_does_not_restart_on_display_and_question_is_bilingual():
    async def run():
        setup = Setup()
        call = await setup.start()
        expired = QuestionAsked(call.request.request_id, 'old', 'old question', deadline_s=60,
                                deadline_at=time.monotonic() - 1, consent_generation=1)
        await deliver(call, expired)
        assert 'old' not in call.questions
        assert not any('old question' in (message.content or '') for message in setup.messages)
        deadline = time.monotonic() + .05
        event = QuestionAsked(call.request.request_id, 'q', 'does six work?', deadline_s=60,
                              deadline_at=deadline, text_original='六時でよろしいですか。', consent_generation=1)
        await deliver(call, event)
        view = call.questions['q']
        assert view.deadline == deadline and view.timeout <= .05
        assert '六時でよろしいですか。\n↳ does six work?' in view.message.content
        view.deadline = time.monotonic() - 1
        await view.submit(setup.interaction(), 'yes')
        setup.session.answer.assert_not_awaited()
        await call.stop('test')
    asyncio.run(run())


def test_unknown_session_and_old_generation_never_backfill_callee_text():
    async def run():
        setup = Setup()
        call = await setup.start()
        session, call.session = call.session, None
        await deliver(call, Utterance(call.request.request_id, 'unknown', 'callee', 'private-before', 0, consent_generation=1))
        call.session = session
        session.publication_allowed = False
        await deliver(call, Utterance(call.request.request_id, 'denied', 'callee', 'private-denied', 1, consent_generation=1))
        session.publication_allowed = True
        await call.emit(Utterance(call.request.request_id, 'stale', 'callee', 'private-queued', 2, consent_generation=1))
        session.consent_generation += 1
        await call.flush_events()
        await deliver(call, Translation(call.request.request_id, 'unknown', 'do not backfill', consent_generation=1))
        await deliver(call, Utterance(call.request.request_id, 'new', 'callee', 'permitted', 3, consent_generation=2))
        await call.pager.flush()
        text = '\n'.join(message.content or '' for message in setup.messages)
        assert 'private-' not in text and 'do not backfill' not in text
        assert 'permitted' in text
        await call.stop('test')
    asyncio.run(run())


def test_pager_revocation_during_throttle_drops_unposted_translation_and_rows():
    async def run():
        messages = Messages()
        current = [1]
        pager = TranscriptPager(messages.send, messages.edit, interval=60,
                                allowed=lambda generation: generation == current[0])
        await pager.emit(Utterance('r', '1', 'callee', 'already shown', 0, consent_generation=1))
        await pager.flush()
        await pager.emit(Translation('r', '1', 'withheld translation', consent_generation=1))
        await pager.emit(Utterance('r', '2', 'callee', 'withheld new line', 1, consent_generation=1))
        await asyncio.sleep(0)
        current[0] = 2
        await pager.flush()
        assert len(messages.writes) == 1
        await pager.emit(Utterance('r', '3', 'callee', 'new consent', 2, consent_generation=2))
        await pager.flush()
        text = ''.join(messages.contents)
        assert 'already shown' in text and 'new consent' in text
        assert 'withheld' not in text
    asyncio.run(run())


def test_pager_rechecks_generation_between_page_writes():
    async def run():
        current, messages = [1], Messages()

        async def send(content):
            message = await messages.send(content)
            current[0] = 2
            return message

        pager = TranscriptPager(send, messages.edit,
                                allowed=lambda generation: generation == current[0])
        await pager.emit(Utterance('r', '1', 'callee', 'a' * 1900, 0, consent_generation=1))
        await pager.emit(Utterance('r', '2', 'callee', 'withheld-second-page' * 100, 1, consent_generation=1))
        await pager.flush()
        assert not any('withheld-second-page' in text for text in messages.contents)
    asyncio.run(run())


def test_late_asr_uses_source_time_and_translation_failure_and_partial_speech_are_explicit():
    async def run():
        messages = Messages()
        pager = TranscriptPager(messages.send, messages.edit)
        await pager.emit(Utterance('r', 'later', 'fri', 'later speech', 5, 6, 'interrupted', consent_generation=1))
        await pager.emit(Translation('r', 'earlier', '', status='unavailable', consent_generation=1))
        await pager.emit(Utterance('r', 'earlier', 'callee', 'earlier speech', 1, 2, consent_generation=1))
        await pager.flush()
        text = ''.join(messages.contents)
        assert text.index('earlier speech') < text.index('later speech')
        assert 'earlier speech\n↳ translation unavailable' in text
        assert 'fri · interrupted; wording uncertain · later speech' in text
    asyncio.run(run())


def test_full_result_attachment_preserves_conditions_without_content_in_persistent_controls():
    async def run():
        uploads = []

        async def send(content, **kwargs):
            attachment = kwargs['file']
            uploads.append((attachment.filename, attachment.fp.read(), attachment.fp))
            return Message(content, **kwargs)

        summary = CostSummary(send, AsyncMock())
        outcome = 'Conditional answer. ' * 100 + '\nOnly after manager approval. Reference AB12.'
        await summary.emit(CallFinished('r', outcome, 60, termination_confirmed=False))
        assert 'termination unconfirmed' in summary.message.content and '…' in summary.message.content
        assert uploads[0][:2] == ('call-result.txt', outcome.encode('utf-8'))
        assert uploads[0][2].closed
        view = summary.view
        assert view.is_persistent() and not hasattr(view, 'outcome')
        assert [child.custom_id for child in view.children] == ['jpcall:cost_details']
        interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()),
                                      followup=SimpleNamespace(send=AsyncMock()))
        await view.children[0].callback(interaction)
        assert interaction.response.send_message.call_args.kwargs['ephemeral']
        assert 'Reference AB12' not in interaction.response.send_message.call_args.args[0]
        view.stop()
    asyncio.run(run())



def test_detached_usage_sink_releases_call_and_edits_only_cost_embed():
    async def run():
        edits = []

        async def edit_message(channel, message, *, params):
            edits.append((channel, message, params.payload.copy()))

        http = SimpleNamespace(edit_message=edit_message)
        owner = SimpleNamespace(active={}, limits=SimpleNamespace(allowed_user_ids={10}))
        from fri.jpcall.contract import CallRequest
        call = DiscordCall(owner, CallRequest('r', 1, 2, 10, '+81300000000', 'private goal'),
                           None, SimpleNamespace(id=3, _state=SimpleNamespace(http=http)))
        call.session = SimpleNamespace(secret='private session')
        async def send(content, **kwargs):
            return Message(content, **kwargs)
        call.text_channel.send = send
        call.cost = CostSummary(call.send, call.edit, http=http, channel_id=3)
        await call.cost.emit(CallFinished('r', 'private full outcome', 1))
        message_id = call.cost.message.id
        late = call.usage_sink()
        call_ref, cost_ref = weakref.ref(call), weakref.ref(call.cost)
        call.cost.view.stop()
        del call
        await asyncio.sleep(0)
        gc.collect()
        assert call_ref() is None and cost_ref() is None
        await late(UsageUpdated('r', (UsageLine('twilio', 'phone', {'minutes': 1}, .05, True),), True))
        channel, message, payload = edits[-1]
        assert (channel, message) == (3, message_id)
        assert set(payload) == {'embeds', 'tts'} or set(payload) == {'embeds'}
        assert payload['embeds'][0]['description'] == 'cost total: $0.05'
        assert 'private' not in repr(late.__self__.__dict__)
    asyncio.run(run())


def test_late_price_while_first_report_send_is_pending_is_not_lost():
    async def run():
        accepted, release = asyncio.Event(), asyncio.Event()
        edits = []
        async def edit_message(channel, message, *, params):
            edits.append(params.payload['embeds'][0]['description'])
        async def send(content, **kwargs):
            accepted.set()
            await release.wait()
            return Message(content, **kwargs)
        summary = CostSummary(send, AsyncMock(), http=SimpleNamespace(edit_message=edit_message),
                              channel_id=3)
        publishing = asyncio.create_task(summary.emit(CallFinished('r', 'result', 1)))
        await accepted.wait()
        await summary.receipt.emit(UsageUpdated('r',
            (UsageLine('twilio', 'phone', {'minutes': 1}, .05, True),), True))
        release.set()
        await publishing
        assert edits[-1] == 'cost total: $0.05'
        await summary.emit(CallFinished('r', 'result', 1))
        assert summary.receipt.usage[-1].exact
        summary.view.stop()
    asyncio.run(run())


def test_real_view_store_retains_only_cost_receipt_and_open_modal_does_not_keep_call_alive():
    from discord.ui.view import ViewStore

    from fri.jpcall.contract import CallBrief, CallRequest, Limits
    from fri.jpcall.discord_command import AnswerModal, BriefView, HangupView, QuestionView

    async def run():
        store = ViewStore(None)
        uploads = []

        class StoredMessage:
            def __init__(self, message_id):
                self.id = message_id

            async def edit(self, **kwargs):
                view = kwargs.get('view')
                if view is not None and not view.is_finished():
                    store.add_view(view, self.id)

        async def send(content, **kwargs):
            message = StoredMessage(40)
            view = kwargs.get('view')
            if view is not None:
                store.add_view(view, message.id)
            if 'file' in kwargs:
                uploads.append(kwargs['file'].fp.read())
            return message

        owner = SimpleNamespace(active={}, limits=Limits(allowed_user_ids=frozenset({10})),
                                _event_tasks=set())
        channel = SimpleNamespace(send=send, id=3, _state=SimpleNamespace(http=None))
        request = CallRequest('r', 1, 2, 10, '+81300000000', 'private goal', ('private name',))
        call = DiscordCall(owner, request, None, channel)
        call.phase = 'active'
        call.session = SimpleNamespace(publication_allowed=True, consent_generation=1)
        call.brief = CallBrief('r', 'private brief', 'private opener', request.share, ())
        call.brief_view, call.hangup = BriefView(call), HangupView(call)
        call.brief_message, call.hangup_message = StoredMessage(10), StoredMessage(20)
        store.add_view(call.brief_view, 10)
        store.add_view(call.hangup, 20)
        question = QuestionView(call, QuestionAsked('r', 'q', 'private question', ('private option',), consent_generation=1))
        question.message = StoredMessage(30)
        call.questions['q'] = question
        store.add_view(question, 30)
        modal = AnswerModal(question)
        store.add_view(modal)
        call.pager = TranscriptPager(call.send, call.edit, allowed=call.publication_allowed)
        await call.pager.emit(Utterance('r', 'line', 'callee', 'private transcript', 0, consent_generation=1))
        await call.pager.flush()
        call.cost = CostSummary(call.send, call.edit)
        owner.active[1] = call
        request_ref, call_ref = weakref.ref(request), weakref.ref(call)
        pager_ref, cost_ref, brief_ref = weakref.ref(call.pager), weakref.ref(call.cost), weakref.ref(call.brief)
        finished = CallFinished('r', 'private full result with condition and Reference AB12', 1)
        finished_ref = weakref.ref(finished)
        await deliver(call, finished)
        receipt = call.cost.receipt
        assert store.persistent_views == [call.cost.view]
        assert modal.custom_id in store._modals
        assert uploads == [finished.outcome_en.encode('utf-8')]
        del call, request, question, finished
        await asyncio.sleep(0)
        gc.collect()
        assert all(ref() is None for ref in
                   (request_ref, call_ref, pager_ref, cost_ref, brief_ref, finished_ref))
        assert store.persistent_views[0].receipt is receipt
        interaction = SimpleNamespace(response=SimpleNamespace(is_done=lambda: False,
                                                               send_message=AsyncMock()))
        await modal.on_submit(interaction)
        assert interaction.response.send_message.call_args.args == ('that question is closed.',)
        modal.stop()
        store.persistent_views[0].stop()
    asyncio.run(run())


def test_result_attachment_retry_reuses_nonce_and_closes_failed_upload_buffer():
    async def run():
        attempts = []

        async def send(content, **kwargs):
            attachment = kwargs['file']
            attempts.append((kwargs['nonce'], attachment.fp.read(), attachment.fp))
            if len(attempts) == 1:
                raise OSError('synthetic response lost')
            return Message(content, **kwargs)

        summary = CostSummary(send, AsyncMock())
        await summary.emit(CallFinished('r', 'conditional answer; Reference AB12', 1))
        assert summary.message is not None and len(attempts) == 2 and attempts[0][2].closed
        await summary.emit(UsageUpdated('r',
            (UsageLine('twilio', 'phone', {'minutes': 1}, .05, True),), True))
        assert attempts[0][:2] == attempts[1][:2]
        assert all(body.closed for _, _, body in attempts)
        assert summary.message is not None
        summary.view.stop()
    asyncio.run(run())


@pytest.mark.parametrize('accepted_before_failure', [False, True])
def test_r1_s1_production_usage_sink_gets_recovered_report_without_duplicate(accepted_before_failure):
    async def run():
        setup = Setup()
        call = await setup.start()
        late = call.usage_sink()  # The production late-price seam, captured before final emission.
        original = setup.channel.send.side_effect
        attempts, accepted = [], {}

        async def send(content, **kwargs):
            nonce = kwargs['nonce']
            attempts.append(nonce)
            if nonce not in accepted and (len(attempts) > 1 or accepted_before_failure):
                accepted[nonce] = await original(content, **kwargs)
            if len(attempts) == 1:
                raise discord.HTTPException(SimpleNamespace(status=503, reason='unavailable'),
                                            'synthetic response lost')
            return accepted[nonce]

        setup.channel.send.side_effect = send
        await deliver(call, CallFinished(call.request.request_id, 'conditional answer; Reference AB12', 1))
        assert len(attempts) == 2 and attempts[0] == attempts[1]
        assert call.cost.message is accepted[attempts[0]]
        reports = [m for m in setup.messages if (m.content or '').startswith('call ended')]
        assert reports == [call.cost.message]
        await late(UsageUpdated(call.request.request_id,
            (UsageLine('twilio', 'phone', {'minutes': 1}, .05, True),), True))
        assert len(attempts) == 2 and len(reports) == 1
        assert call.cost.message.kwargs['embed'].description == 'cost total: $0.05'
        assert late.__self__ is call.cost.receipt
        assert 'conditional answer' not in repr(late.__self__.__dict__)
        call.cost.view.stop()
    asyncio.run(run())


def test_r1_s1_both_initial_attempts_fail_and_detached_usage_does_not_claim_report(caplog):
    async def run():
        setup = Setup()
        call = await setup.start()
        late = call.usage_sink()
        setup.channel.send.side_effect = discord.HTTPException(
            SimpleNamespace(status=503, reason='unavailable'), 'synthetic private failure')
        await deliver(call, CallFinished(call.request.request_id, 'private full outcome', 1))
        assert setup.channel.send.await_count == 3  # Existing brief, then exactly two final attempts.
        assert call.cost.message is None and call.cost.receipt.message_id is None
        await late(UsageUpdated(call.request.request_id, (), True))
        assert setup.channel.send.await_count == 3
        assert not any((m.content or '').startswith('call ended') for m in setup.messages)
        assert 'final report failed' in caplog.text and 'synthetic private failure' not in caplog.text
        call.cost.view.stop()
    asyncio.run(run())


@pytest.mark.parametrize('deny_attachment', [False, True])
def test_r1_s1_s2_initial_publication_and_text_fallback_share_deadline(monkeypatch, deny_attachment):
    from fri.jpcall import discord_cost

    async def run():
        monkeypatch.setattr(discord_cost, 'FINAL_REPORT_TIMEOUT_S', .01)
        setup = Setup()
        call = await setup.start()
        late = call.usage_sink()
        attempts = []

        async def send(content, **kwargs):
            attempts.append(kwargs)
            if deny_attachment and 'file' in kwargs:
                raise discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'),
                                        {'code': 50013, 'message': 'Missing Permissions'})
            await asyncio.Future()

        setup.channel.send.side_effect = send
        async with asyncio.timeout(.5):
            await deliver(call, CallFinished(call.request.request_id, 'complete result', 1))
        assert len(attempts) == (2 if deny_attachment else 1)
        assert call.cost._publish_task.done() and call.cost.message is None
        assert attempts[0]['file'].fp.closed
        await late(UsageUpdated(call.request.request_id, (), True))
        assert len(attempts) == (2 if deny_attachment else 1)
        call.cost.view.stop()
    asyncio.run(run())


def test_r1_s2_real_attachment_403_preserves_complete_text_with_stable_page_nonces():
    async def run():
        setup = Setup()
        call = await setup.start()
        late = call.usage_sink()
        original = setup.channel.send.side_effect
        attempts, accepted = [], {}
        lost_page = None

        async def send(content, **kwargs):
            nonlocal lost_page
            nonce = kwargs['nonce']
            attempts.append((content, kwargs))
            if 'file' in kwargs:
                raise discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'),
                                        {'code': 50013, 'message': 'Missing Permissions'})
            if nonce in accepted:
                return accepted[nonce]
            accepted[nonce] = await original(content, **kwargs)
            if content.startswith('full result · 2/') and lost_page is None:
                lost_page = nonce
                raise discord.HTTPException(SimpleNamespace(status=503, reason='unavailable'),
                                            'synthetic page response lost')
            return accepted[nonce]

        setup.channel.send.side_effect = send
        outcome = ('🦉 conditional answer; ' * 300) + '\nOnly after manager approval. Reference AB12.'
        await deliver(call, CallFinished(call.request.request_id, outcome, 1))
        pages = [message for message in setup.messages if (message.content or '').startswith('full result')]
        assert ''.join(message.content.split('\n', 1)[1] for message in pages) == outcome
        assert all(len(message.content.encode('utf-16-le')) // 2 <= 2000 for message in pages)
        reports = [message for message in setup.messages if (message.content or '').startswith('call ended')]
        assert reports == [call.cost.message] and call.cost.message.kwargs['view'] is call.cost.view
        assert len([kwargs for _, kwargs in attempts if kwargs['nonce'] == lost_page]) == 2
        assert attempts[0][1]['nonce'] == reports[0].kwargs['nonce']
        assert attempts[0][1]['file'].fp.closed
        sent = len(attempts)
        await late(UsageUpdated(call.request.request_id,
            (UsageLine('twilio', 'phone', {'minutes': 1}, .05, True),), True))
        assert len(attempts) == sent
        assert reports[0].kwargs['embed'].description == 'cost total: $0.05'
        call.cost.view.stop()
    asyncio.run(run())


def test_r1_s2_text_permission_failure_does_not_claim_complete_delivery(caplog):
    async def run():
        setup = Setup()
        call = await setup.start()
        late = call.usage_sink()
        attempts = []

        async def send(content, **kwargs):
            attempts.append(kwargs)
            raise discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'),
                                    {'code': 50013, 'message': 'Missing Permissions'})

        setup.channel.send.side_effect = send
        await deliver(call, CallFinished(call.request.request_id, 'complete result', 1))
        assert len(attempts) == 2 and 'file' in attempts[0] and 'file' not in attempts[1]
        assert call.cost.message is None
        await late(UsageUpdated(call.request.request_id, (), True))
        assert len(attempts) == 2 and 'final report failed' in caplog.text
        call.cost.view.stop()
    asyncio.run(run())


@pytest.mark.parametrize('generation', [None, 1])
def test_r1_b1_source_identity_is_not_retagged_after_permission_regrant(generation):
    async def run():
        setup = Setup()
        call = await setup.start()
        setup.session.consent_generation = 3
        await deliver(call, Utterance(call.request.request_id, 'old', 'callee', 'private-old', 0,
                                     consent_generation=generation))
        await deliver(call, QuestionAsked(call.request.request_id, 'old-q', 'private-question',
                                         consent_generation=generation))
        assert not call.questions
        await deliver(call, Utterance(call.request.request_id, 'current', 'callee', 'current-original', 1,
                                     consent_generation=3))
        await deliver(call, Translation(call.request.request_id, 'current', 'private-stale-translation',
                                       consent_generation=generation))
        await call.pager.flush()
        text = '\n'.join(message.content or '' for message in setup.messages)
        assert 'private-' not in text and 'current-original' in text
        await deliver(call, Translation(call.request.request_id, 'current', 'current-English',
                                       consent_generation=3))
        await call.pager.flush()
        assert 'current-English' in call.pager.pages[0].message.content
        await call.stop('test')
    asyncio.run(run())


def test_r1_b1_pager_refuses_untagged_or_stale_source_events_without_override():
    async def run():
        messages = Messages()
        pager = TranscriptPager(messages.send, messages.edit, allowed=lambda gen: gen == 3)
        for generation in (None, 1):
            await pager.emit(Utterance('r', f'old-{generation}', 'callee', 'private-old', 0,
                                      consent_generation=generation))
            await pager.emit(QuestionAsked('r', f'q-{generation}', 'private-question',
                                          consent_generation=generation))
        await pager.emit(Utterance('r', 'current', 'callee', 'current-original', 1,
                                  consent_generation=3))
        await pager.emit(Translation('r', 'current', 'private-translation', consent_generation=None))
        await pager.flush()
        text = ''.join(messages.contents)
        assert 'private-' not in text and 'current-original' in text
    asyncio.run(run())


def test_r1_s1_timed_out_first_attempt_retries_same_nonce_before_detachment(monkeypatch):
    from fri.jpcall import discord_cost

    async def run():
        monkeypatch.setattr(discord_cost, 'FINAL_SEND_TIMEOUT_S', .01)
        setup = Setup()
        call = await setup.start()
        late = call.usage_sink()
        original = setup.channel.send.side_effect
        attempts = []

        async def send(content, **kwargs):
            attempts.append(kwargs['nonce'])
            if len(attempts) == 1:
                await asyncio.Future()
            return await original(content, **kwargs)

        setup.channel.send.side_effect = send
        async with asyncio.timeout(.5):
            await deliver(call, CallFinished(call.request.request_id, 'complete result', 1))
        assert len(attempts) == 2 and attempts[0] == attempts[1]
        assert call.cost.message is not None
        await late(UsageUpdated(call.request.request_id, (), True))
        assert len(attempts) == 2
        call.cost.view.stop()
    asyncio.run(run())
