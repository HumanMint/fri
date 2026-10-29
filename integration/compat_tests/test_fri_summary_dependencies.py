"""fri #193 items 1, 2 and 4: what repeats a reply of Fri's that drew on a member's summary depends on that summary too.

A reply drawn from another member's summary-derived echo, or from a summary-derived reply of Fri's it answers, carries
that summary's generation through its checks and its save (`summary_dependencies`), so Gene refuses it once the summary
goes and retires the saved reply with it. Its own echo is then rechecked like the first. A marked echo stays marked as
long as any turn could hold a copy of it. Against the real controller; one without summary-dependencies runs Fri as
before, and an ordinary turn sends nothing new.
"""

import time
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from compat_tests.test_fri_member_profiles import GENERAL, model, run, world
from compat_tests.test_fri_member_summaries import CONTINUATION
from compat_tests.test_fri_summary_echoes import SOURCES, echoed, later, recalls
from compat_tests.test_fri_summary_generations import deleted, summarized_from
from fri import feedback as commands
from fri.fri_chat import ChatResult

DEPENDENCIES = 'summary-dependencies'
REPEATED = 'so like they said: '+CONTINUATION
TURN, CHECK = '/chat/turn', '/chat/turn/check'


async def repeats(text, context):
    """The model repeats Fri's earlier reply, from the reply it answers or the channel's view."""
    if any(CONTINUATION in row.get('text', '') for row in [*context.get('reply_target', []),
                                                         *context.get('recent', [])]):
        return ChatResult(status='completed', reply=REPEATED)
    return await recalls(text, context)


def sent(w, *paths, since=0):
    return [body for path, body in w.calls[since:] if path in paths]


def generation(w):
    return w.store.chat_context('123', f'public:{GENERAL}', '20', True, True)['member_summary']['generation']


def saved_replies(w, user):
    return str(w.store.chat_context('123', f'public:{GENERAL}', str(user), True, True)['turns'])


def test_a_reply_repeating_a_summary_echo_goes_with_that_summary(monkeypatch, tmp_path):
    """Item 1's repro: A's summary shapes an echo, B's reply repeats it, then A's source is withdrawn. Neither reaches C's
    request, and B's saved reply keeps only a breadcrumb."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            events = await summarized_from(monkeypatch, w)
            model(w, recalls)
            assert await echoed(w) == [CONTINUATION]
            depends = [{'user_id': '20', 'generation': generation(w)}]
            model(w, repeats)
            before = len(w.calls)
            assert REPEATED in await echoed(w, 'fri what did they say', user=30)
            b = sent(w, TURN, CHECK, since=before)
            assert REPEATED in saved_replies(w, 30)
            await deleted(w, events[0])
            carried = later(w)
            await w.say(GENERAL, 'fri what is going on', user=31)
            assert carried and CONTINUATION not in str(carried)
            assert REPEATED not in saved_replies(w, 30)
            assert CONTINUATION not in str(w.fri.agent.messages(GENERAL))
            assert b and all(body.get('summary_dependencies') == depends for body in b)  # B's checks and save.
        finally:
            await w.fri.close()
    run(scenario)


def test_a_reply_repeating_that_one_depends_on_the_same_summary(monkeypatch, tmp_path):
    """Carried on: C's reply repeats B's echo alone, so C's turn depends on A's summary too."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        agent = w.fri.agent
        try:
            events = await summarized_from(monkeypatch, w)
            model(w, recalls)
            await echoed(w)
            depends = [{'user_id': '20', 'generation': generation(w)}]
            model(w, repeats)
            await echoed(w, 'fri what did they say', user=30)
            agent.recent[GENERAL] = deque((row for row in agent.recent[GENERAL] if row['text'] != CONTINUATION), maxlen=30)
            before = len(w.calls)
            assert await echoed(w, 'fri and what was that', user=31) == [REPEATED, REPEATED]
            c = sent(w, TURN, since=before)[-1]
            await deleted(w, events[0])
            carried = later(w)
            await w.say(GENERAL, 'fri what is going on', user=32)
            assert carried and CONTINUATION not in str(carried)
            assert REPEATED not in saved_replies(w, 31)
            assert c.get('summary_dependencies') == depends
        finally:
            await w.fri.close()
    run(scenario)


def reply_to_fri(w, sent):
    """A member's reply to Fri's delivered message, which Discord can fetch, as can their own."""
    target = w.message(GENERAL, sent.content, user=99)
    target.id, target.author.bot = sent.id, True
    posts = {target.id: target}

    async def fetch(mid):
        if mid not in posts:
            raise discord.NotFound(SimpleNamespace(status=404, reason='gone'), 'gone')
        return posts[mid]
    w.channels[GENERAL].fetch_message = AsyncMock(side_effect=fetch)
    event = w.message(GENERAL, 'fri wait what was that', user=30)
    event.reference = SimpleNamespace(message_id=target.id, channel_id=GENERAL)
    posts[event.id] = event
    return event


def test_a_reply_target_on_a_summary_withdrawn_while_the_model_writes_keeps_the_answer_unsaid(monkeypatch, tmp_path):
    """Item 2's repro: a summary-derived reply of Fri's, out of the live view, is referenced. Its summary goes during
    inference: the answer quoting it is neither delivered nor saved."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            events = await summarized_from(monkeypatch, w)
            model(w, recalls)
            result, saved, delivered = await w.say(GENERAL, 'fri what were we on about')
            assert result.reply == CONTINUATION and saved
            event = reply_to_fri(w, delivered)

            async def respond(text, context):
                assert CONTINUATION in str(context['reply_target'])
                await deleted(w, events[0])
                return await repeats(text, context)
            model(w, respond)
            turns = len(sent(w, TURN))
            with pytest.raises(commands.UserFeedbackCheckFailure):
                await w.say(GENERAL, event.content, user=30, event=event)
            assert len(sent(w, TURN)) == turns and REPEATED not in saved_replies(w, 30)
        finally:
            await w.fri.close()
    run(scenario)


def test_a_current_reply_target_goes_out_and_is_saved_depending_on_its_summary(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            events = await summarized_from(monkeypatch, w)
            model(w, recalls)
            _, _, delivered = await w.say(GENERAL, 'fri what were we on about')
            event = reply_to_fri(w, delivered)
            model(w, repeats)
            before = len(w.calls)
            result, saved, _ = await w.say(GENERAL, event.content, user=30, event=event)
            assert result.reply == REPEATED and saved
            depends = [{'user_id': '20', 'generation': generation(w)}]
            bodies = sent(w, TURN, CHECK, since=before)
            await deleted(w, events[0])
            assert REPEATED not in saved_replies(w, 30)
            assert [body.get('summary_dependencies') for body in bodies] == [depends]*3  # Two checks and the save.
        finally:
            await w.fri.close()
    run(scenario)


@pytest.mark.parametrize('dependencies', [True, False], ids=['dependencies', 'generations-only'])
def test_a_marked_echo_whose_mark_would_expire_mid_turn_is_still_rechecked(monkeypatch, tmp_path, dependencies):
    """Item 4's repro: the turn starts with a marked echo 899 seconds old. At 901 another summary-derived echo would
    prune its mark, and the summary's source is withdrawn while the model writes: the answer stays unsaid. A
    controller without summary-dependencies gets the same, from Fri's own recheck."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        agent = w.fri.agent
        if not dependencies:
            w.fri.capabilities -= {DEPENDENCIES}
        try:
            events = await summarized_from(monkeypatch, w)
            model(w, recalls)
            await echoed(w)
            [mid] = list(agent.summarized)

            def aged(seconds):
                agent.summarized[mid] = time.time()-seconds
                for row in agent.recent[GENERAL]:
                    if row['id'] == mid:
                        row['at'] = time.time()-seconds
            aged(899)

            async def expiring():
                aged(901)
                agent.echoed(GENERAL, SimpleNamespace(id=5, content='another one'),
                             {'text': 'another one', 'provenance': [], 'summarized': True})
                await deleted(w, events[0])
            turns = len(sent(w, TURN))
            carried = later(w, after=expiring)
            with pytest.raises(commands.UserFeedbackCheckFailure):
                await w.say(GENERAL, 'fri what is going on', user=30)
            assert CONTINUATION in str(carried)  # Current when the request went.
            assert len(sent(w, TURN)) == turns
        finally:
            await w.fri.close()
    run(scenario)


def test_without_summary_dependencies_fri_sends_none_as_before(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        w.fri.capabilities -= {DEPENDENCIES}
        try:
            await summarized_from(monkeypatch, w)
            model(w, recalls)
            await echoed(w)
            model(w, repeats)
            before = len(w.calls)
            await echoed(w, 'fri what did they say', user=30)
            _, _, delivered = await w.say(GENERAL, 'fri what were we on about')
            event = reply_to_fri(w, delivered)
            await w.say(GENERAL, event.content, user=30, event=event)
            assert not [body for body in sent(w, TURN, CHECK, since=before) if 'summary_dependencies' in body]
            asked = [ids for path, body in w.calls[before:] if path == SOURCES for ids in body['message_ids']]
            assert len(set(asked)) == 2  # The first echo, and the reply target: B's echo isn't marked, as before.
        finally:
            await w.fri.close()
    run(scenario)


def test_an_ordinary_turn_sends_nothing_new(monkeypatch, tmp_path):
    """No marked echo and no summary-derived reply target: nothing depends on anything, and nothing extra is asked."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            model(w, recalls)
            await echoed(w)
            _, _, delivered = await w.say(GENERAL, 'fri hi')
            event = reply_to_fri(w, delivered)
            before = len(w.calls)
            await echoed(w, 'fri what is going on', user=30)
            await w.say(GENERAL, event.content, user=30, event=event)
            assert not [body for body in sent(w, TURN, CHECK, since=before) if 'summary_dependencies' in body]
            assert len(sent(w, SOURCES, since=before)) == 1  # The reply target's own lookup, as before.
        finally:
            await w.fri.close()
    run(scenario)


def test_a_reply_whose_request_carried_one_depends_on_it_whether_or_not_it_repeats_it(monkeypatch, tmp_path):
    """Fri can't tell a paraphrase from a reply that drew on nothing, so, as with provenance, what the request carried
    counts: B's reply goes with A's summary too, and its echo is rechecked like A's."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            events = await summarized_from(monkeypatch, w)
            model(w, recalls)
            await echoed(w)
            assert await echoed(w, 'fri hi', user=30) == [CONTINUATION, 'nothing to recall']
            assert len(w.fri.agent.summarized) == 2
            await deleted(w, events[0])
            assert 'nothing to recall' not in saved_replies(w, 30)
            carried = later(w)
            await w.say(GENERAL, 'fri what is going on', user=31)
            assert carried and 'nothing to recall' not in str(carried)
        finally:
            await w.fri.close()
    run(scenario)


def test_a_dependency_counts_only_once_the_request_carries_it(monkeypatch, tmp_path):
    """A's echo is deleted while B's turn runs its check, so the request never carries it: B's unrelated answer depends on
    no summary and stays once A's summary goes (review of fri #213, round 1)."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        agent = w.fri.agent
        try:
            events = await summarized_from(monkeypatch, w)
            model(w, recalls)
            assert await echoed(w) == [CONTINUATION]
            echo = next(int(row['id']) for row in agent.recent[GENERAL] if row['text'] == CONTINUATION)
            api = w.fri.api

            async def checking(method, path, payload=None):
                if path == CHECK and (payload or {}).get('user_id') == '30':
                    agent.unsend(GENERAL, [echo])  # Deleted while the turn checks, after its context was read.
                return await api(method, path, payload)
            w.fri.api = checking

            async def unrelated(text, context):
                assert not any(CONTINUATION in row.get('text', '') for row in context.get('recent', []))
                return ChatResult(status='completed', reply='no idea, ask them')
            model(w, unrelated)
            before = len(w.calls)
            await echoed(w, 'fri what did they say', user=30)
            saves = sent(w, TURN, since=before)
            w.fri.api = api
            assert 'no idea, ask them' in saved_replies(w, 30)
            await deleted(w, events[0])
            assert 'no idea, ask them' in saved_replies(w, 30)
            assert saves and all('summary_dependencies' not in body for body in saves)
        finally:
            await w.fri.close()
    run(scenario)
