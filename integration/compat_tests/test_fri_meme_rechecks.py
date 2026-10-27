"""The timer's meme check takes one last look at a post right before the model sees it, and a renewed CDN
signature is the same upload, not a change."""

import asyncio
import copy
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from compat_tests.test_fri_learning import learning as learning  # noqa: F401
from compat_tests.test_fri_lurker_reactions import chat as chat  # noqa: F401
from compat_tests.test_fri_lurker_reactions import model, waiting
from compat_tests.test_fri_memes import attachment
from compat_tests.test_fri_memes import harness as harness  # noqa: F401
from fri.fri_errors import ControllerUnavailable
from fri.privacy import clean

CAPTION = 'me explaining the group project to the one guy who joined yesterday. ' * 9  # Over 600 characters.
UPLOAD = 'https://cdn.discordapp.com/attachments/3/55/meme.png'


def signed(url, hm):
    """A Discord CDN link as Discord serves it: signed, with a signature it renews on refetch."""
    return f'{url}?ex=6720d7a1&is=671f8621&hm={hm}'


def meme(h):
    picture = attachment()
    picture.url = signed(UPLOAD, 'a1')
    return h.add(100, content=CAPTION, attachments=[picture])


def discord_has(h, post, **changes):
    """Discord's copy of the post from now on; the timer's copy stays as it was read."""
    current = copy.copy(post)
    for key, value in changes.items():
        setattr(current, key, value)
    h.rows[h.rows.index(post)] = current
    return current


async def checked_while_waiting(h, change):
    """The timer's check of post 100, with `change` made while it waits on the model slot."""
    await h.agent.slots.acquire()  # Another model call is running.
    check = asyncio.create_task(h.agent.memes.check())
    await waiting(h, 100)
    change()
    h.agent.slots.release()
    await check


@pytest.mark.parametrize('change', ['edited_past_500', 'replaced', 'deleted', 'opted_out', 'forgotten',
                                    'controller_down'])
def test_a_meme_post_changed_while_its_check_waits_never_reaches_the_model(harness, monkeypatch, change):  # noqa: F811
    async def scenario():
        h = harness
        analyze = model(h, monkeypatch)
        post = meme(h)
        now = dt.datetime.fromtimestamp(h.clock[0], dt.timezone.utc)
        replaced = attachment()
        replaced.url = signed('https://cdn.discordapp.com/attachments/3/56/meme.png', 'c3')
        changes = {
            # Only past the first 500 characters: a cached copy of the caption would look the same.
            'edited_past_500': lambda: discord_has(h, post, content=CAPTION[:600]+'and my old address', edited_at=now),
            # Another upload with no edit time, as an embed can change: only the images tell.
            'replaced': lambda: discord_has(h, post, attachments=[replaced]),
            'deleted': lambda: setattr(h.channel.fetch_message, 'side_effect',
                                       discord.NotFound(SimpleNamespace(status=404, reason='gone'), 'deleted')),
            'opted_out': lambda: h.store.personal_control('11', 'disable'),
            'forgotten': lambda: h.agent.forget(11),
            'controller_down': lambda: setattr(h.agent, 'profile', AsyncMock(side_effect=ControllerUnavailable('down')))}
        await checked_while_waiting(h, changes[change])
        analyze.assert_not_awaited()
        post.add_reaction.assert_not_awaited()
        with h.agent.memes.state(write=False) as state:
            assert state['entries']['100']['status'] == 'skipped'  # Considered, so not judged again later.
    asyncio.run(scenario())


def test_the_last_look_lets_an_unchanged_meme_through_with_a_renewed_cdn_signature(harness, monkeypatch):  # noqa: F811
    async def scenario():
        h = harness
        analyze = model(h, monkeypatch)
        post = meme(h)
        renewed = copy.copy(post.attachments[0])
        renewed.url = signed(UPLOAD, 'b2')  # Only the signature: the same upload.
        await checked_while_waiting(h, lambda: discord_has(h, post, attachments=[renewed]))
        analyze.assert_awaited_once()
        data = analyze.await_args.args[2]
        assert data['caption'] == clean(CAPTION, 1000) and data['image_count'] == 1
        post.add_reaction.assert_awaited_once_with('💀')  # On Discord's copy: the same post.
        with h.agent.memes.state(write=False) as state:
            assert state['entries']['100']['status'] == 'reacted'
    asyncio.run(scenario())


@pytest.mark.parametrize('kind,change', [('plain', 'deleted'), ('plain', 'forgotten'), ('chat', 'edited')])
def test_a_meme_withdrawn_during_the_last_looks_privacy_read_never_reaches_the_model(request, monkeypatch, kind, change):
    """The last look reads Discord first and checks Fri's own marks last, so a change while it reads still counts."""
    async def scenario():
        if kind == 'chat':  # A meme channel Fri also chats in: the post is in Fri's live view.
            h = request.getfixturevalue('chat')
            analyze = model(h, monkeypatch)
            picture = attachment()
            picture.url = signed(UPLOAD, 'a1')
            post = await h.say(100, 11, CAPTION, attachments=[picture])
            assert any(row['id'] == '100' for row in h.agent.recent[3])
        else:
            h = request.getfixturevalue('harness')
            analyze = model(h, monkeypatch)
            post = meme(h)
        armed, real = [], h.agent.profile

        def withdraw():
            if change == 'deleted':  # Discord's delete event, as Fri.on_raw_message_delete passes it on.
                h.channel.fetch_message.side_effect = discord.NotFound(SimpleNamespace(status=404, reason='gone'), 'x')
                h.agent.unsend(3, [100])
            elif change == 'forgotten':
                h.agent.forget(11)
            else:  # Discord's edit event, as Fri.on_raw_message_edit passes it on.
                h.agent.edit(3, discord_has(h, post, content='nvm '+CAPTION, edited_at=dt.datetime.now(dt.timezone.utc)))

        async def profile(*args):
            answer = await real(*args)  # The controller's answer, from before the change below.
            if armed and armed.pop():
                withdraw()
            return answer

        h.agent.profile = profile
        await checked_while_waiting(h, lambda: armed.append(True))  # The next privacy read is the last look's.
        analyze.assert_not_awaited()
        post.add_reaction.assert_not_awaited()
        with h.agent.memes.state(write=False) as state:
            assert state['entries']['100']['status'] == 'skipped'
    asyncio.run(scenario())

