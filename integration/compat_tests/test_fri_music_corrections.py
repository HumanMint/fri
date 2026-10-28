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
                           (FIX, 'someone else song'), ('for example ' + FIX, 'join us for a bite'),
                           ('not that one\n> alex: Never Gonna Give You Up', 'Never Gonna Give You Up'),
                           ('i meant to say thanks', 'thanks'), ('not the one i wanted lol', 'the one'),
                           ('wrong song, "join us for a bite"', 'join us for a bite'),
                           ('not that one, i meant to say thanks', 'thanks'), ('wrong song, The One', 'The One'),
                           ('wrong song, i meant All I Got by Said The Sky', 'All I Got by Said The Sky'),
                           ('"wrong song, Never Gonna Give You Up"\nnot that one, i meant Join Us For A Bite',
                            'Never Gonna Give You Up'),
                           ('not that one\n>>> alex said\nNever Gonna Give You Up', 'Never Gonna Give You Up')]:
        assert correction(text, argument) is genes(text, argument)


@pytest.mark.parametrize('case', ['corrects', 'no recent play', 'old play', 'not their words', 'plain title',
                                  'past three reads', 'controller says no', 'quoted evidence'])
def test_a_correction_queues_the_song_only_right_after_their_own_play(monkeypatch, tmp_path, case):
    async def scenario():
        fri, agent, channels, _ = harness(monkeypatch, tmp_path)
        text = {'plain title': 'join us for a bite',
                'quoted evidence': 'not that one\n> alex: join us for a bite'}.get(case, FIX)
        actions = Actions(agent, 20, 1, True)
        actions.event = event(user=20, channel=1, content=text)
        actions.event.webhook_id = None
        actions.event.channel.fetch_message = AsyncMock(return_value=actions.event)
        actions.message = text
        actions.scope = {'guild_id': '123', 'user_id': '20', 'channel_id': '1', 'conversation_id': 'public:1'}
        actions.revision = 0
        at = time.time()-(700 if case == 'old play' else 60)
        # The controller says whether a play would count (Sol r1 S3: its three receipts may all be reads).
        actions.music_correctable = {'past three reads': True, 'controller says no': False}.get(case)
        actions.music_receipts = [] if case in ('no recent play', 'past three reads') else [
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
        if case in ('corrects', 'past three reads'):
            assert posted and posted[0]['continuation_id'] == '' and posted[0]['argument'] == 'join us for a bite'
            assert 'skip' in result['note'] and 'Track Enqueued' in result['output']
        else:
            assert 'blocked' in result and not posted
    asyncio.run(scenario())
