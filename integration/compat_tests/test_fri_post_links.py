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
PATH, ID = NOT_SHOWN.split('/', 1)[1], MADE_UP.rsplit('/', 1)[1]  # channels/123/2/1300…9, and its message's ID.


@pytest.mark.parametrize('url', [
    # fri #178: every way Discord links a post the model wasn't shown, each compared whole against what it was shown.
    f'{MADE_UP}/', f'{MADE_UP.replace("discord.com", "ptb.discord.com")}/',
    f'{MADE_UP.replace("discord.com", "canary.discord.com")}/', f'https://DISCORD.COM/{NOT_SHOWN.split("/", 1)[1]}',
    f'http://{NOT_SHOWN}', f'HTTPS://{NOT_SHOWN}', f'https://www.{NOT_SHOWN}', f'https://Discord.com/{NOT_SHOWN.split("/", 1)[1]}',
    f'{MADE_UP}?x=1', f'{MADE_UP}#top', f'{MADE_UP}/extra', f'https://{NOT_SHOWN.replace(".com", ".com:443")}',
    f'https://kev@{NOT_SHOWN}', f'https://discordapp.com/{NOT_SHOWN.split("/", 1)[1]}/',
    f'https://discord.com/CHANNELS/{NOT_SHOWN.split("/", 2)[2]}', f'{FOUND}/', f'{FOUND}?ref=1',
    f'https://www.{FOUND.split("://", 1)[1]}', f'http://{FOUND.split("://", 1)[1]}',
    # fri #200: forms a browser reads as a post link: an encoded or mapped host, dot segments, a backslash, extra
    # slashes, an @ in a sign-in, an encoded path.
    *(f'https://{host}/{PATH}' for host in ('%64iscord.com', 'disc%6Frd.com', 'discord%2Ecom', 'DISCORD%2ECOM',
                                            'ｄｉｓｃｏｒｄ.com', 'discord。com', 'dis\u00adcord.com', 'kev@x@discord.com')),
    *(f'https://discord.com/{dots}/{PATH}' for dots in ('unused/..', 'unused/%2e%2E', 'a/b/../..', '.', '%2e')),
    f'https://discord.com/channels/123/x/../2/{ID}', f'https://discord.com/channels/123/2/./{ID}',
    'https://discord.com\\'+PATH.replace('/', '\\'), f'https:\\\\discord.com/{PATH}', f'https:////discord.com/{PATH}',
    f'https:discord.com/{PATH}', f'https://discord.com/%63hannels/123/2/{ID}',
    f'https://discord.com/channels/%31%32%33/2/{ID}', f'https://discord.com/channels/123/2/%31{ID[1:]}',
    f'https://%64iscord.com/{FOUND.split("/", 3)[3]}', f'https://discord.com/x/../{FOUND.split("/", 3)[3]}',
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
    (f'{MADE_UP}/?utm_source=discord', 'can’t link that one.'), (f'see {MADE_UP}?a_b~c=1 ok', 'see  ok'),
    # fri #203: markdown opened before a link closes inside its tail, and the word after it stays. Not opened
    # there, "__" and "~~" are the link's own, as "_" and "~" are.
    (f'before __{MADE_UP}?x=1__z after', 'before ____z after'), (f'~~{MADE_UP}?x=1~~y', '~~~~y'),
    (f'__{FOUND}?x=1__z', '____z'), (f'~~{FOUND}?x=1~~y', '~~~~y'),  # Not the exact link shown: it goes too.
    (f'**{MADE_UP}?x=1**this', '****this'), (f'||{MADE_UP}?x=1||x', '||||x'),
    (f'__see {MADE_UP}?utm_source=x__z', '__see __z'), (f'~~see {MADE_UP}#a~b~~ok', '~~see ~~ok'),
    (f'__a__ {MADE_UP}?a__b=1 ok', '__a__  ok'), (f'see {MADE_UP}?a~~b=1 ok', 'see  ok'),
    (f'__<{MADE_UP}?x=1>__z', '____z'), (f'__[clip]({MADE_UP}?x=1)__z', '__clip__z'),
    (f'__{FOUND}__z and __{MADE_UP}?x__y', f'__<{FOUND}>__z and ____y'),
    # fri #200: a browser drops tabs and newlines, which only <…> can hold. And a link it reads as another path isn't
    # a post link.
    ('this one: <'+MADE_UP.replace('channels', 'chan\nnels')+'>', 'this one: '),
    ('this one: <'+MADE_UP.replace('discord', 'disc\tord')+'> ok', 'this one:  ok'),
    ('https://discord.com/channels/123/../2/1300000000000000009', 'https://discord.com/channels/123/../2/1300000000000000009'),
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


# fri #208: a browser drops a tab or newline anywhere in a link, the scheme too, and a control character before it.
SPLIT = [MADE_UP.replace('https', 'htt\tps'), MADE_UP.replace('https', 'h\nttps'), MADE_UP.replace('https', 'HTTPS\r\n'),
         MADE_UP.replace('https:', 'h\tt\tt\tp\ts\t:'), MADE_UP.replace('https', 'http\ns')]


@pytest.mark.parametrize('url', SPLIT)
@pytest.mark.parametrize('form, sent', [
    ('this one: <{}>', 'this one: '),  # Nothing else in it reads as a link, so this is the preflight's.
    ('docs at https://example.com/a, this one: <{}>', 'docs at https://example.com/a, this one: '),  # The pattern's.
    ('[kev’s clip]({})', 'kev’s clip'), ('[kev’s clip](<{}>)', 'kev’s clip'),
    ('this one: {} ok', 'this one:  ok'),
])
def test_a_post_link_with_a_tab_or_newline_in_its_scheme_goes(url, form, sent):
    from fri.fri_links import link_posts
    assert link_posts(form.format(url), read()) == sent


@pytest.mark.parametrize('reply, sent', [
    # Another form of the shown link: only the exact one stays.
    ('this one: <'+FOUND.replace('https', 'htt\tps')+'>', 'this one: '),
    (f'{FOUND} or <'+MADE_UP.replace('https', 'htt\nps')+'>', f'<{FOUND}> or '),
    # A tab, newline or control character before the scheme, with more in the host.
    ('this one: <\thttps://disc\tord.com/'+PATH+'>', 'this one: '),
    ('this one: <\x0bhtt\tps://disc\nord.com/'+PATH+'>', 'this one: '),
    ('this one: [kev’s clip](<\x01htt\tps://discord.com/'+PATH+'>)', 'this one: kev’s clip'),
])
def test_a_split_scheme_never_passes_for_the_shown_link_or_hides_another(reply, sent):
    from fri.fri_links import link_posts
    assert link_posts(reply, read()) == sent


@pytest.mark.parametrize('reply, sent', [
    # fri #209: inside <…>, its whole path is the link's, so a browser resolves ".." through to the end: these name
    # the channel, not a post.
    ('<https://discord.com/x/../channels/123/2/1300000000000000009/..>',
     '<https://discord.com/x/../channels/123/2/1300000000000000009/..>'),
    ('[the channel](<https://%64iscord.com/x/../'+PATH+'/%2e%2e/>)', '[the channel](<https://%64iscord.com/x/../'+PATH+'/%2e%2e/>)'),
    (f'see <https://discord.com/x/../{PATH}/..?next={MADE_UP}> ok', 'see  ok'),  # A post link inside one still goes.
    # Still a post: as written, or once its path is resolved.
    (f'<{MADE_UP}/..>', 'can’t link that one.'), ('<https://discord.com/x/../'+PATH+'/x/..>', 'can’t link that one.'),
    ('<https://discord.com/x/../'+PATH+'/../8>', 'can’t link that one.'),
    # A bare link never ends on "." (see TAIL), so the ".." after it isn't its own, and it links the post. Out of <…>,
    # a masked link's target is read as a bare one would be, since Discord can refuse the masked link; and a "\\" can
    # escape a masked link's ">" or ")", making what follows its own.
    ('https://discord.com/x/../'+PATH+'/..', '..'), ('[clip](https://discord.com/x/../'+PATH+'/%2e%2e)', 'clip'),
    ('[clip](<https://discord.com/x/../'+PATH+'/..\\>)', 'clip'), ('[clip](https://discord.com/x/../'+PATH+'/..\\))', 'clip)'),
    # In <…> but maybe a masked link's target this pattern doesn't take (an empty label), where "\\." is an escape and
    # a ")" can end it: its starts count too.
    ('[](<https://discord.com/x/../'+PATH+'/\\..>)', '[]()'), ('[](\ufeff <https://discord.com/x/../'+PATH+')/..>)', '[](\ufeff )'),
    ('<https://discord.com/x/../'+PATH+'\\..>', 'can’t link that one.'),
])
def test_a_link_in_brackets_names_a_post_only_once_its_whole_path_is_read(reply, sent):
    from fri.fri_links import link_posts
    assert link_posts(reply, read()) == sent


def respond_with(action, context, **fields):
    import json

    import httpx

    from compat_tests.test_fri_chat import response_event
    from fri.fri_chat import Chat

    def model_reply(request):
        output = {'reply': '', 'request_draft': None, 'action': action, 'notes': [], **fields}
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


@pytest.mark.parametrize('length, status', [(298, 'completed'), (299, 'invalid_output'), (300, 'invalid_output')])
def test_a_clarifying_question_the_link_rule_lengthens_past_its_limit_fails_safely(length, status):
    """A kept link gains <…>, so a question at the 300-character limit can grow past it: that's an unreadable reply,
    never a turn that crashes later on the oversized action."""
    head = f'this one {FOUND} or '
    question = head + 'x'*(length-len(head)-1) + '?'
    assert len(question) == length
    context = {'turns': [], 'facts': [], 'model': 'gpt-6.1-sol', 'effort': 'high',
               'actions': {'clarify': {'goals': ['web']}}, **read()}
    result = respond_with({'kind': 'clarify', 'goal': 'web', 'topic': 'which clip', 'question': question}, context)
    assert result.status == status
    if status == 'completed':
        assert result.action['question'] == question.replace(FOUND, f'<{FOUND}>')


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


@pytest.mark.parametrize('draft, kept', [
    (f'Please fix the example reported in {MADE_UP}', 'Please fix the example reported in '),
    (f'Please fix the example reported in [this post]({MADE_UP.upper()})', 'Please fix the example reported in this post'),
    (f'Please fix the example reported in {FOUND}', f'Please fix the example reported in <{FOUND}>'),
    (f'Fix the one at {FOUND} not {MADE_UP}/', f'Fix the one at <{FOUND}> not '),
])
def test_a_request_draft_keeps_only_post_links_the_model_was_shown(draft, kept):
    """fri #204: a draft's body is shown in its preview and submitted as Fri's words, so it gets the reply's filter."""
    context = {'turns': [], 'facts': [], 'model': 'gpt-6.1-sol', 'effort': 'high', **read()}
    result = respond_with(None, context, reply='drafted it', request_draft=draft)
    assert (result.status, result.reply, result.draft) == ('completed', 'drafted it', kept)


@pytest.mark.parametrize('length, status', [(3998, 'completed'), (3999, 'invalid_output'), (4000, 'invalid_output')])
def test_a_request_draft_the_link_rule_lengthens_past_its_limit_fails_safely(length, status):
    """A kept link gains <…>, so a draft at its 4000-character limit can grow past it: that's an unreadable reply,
    never a preview that fails later."""
    head = f'see {FOUND} '
    draft = head + 'x'*(length-len(head))
    context = {'turns': [], 'facts': [], 'model': 'gpt-6.1-sol', 'effort': 'high', **read()}
    result = respond_with(None, context, reply='drafted it', request_draft=draft)
    assert result.status == status
    if status == 'completed':
        assert result.draft == draft.replace(FOUND, f'<{FOUND}>') and len(result.draft) == 4000


# fri #209: the controller takes a draft at least 5 characters long once stripped, so "fix: " and "  fix " are too short.
@pytest.mark.parametrize('draft', [MADE_UP, f'fix {MADE_UP}', f'<{MADE_UP}>', f'fix: {MADE_UP}', f'  fix {MADE_UP}',
                                   f'fix:\n{MADE_UP}'])
def test_a_request_draft_left_too_short_by_a_dropped_link_fails_safely(draft):
    context = {'turns': [], 'facts': [], 'model': 'gpt-6.1-sol', 'effort': 'high', **read()}
    result = respond_with(None, context, reply='drafted it', request_draft=draft)
    assert result.status == 'invalid_output' and result.draft is None


def diagnosed(monkeypatch, tmp_path, proposal, receipts=()):
    """A Gene check whose diagnosis, having read `receipts`, proposes `proposal`: the turn it ends in."""
    from compat_tests.test_fri_actions import harness
    from compat_tests.test_fri_chat import CONTEXT
    from compat_tests.test_fri_runtime import event
    from fri.fri_actions import Actions
    from fri.fri_chat import ChatResult

    async def scenario():
        fri, _, _, _ = harness(monkeypatch, tmp_path)
        source = event(user=20, channel=1, content='gene doesnt reply')
        calls = []

        async def api(method, path, body=None):
            calls.append(path)
            if path == '/agent/admit':
                return {'allowed': True}
            if path == '/chat/context':
                return {**CONTEXT, 'revision': 0}
            return {}
        fri.api = api
        fri.chat.respond = AsyncMock(side_effect=[
            ChatResult(status='completed', action={'kind': 'gene_diagnose', 'channel_id': ''}),
            ChatResult(status='completed', reply='gene restarted, cause is still unclear. got a repair preview for you')])
        receipt = {'kind': 'gene_diagnose', 'receipts': list(receipts), 'proposed_request': proposal,
                   'diagnosis_status': 'completed'}
        try:
            with patch.object(Actions, 'execute', new=AsyncMock(return_value=receipt)):
                return await fri.talk(123, 1, 20, source.content, 'public', event=source), calls
        finally:
            await fri.close()
    return asyncio.run(scenario())


@pytest.mark.parametrize('proposal, draft', [
    (f'Investigate the missed command at {MADE_UP} again', 'Investigate the missed command at  again'),
    (f'Check [the report]({MADE_UP}) and the restart', 'Check the report and the restart'),
    ('Investigate the missed command', 'Investigate the missed command'),
])
def test_a_diagnosis_draft_keeps_only_post_links_its_check_was_shown(monkeypatch, tmp_path, proposal, draft):
    """fri #207: a diagnosis's proposal becomes the turn's draft, shown in its preview and submitted as Fri's words, so
    it gets the reply's filter, against what the diagnosis read."""
    result, _ = diagnosed(monkeypatch, tmp_path, proposal)
    assert result.draft == draft


def test_a_post_link_in_genes_output_is_not_one_the_diagnosis_was_shown(monkeypatch, tmp_path):
    """Red's output can echo anyone's words: a link in it isn't a post's source."""
    receipts = [{'channel_id': '1', 'action': 'play', 'result': {'output': f'queued from {FOUND}'}}]
    result, _ = diagnosed(monkeypatch, tmp_path, f'Investigate the play at {FOUND}', receipts)
    assert result.draft == 'Investigate the play at '


@pytest.mark.parametrize('proposal', [MADE_UP, f'<{MADE_UP}>', f'fix: {MADE_UP}', f'fix\n{MADE_UP}'])
def test_a_diagnosis_draft_left_too_short_by_a_dropped_link_is_unreadable(monkeypatch, tmp_path, proposal):
    """What's left must still be a request the controller takes (5 characters once stripped); otherwise the turn says
    the reply was unreadable, without hiding that the check ran, rather than a preview whose submit fails."""
    result, calls = diagnosed(monkeypatch, tmp_path, proposal)
    assert result.draft is None and 'unreadable' in result.reply and 'that check completed.' in result.reply
    assert '/requests' not in calls
