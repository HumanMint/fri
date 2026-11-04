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
    assert text.endswith('total · ~$0.1200 + pending')
    assert render_usage((UsageLine('twilio', 'phone', {'minutes': 1}, 0.05, True),), True
                        ).endswith('total · $0.0500')


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
