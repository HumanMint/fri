"""App-owned hold deadlines. Closing a question takes effect before any await."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .config import CALL_LANGUAGES, CallLanguage
from .contract import Limits, QuestionAsked, QuestionClosed
from .timeline import DebugTimeline

LOG = logging.getLogger(__name__)


@dataclass
class _Question:
    question_id: str
    call_id: str
    deadline: float
    next_apology: float
    checked_in: bool = False


class HoldController:
    def __init__(self, request_id: str, limits: Limits, emit, say, tool_result, state,
                 *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 language: CallLanguage = CALL_LANGUAGES['ja'],
                 timeline: DebugTimeline | None = None):
        self.request_id, self.limits = request_id, limits
        self.emit, self.say, self.tool_result, self.state = emit, say, tool_result, state
        self.clock, self.sleep = clock, sleep
        self.language = language
        self.timeline = timeline or DebugTimeline()
        self.question: _Question | None = None
        self._timer: asyncio.Task | None = None
        self._ended = False
        self._speech: set[asyncio.Task] = set()
        self._resolving: set[asyncio.Task] = set()
        self._checkin: asyncio.Task | None = None

    async def ask(self, call_id: str, text_en: str, options: tuple[str, ...]) -> bool:
        if self._ended or self.question is not None:
            return False
        now = self.clock()
        question = _Question(uuid.uuid4().hex, call_id, now + self.limits.max_hold_s,
                             now + self.limits.hold_apology_every_s)
        self.question = question
        self.timeline.record('hold_start', question_id=question.question_id)
        self._speak(self.language.hold)
        if self.question is not question or self._ended:
            return False
        await self.state(True)
        if self.question is not question or self._ended:
            return False
        await self.emit(QuestionAsked(self.request_id, question.question_id, text_en,
                                      options, max(0, question.deadline - self.clock())))
        if self.question is question:
            self._timer = asyncio.create_task(self._run(question))
        return True

    async def _run(self, question: _Question) -> None:
        try:
            while self.question is question:
                wake = (question.deadline if question.checked_in
                        else min(question.deadline, question.next_apology))
                delay = max(0, wake - self.clock())
                await self.sleep(delay)
                try:
                    await self.tick()
                except Exception:
                    LOG.warning('jpcall hold action failed')
        except asyncio.CancelledError:
            raise

    async def tick(self) -> None:
        question = self.question
        if question is None or self._ended:
            return
        now = self.clock()
        if now >= question.deadline:
            self.question = None
            await self._cancel_speech()
            await self.emit(QuestionClosed(self.request_id, question.question_id, 'expired'))
            if not self._ended:
                await self.state(False)
                await self.tool_result(question.call_id, {
                    'result': 'no answer from the group; offer to call back'})
        elif not question.checked_in and now >= question.next_apology:
            question.checked_in = True  # At most one proactive check-in per question.
            self.timeline.record('hold_check_in', question_id=question.question_id,
                                 source='timer')
            self._checkin = self._speak(self.language.apology)

    def callee_speech(self, started: bool, item_id: str) -> None:
        question = self.question
        if question is None:
            return
        question.next_apology = self.clock() + self.limits.hold_apology_every_s
        if not question.checked_in:
            self.timeline.record('hold_check_in', question_id=question.question_id,
                                 source='callee', item_id=item_id)
        question.checked_in = True  # A natural answer replaces the proactive check-in.
        if self._checkin is not None:
            self._checkin.cancel()  # Also cancel one still queued behind another response.

    async def answer(self, question_id: str, answer: str, answered_by: int) -> bool:
        question = self.question
        if (self._ended or question is None or question.question_id != question_id
                or not answer.strip() or answered_by <= 0):
            return False
        if self.clock() >= question.deadline:
            await self.tick()
            return False
        self.question = None  # First answer wins, including while emit/tool_result are awaiting.
        self.timeline.record('hold_answer', question_id=question_id, answer=answer.strip())
        await self.emit(QuestionClosed(self.request_id, question_id, 'answered', answer.strip()))

        async def resolve():
            await self._cancel_timer()
            await self._cancel_speech()
            if not self._ended:
                await self.state(False)
                await self.tool_result(question.call_id, {'answer': answer.strip()})

        task = asyncio.create_task(resolve())
        self._resolving.add(task)

        def done(result):
            self._resolving.discard(result)
            if not result.cancelled() and result.exception() is not None:
                LOG.warning('jpcall group answer delivery unavailable')
        task.add_done_callback(done)
        # A canceled Discord interaction must not abandon its winning answer.
        await asyncio.shield(task)
        return True

    def _speak(self, text: str) -> asyncio.Task:
        task = asyncio.create_task(self.say(text, out_of_band=True))
        self._speech.add(task)

        def done(result):
            self._speech.discard(result)
            if not result.cancelled() and result.exception() is not None:
                LOG.warning('jpcall hold speech unavailable')
        task.add_done_callback(done)
        return task

    async def _cancel_speech(self) -> None:
        tasks = tuple(self._speech)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _cancel_timer(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)

    async def close(self) -> None:
        self._ended = True
        question, self.question = self.question, None
        await self._cancel_timer()
        await self._cancel_speech()
        resolving = tuple(self._resolving)
        for task in resolving:
            task.cancel()
        await asyncio.gather(*resolving, return_exceptions=True)
        if question:
            await self.emit(QuestionClosed(self.request_id, question.question_id, 'call_ended'))
