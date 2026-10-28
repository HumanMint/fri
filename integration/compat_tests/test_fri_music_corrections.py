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


@pytest.mark.parametrize('text,argument,expected', [
    # Review of #98/#157: the song is the start of the replacement, never words from a later clause, and a
    # change of heart after it withdraws it.
    ('wrong song, To The Moon, not To Be With You', 'To Be With You', False),
    ('wrong song, To The Moon, not To Be With You', 'To The Moon', True),
    ('i meant to skip To The Moon', 'To The Moon', False),
    ('wrong song, Love Story, actually no', 'Love Story', False),
    ('wrong song, Love Story... i changed my mind', 'Love Story', False),
    ('not that one, Love Story, no wait', 'Love Story', False),
    ('actually no, wrong song, Love Story', 'Love Story', True),
    ('not that one, Love Story by Taylor Swift', 'Love Story Taylor Swift', True),
    ('not that one, Love Story by Taylor Swift', 'Taylor Swift', False),
    ('wrong song, "Actually No" by someone', 'Actually No', True),
    (FIX, 'join us for a bite', True), ('join us for a bite', 'join us for a bite', False),
    (FIX, 'someone else song', False), ('for example ' + FIX, 'join us for a bite', False),
    ('not that one\n> alex: Never Gonna Give You Up', 'Never Gonna Give You Up', False),
    ('i meant to say thanks', 'thanks', False), ('not the one i wanted lol', 'the one', False),
    ('wrong song, "join us for a bite"', 'join us for a bite', True),
    ('not that one, i meant to say thanks', 'thanks', False), ('wrong song, The One', 'The One', True),
    ('wrong song, i meant All I Got by Said The Sky', 'All I Got by Said The Sky', True),
    ('"wrong song, Never Gonna Give You Up"\nnot that one, i meant Join Us For A Bite', 'Never Gonna Give You Up', False),
    ('not that one\n>>> alex said\nNever Gonna Give You Up', 'Never Gonna Give You Up', False),
    # gene #72: negation through the link, a later "do not", someone else's words or an example, and "To ..." titles.
    ("not that one, i meant don't play join us for a bite", 'join us for a bite', False),
    ('not that one, join us for a bite. do not queue anything yet', 'join us for a bite', False),
    ('not that one, alex said "Never Gonna Give You Up"', 'Never Gonna Give You Up', False),
    ('not that one, for example Never Gonna Give You Up', 'Never Gonna Give You Up', False),
    ('wrong song, join us for a bite, not never gonna give you up', 'never gonna give you up', False),
    ('wrong song, To The Moon', 'To The Moon', True), ('not that one, i meant To The Moon', 'To The Moon', True),
    ('wrong song, i meant to say To The Moon', 'To The Moon', False),
    ("wrong song, I Don't Care", "I Don't Care", True),
    ('not that one, play That Funky Music', 'Play That Funky Music', True),
    # gene #107 (1): after "i meant to", only a word that can't start an infinitive may start a title, whatever
    # the proposed song.
    ('i meant to skip To The Moon', 'to skip To The Moon', False),
    ('not that one, i meant to pause To The Moon', 'to pause To The Moon', False),
    ('wrong song, i meant to tag alex, To The Moon', 'to tag alex To The Moon', False),
    ('not that one, i meant To My Love', 'To My Love', True),
    ('not that one, i meant To Love Somebody', 'To Love Somebody', False),  # Fails closed; these two still count:
    ('wrong song, To Love Somebody', 'To Love Somebody', True),
    ("not that one, i meant 'To Love Somebody'", 'To Love Somebody', True),
    # gene #107 (2): more ways to take the replacement back.
    ('wrong song, Love Story, not that one either', 'Love Story', False),
    ("wrong song, Love Story, that's not it either", 'Love Story', False),
    ('wrong song, Love Story, nope', 'Love Story', False),
    ('wrong song, Love Story nope', 'Love Story', False),
    ('not that one, Love Story... nah', 'Love Story', False),
    ('wrong song, Love Story, no', 'Love Story', False),
    ('wrong song, "Not That One Either"', 'Not That One Either', True),
    ('wrong song, Hell No', 'Hell No', True),
    # gene #107 (3): connectors are skipped only after the title's first word.
    ('wrong song, From Me to You', 'Me to You', False),
    ('wrong song, From Me to You', 'From Me to You', True),
    ('not that one, By The Way', 'The Way', False),
    ('not that one, By The Way', 'By The Way', True),
    ('not that one, play From Me to You', 'Me to You', False),
    # gene #107 (4): single quotes, straight or curly, pair like double quotes; an apostrophe inside a word, or
    # leading one in "'til" or "'93", never opens or closes one.
    ('wrong song, \u2018Actually No\u2019', 'Actually No', True),
    ("wrong song, 'Actually No'", 'Actually No', True),
    ("wrong song, 'Not That One Either' by someone", 'Not That One Either', True),
    ("wrong song, 'Don't Stop Believin''", "Don't Stop Believin'", True),
    ('wrong song, \u2018Don\u2019t Stop Believin\u2019\u2019', "Don't Stop Believin'", True),
    ("wrong song, it's Love Story, don't play it", 'Love Story', False),
    ("not that one, 'Til I Collapse, don't play rockin' robin", "'Til I Collapse", False),
    ("not that one, '93 Til Infinity, don't play rockin' robin", '93 Til Infinity', False),
    ("wrong song, 'Til I Collapse", "'Til I Collapse", True),
    ("not that one, alex said 'Never Gonna Give You Up'", 'Never Gonna Give You Up', False),
])
def test_fri_and_gene_hold_the_same_correction_rule(text, argument, expected):
    from gene.gene_controls import correction as genes  # The controller this Fri is pinned to.
    assert correction(text, argument) is expected
    assert genes(text, argument) is expected


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
