"""USD estimates checked 2026-11-03; injected tables override any rate.

Sources: https://developers.openai.com/api/docs/pricing
https://www.twilio.com/en-us/voice/pricing/jp
Twilio rates vary by destination and account. The posted REST price wins later.
Token units below are disjoint: cached inputs are subtracted from regular inputs.
"""

from dataclasses import dataclass, field


def _rates() -> dict[str, dict[str, float]]:
    return {
        'gpt-realtime-2.1': {
            'text_in_tokens': 4.0, 'cached_text_in_tokens': 0.4,
            'audio_in_tokens': 32.0, 'cached_audio_in_tokens': 0.4,
            'text_out_tokens': 24.0, 'audio_out_tokens': 64.0,
        },
        'gpt-4o-transcribe': {
            'text_in_tokens': 2.5, 'audio_in_tokens': 2.5, 'text_out_tokens': 10.0,
        },
        'gpt-4o-mini-transcribe': {
            'text_in_tokens': 1.25, 'audio_in_tokens': 1.25, 'text_out_tokens': 5.0,
        },
        'gpt-6.1-terra': {
            'text_in_tokens': 1.0, 'cached_text_in_tokens': 0.05, 'text_out_tokens': 5.0,
        },
        'gpt-5-mini': {
            'text_in_tokens': 0.25, 'cached_text_in_tokens': 0.025, 'text_out_tokens': 2.0,
        },
    }


@dataclass(frozen=True)
class PriceTable:
    as_of: str = '2026-11-03'
    per_million: dict[str, dict[str, float]] = field(default_factory=_rates)
    transcription_per_minute: dict[str, float] = field(default_factory=lambda: {
        'gpt-transcribe': 0.0045, 'gpt-realtime-whisper': 0.017,
    })
    twilio_per_minute: float = 0.0746
    twilio_mobile_per_minute: float = 0.185
    media_streams_per_minute: float = 0.0044
    amd_per_call: float = 0.0075

    def token_cost(self, model: str, units: dict[str, float]) -> float | None:
        rates = self.per_million.get(model, {})
        if any(amount and kind not in rates for kind, amount in units.items()):
            return None
        return sum(amount * rates.get(kind, 0) / 1_000_000
                   for kind, amount in units.items())

    def voice_rate(self, destination: str) -> float:
        return (self.twilio_mobile_per_minute
                if destination.startswith(('+8170', '+8180', '+8190'))
                else self.twilio_per_minute)
