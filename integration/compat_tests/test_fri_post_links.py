"""Fri links a post it found by pointing at it: only a link it was shown goes out, never a made-up one."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_times import delivered, model, saved_replies, shown

FOUND = 'https://discord.com/channels/123/2/1300000000000000005'
MADE_UP = 'https://discord.com/channels/123/2/1300000000000000009'


def read(url=FOUND):
    """What a read showed the model: one post, with its link."""
    return {'action_result': {'kind': 'history_search', 'messages': [
        {'id': url.rsplit('/', 1)[1], 'author': 'kev', 'text': '', 'attachments': ['video'], 'source': url}]}}


@pytest.mark.parametrize('reply, sent', [
    (f'this one: {FOUND}', f'this one: <{FOUND}>'),  # Kept, with no preview.
    (f'this one: <{FOUND}>', f'this one: <{FOUND}>'),
    (f'[kev’s clip]({FOUND})', f'[kev’s clip](<{FOUND}>)'),
    (f'[kev’s clip](<{FOUND}>)', f'[kev’s clip](<{FOUND}>)'),
    (f'this one: {MADE_UP}', 'this one: '),  # Never shown: dropped.
    (f'[kev’s clip]({MADE_UP})', 'kev’s clip'),  # The label stays.
    (f'{FOUND.replace("discord.com", "ptb.discord.com")}', 'can’t link that one.'),  # Not the exact link shown.
    (f'{FOUND}5', 'can’t link that one.'),
    ('kev posted it tuesday, no link needed', 'kev posted it tuesday, no link needed'),
    ('the docs: https://example.com/a', 'the docs: https://example.com/a'),  # Not a post link: left to cite_reply.
])
def test_a_reply_keeps_only_post_links_the_model_was_shown(reply, sent):
    from fri.fri_links import link_posts
    assert link_posts(reply, read()) == sent


@pytest.mark.parametrize('where', ['source', 'reply_target', 'invited_context', 'inspected_images', 'action_results'])
def test_shown_links_come_from_what_the_model_read(where):
    from fri.fri_links import link_posts, shown
    row = {'source': {'id': '1300000000000000005', 'url': FOUND}, 'text': f'see {MADE_UP}'}
    context = {'source': {'url': FOUND}} if where == 'source' else {
        'action_results': [{'messages': [{'source': FOUND}]}]} if where == 'action_results' else {where: [row]}
    assert shown(context) == {FOUND}  # A link typed in someone's words isn't one Fri was shown.
    assert link_posts(f'{FOUND} {MADE_UP}', context) == f'<{FOUND}> '
    assert shown({'recent': [row], 'turns': [{'reply': FOUND}], 'summary': FOUND}) == set()


def test_a_read_then_the_answer_links_the_post_it_found_and_drops_a_made_up_one(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, channel, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        clip = message('the clip from tuesday', user=30, age=15*60)
        clip.jump_url = f'https://discord.com/channels/123/1/{clip.id}'
        event = message('<@99> where was kev’s clip')
        event.jump_url = f'https://discord.com/channels/123/1/{event.id}'
        fake = f'https://discord.com/channels/123/1/{clip.id+1}'
        sent = model(fri, tmp_path, [{'action': {'kind': 'read_recent', 'channel_id': str(channel.id), 'window': 'recent'}},
                                     f'here: {clip.jump_url} (or {fake})'])
        texts = delivered(event)
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            assert clip.jump_url in str(shown(sent[1])['action_result_untrusted'])
            assert texts == [f'here: <{clip.jump_url}> (or )']
            assert saved_replies(calls)[0].startswith('Earlier check: read_recent')  # A read's answer isn't kept.
            assert 'link posts you found' in sent[0]['instructions']
        finally:
            await fri.close()
    asyncio.run(scenario())
