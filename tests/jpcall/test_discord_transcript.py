import asyncio
from types import SimpleNamespace

import discord

from fri.jpcall.contract import (
    CallState,
    QuestionAsked,
    QuestionClosed,
    StatusChanged,
    Translation,
    Utterance,
)
from fri.jpcall.discord_transcript import TranscriptPager


class Messages:
    def __init__(self):
        self.contents, self.writes = [], []
        self.fail = False

    async def send(self, content):
        if self.fail:
            raise discord.HTTPException(SimpleNamespace(status=429, reason='rate limit'), 'retry later')
        self.contents.append(content)
        self.writes.append(('send', len(self.contents) - 1, content))
        return len(self.contents) - 1

    async def edit(self, message, content):
        if self.fail:
            raise discord.HTTPException(SimpleNamespace(status=503, reason='unavailable'), 'unavailable')
        self.contents[message] = content
        self.writes.append(('edit', message, content))


def test_late_translation_reflows_long_pages_without_losing_text():
    async def run():
        messages = Messages()
        pager = TranscriptPager(messages.send, messages.edit)
        for i in range(4):
            await pager.emit(Utterance('r', str(i), 'callee', f'line-{i}' + '日' * 800, i))
        await pager.flush()
        original = len(messages.contents)
        await pager.emit(Translation('r', '0', 'late english ' + '🦉' * 1300))
        await pager.close()
        assert len(messages.contents) > original
        assert all(len(text.encode('utf-16-le')) // 2 <= 2000 for text in messages.contents)
        text = ''.join(messages.contents)
        for i in range(4):
            assert text.count(f'line-{i}') == 1
        assert text.count('🦉') == 1300
        assert text.index('line-0') < text.index('late english') < text.index('line-1')
    asyncio.run(run())


def test_reordered_translation_status_and_question():
    async def run():
        messages = Messages()
        pager = TranscriptPager(messages.send, messages.edit)
        await pager.emit(Translation('r', 'x', 'hello'))
        await pager.emit(Utterance('r', 'x', 'fri', 'こんにちは', 0))
        await pager.emit(StatusChanged('r', CallState.ON_HOLD))
        await pager.emit(QuestionAsked('r', 'q', 'six or seven?', ('six', 'seven')))
        await pager.emit(QuestionClosed('r', 'q', 'answered', 'six'))
        await pager.close()
        text = ''.join(messages.contents)
        assert 'fri · こんにちは\n↳ hello' in text
        assert 'on hold' in text and 'six or seven?\nanswered · six' in text
    asyncio.run(run())


def test_coalesces_edits_and_flushes_at_end():
    async def run():
        messages = Messages()
        pager = TranscriptPager(messages.send, messages.edit, interval=0.1)
        await pager.emit(Utterance('r', '0', 'fri', 'first', 0))
        await asyncio.sleep(0)
        assert len(messages.writes) == 1
        for i in range(1, 10):
            await pager.emit(Utterance('r', str(i), 'fri', f'line-{i}', i))
        await asyncio.sleep(0.02)
        assert len(messages.writes) == 1
        await pager.close()
        assert len(messages.writes) == 2
        assert all(f'line-{i}' in messages.contents[0] for i in range(1, 10))
    asyncio.run(run())


def test_failed_writes_preserve_pending_lines_without_spinning():
    async def run():
        messages = Messages()
        messages.fail = True
        pager = TranscriptPager(messages.send, messages.edit)
        await pager.emit(Utterance('r', '0', 'callee', 'retained', 0))
        await asyncio.sleep(0)
        assert pager._task.done()
        messages.fail = False
        assert await pager.flush()
        messages.fail = True
        await pager.emit(Translation('r', '0', 'also retained'))
        assert not await pager.flush()
        messages.fail = False
        assert await pager.close()
        assert 'retained\n↳ also retained' in messages.contents[0]
    asyncio.run(run())


def test_flush_does_not_cancel_or_duplicate_an_inflight_send():
    async def run():
        messages = Messages()
        started, release = asyncio.Event(), asyncio.Event()

        async def send(content):
            started.set()
            await release.wait()
            return await messages.send(content)

        pager = TranscriptPager(send, messages.edit)
        await pager.emit(Utterance('r', '0', 'fri', 'one line', 0))
        await started.wait()
        closing = asyncio.create_task(pager.close())
        await asyncio.sleep(0)
        release.set()
        assert await closing
        assert len(messages.contents) == 1
        assert messages.contents[0].count('one line') == 1
    asyncio.run(run())


def test_call_language_original_and_english_have_neutral_labels():
    async def run():
        messages = Messages()
        pager = TranscriptPager(messages.send, messages.edit)
        await pager.emit(Utterance('r', 'ko', 'fri', '안녕하세요', 0))
        await pager.emit(Translation('r', 'ko', 'hello'))
        await pager.close()
        assert messages.contents == ['fri · 안녕하세요\n↳ hello']
    asyncio.run(run())
