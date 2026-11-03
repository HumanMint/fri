"""A queued message its author sent before they turned memory off, handled once it's on again, goes unanswered and
keeps nothing (fri #331, HumanMint/gene#206). What they say after it is answered and kept as before."""

import asyncio
import time

from compat_tests.test_fri_turn_withdrawal import MARKER, saved, world


def test_a_queued_message_from_before_memory_was_turned_off_and_on_goes_unanswered(monkeypatch, tmp_path):
    """fri #331: a member's message waited, they turned memory off and on again through Gene, and only then did Fri
    handle it. Fri read their new revision, kept the message out of recent context, and answered it all the same: the
    model got their words from before the erasure, and Gene saved the exchange and a note under the new revision. It
    goes unanswered, quietly, as a turn whose author changed their privacy does."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            queued = w.said('fri remember '+MARKER)
            time.sleep(0.01)  # Snowflakes count milliseconds: it was sent before what follows.
            w.store.personal_control('20', 'disable')
            w.store.personal_control('20', 'enable')
            time.sleep(0.01)
            await w.agent.handle(queued)
            assert MARKER not in str(w.requests) and MARKER not in saved(w)
            queued.reply.assert_not_awaited()
            fresh = w.said('fri and what about now?')
            await w.agent.handle(fresh)
            assert 'what about now' in str(w.requests) and 'what about now' in saved(w)
        finally:
            await w.fri.close()
    asyncio.run(scenario())
