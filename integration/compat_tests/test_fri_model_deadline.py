"""A live SSE connection must not monopolize both conversation slots."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from compat_tests.test_fri_chat import CONTEXT, response_event
from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_timing import record_typing
from fri.fri import Fri
from fri.fri_chat import Chat


class Stream(httpx.AsyncByteStream):
    def __init__(self, *, finish=False):
        self.finish = finish
        self.started = asyncio.Event()
        self.closed = False
        self.cancelled = False

    async def __aiter__(self):
        self.started.set()
        try:
            # Even valid-looking partial output must never be delivered before completion.
            yield b'data: {"type":"response.output_text.delta","delta":"unchecked"}\n\n'
            if self.finish:
                for _ in range(3):
                    await asyncio.sleep(0.02)
                    yield b': keepalive\n\n'
                event = response_event(content=json.dumps({'reply': 'finished', 'request_draft': None}))
                yield ('data: '+json.dumps(event)+'\n\n').encode()
            else:
                while True:
                    await asyncio.sleep(0.01)
                    yield b': keepalive\n\n'
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def aclose(self):
        self.closed = True


def test_endless_streams_release_both_slots_and_serve_next_member(monkeypatch, tmp_path):
    monkeypatch.setenv('FRI_DATA', str(tmp_path))
    monkeypatch.setattr('fri.fri_chat.MODEL_DEADLINE', 0.15, raising=False)

    async def scenario():
        fri, agent, _, _, channel, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        fri.chat_once = Fri.chat_once.__get__(fri)
        streams = [Stream(), Stream(), Stream(finish=True)]
        waiting = iter(streams)
        fri.chat = Chat(SimpleNamespace(access_token=lambda: 'synthetic'),
                        httpx.MockTransport(lambda _: httpx.Response(200, stream=next(waiting))))
        log = []
        record_typing(channel, log)
        events = [message('<@99> explain this please', user=user) for user in (20, 21, 22)]
        tasks = []
        try:
            tasks = [asyncio.create_task(agent.reply_once(event, event.content, addressed=True)) for event in events[:2]]
            await asyncio.wait_for(asyncio.gather(*(s.started.wait() for s in streams[:2])), 1)
            assert fri.chat_slots.locked() and fri.busy == {20, 21}
            done, pending = await asyncio.wait(tasks, timeout=1)
            assert not pending, 'both slots remain held beyond the model deadline'
            for task in done:
                task.result()
            assert fri.chat_slots._value == 2 and not fri.busy and not fri.usage_lock.locked()
            assert all(s.closed and s.cancelled for s in streams[:2])
            assert log.count('typing') == log.count('stopped') == 2
            for event in events[:2]:
                event.reply.assert_awaited_once()
                assert 'connection ended' in event.reply.await_args.args[0]
                assert 'unchecked' not in event.reply.await_args.args[0]
            assert not any(path in ('/chat/turn', '/chat/pending') for path, _ in calls)
            assert not agent.saved_turns.saved
            await asyncio.wait_for(agent.reply_once(events[2], events[2].content, addressed=True), 1)
            events[2].reply.assert_awaited_once()
            assert events[2].reply.await_args.args[0] == 'finished'
            assert fri.chat_slots._value == 2 and not fri.busy and not fri.usage_lock.locked()
            assert log.count('typing') == log.count('stopped') == 3
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await fri.close()
    asyncio.run(scenario())


def test_slow_finishing_stream_is_unchanged(monkeypatch):
    monkeypatch.setattr('fri.fri_chat.MODEL_DEADLINE', 0.5, raising=False)

    async def scenario():
        stream = Stream(finish=True)
        chat = Chat(SimpleNamespace(access_token=lambda: 'synthetic'),
                    httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)))
        result = await chat.respond('explain this please', CONTEXT)
        assert result.status == 'completed' and result.reply == 'finished' and not result.error
        assert result.counters['input_tokens'] == 100
        assert stream.closed and not stream.cancelled
    asyncio.run(scenario())


@pytest.mark.parametrize('stage', ['connect', 'read'])
def test_deadline_covers_waiting_for_headers_and_silent_read(monkeypatch, stage):
    monkeypatch.setattr('fri.fri_chat.MODEL_DEADLINE', 0.02, raising=False)

    async def scenario():
        class Silent(Stream):
            async def __aiter__(self):
                await asyncio.Event().wait()
                yield b''

        stream = Silent()

        async def handler(_):
            if stage == 'connect':
                await asyncio.Event().wait()
            return httpx.Response(200, stream=stream)

        chat = Chat(SimpleNamespace(access_token=lambda: 'synthetic'), httpx.MockTransport(handler))
        result = await asyncio.wait_for(chat.respond('explain this please', CONTEXT), 1)
        assert result.status != 'completed' and result.error and not result.reply
        if stage == 'read':
            assert stream.closed
    asyncio.run(scenario())
