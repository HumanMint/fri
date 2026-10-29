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
    # A masked link's label follows the same rule, even when its target is another form of a shown link.
    (f'[{MADE_UP}]({FOUND}/)', 'can’t link that one.'),
    (f'[{MADE_UP}](<{FOUND}>)', f'<{FOUND}>'),
    # Markdown right after a shown link isn't part of it, and neither is the word after that.
    (f'**{FOUND}**this is the clip', f'**<{FOUND}>**this is the clip'),
    (f'||{FOUND}||spoiler', f'||<{FOUND}>||spoiler'), (f'~~{FOUND}~~gone', f'~~<{FOUND}>~~gone'),
    (f'__{FOUND}__under', f'__<{FOUND}>__under'),
    # Dropping one link can't join the text around it into another post link nobody was shown.
    (f'https://discord.com/channels/123<{MADE_UP}>/2/1300000000000000009', 'can’t link that one.'),
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


NOT_SHOWN = MADE_UP.split('://', 1)[1]  # discord.com/channels/…, written below with each variant's scheme and host.


@pytest.mark.parametrize('url', [
    # fri #178: every way Discord links a post the model wasn't shown, each compared whole against what it was shown.
    f'{MADE_UP}/', f'{MADE_UP.replace("discord.com", "ptb.discord.com")}/',
    f'{MADE_UP.replace("discord.com", "canary.discord.com")}/', f'https://DISCORD.COM/{NOT_SHOWN.split("/", 1)[1]}',
    f'http://{NOT_SHOWN}', f'HTTPS://{NOT_SHOWN}', f'https://www.{NOT_SHOWN}', f'https://Discord.com/{NOT_SHOWN.split("/", 1)[1]}',
    f'{MADE_UP}?x=1', f'{MADE_UP}#top', f'{MADE_UP}/extra', f'https://{NOT_SHOWN.replace(".com", ".com:443")}',
    f'https://kev@{NOT_SHOWN}', f'https://discordapp.com/{NOT_SHOWN.split("/", 1)[1]}/',
    f'https://discord.com/CHANNELS/{NOT_SHOWN.split("/", 2)[2]}', f'{FOUND}/', f'{FOUND}?ref=1',
    f'https://www.{FOUND.split("://", 1)[1]}', f'http://{FOUND.split("://", 1)[1]}',
])
@pytest.mark.parametrize('form, sent', [('this one: {}', 'this one: '), ('this one: <{}>', 'this one: '),
                                        ('[kev’s clip]({})', 'kev’s clip'), ('[kev’s clip](<{}>)', 'kev’s clip'),
                                        ('this one: {}.', 'this one: .'), ('(this one: {})', '(this one: )')])
def test_a_post_link_in_any_form_goes_unless_it_is_the_exact_one_shown(url, form, sent):
    from fri.fri_links import link_posts
    assert link_posts(form.format(url), read()) == sent


@pytest.mark.parametrize('reply, sent', [
    (f'here: {FOUND}.', f'here: <{FOUND}>.'),  # Sentence punctuation isn't part of the link, as Discord reads it.
    (f'(here: {FOUND})', f'(here: <{FOUND}>)'), (f'here: {FOUND}, ok', f'here: <{FOUND}>, ok'),
    (f'"{FOUND}"', f'"<{FOUND}>"'), (f'[kev’s clip]( <{FOUND}> )', f'[kev’s clip](<{FOUND}>)'),
    (f'is it {FOUND}?', f'is it <{FOUND}>?'), (f'**{FOUND}**', f'**<{FOUND}>**'), (f'||{FOUND}||', f'||<{FOUND}>||'),
    (f'_{FOUND}_', f'_<{FOUND}>_'), (f'~~{FOUND}~~!', f'~~<{FOUND}>~~!'), (f"'{FOUND}'", f"'<{FOUND}>'"),
    (f'here {FOUND}👀', f'here <{FOUND}>👀'), (f'{FOUND}—that one', f'<{FOUND}>—that one'),
    # Not a Discord post: a channel, an invite or another site.
    ('https://discord.com/channels/123/2', 'https://discord.com/channels/123/2'),
    ('https://discord.com/invite/abc', 'https://discord.com/invite/abc'),
    (f'https://example.com/{NOT_SHOWN}', f'https://example.com/{NOT_SHOWN}'),
    (f'https://notdiscord.com/{NOT_SHOWN.split("/", 1)[1]}', f'https://notdiscord.com/{NOT_SHOWN.split("/", 1)[1]}'),
    (f'https://discord.com.example/{NOT_SHOWN.split("/", 1)[1]}', f'https://discord.com.example/{NOT_SHOWN.split("/", 1)[1]}'),
    (f'https://example.com/?next={MADE_UP}', 'https://example.com/?next='),  # Still shaped like a post link: goes.
    (f'[{MADE_UP}](https://example.com)', '[](https://example.com)'),  # A post link as the label still goes.
])
def test_sentence_punctuation_and_other_links_are_read_as_discord_reads_them(reply, sent):
    from fri.fri_links import link_posts
    assert link_posts(reply, read()) == sent


def respond_with(action, context):
    import json

    import httpx

    from compat_tests.test_fri_chat import response_event
    from fri.fri_chat import Chat

    def model_reply(request):
        output = {'reply': '', 'request_draft': None, 'action': action, 'notes': []}
        return httpx.Response(200, text='data: '+json.dumps(response_event(content=json.dumps(output)))+'\n\n')
    chat = Chat(type('Auth', (), {'access_token': staticmethod(lambda: 'fictional')}), httpx.MockTransport(model_reply))
    return asyncio.run(chat.respond('which clip, the one in https://discord.com/channels/123/2/1 or the other', context))


@pytest.mark.parametrize('question, kept', [
    (f'this one {MADE_UP} or the tuesday one?', 'this one  or the tuesday one?'),
    (f'[this one]({MADE_UP.upper()}) or the tuesday one?', 'this one or the tuesday one?'),
    (f'this one <{FOUND}> or {MADE_UP}/?', f'this one <{FOUND}> or ?'),
])
def test_a_clarifying_question_keeps_only_post_links_the_model_was_shown(question, kept):
    """fri #178: the question is delivered and saved as Fri's words, so it gets the reply's filter. The topic stays
    the member's exact words, link and all."""
    topic = 'the one in https://discord.com/channels/123/2/1'
    context = {'turns': [], 'facts': [], 'model': 'gpt-6.1-sol', 'effort': 'high',
               'actions': {'clarify': {'goals': ['web']}}, **read()}
    result = respond_with({'kind': 'clarify', 'goal': 'web', 'topic': topic, 'question': question}, context)
    assert result.status == 'completed' and result.action == {'kind': 'clarify', 'goal': 'web', 'topic': topic,
                                                              'question': kept}


@pytest.mark.parametrize('question', [MADE_UP, f'{MADE_UP}?', f'<{MADE_UP}>'])
def test_a_clarifying_question_that_was_only_a_made_up_link_says_so_instead(question):
    context = {'turns': [], 'facts': [], 'model': 'gpt-6.1-sol', 'effort': 'high',
               'actions': {'clarify': {'goals': ['web']}}, **read()}
    result = respond_with({'kind': 'clarify', 'goal': 'web', 'topic': 'which clip', 'question': question}, context)
    assert (result.status, result.response_kind, result.action, result.reply) == (
        'completed', 'reply', None, 'can’t link that one.')


def test_a_clarifying_question_goes_out_and_is_saved_without_a_made_up_link(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, _, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        topic = 'what time is the chess final for everyone'
        event = message(f'fri {topic}, tomorrow or friday?')
        fake = f'https://DISCORD.com/channels/123/1/{event.id+7}/'
        model(fri, tmp_path, [{'action': {'kind': 'clarify', 'goal': 'web', 'topic': topic,
                                          'question': f'the one in {fake} or friday?'}}])
        texts = delivered(event)
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            assert texts == ['the one in  or friday?']
            assert saved_replies(calls) and not any('channels' in reply for reply in saved_replies(calls))
        finally:
            await fri.close()
    asyncio.run(scenario())
