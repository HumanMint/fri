"""fri #216 and fri #217: a turn depends on every summary what it carries drew on, and only on those.

1. A member's own saved history: an exchange whose reply depended on another member's summary (or their own) comes
   back from a controller offering saved-dependencies with those summaries, and a turn reading it depends on them in
   turn: its checks and save carry them, Gene retires its reply once one goes, and its echo is rechecked.
2. A tool's writes: the step's check runs for a turn that depends only on other summaries, and a write of the model's
   words carries them, so Gene refuses it once one goes, inside the write, and retires it if one goes later.
3. A turn's check before its request is refused for a dependency its request won't carry (an echo deleted and its
   summary withdrawn while the check waited): it's asked once more without what left the view.

Against the real controller; one without saved-dependencies runs Fri as before.
"""

from unittest.mock import AsyncMock

import pytest

from compat_tests.test_api import headers
from compat_tests.test_fri_member_profiles import DM, GENERAL, model, run, world
from compat_tests.test_fri_member_summaries import CONTINUATION, SUMMARY
from compat_tests.test_fri_summary_dependencies import (
    CHECK,
    REPEATED,
    TURN,
    generation,
    repeats,
    saved_replies,
    sent,
)
from compat_tests.test_fri_summary_echoes import echoed, later, recalls
from compat_tests.test_fri_summary_generation_tools import (
    ASKS,
    TABLES,
    WRITE_PATHS,
    action,
    asked,
    bodies,
    dm_channel,
    kept,
    reply,
)
from compat_tests.test_fri_summary_generations import deleted, summarized_from, withdrawal
from fri import feedback as commands
from fri.fri_chat import ChatResult

SAVED = 'saved-dependencies'
AGAIN = 'like i said: '+CONTINUATION
SAID_BEFORE = 'they said: '+SUMMARY


async def from_history(text, context):
    """The model repeats Fri's earlier answer from the member's own saved history."""
    if any(CONTINUATION in turn.get('reply', '') for turn in context.get('turns', [])):
        return ChatResult(status='completed', reply=AGAIN)
    return await repeats(text, context)


async def repeated_then_cleared(monkeypatch, w):
    """A's summary shapes an echo, B's reply repeats it, then the live view moves on: B's saved history keeps it."""
    events = await summarized_from(monkeypatch, w)
    model(w, recalls)
    assert await echoed(w) == [CONTINUATION]
    model(w, repeats)
    assert REPEATED in await echoed(w, 'fri what did they say', user=30)
    w.fri.agent.recent[GENERAL].clear()  # Past 15 minutes; Fri's marks outlive it, but nothing here carries them.
    return events, [{'user_id': '20', 'generation': generation(w)}]


def test_a_reply_repeating_its_own_dependent_history_goes_with_that_summary(monkeypatch, tmp_path):
    """Item 1's repro: B's next turn repeats that answer from B's saved history. A's source is withdrawn, then C's turn:
    B's second reply goes with A's summary, and its echo never reaches C's request."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            events, depends = await repeated_then_cleared(monkeypatch, w)
            model(w, from_history)
            before = len(w.calls)
            assert AGAIN in await echoed(w, 'fri say that again', user=30)
            b = sent(w, TURN, CHECK, since=before)
            asked = [body for path, body in w.calls[before:] if path == '/chat/context']
            await deleted(w, events[0])
            carried = later(w)
            await w.say(GENERAL, 'fri what is going on', user=31)
            assert carried and CONTINUATION not in str(carried)
            assert AGAIN not in saved_replies(w, 30)
            assert b and all(body.get('summary_dependencies') == depends for body in b)  # B's checks and save.
            assert any(body.get('dependencies') for body in asked)  # The turn's own context read.
        finally:
            await w.fri.close()
    run(scenario)


def test_a_history_dependency_withdrawn_while_the_model_writes_keeps_the_answer_unsaid(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            events, _ = await repeated_then_cleared(monkeypatch, w)

            async def respond(text, context):
                assert REPEATED in str(context['turns'])
                await deleted(w, events[0])
                return await from_history(text, context)
            model(w, respond)
            turns = len(sent(w, TURN))
            with pytest.raises(commands.UserFeedbackCheckFailure):
                await w.say(GENERAL, 'fri say that again', user=30)
            assert len(sent(w, TURN)) == turns and AGAIN not in saved_replies(w, 30)
        finally:
            await w.fri.close()
    run(scenario)


def test_history_the_model_never_reads_adds_no_dependency(monkeypatch, tmp_path):
    """An exchange withheld before the request (its message taken back, fri #215) isn't carried, so doesn't count."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await repeated_then_cleared(monkeypatch, w)
            [mid] = [str(body['source']['id']) for body in sent(w, TURN) if body['user_id'] == '30']
            w.fri.agent.saved_turns.withheld = lambda found: {mid} & set(found)

            async def plain(text, context):
                assert REPEATED not in str(context['turns'])
                return ChatResult(status='completed', reply='no idea')
            model(w, plain)
            before = len(w.calls)
            await echoed(w, 'fri say that again', user=30)
            assert not [body for body in sent(w, TURN, CHECK, since=before) if 'summary_dependencies' in body]
        finally:
            await w.fri.close()
    run(scenario)


def test_without_saved_dependencies_fri_asks_and_sends_nothing_new(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        w.fri.capabilities -= {SAVED}
        try:
            await repeated_then_cleared(monkeypatch, w)
            model(w, from_history)
            before = len(w.calls)
            await echoed(w, 'fri say that again', user=30)
            assert not [body for body in sent(w, TURN, CHECK, since=before) if 'summary_dependencies' in body]
            assert not [body for path, body in w.calls if path == '/chat/context' and 'dependencies' in body]
        finally:
            await w.fri.close()
    run(scenario)


def dependent_history(w, user=30):
    """An earlier exchange in the member's DM whose reply depended on member 20's summary, as Fri saves one."""
    event = w.message(DM, 'fri what did they say', user=user, age=60)
    w.store.save_chat('123', f'private:{DM}', str(user), 0, event.content, SAID_BEFORE, 'model', 'high',
                      source={'kind': 'discord_message', 'id': str(event.id), 'user_id': str(user),
                              'channel_id': str(DM), 'guild_id': None, 'display_name': f'member {user}'},
                      provenance=[{'user_id': '20', 'revision': 0}], reply_message_id=str(event.id+1), verbatim=True,
                      summary_dependencies=[{'user_id': '20', 'generation': generation(w)}])


@pytest.mark.parametrize('tool', TABLES)
def test_a_write_chosen_while_a_dependency_was_withdrawn_keeps_nothing(monkeypatch, tmp_path, tool):
    """Item 2's repro: B's turn depends on A's summary only, through B's history. The source goes while the model writes
    the step: the step's check refuses it, though B has no summary of their own."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        dm_channel(w)
        try:
            events = await summarized_from(monkeypatch, w)
            dependent_history(w)

            async def choose(context):
                assert SUMMARY in str(context['turns']) and not context.get('member_summary')
                await deleted(w, events[0])
                return ChatResult(status='completed', action=action(tool))
            result, saved, _ = await asked(w, ASKS[tool], [choose, reply('done, '+SUMMARY)], user=30)
            assert kept(w, TABLES[tool]) == []
            assert SUMMARY not in result.reply and not saved
        finally:
            await w.fri.close()
    run(scenario)


@pytest.mark.parametrize('tool', TABLES)
def test_a_dependency_withdrawn_during_the_writes_own_wait_is_refused_by_the_write(monkeypatch, tmp_path, tool):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        dm_channel(w)
        try:
            events = await summarized_from(monkeypatch, w)
            dependent_history(w)
            depends = [{'user_id': '20', 'generation': generation(w)}]
            event = w.message(DM, ASKS[tool], user=30)

            async def reread(*_):
                await deleted(w, events[0])
                return event
            w.channels[DM].fetch_message = AsyncMock(side_effect=reread)

            async def choose(context):
                return ChatResult(status='completed', action=action(tool))
            model(w, lambda _, context: (choose if 'action_result' not in context else reply('done'))(context))
            _, saved, _ = await w.say(DM, ASKS[tool], event=event, user=30)
            assert kept(w, TABLES[tool]) == [] and not saved
            assert bodies(w, WRITE_PATHS[tool])[-1]['summary_dependencies'] == depends
        finally:
            await w.fri.close()
    run(scenario)


@pytest.mark.parametrize('tool', TABLES)
def test_a_write_kept_while_current_goes_with_the_summary_it_depends_on(monkeypatch, tmp_path, tool):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        dm_channel(w)
        try:
            events = await summarized_from(monkeypatch, w)
            dependent_history(w)

            async def choose(context):
                return ChatResult(status='completed', action=action(tool))
            await asked(w, ASKS[tool], [choose, reply('done')], user=30)
            assert len(kept(w, TABLES[tool])) == 1 and SUMMARY in str(kept(w, TABLES[tool]))
            await deleted(w, events[0])
            assert len(kept(w, TABLES[tool])) == 1 and SUMMARY not in str(kept(w, TABLES[tool]))
        finally:
            await w.fri.close()
    run(scenario)


def test_an_older_controller_gets_no_dependencies_on_writes(monkeypatch, tmp_path):
    """Without saved-dependencies, its write models refuse the field: none is sent, and the write saves as before."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        dm_channel(w)
        w.fri.capabilities -= {SAVED}
        try:
            await summarized_from(monkeypatch, w)
            dependent_history(w)

            async def choose(context):
                return ChatResult(status='completed', action=action('work_save'))
            await asked(w, ASKS['work_save'], [choose, reply('done')], user=30)
            assert len(kept(w, 'fri_work')) == 1
            assert 'summary_dependencies' not in bodies(w, '/workspace/save')[-1]
        finally:
            await w.fri.close()
    run(scenario)


def raced(w, during):
    """Member 30's checks: `during` runs while the first one waits."""
    api, seen = w.fri.api, []

    async def checking(method, path, payload=None):
        if path == CHECK and (payload or {}).get('user_id') == '30':
            seen.append(payload)
            if len(seen) == 1:
                await during()
        return await api(method, path, payload)
    w.fri.api = checking
    return seen


def test_a_turn_isnt_refused_for_a_dependency_its_request_wont_carry(monkeypatch, tmp_path):
    """fri #217's repro: the echo is deleted and its summary withdrawn while B's check before the request waits. The
    request won't carry it, so B's unrelated question is still answered, depending on nothing."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        agent = w.fri.agent
        try:
            events = await summarized_from(monkeypatch, w)
            model(w, recalls)
            assert await echoed(w) == [CONTINUATION]
            echo = next(int(row['id']) for row in agent.recent[GENERAL] if row['text'] == CONTINUATION)

            async def gone():  # The echo is deleted, and its summary's source withdrawn.
                agent.unsend(GENERAL, [echo])
                await deleted(w, events[0])
            checks = raced(w, gone)

            async def unrelated(text, context):
                assert not any(CONTINUATION in row.get('text', '') for row in context.get('recent', []))
                return ChatResult(status='completed', reply='four')
            model(w, unrelated)
            before = len(w.calls)
            assert 'four' in await echoed(w, 'fri what is two plus two', user=30)
            assert 'four' in saved_replies(w, 30)
            assert checks[0].get('summary_dependencies') and 'summary_dependencies' not in checks[1]
            saves = sent(w, TURN, since=before)
            assert saves and all('summary_dependencies' not in body for body in saves)
        finally:
            await w.fri.close()
    run(scenario)


def test_a_dependency_the_request_still_carries_refuses_it_without_a_retry(monkeypatch, tmp_path):
    """The echo stays in view, so the request would carry it: refused, as before, with no second check. The controller
    learns of the withdrawal from elsewhere (another process's queue, say), so Fri's view still holds the echo."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            events = await summarized_from(monkeypatch, w)
            model(w, recalls)
            await echoed(w)

            async def elsewhere():
                answer = w.client.post('/chat/turn/withdraw', headers=headers('fri'), json=withdrawal(events[0]))
                assert answer.json()['removed'] == 1
            checks = raced(w, elsewhere)
            before = len(w.calls)
            await echoed(w, 'fri what is two plus two', user=30)
            assert len(checks) == 1 and not sent(w, TURN, since=before)
        finally:
            await w.fri.close()
    run(scenario)
