"""Right after Gene played the wrong song for them, "not that one, X" queues X, and Fri says the other's still on."""
import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from compat_tests.test_fri_actions import harness
from compat_tests.test_fri_runtime import event
from fri.fri_actions import Actions
from fri.gene_controls import correction

FIX = 'not the one, i meant join us for a bite'


def test_fri_and_gene_hold_the_same_correction_rule():
    from gene.gene_controls import correction as genes  # The controller this Fri is pinned to.
    for text, argument in [(FIX, 'join us for a bite'), ('join us for a bite', 'join us for a bite'),
                           (FIX, 'someone else song'), ('for example ' + FIX, 'join us for a bite')]:
        assert correction(text, argument) is genes(text, argument)


@pytest.mark.parametrize('case', ['corrects', 'no recent play', 'old play', 'not their words', 'plain title'])
def test_a_correction_queues_the_song_only_right_after_their_own_play(monkeypatch, tmp_path, case):
    async def scenario():
        fri, agent, channels, _ = harness(monkeypatch, tmp_path)
        text = 'join us for a bite' if case == 'plain title' else FIX
        actions = Actions(agent, 20, 1, True)
        actions.event = event(user=20, channel=1, content=text)
        actions.event.webhook_id = None
        actions.event.channel.fetch_message = AsyncMock(return_value=actions.event)
        actions.message = text
        actions.scope = {'guild_id': '123', 'user_id': '20', 'channel_id': '1', 'conversation_id': 'public:1'}
        actions.revision = 0
        at = time.time()-(700 if case == 'old play' else 60)
        actions.music_receipts = [] if case == 'no recent play' else [
            {'at': at, 'action': 'play', 'argument': 'join us for a bite remix', 'grounded': True,
             'state': 'finished', 'channel_id': '1', 'result': {'done': True, 'output': 'Track Enqueued'}}]
        posted = []
        async def api(method, path, payload=None):
            if path == '/controls':
                posted.append(payload)
                return {'id': 'request'}
            if path == '/controls/request/result':
                return {'done': True, 'output': 'Track Enqueued: Join Us For A Bite'}
            return {'revision': 0}
        fri.api = api
        argument = 'someone else song' if case == 'not their words' else 'join us for a bite'
        with patch('fri.fri_actions.asyncio.sleep', new=AsyncMock()):
            result = await actions.execute({'kind': 'gene_command', 'action': 'play', 'argument': argument})
        if case == 'corrects':
            assert posted and posted[0]['continuation_id'] == '' and posted[0]['argument'] == 'join us for a bite'
            assert 'skip' in result['note'] and 'Track Enqueued' in result['output']
        else:
            assert 'blocked' in result and not posted
    asyncio.run(scenario())
