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


def test_new_ask_is_rejected_while_winning_answer_is_resolving():
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        now = [0]
        events = []

        async def emit(event):
            events.append(event)
            if isinstance(event, QuestionClosed) and event.reason == 'answered':
                entered.set()
                await release.wait()

        async def noop(*args, **kwargs):
            pass

        hold = HoldController('request1', Limits(), emit, noop, noop, noop, clock=lambda: now[0])
        try:
            await hold.ask('tool1', 'First?', ())
            answer = asyncio.create_task(hold.answer(hold.question.question_id, 'yes', 12))
            await entered.wait()
            assert not await hold.ask('tool2', 'Second?', ())
            release.set()
            assert await answer
            assert await hold.ask('tool2', 'Second?', ())
            timer = hold._timer
            now[0] = 60
            await hold.tick()
            assert hold.question is None and timer.cancelled()
            assert sum(isinstance(e, QuestionClosed) and e.reason == 'expired' for e in events) == 1
        finally:
            release.set()
            await hold.close()
    asyncio.run(run())


def test_deadline_is_absolute_and_timer_runs_while_display_is_stalled():
    async def run():
        from fri.jpcall.contract import QuestionAsked

        now = [100]
        events = []
        entered, release = asyncio.Event(), asyncio.Event()

        async def emit(event):
            events.append(event)
            if isinstance(event, QuestionAsked):
                entered.set()
                await release.wait()

        async def noop(*args, **kwargs):
            pass

        hold = HoldController('request1', Limits(), emit, noop, noop, noop, clock=lambda: now[0])
        ask = asyncio.create_task(hold.ask('tool1', 'Which time?', (), text_original='何時ですか？'))
        try:
            await entered.wait()
            question = next(e for e in events if isinstance(e, QuestionAsked))
            assert question.deadline_at == 160 and question.text_original == '何時ですか？'
            assert hold._timer is not None
            now[0] = 161
            await hold.tick()
            assert hold.question is None
            assert not await hold.answer(question.question_id, 'noon', 12)
            release.set()
            await ask
        finally:
            release.set()
            await hold.close()
    asyncio.run(run())


def test_noise_postpones_checkin_but_only_delivered_reply_consumes_it():
    async def run():
        now = [0]
        spoken = []
        pending = []

        async def noop(*args, **kwargs):
            pass

        async def say(text, **kwargs):
            spoken.append(text)
            generated = asyncio.get_running_loop().create_future()
            pending.append(generated)
            return generated

        hold = HoldController('request1', Limits(max_hold_s=180), noop, say, noop, noop,
                              clock=lambda: now[0])
        try:
            await hold.ask('tool1', 'When?', ())
            await asyncio.sleep(0)
            now[0] = 44
            hold.callee_speech(True, 'noise')
            now[0] = 46
            hold.callee_speech(False, 'noise')
            assert not hold.question.checked_in and hold.question.next_apology == 91
            now[0] = 91
            await hold.tick()
            await asyncio.sleep(0)
            assert spoken == [hold.language.hold, hold.language.apology]
            pending[-1].set_result('completed')
            await asyncio.sleep(0)
            assert not hold.question.checked_in  # Generation is not playback.
            hold.callee_response()
            assert hold.question.checked_in
            now[0] = 140
            await hold.tick()
            assert spoken == [hold.language.hold, hold.language.apology]
        finally:
            await hold.close()
    asyncio.run(run())


def test_failed_checkin_can_retry_without_consuming_delivery():
    async def run():
        now = [0]
        spoken = []

        async def noop(*args, **kwargs):
            pass

        async def say(text, **kwargs):
            spoken.append(text)
            done = asyncio.get_running_loop().create_future()
            done.set_result('failed')
            return done

        hold = HoldController('request1', Limits(max_hold_s=180), noop, say, noop, noop,
                              clock=lambda: now[0])
        try:
            await hold.ask('tool1', 'When?', ())
            now[0] = 45
            await hold.tick()
            for _ in range(5):
                await asyncio.sleep(0)
            assert not hold.question.checked_in and hold._checkin is None
            now[0] = 90
            await hold.tick()
            await asyncio.sleep(0)
            assert spoken.count(hold.language.apology) == 2
        finally:
            await hold.close()
    asyncio.run(run())


def test_expiry_hook_ends_instead_of_restarting_model_inquiry():
    async def run():
        now = [0]
        expired, results = [], []

        async def noop(*args, **kwargs):
            pass

        async def result(*args):
            results.append(args)

        async def on_expired():
            expired.append(True)

        hold = HoldController('request1', Limits(), noop, noop, result, noop,
                              clock=lambda: now[0], on_expired=on_expired)
        try:
            await hold.ask('tool1', 'When?', ())
            now[0] = 60
            await hold.tick()
            await hold.tick()
            assert expired == [True] and not results
        finally:
            await hold.close()
    asyncio.run(run())


def test_question_keeps_supplied_consent_before_delayed_state_publication():
    async def run():
        from fri.jpcall.contract import QuestionAsked

        events = []
        entered, release = asyncio.Event(), asyncio.Event()

        async def emit(event):
            events.append(event)

        async def state(held):
            if held:
                entered.set()
                await release.wait()

        async def noop(*args, **kwargs):
            pass

        hold = HoldController('request1', Limits(), emit, noop, noop, state)
        question = asyncio.create_task(hold.ask('tool1', 'When?', (), consent_generation=1))
        try:
            await entered.wait()
            release.set()  # Parent may now be in generation 3; the captured value stays 1.
            await question
            assert next(e for e in events if isinstance(e, QuestionAsked)).consent_generation == 1
        finally:
            release.set()
            await hold.close()
    asyncio.run(run())
