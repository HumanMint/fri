import asyncio

from fri.jpcall.contract import Limits, QuestionClosed
from fri.jpcall.hold import HoldController


def test_hold_deadline_survives_a_stalled_apology():
    async def run():
        now = [0]
        events, results = [], []
        speech_gate = asyncio.Event()

        async def emit(event):
            events.append(event)

        async def say(*args, **kwargs):
            await speech_gate.wait()

        async def result(call_id, data):
            results.append(data)

        async def state(held):
            pass

        hold = HoldController('request1', Limits(), emit, say, result, state, clock=lambda: now[0])
        try:
            assert await hold.ask('tool1', 'Proceed?', ())
            now[0] = 45
            await hold.tick()
            now[0] = 60
            await hold.tick()
            assert hold.question is None
            assert next(e for e in events if isinstance(e, QuestionClosed)).reason == 'expired'
            assert results == [{'result': 'no answer from the group; offer to call back'}]
        finally:
            await hold.close()
    asyncio.run(run())


def test_canceled_winning_answer_still_reaches_model_once():
    async def run():
        events, results = [], []
        entered, release = asyncio.Event(), asyncio.Event()

        async def emit(event):
            events.append(event)

        async def say(*args, **kwargs):
            pass

        async def result(call_id, data):
            entered.set()
            await release.wait()
            results.append(data)

        async def state(held):
            pass

        hold = HoldController('request1', Limits(), emit, say, result, state)
        try:
            await hold.ask('tool1', 'Proceed?', ())
            question_id = hold.question.question_id
            answer = asyncio.create_task(hold.answer(question_id, 'yes', 12))
            await entered.wait()
            answer.cancel()
            try:
                await answer
            except asyncio.CancelledError:
                pass
            assert not await hold.answer(question_id, 'no', 34)
            release.set()
            for _ in range(20):
                if results:
                    break
                await asyncio.sleep(0)
            assert results == [{'answer': 'yes'}]
            assert sum(isinstance(e, QuestionClosed) for e in events) == 1
        finally:
            await hold.close()
    asyncio.run(run())
