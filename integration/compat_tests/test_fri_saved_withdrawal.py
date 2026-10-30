"""fri #174: a member's edit or delete takes their saved exchange's old words whenever Fri last carried the message, and
after Fri restarts too.

Against the real controller. Fri withdraws what it saved since it started, as before, and an edit now counts however
long ago a model request carried the message. Anything else edited or deleted where Fri chats, young enough to be
saved still, is asked about by ID from a controller offering saved-turn-withdrawal, which drops only an exchange that
holds it. An older controller is asked nothing by ID.
"""
import asyncio
import copy
import datetime as dt
import logging
from types import SimpleNamespace

import discord
import pytest

from compat_tests.test_fri_turn_withdrawal import MARKER, WITHDRAW, saved, settled
from compat_tests.test_fri_turn_withdrawal import world as turn_world

SAVED = '/chat/turn/withdraw/saved'
SAVED_CAPABILITY = 'saved-turn-withdrawal'


async def world(monkeypatch, tmp_path, capable=True):
    """capable=False: a controller without saved-turn-withdrawal, a genuinely older one (offline-checks with GENE_SHA
    set to one) or the paired one with the capability taken away. A case that needs it skips on an older one."""
    w = await turn_world(monkeypatch, tmp_path)
    if capable and SAVED_CAPABILITY not in w.fri.capabilities:
        await w.fri.close()
        pytest.skip('needs a controller offering '+SAVED_CAPABILITY)
    if not capable:
        w.fri.capabilities = w.fri.capabilities-{SAVED_CAPABILITY}
    return w


def asked(w, path=SAVED):
    return [payload for p, payload in w.calls if p == path]


def edited(message, text='current safe caption'):
    current = copy.copy(message)
    current.content, current.edited_at = text, discord.utils.utcnow()
    return current


async def edit(w, message, guild=123, channel=1):
    await w.fri.on_raw_message_edit(SimpleNamespace(channel_id=channel, guild_id=guild, message=message))
    await settled(w)


async def delete(w, *ids, guild=123, channel=1):
    if len(ids) == 1:
        await w.fri.on_raw_message_delete(SimpleNamespace(channel_id=channel, guild_id=guild, message_id=ids[0]))
    else:
        await w.fri.on_raw_bulk_message_delete(SimpleNamespace(channel_id=channel, guild_id=guild, message_ids=set(ids)))
    await settled(w)


def long_after(w):
    """Over an hour since any model request carried what Fri answered, and out of the live view."""
    w.agent.looked.clear()
    w.agent.recent.clear()


def restarted(w):
    long_after(w)
    w.agent.saved_turns.saved.clear()


def test_an_edit_long_after_fri_last_carried_it_takes_the_exchange(monkeypatch, tmp_path):
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            earlier = w.said('fri explain '+MARKER)
            await w.agent.handle(earlier)
            await w.agent.handle(w.said('fri and lunch?'))
            assert MARKER in saved(w)
            long_after(w)
            await edit(w, edited(earlier))
            assert MARKER not in saved(w) and 'fri and lunch?' in saved(w)
            assert len(asked(w, WITHDRAW)) == 1 and not asked(w)  # Fri saved it this run: its own record names it.
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('how', ['edit', 'delete', 'bulk'])
def test_after_a_restart_it_is_found_by_id(monkeypatch, tmp_path, how):
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            earlier = w.said('fri explain '+MARKER)
            await w.agent.handle(earlier)
            await w.agent.handle(w.said('fri and lunch?'))
            restarted(w)
            if how == 'edit':
                await edit(w, edited(earlier))
            elif how == 'delete':
                await delete(w, earlier.id)
            else:
                await delete(w, earlier.id, earlier.id+5000)
            assert MARKER not in saved(w) and 'fri and lunch?' in saved(w)
            assert not asked(w, WITHDRAW)
            assert [sorted(p['message_ids']) for p in asked(w)] == [
                sorted({str(earlier.id), *([str(earlier.id+5000)] if how == 'bulk' else [])})]
            assert all(p['guild_id'] == '123' for p in asked(w))
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_dm_is_asked_about_under_fris_server(monkeypatch, tmp_path):
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            when = dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=2)
            mid = str(discord.utils.time_snowflake(when))
            source = {'kind': 'discord_message', 'id': mid, 'user_id': '20', 'channel_id': '600', 'guild_id': '123',
                      'display_name': 'friend'}
            w.store.save_chat('123', 'private:600', '20', 0, 'dm about '+MARKER, 'okay', 'model', 'high',
                              source=source, provenance=[])
            dm = SimpleNamespace(id=int(mid), content='changed it', edited_at=discord.utils.utcnow(),
                                 author=SimpleNamespace(id=20, bot=False))
            await edit(w, dm, guild=None, channel=600)
            assert asked(w) == [{'guild_id': '123', 'message_ids': [mid]}]
            assert MARKER not in str(w.store.chat_context('123', 'private:600', '20')['turns'])
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_what_fri_couldnt_have_saved_asks_nothing(monkeypatch, tmp_path):
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        elsewhere = SimpleNamespace(id=2, guild=SimpleNamespace(id=123), parent_id=None)
        abroad = SimpleNamespace(id=3, guild=SimpleNamespace(id=456), parent_id=None)
        get_channel = w.fri.get_channel
        w.fri.get_channel = lambda cid: {2: elsewhere, 3: abroad}.get(cid) or get_channel(cid)
        try:
            unrelated = w.said('just chatting')
            restarted(w)
            await edit(w, edited(unrelated), channel=2)  # A channel Fri doesn't chat in.
            await delete(w, unrelated.id, channel=2)
            await edit(w, edited(unrelated), guild=456, channel=3)  # Another server.
            await edit(w, edited(unrelated), guild=456, channel=4)  # One whose channel Fri hasn't cached.
            # Older than any exchange the controller keeps, or any note, which one offering withdrawal-sources drops
            # with its message too (gene #141).
            days = 91 if 'withdrawal-sources' in w.fri.capabilities else 8
            old = SimpleNamespace(id=discord.utils.time_snowflake(discord.utils.utcnow()-dt.timedelta(days=days)),
                                  content='x', edited_at=discord.utils.utcnow(), author=SimpleNamespace(id=20, bot=False))
            await edit(w, old)
            await delete(w, old.id)
            card = copy.copy(unrelated)  # A link card or embed update: no edit of theirs.
            await edit(w, card)
            await w.fri.on_raw_message_edit(SimpleNamespace(channel_id=1, message=edited(unrelated)))  # Names no server.
            await settled(w)
            assert not asked(w)
            await edit(w, edited(unrelated))  # Where Fri chats: asked, and nothing holds it.
            assert asked(w) == [{'guild_id': '123', 'message_ids': [str(unrelated.id)]}]
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_bulk_delete_asks_ten_at_a_time(monkeypatch, tmp_path):
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            first = w.said('one of many')
            await delete(w, *range(first.id, first.id+25))
            assert sorted(len(p['message_ids']) for p in asked(w)) == [5, 10, 10]
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_an_older_controller_is_asked_nothing_by_id(monkeypatch, tmp_path):
    async def scenario():
        w = await world(monkeypatch, tmp_path, capable=False)
        try:
            earlier, later = w.said('fri explain '+MARKER), w.said('fri more on the '+MARKER+' thing')
            await w.agent.handle(earlier)
            await w.agent.handle(later)
            long_after(w)
            await edit(w, edited(later))  # Saved this run: withdrawn as a delete of it already was.
            assert len(asked(w, WITHDRAW)) == 1
            restarted(w)
            await edit(w, edited(earlier))
            await delete(w, earlier.id)
            assert not asked(w) and len(asked(w, WITHDRAW)) == 1
            assert 'fri explain '+MARKER in saved(w)  # What a restart forgot stays, as before.
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_failed_ask_is_logged_without_words(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        api = w.fri.api
        async def failing(method, path, payload=None):
            if path == SAVED:
                raise TimeoutError()
            return await api(method, path, payload)
        w.fri.api = failing
        try:
            earlier = w.said('fri explain '+MARKER)
            await w.agent.handle(earlier)
            restarted(w)
            await edit(w, edited(earlier))
            later = w.said('fri hi')
            await w.agent.handle(later)
            later.reply.assert_awaited()
            assert any('not dropped' in line and 'TimeoutError' in line for line in caplog.messages)
            assert MARKER not in caplog.text
        finally:
            await w.fri.close()
    asyncio.run(scenario())
