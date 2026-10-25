"""History search finds a post by what it carries ("that meme kev posted"), like Discord's has: filter."""

import asyncio
from unittest.mock import Mock

import discord
import pytest

from compat_tests.test_fri_history_search import query, setup
from compat_tests.test_fri_looking import loads, picture
from fri.fri_chat import INSTRUCTIONS
from fri.fri_history_search import HAS, requested, valid_action


def test_has_takes_only_discords_own_kinds():
    assert all(valid_action(query(has=kind)) for kind in HAS)
    assert not valid_action(query(has='pdf')) and not valid_action(query(has='image,video'))
    assert not valid_action({key: value for key, value in query().items() if key != 'has'})


@pytest.mark.parametrize('text', ['find the meme kev posted last week', 'pull up the pic jo sent',
                                  'can you dig up that clip sam shared', 'search for the screenshot i uploaded'])
def test_asking_for_a_post_by_what_it_carries_opens_search(text):
    assert requested(text)


@pytest.mark.parametrize('text', ['find me a funny meme', 'find a picture of a cat', 'send me a pic',
                                  'look up gif keyboards', 'what is the best video editor',
                                  'find a file on a shared drive', 'look up images of shared office spaces'])
def test_asking_for_media_in_general_does_not(text):
    assert not requested(text)


def test_a_has_image_search_finds_an_uncaptioned_meme_and_looks_at_it(monkeypatch, tmp_path):
    async def scenario():
        text = 'find the meme Dipper posted last week'
        fri, agent, actions, channels, rows, messages = setup(monkeypatch, tmp_path, text=text)
        meme, plain = rows[2], rows[3]
        meme['content'] = plain['content'] = ''
        messages[int(plain['id'])].content = ''
        old = messages[int(meme['id'])]  # A real-shaped Discord message, as a look needs.
        messages[old.id] = Mock(spec=discord.Message, id=old.id, author=old.author, content='', webhook_id=None,
                                created_at=old.created_at, jump_url=old.jump_url, edited_at=None, embeds=[],
                                attachments=[picture(1, 'meme.png')], channel=channels[1], guild=channels[1].guild)
        loads(monkeypatch)
        actions.turn_context, actions.media_allowed = {}, True
        try:
            receipt = await actions.execute(query(query='', author='Dipper', has='image'))
            assert fri.http.request.await_args_list[-1].kwargs['params']['has'] == 'image'
            found = [row for row in receipt['messages'] if row['id'] == meme['id']]
            assert found and found[0]['text'] == '' and found[0]['attachments'] == ['image']
            # An empty post that carries nothing is still no find.
            assert not any(row['id'] == plain['id'] for row in receipt['messages'])
            assert receipt['images_inspected'] == 1 and receipt['has'] == 'image'
            assert actions.turn_context['inspected_images'][0]['source']['id'] == meme['id']
            assert await actions.valid()
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_the_instructions_name_the_filter():
    assert 'has=image' in INSTRUCTIONS


@pytest.mark.parametrize('why', ['download_failed', 'no_inspection'])
def test_an_image_hit_fri_didnt_look_at_is_still_rechecked(monkeypatch, tmp_path, why):
    """Evidence keeps each hit's edit time and attachments, so removing the image fails the recheck."""
    async def scenario():
        text = 'find the meme Dipper posted last week'
        fri, agent, actions, channels, rows, messages = setup(monkeypatch, tmp_path, text=text)
        meme = rows[2]
        meme['content'] = ''
        old = messages[int(meme['id'])]
        messages[old.id] = Mock(spec=discord.Message, id=old.id, author=old.author, content='', webhook_id=None,
                                created_at=old.created_at, jump_url=old.jump_url, edited_at=None, embeds=[],
                                attachments=[picture(1, 'meme.png')], channel=channels[1], guild=channels[1].guild)
        loads(monkeypatch, fail={'meme.png'} if why == 'download_failed' else ())
        actions.turn_context, actions.media_allowed = {}, why != 'no_inspection'
        try:
            receipt = await actions.execute(query(query='', author='Dipper', has='image'))
            assert receipt.get('images_inspected', 0) == 0
            assert [row['attachments'] for row in receipt['messages'] if row['id'] == meme['id']] == [['image']]
            assert await actions.valid()
            edited = messages[old.id]
            edited.attachments, edited.edited_at = [], discord.utils.utcnow()  # The image is taken down.
            assert not await actions.valid()
        finally:
            await fri.close()
    asyncio.run(scenario())
