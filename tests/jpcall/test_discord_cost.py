import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from fri.jpcall.contract import CallFinished, CallState, StatusChanged, UsageLine, UsageUpdated
from fri.jpcall.discord_cost import CostSummary, render_final, render_usage


def test_usage_units_estimates_pending_and_exact_total():
    rows = (UsageLine('realtime', 'voice-model', {'audio_in_tokens': 100, 'text_out_tokens': 20},
                      0.12),
            UsageLine('twilio', 'twilio-voice', {'minutes': 2.5}, None))
    text = render_usage(rows, False)
    assert 'audio in tokens 100' in text and 'text out tokens 20' in text
    assert '~$0.1200' in text and 'minutes 2.5' in text
    assert text.endswith('total · input 100 / output 20 tokens · ~$0.1200 + pending')
    assert render_usage((UsageLine('twilio', 'phone', {'minutes': 1}, 0.05, True),), True
                        ).endswith('total · input 0 / output 0 tokens · $0.0500')


def test_late_price_edits_original_summary_and_keeps_outcome():
    async def run():
        messages, edits = [], []

        async def send(content, **kwargs):
            messages.append(content)
            return len(messages) - 1

        async def edit(message, content, **kwargs):
            edits.append(message)
            messages[message] = content

        summary = CostSummary(send, edit)
        await summary.emit(CallFinished('r', 'booked for six.', 120,
                                        (UsageLine('twilio', 'phone', {'minutes': 2}, None),)))
        assert summary.receipt.usage[0].usd is None
        await summary.emit(UsageUpdated('r', (UsageLine('twilio', 'phone', {'minutes': 2},
                                                       0.1, True),), True))
        assert len(messages) == 1 and edits == [0]
        assert 'booked for six.' in messages[0] and summary.usage_final
    asyncio.run(run())


def test_token_totals_include_cached_input_and_all_models_without_double_counting():
    rows = (
        UsageLine('realtime', 'voice', {'audio_in_tokens': 100, 'text_in_tokens': 40,
                                      'cached_audio_in_tokens': 30, 'cached_text_in_tokens': 10,
                                      'audio_out_tokens': 70, 'text_out_tokens': 20}, .1),
        UsageLine('transcription', 'transcribe', {'unknown_in_tokens': 20,
                                                'text_out_tokens': 12}, .01),
        UsageLine('translation', 'text', {'input_tokens': 200, 'text_in_tokens': 180,
                                         'cached_text_in_tokens': 20, 'output_tokens': 30,
                                         'text_out_tokens': 30}, .01),
        UsageLine('summary', 'text', {'text_in_tokens': 50, 'cached_text_in_tokens': 5,
                                     'text_out_tokens': 8}, .01),
        UsageLine('twilio', 'phone', {'minutes': 1}, None),
    )
    text = render_usage(rows, False)
    assert 'input 455 / output 140 tokens' in text
    assert 'cached audio in tokens 30' in text
    assert text.endswith('~$0.1300 + pending')


def test_unsplit_cache_metadata_does_not_inflate_input_total():
    text = render_usage((UsageLine('realtime', 'voice', {'text_in_tokens': 100,
                                                      'unknown_cached_in_tokens': 20}, None),), False)
    assert 'input 100 / output 0 tokens' in text
    assert 'pending' in text


def test_declining_ai_interpreter_is_shown_plainly():
    async def run():
        messages = []

        async def send(content, **kwargs):
            messages.append(content)
            return len(messages) - 1

        async def edit(message, content, **kwargs):
            messages[message] = content

        for outcome in ('declined to talk with an AI interpreter',
                        'they declined to talk with an ai interpreter.'):
            summary = CostSummary(send, edit)
            await summary.emit(CallFinished('r', outcome, 12))
            assert messages[-1].splitlines()[1] == outcome
    asyncio.run(run())


def test_compact_final_has_one_short_outcome_and_total_without_breakdown():
    usage = (UsageLine('realtime', 'voice-model', {'audio_in_tokens': 200}, .02),
             UsageLine('twilio', 'phone', {'minutes': .4}, .03))
    finished = CallFinished('r', 'Booked for six. reference AB12', 24, usage)
    assert render_final(finished, usage, False) == (
        'call ended · 0.4 min\nbooked for six. reference AB12')
    reference = CallFinished('r', 'AB12 is the confirmed reference.', 24, usage)
    assert render_final(reference, usage, False).splitlines()[1].startswith('AB12')
    long = CallFinished('r', ('long outcome ' * 1000) + '🦉' * 3000, 24, usage)
    text = render_final(long, usage, False)
    assert len(text.splitlines()) == 4  # Header and at most three short outcome lines; full result is accessible.
    assert len(text.encode('utf-16-le')) // 2 < 2000 and '…' in text
    assert 'audio in tokens' not in text and 'voice-model' not in text


@pytest.mark.parametrize(('detail', 'outcome', 'duration', 'reason'), [
    ('The business line was busy.', 'no confirmed outcome.', 0, 'busy'),
    ('The business did not answer.', 'no confirmed outcome.', 0, 'no answer'),
    ('', 'went to voicemail', 24, 'went to voicemail'),
    ('', 'The call went to voicemail. No booking was made.', 24, 'went to voicemail'),
])
def test_failed_call_has_one_plain_failure_line_and_cost(detail, outcome, duration, reason):
    usage = (UsageLine('twilio', 'phone', {'minutes': 1}, .05),)
    finished = CallFinished('r', outcome, duration, usage)
    failure = StatusChanged('r', CallState.FAILED, detail) if detail else None
    assert render_final(finished, usage, False, failure) == (
        f"call didn't connect · {reason}")


def test_breakdown_is_private_and_tracks_late_exact_phone_price():
    async def run():
        sent, edited = [], []

        async def send(content, **kwargs):
            sent.append((content, kwargs))
            return object()

        async def edit(message, content, **kwargs):
            edited.append((message, content, kwargs))

        cost = CostSummary(send, edit)
        rows = tuple(UsageLine(f'component-{i}', f'model-{i}', {'input_tokens': 1000}, .01)
                     for i in range(30)) + (UsageLine('twilio', 'phone', {'minutes': 1}, None),)
        await cost.emit(CallFinished('r', 'done.', 24, rows))
        assert len(sent) == 1 and 'model-' not in sent[0][0]
        view = sent[0][1]['view']
        assert view.children[0].custom_id == 'jpcall:cost_details'
        member = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()),
                                 followup=SimpleNamespace(send=AsyncMock()))
        await view.children[0].callback(member)
        all_sends = [member.response.send_message.call_args, *member.followup.send.call_args_list]
        private_text = '\n'.join(call.args[0] for call in all_sends)
        assert 'model-0' in private_text and '~$0.0100' in private_text and 'pending' in private_text
        assert all(call.kwargs['ephemeral'] for call in all_sends)
        assert all(len(call.args[0]) <= 2000 for call in all_sends)
        await cost.emit(UsageUpdated('r', (UsageLine('twilio', 'phone', {'minutes': 1}, .05, True),), True))
        assert len(sent) == 1 and edited[-1][0] is cost.message
        assert edited[-1][2]['embed'].description == 'cost total: $0.05'
        assert view.receipt.usage[-1].exact
        view.stop()
    asyncio.run(run())


def test_cancelled_waiter_cannot_orphan_initial_report_or_resend_on_update():
    async def run():
        accepted, edits = [], []
        started, release = asyncio.Event(), asyncio.Event()

        async def send(content, **kwargs):
            message = object()
            accepted.append((message, kwargs['nonce']))
            started.set()
            await release.wait()
            return message

        async def edit(message, content, **kwargs):
            edits.append((message, content, kwargs))

        cost = CostSummary(send, edit)
        publishing = asyncio.create_task(cost.emit(CallFinished('r', 'done.', 24)))
        await started.wait()
        publishing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await publishing
        updating = asyncio.create_task(cost.emit(UsageUpdated('r',
            (UsageLine('twilio', 'phone', {'minutes': 1}, .05, True),), True)))
        await asyncio.sleep(0)
        release.set()
        await updating
        await cost.flush()
        assert len(accepted) == 1
        assert cost.message is accepted[0][0] and edits[-1][0] is cost.message
        assert edits[-1][2]['embed'].description == 'cost total: $0.05'
        cost.view.stop()
    asyncio.run(run())


def test_replayed_finish_keeps_one_message_and_does_not_replace_exact_price():
    async def run():
        send, edit = AsyncMock(return_value=object()), AsyncMock()
        cost = CostSummary(send, edit)
        finished = CallFinished('r', 'done.', 24,
                                (UsageLine('twilio', 'phone', {'minutes': 1}, None),))
        await cost.emit(finished)
        await cost.emit(UsageUpdated('r',
            (UsageLine('twilio', 'phone', {'minutes': 1}, .05, True),), True))
        await cost.emit(finished)
        send.assert_awaited_once()
        assert cost.usage[-1].exact and cost.usage[-1].usd == .05
        assert edit.call_args.kwargs['embed'].description == 'cost total: $0.05'
        cost.view.stop()
    asyncio.run(run())


def test_failed_edit_never_posts_a_replacement_and_retry_send_reuses_nonce():
    async def run():
        send = AsyncMock(side_effect=discord.HTTPException(
            SimpleNamespace(status=503, reason='unavailable'), 'synthetic details'))
        edit = AsyncMock()
        cost = CostSummary(send, edit)
        finished = CallFinished('r', 'done.', 24)
        await cost.emit(finished)
        nonce = send.call_args.kwargs['nonce']
        send.side_effect = None
        send.return_value = object()
        update = UsageUpdated('r', (UsageLine('twilio', 'phone', {'minutes': 1}, .05, True),), True)
        await cost.emit(update)
        assert send.call_args.kwargs['nonce'] == nonce
        assert 0 <= nonce < 2 ** 64
        edit.side_effect = discord.HTTPException(SimpleNamespace(status=404, reason='missing'), 'gone')
        message = cost.message
        await cost.emit(update)
        assert cost.message is message and send.await_count == 2
        from discord.http import handle_message_parameters
        with handle_message_parameters(nonce=nonce) as params:
            assert params.payload['enforce_nonce'] is True
        cost.view.stop()
    asyncio.run(run())


def test_nonce_retry_recovers_old_message_then_edits_latest_price():
    async def run():
        accepted = []
        first = True

        async def send(content, **kwargs):
            nonlocal first
            if first:
                first = False
                accepted.append(SimpleNamespace(content=content, nonce=kwargs['nonce']))
                raise discord.HTTPException(SimpleNamespace(status=503, reason='unavailable'),
                                            'response lost after acceptance')
            assert kwargs['nonce'] == accepted[0].nonce
            return accepted[0]  # Discord deduplicates, returning its earlier body.

        async def edit(message, content, **kwargs):
            message.content = content
            message.embed = kwargs['embed']

        cost = CostSummary(send, edit)
        await cost.emit(CallFinished('r', 'done.', 24))
        await cost.emit(UsageUpdated('r',
            (UsageLine('twilio', 'phone', {'minutes': 1}, .05, True),), True))
        assert len(accepted) == 1 and cost.message is accepted[0]
        assert accepted[0].embed.description == 'cost total: $0.05'
        cost.view.stop()
    asyncio.run(run())
