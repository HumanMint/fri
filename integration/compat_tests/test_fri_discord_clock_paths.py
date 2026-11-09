"""HumanMint/fri#368 as Fri reads it, with HumanMint/gene#219's follow-up in Gene: a member's note removal was dated by
Gene's clock, so with Gene's clock behind Discord's, their post from just before the removal counted as after it and
reached the model through a link; ahead, one from just after it was left out. Gene dates a removal by the panel press,
and says it on Discord's clock; and in a history channel, whose narrower answer older Fris check field for field, Fri
asks for it (discord_cutoffs, where Gene offers history-cutoffs)."""

import asyncio
import datetime as dt
import time
from contextlib import contextmanager

import discord
import pytest

from compat_tests.test_fri_links_fences import everyone_reads, hosted, signing_in
from compat_tests.test_fri_member_profiles import DM, GAMING, GENERAL, world

MARKER = 'the alder marker'
REAL = time.time


@contextmanager
def gene_clock(offset):
    """Gene's clock `offset` seconds off Discord's, which the snowflakes here keep."""
    time.time = lambda: REAL() + offset
    try:
        yield
    finally:
        time.time = REAL


def removes_a_note(w, offset, at, user='30'):
    """Member `user` removes a profile note through My memory, the panel press sent at `at` on Discord's clock, with
    Gene's clock `offset` seconds off Discord's while it carries it out."""
    from gene.fri_memory_store import edit, entries, save
    with w.store.connection() as db:
        save(db, '123', 'server', user, ('games', 'I like Minecraft'), REAL() - 60, provenance=[])
    conversation = f'private:{DM}'
    note = entries(w.store, '123', conversation, user)['entries'][0]
    pressed = {'kind': 'discord_interaction', 'user_id': user, 'display_name': f'member {user}', 'channel_id': str(DM),
               'guild_id': None, 'id': str(discord.utils.time_snowflake(dt.datetime.fromtimestamp(at, dt.timezone.utc)))}
    with gene_clock(offset + at - REAL()):
        edit(w.store, '123', conversation, user, note['id'], w.store.chat_context('123', conversation, user)['revision'],
             pressed, 'remove')


@pytest.mark.parametrize('where', ['a chat channel', 'a history channel'])
@pytest.mark.parametrize('offset', [-2, 2])
def test_a_linked_post_is_held_to_its_authors_note_removal_on_discords_clock(monkeypatch, tmp_path, offset, where):
    """Member 30 removes a note at T. With Gene two seconds behind, their post from T-0.5, linked from member 20's DM,
    reached the model: Gene had stamped the removal T-2. With Gene two seconds ahead, their post from T+0.5 was left
    out: stamped T+2. On Discord's clock the first is before the removal and stays out, and the second is read. In a
    history channel too, through its narrower answer."""
    async def scenario():
        history = where == 'a history channel'
        w = world(monkeypatch, tmp_path, **({'agent_channels': '1,5,6', 'history_channels': str(GAMING)}
                                            if history else {}))
        cid = GAMING if history else GENERAL
        removal = REAL() - 1  # A second ago, so a post from after it is already sent.
        removes_a_note(w, offset, removal)
        post_at = removal - 0.5 if offset < 0 else removal + 0.5
        post = w.message(cid, 'my answer is ' + MARKER, user=30, age=REAL() - post_at)
        hosted(w, cid, post)
        everyone_reads(w, cid)
        text = f'fri thoughts on https://discord.com/channels/123/{cid}/{post.id}'
        sent = signing_in(w, lambda: None)
        try:
            await w.fri.talk(123, DM, 20, text, 'private', 'member 20', event=w.message(DM, text))
            assert sent, 'no model call'
            assert (MARKER in str(sent[0]['reply_target'])) is (offset > 0)
        finally:
            await w.fri.close()
    asyncio.run(scenario())
