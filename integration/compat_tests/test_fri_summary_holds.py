"""A member summary held back until its messages are read again (fri #349) holds back what was made from it too (fri
#355), against the real controller.

The controller keeps it all out of use while the summary is held, without retiring any of it (HumanMint/gene#GENEPR,
summary-holds): a turn that copied the summary is refused at its checks, and a saved reply that drew on it reads as
withheld. From a controller that can't do that, Fri uses no summary at all, so nothing is made from one it may have to
hold back.
"""
import pytest

from compat_tests.test_fri_member_profiles import (
    DM,
    GAMING,
    GENERAL,
    model,
    model_context,
    run,
    turns,
    world,
)
from compat_tests.test_fri_member_summaries import CONTINUATION, SUMMARY, settled, summarizer
from compat_tests.test_fri_summary_generations import CHECK, REFUSED, sent, summarized_from
from fri.fri_chat import ChatResult
from fri.fri_errors import ControllerRejected

HOLDS = 'summary-holds'


def holds(w):
    if HOLDS not in w.fri.capabilities:
        pytest.skip('needs a controller offering '+HOLDS)


async def hold(w):
    """A start's first recheck claim, as Fri makes it: every summary is held back until it's read again."""
    assert (await w.fri.api('POST', '/agent/member-summary/recheck', {'fresh': True}))['summaries']


async def recalls(text, context):
    return ChatResult(status='completed', reply=CONTINUATION)


def stored(w):
    """The kept summary's body and the member's summary generation, held back or not."""
    with w.store.connection() as db:
        body = db.execute("SELECT body FROM fri_member_summaries WHERE user_id='20'").fetchone()
        generation = db.execute("SELECT generation FROM fri_member_summary_generations WHERE user_id='20'").fetchone()
    return (body[0] if body else None), (generation[0] if generation else 0)


def test_fri_uses_no_summary_from_a_controller_that_cant_hold_back_what_was_made_from_it(monkeypatch, tmp_path):
    """Without summary-holds, a summary that's held back still reaches replies and drafts made from it while it was
    shown. Fri leaves summaries out of every request instead. From one that offers it, the summary goes as before."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            holds(w)
            await summarized_from(monkeypatch, w)
            model(w, recalls)
            await w.say(GAMING, 'fri what were we on about')
            assert model_context(w)['member_summary']['text'] == SUMMARY
            w.fri.capabilities = frozenset(w.fri.capabilities)-{HOLDS}
            await w.say(GAMING, 'fri and what else')
            assert not model_context(w).get('member_summary')
        finally:
            await w.fri.close()
    run(scenario)


@pytest.mark.parametrize('where', [GAMING, DM])
def test_a_summary_held_while_fri_thinks_keeps_the_reply_unsaid_and_the_summary_kept(monkeypatch, tmp_path, where):
    """The issue's first path: the turn copied member 20's summary, which a restart's recheck claim holds back while
    the model writes. The check after it refuses the turn, so nothing is said or saved; nothing is retired either."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            holds(w)
            await summarized_from(monkeypatch, w)
            saved, checks, before = len(turns(w)), len(sent(w, CHECK)), stored(w)

            async def respond(text, context):
                assert context['member_summary']['text'] == SUMMARY
                await hold(w)
                return ChatResult(status='completed', reply=CONTINUATION)
            model(w, respond)
            with pytest.raises(ControllerRejected, match=REFUSED):
                await w.say(where, 'fri what were we on about')
            assert len(turns(w)) == saved and len(sent(w, CHECK)) == checks+2
            assert stored(w) == before
        finally:
            await w.fri.close()
    run(scenario)


def test_a_saved_reply_on_a_held_summary_reads_as_withheld_in_the_next_request(monkeypatch, tmp_path):
    """The issue's second path: Fri's reply that drew on the summary was saved; the summary is then held back. The
    member's next request carries that exchange as withheld for now, not as said."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            holds(w)
            await summarized_from(monkeypatch, w)
            model(w, recalls)
            await w.say(GAMING, 'fri what were we on about')
            await hold(w)
            await w.say(GAMING, 'fri and what else')
            history = str(model_context(w)['turns'])
            assert CONTINUATION not in history and 'being checked again' in history
            assert not model_context(w).get('member_summary')
        finally:
            await w.fri.close()
    run(scenario)


def test_no_summary_is_built_for_a_controller_that_cant_hold_back_what_is_made_from_it(monkeypatch, tmp_path):
    """None would be used, so none is built: no claim, no model call, and fri health says it waits for the
    controller."""
    async def scenario():
        from fri.fri_member_summary import build
        w = world(monkeypatch, tmp_path)
        try:
            holds(w)
            seen = summarizer(monkeypatch, w)
            for text in ('fri raid tonight?', 'fri which build for it'):
                await w.say(GENERAL, text)
            settled(w, 13*3600)
            w.fri.capabilities = frozenset(w.fri.capabilities)-{HOLDS}
            assert await build(w.fri.agent) is None and w.fri.agent.summaries_wait_for_gene
            assert seen == [] and not sent(w, '/agent/member-summary/claim')
        finally:
            await w.fri.close()
    run(scenario)
