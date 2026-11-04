"""In-memory transcript pages with coalesced, recoverable Discord writes."""

import asyncio
import logging
import time
from dataclasses import dataclass

import discord

from .contract import QuestionAsked, QuestionClosed, StatusChanged, Translation, Utterance
from .discord_text import chunks

LOG = logging.getLogger(__name__)


@dataclass
class Page:
    message: object
    content: str
    edited_at: float


class TranscriptPager:
    """send(content) -> message and edit(message, content) are async callables.

    Late translations reflow the same ordered transcript, including older pages.
    A failed HTTP write retains all text and retries on the next event or flush,
    never in a tight retry loop. Nothing is persisted outside Discord.
    """

    def __init__(self, send, edit, *, interval=1.5, clock=time.monotonic):
        self.send, self.edit = send, edit
        self.interval, self.clock = interval, clock
        self._rows = []
        self._items = {}
        self._translations = {}
        self._questions = {}
        self.pages = []
        self._revision = 0
        self._task = None
        self._lock = asyncio.Lock()
        self._flush_requested = asyncio.Event()

    async def emit(self, event):
        if isinstance(event, Utterance):
            if event.item_id in self._items:
                return
            row = [f'{event.speaker} · {event.text_ja}', '']
            self._items[event.item_id] = row
            if event.item_id in self._translations:
                row[1] = f'\n↳ {self._translations.pop(event.item_id)}'
            self._rows.append(row)
        elif isinstance(event, Translation):
            if event.item_id in self._items:
                self._items[event.item_id][1] = f'\n↳ {event.text_en}'
            else:
                self._translations[event.item_id] = event.text_en
                return
        elif isinstance(event, StatusChanged):
            detail = f' · {event.detail}' if event.detail else ''
            self._rows.append([f'call · {event.state.value.replace("_", " ")}{detail}'])
        elif isinstance(event, QuestionAsked):
            row = [f'group · {event.text_en}']
            self._questions[event.question_id] = row
            self._rows.append(row)
        elif isinstance(event, QuestionClosed):
            row = self._questions.get(event.question_id)
            if row is not None:
                answer = f' · {event.answer}' if event.answer else ''
                row.append(f'\n{event.reason.replace("_", " ")}{answer}')
        else:
            return
        self._revision += 1
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._drain())

    def _contents(self):
        # Join complete lines where possible; split unusually long lines without loss.
        pages, current = [], ''
        for row in self._rows:
            text = ''.join(row)
            combined = f'{current}\n\n{text}' if current else text
            if len(chunks(combined)) <= 1:
                current = combined
                continue
            if current:
                pages.append(current)
            parts = chunks(text)
            pages.extend(parts[:-1])
            current = parts[-1] if parts else ''
        if current:
            pages.append(current)
        return pages

    async def _publish(self, force=False):
        async with self._lock:
            contents = self._contents()
            # Reflow may reduce page count; remove stale duplicate text by editing it.
            contents += ['transcript continued above.'] * (len(self.pages) - len(contents))
            try:
                for index, content in enumerate(contents):
                    if index == len(self.pages):
                        message = await self.send(content)
                        self.pages.append(Page(message, content, self.clock()))
                    elif self.pages[index].content != content:
                        page = self.pages[index]
                        wait = self.interval - (self.clock() - page.edited_at)
                        if not force and wait > 0:
                            try:
                                await asyncio.wait_for(self._flush_requested.wait(), wait)
                            except TimeoutError:
                                pass
                        await self.edit(page.message, content)
                        page.content, page.edited_at = content, self.clock()
            except discord.HTTPException as exc:
                LOG.warning('jpcall transcript write failed: HTTP %s', exc.status)
                return False
            return True

    async def _drain(self):
        while True:
            revision = self._revision
            if not await self._publish() or revision == self._revision:
                return

    async def flush(self):
        # Wake a throttle wait without cancelling an in-flight HTTP write. Cancelling
        # send() after Discord accepted it could orphan a message and duplicate text.
        self._flush_requested.set()
        try:
            if self._task is not None:
                await self._task
            return await self._publish(force=True)
        finally:
            self._flush_requested.clear()

    async def close(self):
        return await self.flush()
