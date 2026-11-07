"""Shared contract between the jpcall call engine and its Discord front end.

The engine (``fri.jpcall.engine`` and friends) owns the phone leg: Twilio, the
OpenAI Realtime session, transcription, translation, hold timers and usage
metering. The Discord side (``fri.jpcall.discord_*``) owns ``/jpcall``, the
voice connection, the audio mixer, transcript pages, question prompts and the
post-call summary. They meet only through the types below.

Changing this file is a coordinated change: propose it, don't fork it.

Fields ending in ``_ja`` hold text in the call language (``JPCALL_CALL_LANGUAGE``:
Japanese for live calls, Korean for development testing).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Literal, Protocol, Union

# Audio crossing the contract is always 8 kHz mono PCM16 little-endian, which is
# what both Twilio Media Streams (after mu-law decode) and OpenAI g711_ulaw use.
# The Discord side resamples to 48 kHz stereo for discord.py.
AUDIO_RATE = 8000
AUDIO_CHANNELS = 1
AUDIO_SAMPLE_WIDTH = 2

Speaker = Literal['callee', 'fri']


@dataclass(frozen=True)
class CallRequest:
    """What a member asked for. Built by the Discord side, validated by the engine."""
    request_id: str
    guild_id: int
    voice_channel_id: int
    requester_id: int
    number_e164: str            # '+81...' (or an allowlisted test number)
    goal: str                   # member's goal, any language
    share: tuple[str, ...] = ()     # facts Fri may disclose (names, dates, party size)
    never: tuple[str, ...] = ()     # hard limits (pay, card numbers, accept fees)


@dataclass(frozen=True)
class CallBrief:
    """The engine's plan, shown to members before dialing."""
    request_id: str
    summary_en: str             # one paragraph members confirm
    opener_ja: str              # first thing Fri says, includes AI + listening/transcription notice
    share: tuple[str, ...]
    never: tuple[str, ...]


class CallState(enum.Enum):
    DIALING = 'dialing'
    RINGING = 'ringing'
    IN_PROGRESS = 'in_progress'
    ON_HOLD = 'on_hold'         # Fri asked the callee to wait while a question is open
    ENDED = 'ended'
    FAILED = 'failed'           # busy, no answer, rejected, error


# ---- engine -> Discord events -------------------------------------------------

@dataclass(frozen=True)
class StatusChanged:
    request_id: str
    state: CallState
    detail: str = ''            # short English, member-safe


@dataclass(frozen=True)
class Utterance:
    """One transcript line. Sent once when the source text is final.

    ``item_id`` is stable; a later ``Translation`` for the same id fills in English.
    """
    request_id: str
    item_id: str
    speaker: Speaker
    text_ja: str
    started_at: float           # seconds since the call was answered
    ended_at: float | None = None
    delivery: Literal['complete', 'interrupted', 'unknown'] = 'complete'
    consent_generation: int | None = None


@dataclass(frozen=True)
class Translation:
    request_id: str
    item_id: str
    text_en: str
    status: Literal['ready', 'unavailable'] = 'ready'
    consent_generation: int | None = None


@dataclass(frozen=True)
class QuestionAsked:
    """Fri needs the group. The engine is already holding the callee."""
    request_id: str
    question_id: str
    text_en: str
    options: tuple[str, ...] = ()   # quick-reply choices; free text is always allowed
    deadline_s: float = 60.0
    deadline_at: float | None = None  # Authoritative process-monotonic expiry.
    text_original: str = ''
    consent_generation: int | None = None


@dataclass(frozen=True)
class QuestionClosed:
    request_id: str
    question_id: str
    reason: Literal['answered', 'expired', 'call_ended']
    answer: str = ''


@dataclass(frozen=True)
class UsageLine:
    """One metered component. Costs are estimates from the price table unless exact=True."""
    component: str              # 'realtime', 'transcription', 'translation', 'summary', 'twilio', ...
    model: str                  # model id or 'twilio-voice'
    units: dict[str, float]     # e.g. {'audio_in_tokens': 1234, 'text_out_tokens': 56} or {'minutes': 3.2}
    usd: float | None           # None when unknown (e.g. Twilio price not posted yet)
    exact: bool = False


@dataclass(frozen=True)
class CallFinished:
    request_id: str
    outcome_en: str             # short result members need (confirmed or not, time, price, reference)
    duration_s: float
    usage: tuple[UsageLine, ...] = ()
    usage_final: bool = False   # False while late prices (Twilio) are still pending
    termination_confirmed: bool | None = None


@dataclass(frozen=True)
class UsageUpdated:
    """Late cost data after CallFinished (e.g. Twilio's posted price)."""
    request_id: str
    usage: tuple[UsageLine, ...]
    usage_final: bool


CallEvent = Union[StatusChanged, Utterance, Translation, QuestionAsked, QuestionClosed,
                  CallFinished, UsageUpdated]


# ---- interfaces --------------------------------------------------------------

class AudioSink(Protocol):
    """Implemented by the Discord side. Called by the engine from its event loop.

    ``push`` must not block. The engine pushes callee audio as it arrives from
    Twilio and Fri audio at the moment it forwards it to Twilio, so both legs
    stay aligned with what the phone hears.
    """
    def push(self, speaker: Speaker, pcm16: bytes) -> None: ...
    def clear(self, speaker: Speaker) -> None: ...   # drop unplayed audio (barge-in)
    def close(self) -> None: ...


class EventSink(Protocol):
    """Implemented by the Discord side. Must not raise into the engine."""
    async def emit(self, event: CallEvent) -> None: ...


class CallSession(Protocol):
    """Implemented by the engine, one per call."""
    request: CallRequest

    async def answer(self, question_id: str, answer: str, answered_by: int) -> bool:
        """First valid answer wins; returns False when closed, expired or unknown."""

    async def hang_up(self, reason: str = 'requested') -> None: ...


class CallEngine(Protocol):
    """Implemented by the engine. Entry points the Discord side calls."""

    async def plan(self, request: CallRequest) -> CallBrief:
        """Validate the request and draft the brief. Raises ``CallRejected``."""

    async def start(self, brief: CallBrief, request: CallRequest, audio: AudioSink,
                    events: EventSink) -> CallSession: ...


class CallRejected(Exception):
    """Member-safe reason a request can't be planned or started."""


@dataclass
class Limits:
    max_call_s: int = 15 * 60
    max_hold_s: int = 60
    hold_apology_every_s: int = 45
    max_calls_per_user_per_day: int = 5
    allowed_user_ids: frozenset[int] = field(default_factory=frozenset)   # empty = nobody
    test_numbers: frozenset[str] = field(default_factory=frozenset)       # non-+81 numbers allowed for testing
