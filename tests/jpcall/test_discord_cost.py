import asyncio

from fri.jpcall.contract import CallFinished, UsageLine, UsageUpdated
from fri.jpcall.discord_cost import CostSummary, render_usage


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

        async def send(content):
            messages.append(content)
            return len(messages) - 1

        async def edit(message, content):
            edits.append(message)
            messages[message] = content

        summary = CostSummary(send, edit)
        await summary.emit(CallFinished('r', 'booked for six.', 120,
                                        (UsageLine('twilio', 'phone', {'minutes': 2}, None),)))
        assert 'pending' in messages[0]
        await summary.emit(UsageUpdated('r', (UsageLine('twilio', 'phone', {'minutes': 2},
                                                       0.1, True),), True))
        assert len(messages) == 1 and edits == [0]
        assert 'booked for six.' in messages[0] and 'pending' not in messages[0]
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
