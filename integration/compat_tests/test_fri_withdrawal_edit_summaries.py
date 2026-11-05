"""An edit against the member summary that drew on its message (fri #358 E1, Sol rounds 1 and 2 of #364, B1 and B2);
and the versions a learning pass names for the notes it may teach.

Built from member 20's messages, the summary names them as its sources, with the version of each. One of those
messages is then answered again after an edit, and the edit's event, delivered after a restart, finds the save of the
edited version, and the one from before it too unless that was trimmed since: the controller keeps the new exchange
(and, with both, withdraws nothing and Fri holds the message back). Nothing in a later request elsewhere names that
message any more, as its exchanges aged out of what other channels see, so the summary still carried what the edit took
away. The summary is placed by itself: it drew on the version before the edit, so it goes.
"""
import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest

from compat_tests.test_fri_learning import learning  # noqa: F401 (the fixture)
from compat_tests.test_fri_member_profiles import GAMING, GENERAL, model, model_context, run, world
from compat_tests.test_fri_member_summaries import CONTINUATION, settled
from compat_tests.test_fri_panel_and_learning_fences import batch
from compat_tests.test_fri_summary_generations import summarized_from
from fri.fri_chat import ChatResult
from fri.fri_turn_withdrawal import SavedTurns, sent_at

EDIT_TIMES = 'withdrawal-edit-times'


async def recalls(text, context):
    return ChatResult(status='completed', reply=CONTINUATION)


@pytest.mark.parametrize('earlier', ['kept', 'trimmed'])
def test_an_edit_takes_the_summary_that_drew_on_the_version_before_it(monkeypatch, tmp_path, earlier):
    """B1 (round 1): with both exchanges, the edit can't be placed, and the summary was carried on. B2 (round 2): the
    earlier exchange trimmed, the edited one vouched for the message, summary and all."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            if EDIT_TIMES not in w.fri.capabilities:
                pytest.skip('needs a controller offering '+EDIT_TIMES)
            events = await summarized_from(monkeypatch, w)
            message, later = events[0], sent_at(events[0].id)+1  # Edited a second after it was sent.
            source = {'kind': 'discord_message', 'id': str(message.id), 'user_id': '20', 'channel_id': str(GENERAL),
                      'guild_id': '123', 'display_name': 'member'}
            if earlier == 'trimmed':  # As six later saves would.
                with w.store.connection() as db:
                    db.execute("DELETE FROM chat_turns WHERE json_extract(NULLIF(source,''),'$.id')=?",
                               (str(message.id),))
            # Answered again as edited, with a note kept in this conversation only, both from the edited version.
            w.store.save_chat('123', f'public:{GENERAL}', '20', 0, 'fri raid tonight? at nine', 'okay', 'model', 'high',
                              source=source, provenance=[], versions={str(message.id): later},
                              notes=[{'category': 'games', 'evidence': 'i play raids'}])
            settled(w, 13*3600)  # Out of what other channels see.
            w.fri.agent.saved_turns = SavedTurns(w.fri.agent)  # Restarted: what it saved is known by ID only.
            edited = SimpleNamespace(id=message.id, author=SimpleNamespace(id=20, bot=False), content='at nine',
                                     edited_at=dt.datetime.fromtimestamp(later, dt.timezone.utc), attachments=[],
                                     embeds=[])
            await w.fri.on_raw_message_edit(SimpleNamespace(channel_id=GENERAL, guild_id=123, message=edited))
            await asyncio.gather(*w.fri.agent.saved_turns.tasks)
            held = w.fri.agent.saved_turns.pending.get(str(message.id), {}).get('hold')
            assert held is (True if earlier == 'kept' else None)
            model(w, recalls)
            await w.say(GAMING, 'fri what were we on about')
            assert not model_context(w).get('member_summary')
            with w.store.connection() as db:
                assert db.execute('SELECT count(*) FROM fri_member_summaries').fetchone()[0] == 0
        finally:
            await w.fri.close()
    run(scenario)


@pytest.mark.parametrize('offered', [True, False])
def test_a_learning_pass_names_the_version_of_each_message_it_read(learning, monkeypatch, offered):  # noqa: F811
    """Beside the records at its finish, not in them: the model reads the records. To a controller offering edit
    times; one without them gets the finish as before."""
    async def scenario():
        agent, store, (first, second), finishes, asked, signing = batch(learning, monkeypatch)
        capabilities = frozenset((await agent.fri.api('GET', '/fri/contract'))['capabilities'])
        if EDIT_TIMES not in capabilities:
            pytest.skip('needs a controller offering '+EDIT_TIMES)
        if offered:
            agent.fri.capabilities = frozenset(agent.fri.capabilities) | {EDIT_TIMES}
        await agent.learn()
        assert [sorted(record) for record in asked[0]['messages']] == [sorted(
            ['id', 'user_id', 'role_id', 'revision', 'text', 'at'])]*2
        assert finishes[-1].get('versions') == (
            {first: sent_at(int(first)), second: sent_at(int(second))} if offered else None)
    asyncio.run(scenario())
