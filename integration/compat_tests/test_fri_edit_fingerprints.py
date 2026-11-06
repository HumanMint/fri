"""gene #150 and #246: an edit retires another member's saved reply that holds what the edit took out, against the real
controller.

Member 20 says where they live, then edits it out. Member 30's saved reply, whose request carried the message, quoted
the old words, and stayed usable: an edit withdrew only member 20's own exchange. Now Fri sends, with an edit's
withdrawal, a fingerprint of what it took out (hashed word runs and rare words, salted, never the words), from what its
live view held before the edit, and what the message says now, so the controller can tell from its own saved exchange
too. A reply that carried the message and holds enough of it keeps a breadcrumb; an unrelated one stays. With nothing to
say what the message said before, nothing more goes than on main, and a deletion is as before. A controller that
doesn't offer it gets no fingerprint, and none is ever written down.
"""
import inspect
import json
import time

import pytest

from compat_tests.test_fri_endings import source
from compat_tests.test_fri_saved_withdrawal import delete, edit, edited
from compat_tests.test_fri_saved_withdrawal import world as saved_world

CAPABILITY = 'edit-fingerprints'
BEFORE = 'my new address is 4217 juniper crescent, come by friday'
AFTER = 'come by friday'
QUOTE = 'they said their new address is 4217 juniper crescent'
PARAPHRASE = 'so you live on juniper crescent now?'
UNRELATED = 'the raid starts at nine, bring potions'
DRAWN = 'Earlier answer drawing on a message since edited or deleted. Nothing from it is retained here.'


async def world(monkeypatch, tmp_path, capable=True):
    w = await saved_world(monkeypatch, tmp_path)
    if CAPABILITY not in w.fri.capabilities:
        await w.fri.close()
        pytest.skip('needs a controller offering '+CAPABILITY)
    if not capable:
        w.fri.capabilities = w.fri.capabilities-{CAPABILITY}
    return w


def posted(w, path):
    return [payload for p, payload in w.calls if p == path]


def setting(w, *texts, in_view=True, exchange=False):
    """Member 20's message, in Fri's live view as it reads it (or not), with their exchange saved by this Fri (or not);
    and member 30's saved replies, each request having carried it."""
    message = w.said(BEFORE, user=20)
    if in_view:
        w.agent.recent[1].append({'id': str(message.id), 'user_id': '20', 'name': 'friend', 'text': BEFORE,
                                  'at': time.time(), 'revision': 0, 'gene_error': False})
    if exchange:
        said = source(age=0, channel='1')
        said['id'] = str(message.id)
        w.store.save_chat('123', 'public:1', '20', 0, BEFORE, 'noted', 'm', 'high', source=said, provenance=[])
        w.agent.saved_turns.kept({'guild_id': '123', 'channel_id': '1', 'conversation_id': 'public:1', 'user_id': '20',
                                  'revision': 0, 'source': said, 'drew_on': []}, {}, (time.time(), time.monotonic()))
    for age, text in enumerate(texts):
        w.store.save_chat('123', 'public:1', '30', 0, f'question {age}', text, 'm', 'high', verbatim=True,
                          source=source(age=30-age, user='30', channel='1'), provenance=[{'user_id': '20', 'revision': 0}],
                          drew_on=[str(message.id)])
    return message


def replies(w, user='30'):
    return [turn['reply'] for turn in w.store.chat_context('123', 'public:1', user)['turns']]


def test_fri_and_gene_share_the_fingerprint_word_for_word():
    """Fri's fingerprint is Gene's, character for character, so what each reads as an element can't drift."""
    gene = pytest.importorskip('gene.fri_edit_fingerprint')  # The controller this Fri is pinned to.
    from fri import fri_edit_fingerprint as fri
    assert inspect.getsource(fri) == inspect.getsource(gene)


@pytest.mark.parametrize('how', ['in view, by its ID', 'its exchange, saved by this Fri'])
def test_the_repro_an_edit_retires_a_reply_quoting_what_it_took_out(monkeypatch, tmp_path, how):
    """The issue: member 30's saved reply quoting member 20's old words stayed usable after the edit. Now it keeps a
    breadcrumb, and their unrelated reply in the same channel stays."""
    import asyncio

    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            message = setting(w, QUOTE, UNRELATED, exchange=how.startswith('its exchange'))
            await edit(w, edited(message, AFTER))
            assert replies(w) == [DRAWN, UNRELATED]
            assert '4217' not in str(w.store.chat_context('123', 'public:1', '30'))
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_close_paraphrase_goes_too(monkeypatch, tmp_path):
    import asyncio

    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            message = setting(w, PARAPHRASE, UNRELATED)
            await edit(w, edited(message, AFTER))
            assert replies(w) == [DRAWN, UNRELATED]
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_its_exchange_alone_says_what_it_said_once_it_left_the_view(monkeypatch, tmp_path):
    """Out of the live view, the controller's saved exchange says what the message said: Fri sends what it says now."""
    import asyncio

    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            message = setting(w, QUOTE, 'come by friday works for me', UNRELATED, in_view=False, exchange=True)
            await edit(w, edited(message, AFTER))
            assert replies(w) == [DRAWN, 'come by friday works for me', UNRELATED]  # What it still says stays.
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_with_nothing_to_say_what_it_said_before_nothing_more_goes(monkeypatch, tmp_path):
    """Neither in view nor saved: as on main, the replies that carried it stay."""
    import asyncio

    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            message = setting(w, QUOTE, UNRELATED, in_view=False)
            await edit(w, edited(message, AFTER))
            assert replies(w) == [QUOTE, UNRELATED]
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_deletion_still_takes_every_reply_that_carried_it(monkeypatch, tmp_path):
    import asyncio

    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            message = setting(w, QUOTE, UNRELATED)
            await delete(w, message.id)
            assert replies(w) == [DRAWN, DRAWN]
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_controller_without_it_gets_no_fingerprint_and_an_edit_is_as_on_main(monkeypatch, tmp_path):
    import asyncio

    async def scenario():
        w = await world(monkeypatch, tmp_path, capable=False)
        try:
            message = setting(w, QUOTE, UNRELATED)
            await edit(w, edited(message, AFTER))
            sent = posted(w, '/chat/turn/withdraw/saved')
            assert sent and not any('fingerprints' in payload for payload in sent)
            assert replies(w) == [QUOTE, UNRELATED]
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_what_it_sends_holds_no_words_and_nothing_of_it_is_written_down(monkeypatch, tmp_path):
    """The fingerprint goes to the controller as salted hashes only, and never into the withdrawal record on disk,
    which a restart reads: an edit's queued then holds the message back as before, with no fingerprint."""
    import asyncio

    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            message = setting(w, QUOTE, UNRELATED, exchange=True)
            turns = w.agent.saved_turns
            turns.kick = lambda: None  # Queued, as when the controller can't be reached.
            await edit(w, edited(message, AFTER))
            [entry] = turns.pending.values()
            assert entry['fingerprint']['removed'] and entry['fingerprint']['kept']
            record = turns.record().read_text()
            assert 'fingerprint' not in record and entry['fingerprint']['salt'] not in record
            path, payload = turns.request([(str(message.id), entry)])
            assert payload['fingerprint'] == entry['fingerprint']
            assert not any(word.strip(',') in json.dumps(payload['fingerprint']) for word in BEFORE.split())
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_second_edit_before_the_first_is_taken_adds_what_it_took_out(monkeypatch, tmp_path):
    """Edited twice before the controller took the first: what each took out goes, and what it says last is kept."""
    import asyncio

    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            message = setting(w, QUOTE, 'see you at 7719 birchwood lane then', UNRELATED)
            turns = w.agent.saved_turns
            turns.kick = lambda: None
            await edit(w, edited(message, 'my new address is 7719 birchwood lane, come by friday'))
            await edit(w, edited(message, AFTER))
            del turns.kick
            turns.kick()
            await asyncio.gather(*turns.tasks)
            assert replies(w) == [DRAWN, DRAWN, UNRELATED]
        finally:
            await w.fri.close()
    asyncio.run(scenario())
