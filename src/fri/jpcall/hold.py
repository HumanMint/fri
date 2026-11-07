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
                 timeline: DebugTimeline | None = None,
                 on_expired: Callable[[], Awaitable[None]] | None = None):
        self.request_id, self.limits = request_id, limits
        self.emit, self.say, self.tool_result, self.state = emit, say, tool_result, state
        self.clock, self.sleep = clock, sleep
        self.language = language
        self.timeline = timeline or DebugTimeline()
        self._on_expired = on_expired
        self.question: _Question | None = None
        self._timer: asyncio.Task | None = None
        self._ended = False
        self._speech: set[asyncio.Task] = set()
        self._resolving: set[asyncio.Task] = set()
        self._transitioning = False
        self._checkin: asyncio.Task | None = None
        self._callee_waiting = False

    async def ask(self, call_id: str, text_en: str, options: tuple[str, ...], *,
                  text_original: str = '') -> bool:
        if self._ended or self.question is not None or self._transitioning:
            return False
        now = self.clock()
        question = _Question(uuid.uuid4().hex, call_id, now + self.limits.max_hold_s,
                             now + self.limits.hold_apology_every_s)
        self.question = question
        self._callee_waiting = False
        self.timeline.record('hold_start', question_id=question.question_id)
        self._reset_timer(question)
        self._speak(self.language.hold)
        await self.state(True)
        if self.question is not question or self._ended:
            return False
        await self.emit(QuestionAsked(self.request_id, question.question_id, text_en,
                                      options, max(0, question.deadline - self.clock()),
                                      question.deadline, text_original))
        return True

    def _reset_timer(self, question: _Question) -> None:
        if self.question is not question or self._ended:
            return
        if self._timer is not None:
            self._timer.cancel()
        self._timer = asyncio.create_task(self._run(question))

    async def _run(self, question: _Question) -> None:
        try:
            while self.question is question:
                wake = (question.deadline if question.checked_in or self._checkin is not None
                        else min(question.deadline, question.next_apology))
                await self.sleep(max(0, wake - self.clock()))
                if self.question is not question:
                    return
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
            self._transitioning = True
            self.question = None
            try:
                await self._cancel_timer()
                await self._cancel_speech()
                await self.emit(QuestionClosed(self.request_id, question.question_id, 'expired'))
                if not self._ended:
                    await self.state(False)
                    if not self._ended:
                        if self._on_expired is not None:
                            await self._on_expired()
                        else:
                            await self.tool_result(question.call_id, {
                                'result': 'no answer from the group; offer to call back'})
            finally:
                self._transitioning = False
        elif (not question.checked_in and self._checkin is None
              and now >= question.next_apology):
            self.timeline.record('hold_check_in', question_id=question.question_id,
                                 source='timer')
            self._checkin = self._speak(self.language.apology, question=question)

    def callee_speech(self, started: bool, item_id: str) -> None:
        question = self.question
        if question is None:
            return
        question.next_apology = self.clock() + self.limits.hold_apology_every_s
        self._callee_waiting = True
        self._reset_timer(question)
        if self._checkin is not None:
            self._checkin.cancel()
            self._checkin = None

    def callee_response(self) -> None:
        """Called only after a completed Fri reply is confirmed played on the phone."""
        question = self.question
        if question is None or not (self._callee_waiting or self._checkin is not None):
            return
        question.checked_in = True
        self.timeline.record('hold_check_in', question_id=question.question_id,
                             source='delivered')
        if self._checkin is not None:
            self._checkin.cancel()
            self._checkin = None

    async def answer(self, question_id: str, answer: str, answered_by: int) -> bool:
        question = self.question
        if (self._ended or question is None or question.question_id != question_id
                or not answer.strip() or answered_by <= 0):
            return False
        if self.clock() >= question.deadline:
            await self.tick()
            return False
        self._transitioning = True
        self.question = None  # First answer wins before display or model work awaits.
        self.timeline.record('hold_answer', question_id=question_id, answer=answer.strip())

        async def resolve():
            try:
                await self._cancel_timer()
                await self._cancel_speech()
                await self.emit(QuestionClosed(self.request_id, question_id, 'answered',
                                               answer.strip()))
                if not self._ended:
                    await self.state(False)
                    if not self._ended:
                        await self.tool_result(question.call_id, {'answer': answer.strip()})
            finally:
                self._transitioning = False

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

    def _speak(self, text: str, *, question: _Question | None = None) -> asyncio.Task:
        async def speak():
            generated = await self.say(text, out_of_band=True)
            if isinstance(generated, asyncio.Future):
                return await generated

        task = asyncio.create_task(speak())
        self._speech.add(task)

        def done(result):
            self._speech.discard(result)
            failed = result.cancelled() or result.exception() is not None
            if not result.cancelled() and result.exception() is not None:
                LOG.warning('jpcall hold speech unavailable')
            if (question is self.question and self._checkin is result
                    and (failed or result.result() != 'completed')):
                self._checkin = None
                question.next_apology = self.clock() + self.limits.hold_apology_every_s
                self._reset_timer(question)
        task.add_done_callback(done)
        return task

    async def _cancel_speech(self) -> None:
        tasks = tuple(self._speech)
        self._checkin = None
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
        resolving = tuple(task for task in self._resolving if task is not asyncio.current_task())
        for task in resolving:
            task.cancel()
        await asyncio.gather(*resolving, return_exceptions=True)
        if question:
            await self.emit(QuestionClosed(self.request_id, question.question_id, 'call_ended'))
