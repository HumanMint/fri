"""Copied context and interrupted withdrawal writes stay private."""
import asyncio

import pytest

from compat_tests.test_fri_ancestry_gaps import asking
from compat_tests.test_fri_endings import source
from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_panel_and_learning_fences import panel
from compat_tests.test_fri_panel_dependency_holds import MARKER
from compat_tests.test_fri_rerun_fence import direct
from fri.fri_turn_withdrawal import SavedTurns


@pytest.mark.parametrize('where', ['panel', 'dm', 'channel'])
@pytest.mark.parametrize('tracked', [True, False])
def test_context_read_before_settlement_is_refetched(monkeypatch, tmp_path, where, tracked):
    async def scenario():
        fri, agent, store, _, _, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        fri.capabilities = frozenset((await fri.api('GET', '/fri/contract'))['capabilities'])
        if where == 'dm':
            _, message = direct(fri, agent, rows)
        channel, conversation = {'panel': ('456', 'private:456'), 'dm': ('555', 'private:555'),
                                 'channel': ('1', 'public:1')}[where]
        quoted, saved = source(age=1200, channel='1'), source(age=600, channel=channel)
        store.save_chat('123', conversation, '20', 0, 'repeat', MARKER, 'model', 'high', source=saved,
                        provenance=[], drew_on=[quoted['id']])
        turns = agent.saved_turns
        if tracked:
            import time
            turns.kept({'guild_id': '123', 'channel_id': channel, 'conversation_id': conversation, 'user_id': '20',
                        'revision': 0, 'source': saved, 'drew_on': [quoted['id']]}, {},
                       (time.time(), time.monotonic()))
        turns.kick = lambda: None
        turns.withdraw([int(quoted['id'])], 123, deleted=True)
        api, reads = fri.api, []

        async def answering(method, path, payload=None):
            answer = await api(method, path, payload)
            if path == '/chat/context':
                reads.append(True)
                if len(reads) == 1:
                    assert MARKER in str(answer['turns'])
                    await turns.drain()
                    assert not turns.pending
            return answer
        fri.api = answering
        carried = asking(fri, tmp_path, reply='okay')
        try:
            if where == 'panel':
                await fri.chat_interaction(panel(fri), 'repeat')
            else:
                await agent.handle(message('repeat' if where == 'dm' else '<@99> repeat'))
            assert carried and all(MARKER not in text for text in carried)
            assert len(reads) == 2
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_failed_withdrawal_write_is_visible_and_held_after_restart(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
        fri.capabilities = frozenset((await fri.api('GET', '/fri/contract'))['capabilities'])
        fri.data = tmp_path
        turns = agent.saved_turns
        turns.kick = lambda: None
        turns.persist()  # A healthy start, before the disk fills.
        store.save_chat('123', 'private:456', '20', 0, 'repeat', MARKER, 'model', 'high',
                        source=source(age=300, channel='456'), provenance=[])
        from fri import fri_turn_withdrawal as module
        write = module.private_json

        def full(*args, **kwargs):
            raise OSError('disk full')
        monkeypatch.setattr(module, 'private_json', full)
        turns.withdraw([int(source(age=600, channel='1')['id'])], 123, deleted=True)
        assert turns.status()['saved_to_disk'] is False
        restarted = SavedTurns(agent)
        context = {'turns': [{'reply': MARKER}], 'member_summary': {'text': MARKER}}
        restarted.withhold(context, member='20')
        assert not context['turns'] and context['member_summary'] is None
        agent.saved_turns = restarted
        carried = asking(fri, tmp_path, reply='okay')
        await fri.chat_interaction(panel(fri), 'repeat')
        assert carried and all(MARKER not in text for text in carried)
        assert restarted.status()['saved_to_disk'] is False
        monkeypatch.setattr(module, 'private_json', write)
        await turns.drain()
        assert turns.status()['saved_to_disk'] is True
        await fri.close()
    asyncio.run(scenario())
