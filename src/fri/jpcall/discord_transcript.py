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

    def __init__(self, send, edit, *, interval=1.5, clock=time.monotonic, allowed=None):
        self.send, self.edit = send, edit
        self.interval, self.clock = interval, clock
        self.allowed = allowed
        self._generations = {}
        self._committed = {}
        self._times = {}
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
        protected = isinstance(event, (Utterance, Translation, QuestionAsked))
        generation = event.consent_generation if protected else None
        if protected and (generation is None or (self.allowed is not None
                                                 and not self.allowed(generation))):
            return
        if isinstance(event, Utterance):
            if event.item_id in self._items:
                return
            uncertain = (' · interrupted; wording uncertain' if event.delivery == 'interrupted'
                         else ' · delivery unknown' if event.delivery == 'unknown' else '')
            row = [f'{event.speaker}{uncertain} · {event.text_ja}', '\n↳ translation pending']
            self._items[event.item_id] = row
            if event.item_id in self._translations:
                row[1] = self._translations.pop(event.item_id)
            self._generations[id(row)] = generation
            self._times[id(row)] = event.started_at
            # Reinsert only speech rows: progress and group questions keep their anchors.
            speech = sorted([*self._items.values()], key=lambda item: self._times[id(item)])
            self._rows.append(row)
            ordered = iter(speech)
            self._rows = [next(ordered) if id(item) in self._times else item
                          for item in self._rows]
        elif isinstance(event, Translation):
            english = ('\n↳ translation unavailable' if event.status == 'unavailable'
                       or not event.text_en.strip() else f'\n↳ {event.text_en}')
            if event.item_id in self._items:
                self._items[event.item_id][1] = english
            else:
                self._translations[event.item_id] = english
                return
        elif isinstance(event, StatusChanged):
            detail = f' · {event.detail}' if event.detail else ''
            self._rows.append([f'call · {event.state.value.replace("_", " ")}{detail}'])
        elif isinstance(event, QuestionAsked):
            text = (f'{event.text_original}\n↳ {event.text_en}'
                    if event.text_original else event.text_en)
            row = [f'group · {text}']
            self._generations[id(row)] = generation
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
            generation = self._generations.get(id(row))
            permitted = (generation is None or self.allowed is None or self.allowed(generation))
            text = ''.join(row) if permitted else self._committed.get(id(row), '')
            if not text:
                continue
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
            index = 0
            revision = self._revision
            try:
                while True:
                    contents = self._contents()
                    contents += ['transcript continued above.'] * (len(self.pages) - len(contents))
                    if index >= len(contents):
                        break
                    content = contents[index]
                    if index < len(self.pages):
                        page = self.pages[index]
                        if page.content == content:
                            index += 1
                            continue
                        wait = self.interval - (self.clock() - page.edited_at)
                        if not force and wait > 0:
                            try:
                                await asyncio.wait_for(self._flush_requested.wait(), wait)
                            except TimeoutError:
                                pass
                    # Consent can change during the throttle or a previous page's HTTP await.
                    current = self._contents()
                    current += ['transcript continued above.'] * (len(self.pages) - len(current))
                    if current != contents:
                        index = 0
                        continue
                    if index == len(self.pages):
                        message = await self.send(content)
                        self.pages.append(Page(message, content, self.clock()))
                    else:
                        await self.edit(page.message, content)
                        page.content, page.edited_at = content, self.clock()
                    index += 1
            except discord.HTTPException as exc:
                LOG.warning('jpcall transcript write failed: HTTP %s', exc.status)
                return False
            if revision == self._revision:
                for row in self._rows:
                    generation = self._generations.get(id(row))
                    if generation is None or self.allowed is None or self.allowed(generation):
                        self._committed[id(row)] = ''.join(row)
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
