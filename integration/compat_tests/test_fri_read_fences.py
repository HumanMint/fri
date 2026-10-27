"""Every post a read showed, captioned meme or plain text, is found again unchanged by one history
refetch before each later model request and before delivery."""
import asyncio
import copy
import datetime as dt
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from compat_tests.test_fri_looking_fences import model, picture
from compat_tests.test_fri_member_profiles import DM, GENERAL, world
from fri.fri import Fri


def snowflake(bound):
    return discord.utils.time_snowflake(bound) if isinstance(bound, dt.datetime) else bound.id


def read_world(monkeypatch, tmp_path, captions, *, images=False):
    """#general holds `captions`, oldest first, from member 30; Fri reads it from a DM, then answers."""
    w = world(monkeypatch, tmp_path)
    general, rows = w.channels[GENERAL], {}

    def post(text, age):
        item = w.message(GENERAL, text, user=30, age=age)
        if images:
            item.attachments = [picture(GENERAL, f'{item.id}.png')]
        item.created_at = discord.utils.snowflake_time(item.id)
        item.jump_url = f'https://discord.com/channels/123/{GENERAL}/{item.id}'
        rows[item.id] = item
        return item
    posts = [post(text, 300-10*n) for n, text in enumerate(captions)]
    windows = []

    async def history(*, limit=100, after=None, before=None, oldest_first=None):
        windows.append({'after': after, 'before': before})
        found = [item for item in sorted(rows.values(), key=lambda m: m.id, reverse=not oldest_first)
                 if (after is None or item.id > snowflake(after)) and (before is None or item.id < snowflake(before))]
        for item in found[:limit]:
            yield item

    async def fetch(mid):
        if mid not in rows:
            raise discord.NotFound(SimpleNamespace(status=404, reason='deleted'), 'deleted')
        return rows[mid]
    general.history = history
    general.fetch_message = AsyncMock(side_effect=fetch)
    monkeypatch.setattr('fri.fri_images.load_image', AsyncMock(return_value='data:image/png;base64,AA=='))
    w.fri.chat_once = MethodType(Fri.chat_once, w.fri)
    sent = []
    w.fri.chat = model(sent, GENERAL, 'the third one, obviously')
    return w, posts, rows, sent, post, windows


def edited(item):
    changed = copy.copy(item)
    changed.content, changed.edited_at = 'nvm', dt.datetime.now(dt.timezone.utc)
    return changed


def during_second_sign_in(w, happen):
    sign_ins = []

    async def sign_in():
        sign_ins.append(1)
        if len(sign_ins) == 2:  # After the read, while Fri signs in for the request that carries it.
            happen()
        return {'Authorization': 'Bearer synthetic'}
    w.fri.chat.headers = sign_in
    return sign_ins


def test_a_caption_the_image_budget_skipped_is_rechecked_too(monkeypatch, tmp_path):
    """The repro from fri #37: five captioned memes, four looked at, the fifth deleted mid-sign-in."""
    async def scenario():
        w, posts, rows, sent, _, _ = read_world(monkeypatch, tmp_path, [f'meme {n}' for n in range(5)], images=True)
        sign_ins = during_second_sign_in(w, lambda: rows.pop(posts[0].id))
        text = 'fri pick the best meme in general'
        try:
            result = await w.fri.talk(123, DM, 20, text, 'private', event=w.message(DM, text))
            assert len(sign_ins) == 2 and len(sent) == 1  # The caption never went out.
            assert 'access or memory changed' in result.reply
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['none', 'new_post', 'edit', 'delete'])
def test_a_text_post_changed_after_the_read_never_reaches_the_next_request(monkeypatch, tmp_path, change):
    async def scenario():
        w, posts, rows, sent, post, windows = read_world(monkeypatch, tmp_path, ['movie night?', 'friday works'])

        def happen():
            if change == 'new_post':  # Newer than anything read: not a change to what Fri saw.
                post('also bring snacks', age=0)
            elif change == 'edit':
                rows[posts[1].id] = edited(posts[1])
            elif change == 'delete':
                rows.pop(posts[0].id)
        sign_ins = during_second_sign_in(w, happen)
        text = 'fri what did general decide'
        try:
            if change in ('none', 'new_post'):
                result = await w.fri.talk(123, DM, 20, text, 'private', event=w.message(DM, text))
                assert result.reply == 'the third one, obviously' and len(sent) == 2
                # One refetch per read per check, pinned to what the read scanned.
                assert windows[1:] and all(window['before'].id == posts[-1].id+1 for window in windows[1:])
                return
            result = await w.fri.talk(123, DM, 20, text, 'private', event=w.message(DM, text))
            assert len(sign_ins) == 2 and len(sent) == 1
            assert 'access or memory changed' in result.reply
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_post_deleted_while_fri_answers_keeps_the_answer_from_going_out(monkeypatch, tmp_path):
    async def scenario():
        w, posts, rows, sent, _, _ = read_world(monkeypatch, tmp_path, ['movie night?', 'friday works'])
        respond = w.fri.chat.respond

        async def answer_then_delete(*args, **kwargs):
            result = await respond(*args, **kwargs)
            if len(sent) == 2:  # The answer is written; the post it drew on goes away before delivery.
                rows.pop(posts[1].id)
            return result
        w.fri.chat.respond = answer_then_delete
        text = 'fri what did general decide'
        try:
            result = await w.fri.talk(123, DM, 20, text, 'private', event=w.message(DM, text))
            assert len(sent) == 2 and 'access or memory changed' in result.reply
        finally:
            await w.fri.close()
    asyncio.run(scenario())
