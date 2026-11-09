"""A member summary's rechecks withdraw only on Discord's Unknown Message (fri #365).

A summary build and a kept summary's recheck read each message it drew on again by its ID (fri #333, #349). Any 404
there counted as a deletion and withdrew what was saved from the message, but a 404 can be about something else:
Unknown Channel (10003) doesn't show the message is gone (as Sol found in round 1 of #362). Only Unknown Message (10008)
withdraws now. Any other 404 is a read Discord couldn't vouch for: a build sends nothing, and a recheck holds that
summary back, so nothing is withdrawn on it and the summary stays with the controller.
"""
from types import SimpleNamespace

import discord
import pytest

from compat_tests.test_fri_member_profiles import GAMING, GENERAL, run, world
from compat_tests.test_fri_member_summaries import settled, summarizer
from compat_tests.test_fri_summary_rechecks import SAID, asked, kept, model_context, recheck
from compat_tests.test_fri_summary_withdrawals import exchanges
from fri.fri_agent import Agent
from fri.fri_member_summary import build
from fri.fri_turn_withdrawal import SavedTurns

ANSWERS = {'Unknown Message': {'code': 10008, 'message': 'Unknown Message'},
           'Unknown Channel': {'code': 10003, 'message': 'Unknown Channel'}}


def taken(monkeypatch):
    """What the agent takes back as deleted (Agent.unsend): (channel, message IDs)."""
    found, original = [], Agent.unsend

    def unsend(self, channel, ids, saved_in=None):
        found.append((channel, sorted(int(i) for i in ids)))
        return original(self, channel, ids, saved_in)

    monkeypatch.setattr(Agent, 'unsend', unsend)
    return found


def answering(channel, ids, answer):
    """The channel's reads of those messages by ID answered with Discord's 404 `answer`; put back by the return."""
    real = channel.fetch_message

    async def reading(mid):
        if mid in ids:
            raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), ANSWERS[answer])
        return await real(mid)

    channel.fetch_message = reading
    return lambda: setattr(channel, 'fetch_message', real)


@pytest.mark.parametrize('answer', list(ANSWERS))
def test_a_kept_summarys_recheck_withdraws_only_on_unknown_message(monkeypatch, tmp_path, answer):
    """Unknown Message: the summary's message is gone, so it's withdrawn and the summary changed, as before. Unknown
    Channel: Discord couldn't vouch for it, so the summary is held back, unchecked, and nothing is withdrawn; read
    again once Discord answers, it's checked and shown as before."""
    async def scenario():
        found = taken(monkeypatch)
        w = world(monkeypatch, tmp_path)
        try:
            events = await kept(monkeypatch, w)
            agent = w.fri.agent
            put_back = answering(w.fri.get_channel(GENERAL), {event.id for event in events}, answer)
            agent.summaries_rechecked = False  # Started again: it's held back and offered.
            gone = answer == 'Unknown Message'
            assert await recheck(agent) == ['changed' if gone else 'unchecked']
            assert sorted(i for _, ids in found for i in ids) == (sorted(e.id for e in events) if gone else [])
            assert await asked(w) is None
            if not gone:
                put_back()
                agent.summaries_rechecked = False
                assert await recheck(agent) == ['ok']
                await w.say(GAMING, 'fri what were we on about')
                assert model_context(w)['member_summary']['text'] == SAID
        finally:
            await w.fri.close()
    run(scenario)


@pytest.mark.parametrize('answer', list(ANSWERS))
def test_a_build_withdraws_only_on_unknown_message(monkeypatch, tmp_path, answer):
    """Unknown Message: that exchange is left out and withdrawn, and the rest summarized, as before (fri #333).
    Unknown Channel: Discord couldn't vouch for it, so the build sends nothing, as for a failed read, and nothing is
    withdrawn."""
    async def scenario():
        found = taken(monkeypatch)
        w = world(monkeypatch, tmp_path)
        try:
            events = await exchanges(w)
            agent = w.fri.agent
            agent.saved_turns = SavedTurns(agent)  # Restarted, nothing queued.
            answering(w.fri.get_channel(GENERAL), {events[2].id}, answer)
            seen = summarizer(monkeypatch, w)
            await build(agent)
            settled(w)
            if answer == 'Unknown Message':
                assert len(seen) == 1 and 'lavender' not in str(seen[0]['data']) and 'raid' in str(seen[0]['data'])
                assert found == [(GENERAL, [events[2].id])]
            else:
                assert seen == [] and found == []
                assert 'lavender' in str(w.store.chat_context('123', f'public:{GENERAL}', '20')['turns'])
        finally:
            await w.fri.close()
    run(scenario)
