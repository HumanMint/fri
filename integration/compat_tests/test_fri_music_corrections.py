"""Right after Gene played the wrong song for them, "not that one, X" queues X, and Fri says the other's still on."""
import ast
import asyncio
import inspect
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
    # gene #111 (1): only the title they quoted is protected, so a quoted refusal elsewhere still counts, and a
    # quote that might not close (a dropped letter with more words after it) protects nothing.
    ("wrong song, Love Story, 'don't play it' is what I said", 'Love Story', False),
    ('wrong song, Love Story, "don\'t play it" is what I said', 'Love Story', False),
    ("wrong song, 'Love Story, don't play rockin' robin", 'Love Story', False),
    ("wrong song, 'Love Story, don't play rockin' robin", "Love Story, don't play rockin'", False),
    ("wrong song, 'Love Story' 'nope'", 'Love Story', False),
    ("wrong song, 'Love Story, 'nope'", 'Love Story Nope', False),  # A second quote opening inside: ambiguous.
    ("wrong song, 'Love Story ' nope'", 'Love Story Nope', False),  # So is a loose mark.
    ("wrong song, 'Rockin' Robin'", "Rockin' Robin", True),
    ("wrong song, 'Rockin' Robin' by Bobby Day", "Rockin' Robin Bobby Day", True),
    ("wrong song, 'Don't Stop Believin'", "Don't Stop Believin'", True),
    ('wrong song, https://youtu.be/dont-nope', 'https://youtu.be/dont-nope', True),  # A link keeps every word too.
    ('wrong song, https://youtu.be/abc, nope', 'https://youtu.be/abc', False),
    ('wrong song, https://youtu.be/x', 'https://youtu.be', False),
    # gene #111 (2): a withdrawal word right after "by" names the artist.
    ('wrong song, Love Story by Nope', 'Love Story', True),
    ('wrong song, Love Story by Nope', 'Love Story by Nope', True),
    ('wrong song, Love Story by Nope', 'Love Story Nope', True),
    ('wrong song, Love Story by Taylor Swift, nope', 'Love Story Taylor Swift', False),
    ('wrong song, Love Story by Taylor Swift nope', 'Love Story', False),
    ('wrong song, Love Story by. no', 'Love Story by', False),  # "by." ends a sentence.
    ('not that one, ft. nope', 'ft nope', False),  # An artist follows a title, never starts one.
    # gene #111 (3): punctuation and closing filler don't hide a withdrawal.
    ('wrong song, Love Story, no thanks', 'Love Story', False),
    ('wrong song, Love Story, nah, lol', 'Love Story', False),
    ('wrong song, Love Story. no, lol', 'Love Story', False),
    ('not that one, Love Story... nah thanks', 'Love Story', False),
    ('wrong song, Love Story, actually nah', 'Love Story', False),
    ('wrong song, "Love Story" no', 'Love Story', False),
    ('wrong song, "No Thanks"', 'No Thanks', True),
    # gene #111 (4): a quoted title keeps every word, and only the connector before an artist may be skipped.
    ('wrong song, "Stand By Me"', 'Stand Me', False),
    ("wrong song, 'Stand By Me'", 'Stand Me', False),
    ('wrong song, Stand By Me', 'Stand Me', False),
    ('wrong song, Stand By Me', 'Stand By Me', True),
    ('wrong song, "Stand By Me" by Ben E. King', 'Stand By Me Ben E. King', True),
    ('wrong song, "Love Story" by Taylor Swift', 'Love Story Taylor Swift', True),
    ('wrong song, "Love Story by Taylor Swift"', 'Love Story Taylor Swift', False),
    ('wrong song, "The One"', 'One', False),
    # gene #111 (5): after the song, an example or someone else's words take it back; their own words don't.
    ("wrong song, 'Love Story' was an example", 'Love Story', False),
    ("wrong song, 'Love Story' is what I heard Alex say", 'Love Story', False),
    ('wrong song, Love Story was just an example', 'Love Story', False),
    ('wrong song, "Love Story", alex said', 'Love Story', False),
    ('wrong song, Love Story is what alex wants', 'Love Story', False),
    ('wrong song, Love Story according to alex', 'Love Story', False),
    ("wrong song, 'Love Story' is what i said", 'Love Story', True),
    ('wrong song, Love Story, like i said', 'Love Story', True),
    ("wrong song, Love Story, i've literally just said", 'Love Story', True),
    ('wrong song, Love Story by Example', 'Love Story', True),
    ('wrong song, Simon Says', 'Simon Says', True),
    # gene #119 (1): only an artist right after the title (or in it) is protected, and a quote is someone else's.
    ("wrong song, 'Love Story' was a quote from Nope", 'Love Story', False),
    ('wrong song, Love Story was a quote from Nope', 'Love Story', False),
    ("wrong song, 'Love Story' came from Nope", 'Love Story', False),
    ("wrong song, 'Love Story', quoting alex", 'Love Story', False),
    ('wrong song, Love Story is a quote from Taylor Swift', 'Love Story', False),
    ("wrong song, 'Love Story' by Nope", 'Love Story', True),
    # gene #119 (2): an artist's name may carry those words ("The Wanted"), though a withdrawal after its first word,
    # or someone else's words after it, still counts.
    ('wrong song, Glad You Came by The Wanted', 'Glad You Came', True),
    ("wrong song, 'Glad You Came' by The Wanted", 'Glad You Came', True),
    ('wrong song, Glad You Came by The Wanted', 'Glad You Came The Wanted', True),
    ('wrong song, All I Got by Said The Sky', 'All I Got', True),
    ('wrong song, Glad You Came by The Wanted nope', 'Glad You Came', False),
    ('wrong song, Glad You Came by The Wanted, alex said', 'Glad You Came', False),
    ('wrong song, Glad You Came by The Wanted is what alex said', 'Glad You Came', False),
    ('wrong song, Love Story by Taylor Swift alex said', 'Love Story', False),
    ('wrong song, Hell No by The Wanted', 'Hell No', True),
    ('wrong song, Love Story from the example', 'Love Story', False),
    # gene #119 (3): a title ends at its closing quote, or unquoted at a clause break, whatever the proposed song
    # covers...
    ("wrong song, 'Love Story' was an example", 'Love Story was an example', False),
    ("wrong song, 'Love Story' is what Alex said", 'Love Story is what Alex said', False),
    ("wrong song, 'Love Story' alex said", 'Love Story alex said', False),
    ("wrong song, 'Love Story' was a quote from Nope", 'Love Story was a quote from Nope', False),
    ('wrong song, Love Story was an example', 'Love Story was an example', False),
    ('wrong song, Love Story is what alex said', 'Love Story is what alex said', False),
    ('wrong song, Love Story, alex said', 'Love Story alex said', False),
    ('wrong song, Love Story like alex said', 'Love Story like alex said', False),
    ('wrong song, Love Story for example', 'Love Story for example', False),
    ('wrong song, Love Story by Taylor Swift is what alex wants', 'Love Story Taylor Swift is what alex wants', False),
    # ...though a title can still carry those words.
    ('wrong song, Say You, Say Me', 'Say You, Say Me', True),
    ('wrong song, Say, Say, Say', 'Say Say Say', True),
    ('wrong song, Everybody Wants to Rule the World', 'Everybody Wants to Rule the World', True),
    ('wrong song, This Is What You Came For', 'This Is What You Came For', True),
    ("wrong song, I Don't Want to Miss a Thing", "I Don't Want to Miss a Thing", True),
    ('wrong song, Please Please Please Let Me Get What I Want', 'Please Please Please Let Me Get What I Want', True),
    ('wrong song, Stand By Me by Ben E. King', 'Stand By Me Ben E. King', True),
    ('wrong song, Say You, Say Me by Nope', 'Say You, Say Me', True),
    ('wrong song, This Is What You Came For by Said The Sky', 'This Is What You Came For', True),
    # gene #119 (4): their own words don't vouch for a song they deny...
    ('wrong song, Love Story, I said not to play it', 'Love Story', False),
    ('wrong song, Love Story is not what I meant', 'Love Story', False),
    ('wrong song, Love Story is not what I meant', 'Love Story is not what I meant', False),
    ("wrong song, Love Story isn't what i asked for", 'Love Story', False),
    ("wrong song, 'Love Story' is not the one i wanted", 'Love Story', False),
    ('wrong song, Love Story by Taylor Swift is not what i meant', 'Love Story', False),
    ("wrong song, Love Story is what i didn't ask for", 'Love Story', False),
    ('wrong song, Love Story, i said no', 'Love Story', False),
    ('wrong song, Love Story, i told you not to', 'Love Story', False),
    ('wrong song, Say You, Say Me by The Wanted is not what i meant', 'Say You, Say Me', False),
    # ...while they still count, and a complaint about the song Gene played isn't a denial.
    ('wrong song, Love Story is what i meant', 'Love Story', True),
    ('wrong song, Love Story, i said it already', 'Love Story', True),
    ('wrong song, Love Story, not Glad You Came like i said', 'Love Story', True),
    ("wrong song, Love Story, that's not what i asked for", 'Love Story', True),
])
def test_fri_and_gene_hold_the_same_correction_rule(text, argument, expected):
    from gene.gene_controls import correction as genes  # The controller this Fri is pinned to.
    assert correction(text, argument) is expected
    assert genes(text, argument) is expected


def correction_source(module):
    """correction() and every module-level name it reads, as written."""
    source = inspect.getsource(module)
    defined = {}
    for node in ast.parse(source).body:
        names = [node.name] if isinstance(node, ast.FunctionDef) else [
            target.id for target in getattr(node, 'targets', []) if isinstance(target, ast.Name)]
        defined.update(dict.fromkeys(names, node))
    found, reads = {}, ['correction']
    while reads:
        name = reads.pop()
        if name in defined and name not in found:
            found[name] = ast.get_source_segment(source, defined[name])
            reads += [node.id for node in ast.walk(defined[name]) if isinstance(node, ast.Name)]
    return found


def test_fri_and_gene_share_the_correction_code_word_for_word():
    """Fri's copy of correction() and its helpers is Gene's, character for character, so the rule can't drift."""
    from gene import gene_controls as genes  # The controller this Fri is pinned to.

    from fri import gene_controls as fris
    ours = correction_source(fris)
    assert {'correction', 'replacement', 'grounded_song', 'quoted_title', 'REFUSED', 'WITHDRAWN', 'TRAILING'} <= set(ours)
    assert ours == correction_source(genes)


def test_a_long_message_is_read_without_backtracking():
    """Each check runs in about linear time, so a 4000-character worst case takes milliseconds, not minutes."""
    shapes = ["'"+"a' "*1330, "'"+"rockin' "*498, "''a "*997, "' "*1994, '"'+'a '*1993, 'love story nah'+' lol'*990+' x',
              'love story'+', no lol'*497+' x', 'love story'+' ,'*1990+'x', 'love story'+', a b said'*398, 'love '+'by '*1328,
              'love'+' from x'*568, 'love story'+' i just'*664+' said', "love story "+"don't "*664, 'i meant '+'to '*1328,
              'love story'+' is not what'*331, 'love '+'by the '*664, 'love story'+' i said no'*398, 'love story'+', say'*797,
              'love story '+'x '*1990+'is not what i meant']
    for shape in shapes:
        text = ('wrong song, '+shape)[:4000]
        began = time.perf_counter()
        for argument in ('love story', 'a', text[12:200]):
            correction(text, argument)
        assert time.perf_counter()-began < 0.5, shape[:20]


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
