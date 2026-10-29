"""fri #171 and fri #172: what a turn drawing on Fri's summary of the member writes or keeps doesn't outlive the summary.

A tool runs only while the summary the turn's model request had is still the controller's, whether or not typing
started before the model ran, and a write of the model's words sends that summary's generation, so the controller
refuses it once moved. A saved reply that drew on the summary comes back only as a breadcrumb once it goes. Against the
real controller; an older one runs Fri as before."""

import datetime as dt
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from compat_tests.test_fri_member_profiles import DM, model, run, world
from compat_tests.test_fri_member_summaries import SUMMARY
from compat_tests.test_fri_summary_generations import CHECK, deleted, summarized_from
from fri.fri_chat import ChatResult

WRITES = 'summary-generation-writes'
DRAFT = 'raid notes: '+SUMMARY
TABLES = {'work_save': 'fri_work', 'reminder_prepare': 'fri_reminders'}
ASKS = {'work_save': 'fri save that as a draft', 'reminder_prepare': 'fri remind me about the raid tomorrow'}
WRITE_PATHS = {'work_save': '/workspace/save', 'reminder_prepare': '/social/reminders/prepare'}


def dm_channel(w):
    """The member's DM as Discord types it, so reminders are offered there too."""
    old = w.channels[DM]
    dm = Mock(spec=discord.DMChannel, id=DM, guild=None, typing=old.typing, history=old.history)
    dm.name = 'dm'
    w.channels[DM] = dm
    return dm


def action(tool):
    if tool == 'work_save':
        return {'kind': 'work_save', 'work_id': '', 'version': 0, 'title': 'raid notes', 'body': DRAFT, 'state': 'open'}
    when = (dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
    return {'kind': 'reminder_prepare', 'body': DRAFT, 'local_time': when, 'timezone': 'UTC'}


def kept(w, table):
    with w.store.connection() as db:
        return [tuple(row) for row in db.execute(f'SELECT * FROM {table}')]


def bodies(w, path):
    return [body for p, body in w.calls if p == path]


async def asked(w, text, steps, user=20):
    """`text` in the member's DM. The model answers each call with the next step, a coroutine of the context; with
    the default typing callback, typing never starts late, so only the checks under test can stop a step."""
    event = w.message(DM, text, user=user)
    w.channels[DM].fetch_message = AsyncMock(return_value=event)  # A write rereads it, unchanged.
    calls = []

    async def respond(_, context):
        calls.append(context)
        return await steps[min(len(calls), len(steps))-1](context)
    model(w, respond)
    return await w.say(DM, text, event=event, user=user)


def reply(text):
    async def step(context):
        return ChatResult(status='completed', reply=text)
    return step


@pytest.mark.parametrize('tool', TABLES)
def test_a_write_the_model_chose_while_its_summary_was_withdrawn_keeps_nothing(monkeypatch, tmp_path, tool):
    """fri #171's repro: the source goes while the model writes the step; typing had already started."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        dm_channel(w)
        try:
            events = await summarized_from(monkeypatch, w)

            async def choose(context):
                assert context['member_summary']['text'] == SUMMARY and tool in context['actions']
                await deleted(w, events[0])
                return ChatResult(status='completed', action=action(tool))
            result, saved, _ = await asked(w, ASKS[tool], [choose, reply('done, '+DRAFT)])
            assert kept(w, TABLES[tool]) == []
            assert SUMMARY not in result.reply and not saved
            assert bodies(w, CHECK)[-1]['summary_generation'] == 0
        finally:
            await w.fri.close()
    run(scenario)


def test_a_later_step_is_checked_before_it_runs_too(monkeypatch, tmp_path):
    """A step after a read: the request before it was checked, then the source went while the model chose the save."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        dm_channel(w)
        try:
            events = await summarized_from(monkeypatch, w)

            async def look(context):
                return ChatResult(status='completed', action={'kind': 'work_list', 'work_id': ''})

            async def save(context):
                assert context['action_result']['kind'] == 'work_list'
                await deleted(w, events[0])
                return ChatResult(status='completed', action=action('work_save'))
            result, saved, _ = await asked(w, ASKS['work_save'], [look, save, reply('done, '+DRAFT)])
            assert kept(w, 'fri_work') == [] and SUMMARY not in result.reply and not saved
        finally:
            await w.fri.close()
    run(scenario)


@pytest.mark.parametrize('tool', TABLES)
def test_a_withdrawal_during_the_writes_own_wait_is_refused_by_the_write(monkeypatch, tmp_path, tool):
    """After the check before the step, while the write rereads the request: the controller refuses the write itself."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        dm_channel(w)
        try:
            events = await summarized_from(monkeypatch, w)
            event = w.message(DM, ASKS[tool])

            async def reread(*_):
                await deleted(w, events[0])
                return event
            w.channels[DM].fetch_message = AsyncMock(side_effect=reread)

            async def choose(context):
                return ChatResult(status='completed', action=action(tool))
            model(w, lambda _, context: (choose if 'action_result' not in context else reply('done'))(context))
            result, saved, _ = await w.say(DM, ASKS[tool], event=event)
            assert kept(w, TABLES[tool]) == [] and not saved
            assert bodies(w, WRITE_PATHS[tool])[-1]['summary_generation'] == 0
        finally:
            await w.fri.close()
    run(scenario)


@pytest.mark.parametrize('tool', TABLES)
def test_with_the_summary_unchanged_the_write_saves_and_carries_its_generation(monkeypatch, tmp_path, tool):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        dm_channel(w)
        try:
            await summarized_from(monkeypatch, w)

            async def choose(context):
                return ChatResult(status='completed', action=action(tool))
            await asked(w, ASKS[tool], [choose, reply('done')])
            assert len(kept(w, TABLES[tool])) == 1
            assert bodies(w, WRITE_PATHS[tool])[-1]['summary_generation'] == 0
        finally:
            await w.fri.close()
    run(scenario)


def paths_after_the_model(w, start):
    """The controller calls from the model's first answer through the write."""
    paths = [path for path, _ in w.calls[start:]]
    first = paths.index(CHECK)+1  # The check right before the request; the model answers after it.
    return paths[first:paths.index('/workspace/save')+1]


@pytest.mark.parametrize('offered', ['older', 'no-writes'])
def test_an_older_controller_runs_as_before(monkeypatch, tmp_path, offered):
    """Without summary generations, nothing new is checked or sent, and the write saves as before (the gap gene #91
    left). With them but without generation writes, the step is still checked, but the write sends nothing new."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        dm_channel(w)
        w.fri.capabilities -= {WRITES} | ({'summary-generations'} if offered == 'older' else set())
        try:
            events = await summarized_from(monkeypatch, w)
            start = len(w.calls)

            async def choose(context):
                await deleted(w, events[0])
                return ChatResult(status='completed', action=action('work_save'))
            await asked(w, ASKS['work_save'], [choose, reply('done')])
            assert not any('summary_generation' in body for body in bodies(w, '/workspace/save'))
            if offered == 'older':
                assert len(kept(w, 'fri_work')) == 1
                assert CHECK not in paths_after_the_model(w, start)
                assert not any('summary_generation' in (body or {}) for _, body in w.calls)
            else:
                assert kept(w, 'fri_work') == []
        finally:
            await w.fri.close()
    run(scenario)


def test_a_turn_without_the_summary_checks_and_sends_nothing_new(monkeypatch, tmp_path):
    """Another member, whom Fri has no summary of: their step and write run as they always did."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        dm_channel(w)
        try:
            await summarized_from(monkeypatch, w)
            start = len(w.calls)

            async def choose(context):
                assert not context.get('member_summary')
                return ChatResult(status='completed', action=action('work_save'))
            await asked(w, ASKS['work_save'], [choose, reply('done')], user=30)
            assert len(kept(w, 'fri_work')) == 1
            assert CHECK not in paths_after_the_model(w, start)
            assert 'summary_generation' not in bodies(w, '/workspace/save')[-1]
        finally:
            await w.fri.close()
    run(scenario)


def test_a_saved_reply_quoting_the_summary_comes_back_only_as_a_breadcrumb(monkeypatch, tmp_path):
    """fri #172's repro: delivered and saved in their DM, then the summary's source goes. The next turn's history
    holds a breadcrumb, so the model can't repeat it and nothing saves it again."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            events = await summarized_from(monkeypatch, w)
            quoting = 'you were on about this: '+SUMMARY
            _, saved, _ = await asked(w, 'fri what were we on about', [reply(quoting)])
            assert saved
            await deleted(w, events[0])
            later = []

            async def remember(context):
                later.append(context)
                return ChatResult(status='completed', reply='no clue anymore')
            await asked(w, 'fri continue', [remember])
            history = later[0]['turns']
            assert SUMMARY not in str(history) and 'older summary' in history[-1]['reply']
            assert history[-1]['input'] == 'fri what were we on about'  # Their own words stay.
            with w.store.connection() as db:
                assert SUMMARY not in str([tuple(row) for row in db.execute('SELECT reply FROM chat_turns')])
        finally:
            await w.fri.close()
    run(scenario)


def test_generation_writes_are_offered_and_optional():
    from fri.fri_protocol import CAPABILITIES, compatible, contract
    assert WRITES in contract()['capabilities'] and WRITES not in CAPABILITIES
    assert compatible({**contract(), 'capabilities': [c for c in contract()['capabilities'] if c != WRITES]})
