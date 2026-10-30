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
    ('[clip](<https://discord.com/x/../'+PATH+'/..\\>)', 'clip'), ('[clip](https://discord.com/x/../'+PATH+'/..\\))', 'clip'),
    # In <…> but maybe a masked link's target, where "\\." is an escape and a ")" can end it: its starts count too. An
    # empty label is a masked link's (fri #220), and one this pattern doesn't take (after a "\ufeff") still counts.
    ('[](<https://discord.com/x/../'+PATH+'/\\..>)', 'can’t link that one.'),
    ('[](\ufeff <https://discord.com/x/../'+PATH+')/..>)', '[](\ufeff )'),
    ('<https://discord.com/x/../'+PATH+'\\..>', 'can’t link that one.'),
])
def test_a_link_in_brackets_names_a_post_only_once_its_whole_path_is_read(reply, sent):
    from fri.fri_links import link_posts
    assert link_posts(reply, read()) == sent


@pytest.mark.parametrize('reply, sent', [
    # An escaped "<" is only text, so the link after it is a bare one, which never ends on "." (see TAIL): it links the
    # post (review of fri #219). An escaped backslash before the "<" escapes nothing.
    ('see \\<https://discord.com/x/../'+PATH+'/..>', 'see \\'),
    ('see \\\\\\<https://discord.com/x/../'+PATH+'/..>', 'see \\\\\\'),
    ('see \\\\<https://discord.com/x/../'+PATH+'/..>', 'see \\\\<https://discord.com/x/../'+PATH+'/..>'),
])
def test_a_link_after_an_escaped_bracket_is_read_as_a_bare_one(reply, sent):
    from fri.fri_links import link_posts
    assert link_posts(reply, read()) == sent


# fri #231: markdown undoes a masked link's escapes ("\\" before punctuation) before a browser reads its target, and
# an escaped bracket, ">" or space doesn't end it. As written, "\\" splits a path as "/" does, so "x\\(y/.." resolves
# to "x"; undone, "x(y/.." resolves away.
ESCAPED = ['https://discord.com/x\\(y/../'+PATH, 'https://discord.com/x\\)y/../'+PATH, 'https://discord.com/x\\<y/../'+PATH,
           'https://discord.com/x\\>y/../'+PATH, 'https://discord.com/a\\ b/../../'+PATH, 'https://discord.com/a\\\tb/../../'+PATH,
           'https://discord.com/x/\\%2e\\%2e/'+PATH, 'https://discord\\.com/'+PATH, 'https\\://discord.com/'+PATH]


@pytest.mark.parametrize('url', ESCAPED)
@pytest.mark.parametrize('form, sent', [('[clip]({})', 'clip'), ('[clip](<{}>)', 'clip'), ('see [clip]( {} ) ok', 'see clip ok'),
                                        # One the pattern doesn't take (its label holds a newline): read to a space.
                                        ('[kev\n]({})', '[kev\n]()')])
def test_an_escape_in_a_masked_links_target_hides_no_post_link(url, form, sent):
    from fri.fri_links import link_posts
    assert link_posts(form.format(url), read()) == sent


@pytest.mark.parametrize('reply, sent', [
    # Only the exact link shown stays: undone, these are the shown one, but as written they aren't.
    ('[clip](https://discord.com\\/channels/123/2/1300000000000000005)', 'clip'),
    ('[clip](https\\://discord.com/channels/123/2/1300000000000000005)', 'clip'),
    ('[kev\n](https\\://discord.com/channels/123/2/1300000000000000005)', '[kev\n]()'),
    (f'[kev’s clip](<{FOUND}>)', f'[kev’s clip](<{FOUND}>)'),
    # The escaped ")" is the target's own, so the one after it closes the link.
    (f'[clip]({MADE_UP}\\))', 'clip'),
    # Out of a masked link, markdown reads a link as written: "\\" is a "/" to a browser, and "https\\:" is no link.
    ('see https://discord.com/x\\(y/../'+PATH, 'see https://discord.com/x\\(y/../'+PATH),
    ('see https\\://discord.com/'+PATH+' ok', 'see https\\://discord.com/'+PATH+' ok'),
    ('see <https\\://discord.com/'+PATH+'> ok', 'see <https\\://discord.com/'+PATH+'> ok'),
    # A "\\" before a letter or a digit escapes nothing, in a target too: "x\\y/.." resolves to "x" either way.
    ('[clip](https://discord.com/x\\y/../'+PATH+')', '[clip](https://discord.com/x\\y/../'+PATH+')'),
    ('[clip](https://example.com/a\\(b)', '[clip](https://example.com/a\\(b)'),  # Not a post link: left as it is.
])
def test_a_masked_target_is_read_with_its_escapes_undone_and_kept_only_as_shown(reply, sent):
    from fri.fri_links import link_posts
    assert link_posts(reply, read()) == sent


# Review of fri #236: right after a "](", a kept link may be a masked link's target, which runs on to its ")". What comes
# after the link there is its target's too ("<A>(x)/../../3/…" resolves to another post), so it stays only when a space
# or ")" ends it. Dropping an escaped link can join the text around it into one, and an <…> one can be the target too.
SUFFIX = '(x)/../../3/1300000000000000009'
ESCAPED_SPLICE = '[clip](h[](https\\://discord.com/channels/123/2/1300000000000000009)'+FOUND[1:]+SUFFIX+')'


@pytest.mark.parametrize('reply, sent', [
    (ESCAPED_SPLICE, '[clip]('+SUFFIX+')'), (f'[clip]({FOUND}{SUFFIX})', '[clip]('+SUFFIX+')'),
    (f'[kev\n](<{FOUND}>{SUFFIX})', '[kev\n]('+SUFFIX+')'), (f'[kev\n]({FOUND}<x/../../3/{ID})', f'[kev\n](<x/../../3/{ID})'),
    (f'[kev\n]({FOUND}>x/../../3/{ID})', f'[kev\n](x/../../3/{ID})'), (f'[clip]({FOUND}\\(x/../../3/{ID})', 'clip'),
    (f'[clip](\n{FOUND}{SUFFIX})', '[clip](\n'+SUFFIX+')'), (f'[kev\n]({FOUND}<x)', '[kev\n](<x)'),
    # A space or ")" ends the target there, and the end of the text leaves no masked link.
    (f'[kev\n]({FOUND})', f'[kev\n](<{FOUND}>)'), (f'[clip]({FOUND} "the clip")', f'[clip](<{FOUND}> "the clip")'),
    (f'[kev\n](<{FOUND}> )', f'[kev\n](<{FOUND}> )'), (f'[clip]({FOUND}', f'[clip](<{FOUND}>'),
    # Past a space, the target ends unless a ")" or a title closes it, or a "(" before the space runs on through it.
    (f'[kev\n](\n{FOUND}? ok', f'[kev\n](\n<{FOUND}>? ok'), (f'[kev\n]({FOUND}? ok)', f'[kev\n](<{FOUND}>? ok)'),
    (f'[kev\n]({FOUND}? )', '[kev\n](? )'), (f'[kev\n]({FOUND}? "t")', '[kev\n](? "t")'),
    (f'[kev\n]({FOUND}?(a b)/../../3/{ID})', f'[kev\n](?(a b)/../../3/{ID})'),
    # Not after a "](": no masked link's target, so what follows is only text.
    (f'see ({FOUND}{SUFFIX})', f'see (<{FOUND}>{SUFFIX})'), (f'see ({FOUND}.) ok', f'see (<{FOUND}>.) ok'),
])
def test_a_shown_link_a_masked_target_runs_on_past_stays_only_once_its_target_ends(reply, sent):
    from fri.fri_links import link_posts
    assert link_posts(reply, read()) == sent


def test_a_reply_built_from_receipts_never_keeps_a_shown_link_a_masked_target_runs_on_past():
    from fri.fri_outcomes import fallback, receipt
    sent = fallback([receipt('web_search', {'answer': f'[clip]({FOUND}{SUFFIX})', 'sources': [FOUND]})])
    assert sent == '[clip]('+SUFFIX+')'


# Review of fri #236, round 2: dropping a link inside a masked link's target used to leave the rest of that target,
# joined ("h" and "ttps") into a link whose "(a b)" group the filter couldn't read past. Now the whole target goes,
# its label kept, and a target's "(…)" group is read through, its space too (fri #235).
GROUP = 'ttps://discord.com/(a b)/../channels/123/3/'+ID
INNER = ['[](https\\://discord.com/channels/123/2/1300000000000000009)', '<'+MADE_UP+'>', MADE_UP]


@pytest.mark.parametrize('inner', INNER, ids=['escaped', 'angled', 'bare'])
@pytest.mark.parametrize('form, sent', [('[clip](h{}'+GROUP+')', 'clip'), ('[kev\n](h{}'+GROUP+')', 'kev\n'),
                                        ('[a](x[b](h{}'+GROUP+')y)', 'a'), ('see [clip]( h{}'+GROUP+' ) ok', 'see clip ok')])
def test_a_link_dropped_inside_a_masked_target_takes_the_whole_target(inner, form, sent):
    from fri.fri_links import link_posts
    assert link_posts(form.format(inner), read()) == sent


def test_a_reply_built_from_receipts_drops_a_masked_target_a_dropped_link_was_inside():
    from fri.fri_outcomes import fallback, receipt
    answer = '[clip](h'+INNER[0]+GROUP+')'
    assert fallback([receipt('web_search', {'answer': answer, 'sources': [FOUND]})]) == 'clip'


@pytest.mark.parametrize('reply, sent', [
    ('[clip](https://discord.com/x(a b)/../../'+PATH+')', 'clip'),
    ('[kev\n](<https://discord.com/x(a b)/../../'+PATH+'>)', 'kev\n'),
    ('[clip](https://discord.com/x(a\nb)/../../'+PATH+')', 'clip'),
    ('[clip](https://discord.com/x(a b) /../../'+PATH+')', '[clip](https://discord.com/x(a b) /../../'+PATH+')'),
    ('[clip](https://example.com/x(a b)/../../'+PATH+')', '[clip](https://example.com/x(a b)/../../'+PATH+')'),
])
def test_a_masked_targets_group_is_read_through_its_space(reply, sent):
    """fri #235: Discord's target takes a "(…)" group whole, spaces included. One followed by a space ends there."""
    from fri.fri_links import link_posts
    assert link_posts(reply, read()) == sent


@pytest.mark.parametrize('filler', ['](', '[a]('])
def test_shown_links_a_masked_target_may_run_on_past_read_their_runs_once(monkeypatch, filler):
    """Review of fri #236, round 2: each kept link checks what follows it, in one run read once, not once each."""
    import fri.fri_links as links

    class Counted:
        def __init__(self, pattern):
            self.pattern, self.calls = pattern, 0

        def match(self, *args):
            self.calls += 1
            return self.pattern.match(*args)
    counted = Counted(links.TARGET)
    monkeypatch.setattr(links, 'TARGET', counted)
    for size in (2000, 4000):
        counted.calls = 0
        reply = ((filler+FOUND+'<')*(size//60+1))[:size-1]+')'
        links.link_posts(reply, read())
        assert counted.calls <= 8


# Spaces as Discord's markdown reads them (JavaScript's): <…> ends only at a plain space, while a bare link or a masked
# link's target ends at any of these, but not at a control character Python also calls a space (review of fri #219).
@pytest.mark.parametrize('space, ends', [('\u2003', True), ('\xa0', True), ('\v', True), ('\x1c', False), ('\x85', False)])
@pytest.mark.parametrize('form', ['<{}>', '{}', '[clip]({})'])
def test_a_post_link_holding_a_space_a_browser_keeps_goes(space, ends, form):
    """A browser keeps such a character inside a link's path (encoded), so "x{space}/.." resolves away."""
    from fri.fri_links import link_posts
    reply = form.format('https://discord.com/x'+space+'/../'+PATH)
    kept = ends and form != '<{}>'  # Discord links only "https://discord.com/x" here, which names no post.
    assert link_posts(reply, read()) == (reply if kept else 'clip' if form.startswith('[') else 'can’t link that one.')


@pytest.mark.parametrize('filler', ['https:'*330 + '@' + '%61'*700, 'https://discord.com/x/../'*160, 'https:'*700])
def test_a_4000_character_reply_is_filtered_well_under_100_ms(filler):
    """Every refilter forced (each drop joins the halves around it into another link), then many links starting inside
    one bare run: each pass reads the run once, not once for every link in it, and each link once per reply."""
    import time

    from fri.fri_links import link_posts
    nested = MADE_UP
    for _ in range(8):
        nested = MADE_UP[:30] + '<' + nested + '>' + MADE_UP[30:]
    reply = (nested + ' ' + filler)[:4000]
    took = []
    for _ in range(3):
        started = time.perf_counter()
        assert link_posts(reply, read()) == 'can’t link that one.'
        took.append(time.perf_counter() - started)
    assert min(took) < 0.1


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


@pytest.mark.parametrize('kind, value, sent', [
    ('gene_command', {'output': f'queued {MADE_UP}'}, 'gene returned:\nqueued '),
    ('gene_command', {'output': f'queued <{MADE_UP}>'}, 'gene returned:\nqueued '),
    ('web_search', {'answer': f'it is {FOUND}', 'sources': [{'url': FOUND}]}, f'it is <{FOUND}>'),
    ('web_search', {'answer': f'it is {MADE_UP}', 'sources': [{'url': FOUND}]}, 'it is '),
])
def test_a_reply_built_from_receipts_keeps_only_post_links_they_showed(kind, value, sent):
    """When the model's own words can't be used, Fri answers from the step's receipt: a post link in Gene's output or a
    search answer goes out only if the receipts showed it as a source (review of fri #219)."""
    from fri.fri_outcomes import fallback, receipt
    assert fallback([receipt(kind, value)]) == sent


@pytest.mark.parametrize('reply, sent', [
    # fri #220: Discord's <…> runs to a plain space or ">", so a "<" can sit inside it, and a browser encodes it in the
    # path: the dot segments after it still resolve.
    ('<https://discord.com/x<y/../'+PATH+'>', 'can’t link that one.'),
    ('see <https://discord.com/x<y/../'+PATH+'> ok', 'see  ok'),
    ('<https://discord.com/x<<y/../'+PATH+'/?a<b>', 'can’t link that one.'),
    (f'<{FOUND}<x>', 'can’t link that one.'),  # Not the exact link shown.
    ('[clip](<https://discord.com/x<y/../'+PATH+'>)', 'clip'),
    ('[clip](https://discord.com/x<y/../'+PATH+')', 'clip'),  # A masked link's target holds one too.
    # A post link starting inside another <…> link is its own, and a "<" in it that isn't a link's stays.
    ('<https://example.com/<https://discord.com/x<y/../'+PATH+'>', '<https://example.com/'),
    ('<https://discord.com/x<y/../channels/123/2> ok', '<https://discord.com/x<y/../channels/123/2> ok'),
    (f'<{FOUND}> <https://discord.com/x<y/../channels/123/2/1300000000000000005/..>',
     f'<{FOUND}> <https://discord.com/x<y/../channels/123/2/1300000000000000005/..>'),
    # A masked link with an empty label is one too, and its target holds a "<" as well.
    ('see [](https://discord.com/x<y/../'+PATH+') ok', 'see  ok'), (f'see []({FOUND}) ok', f'see <{FOUND}> ok'),
    # Discord's autolink needs a "/" after the scheme, so without one the <…> isn't a link's end for certain, nor a
    # masked link's whose scheme a tab splits.
    ('<https:discord.com/'+PATH[:-3]+'<009/../>', 'can’t link that one.'),
    ('<htt\tps\n:discordapp.com/'+PATH[:-3]+'<009/../>', 'can’t link that one.'),
    ('[kev](<htt\tps://discord.com/'+PATH+'/..>)', 'kev'),
    # A masked link this pattern doesn't take (its label holds a newline, or a "]" Discord allows): right after the "(",
    # its target is read to a space, through a "<" or ">".
    ('[kev\n](https://discord.com/x<y/../'+PATH+')', '[kev\n]()'), ('[a]b](https://discord.com/x<y/../'+PATH+')', '[a]b]()'),
    ('[kev\n](<https://discord.com/a>b<c/../'+PATH+'>)', '[kev\n](<)'),
    (f'[kev\n]({FOUND})', f'[kev\n](<{FOUND}>)'),
])
def test_a_bracket_inside_a_link_in_brackets_is_part_of_it(reply, sent):
    from fri.fri_links import link_posts
    assert link_posts(reply, read()) == sent


@pytest.mark.parametrize('filler', [
    '<https:'*660, '<https://discord.com/x/../'*160, '<\x01htt\tps://discord.com/x/..'*140, '<https://a<'*400,
    '<https://discord.com/'+'x/'*1990, '[a](<https://discord.com/x/../'*130, '<https://discord.com/a/'*180,
    'https://discord.com/x/../'*160, 'https://discord.com/a/'*180, '<h\ttps:'*500, '<https://discord.com\\x/..'*150,
    '(https://discord.com/x/../'*160, '[a\n](<https://discord.com/x/..'*130, '( <\x01https:'*500,
    # fri #231: escapes in masked targets, read as written and undone, and escaped spaces that don't end one.
    '[a](https://discord.com/x\\(y/..'*130, '(https://discord.com/a\\ '*160, '[a\n](https\\://discord.com/x\\(/'*120,
    '[a](<https://discord.com/x\\>/..'*130, '(https\\:'*500,
    # Review of fri #236: shown links a masked target may run on past, dropped or kept.
    ('[a]('+FOUND+'(x)/../')*60, ('](\n'+FOUND+'? ')*60, ('[a]('+FOUND+' ')*60,
])
@pytest.mark.parametrize('end', ['', '>'])
def test_a_4000_character_reply_full_of_links_in_brackets_is_filtered_well_under_100_ms(filler, end):
    """fri #220: a "<" no longer ends a <…> link, so many can start inside one. Each pass reads that run once for all
    of them, as it reads a bare one (fri #223)."""
    import time

    from fri.fri_links import link_posts
    nested = MADE_UP
    for _ in range(8):
        nested = MADE_UP[:30] + '<' + nested + '>' + MADE_UP[30:]
    reply = (nested + ' ' + filler)[:4000-len(end)] + end
    took = []
    for _ in range(3):
        started = time.perf_counter()
        assert link_posts(reply, read()) == 'can’t link that one.'
        took.append(time.perf_counter() - started)
    assert min(took) < 0.1


@pytest.mark.parametrize('filler', ['https://discord.com/x/../', '<https://discord.com/x/../', 'https://discord.com/a/',
                                    '<https://discord.com/a/'])
def test_links_starting_inside_one_run_read_its_path_once(monkeypatch, filler):
    """fri #223: every link starting inside one run has the run's path from where its own address ends, so the path is
    read once for all of them, not once each: the work grows with the reply, not with its square."""
    import fri.fri_links as links

    class Counted:
        def __init__(self, pattern):
            self.pattern, self.calls = pattern, 0

        def match(self, *args):
            self.calls += 1
            return self.pattern.match(*args)
    counted = Counted(links.SEGMENT)
    monkeypatch.setattr(links, 'SEGMENT', counted)
    for size in (2000, 4000, 8000):
        counted.calls = 0
        end = '>' if filler.startswith('<') else ''  # One <…> link, with links starting all through it.
        reply = (filler*(size//len(filler)+1))[:size-len(end)] + end
        assert links.link_posts(reply, read()) == reply  # Nothing in it names a post.
        assert counted.calls <= size//2


CITED = 'citeturn0search0'


@pytest.mark.parametrize('channel_id', [1, 8])  # Asked in Fri's channel, and pinged elsewhere.
@pytest.mark.parametrize('answer', [
    f'it’s this one <https:/discord.com/channels/123/1/{CITED}9>',
    'it’s this one <https:/discord.com/channels/123/1/[source 1](https://www.python.org/downloads/)9>',
])
def test_citation_formatting_never_builds_a_post_link_the_filter_passed(monkeypatch, tmp_path, channel_id, answer):
    """fri #222: removing a citation marker, or a source link the member didn't ask for, can join what's around it
    into a post link the model was never shown. The reply is filtered again once citations are formatted."""
    import datetime as dt

    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=channel_id)
        question = 'what is the current Python release?'
        event = message('<@99> '+question)
        receipt = {'kind': 'web_search', 'query': question, 'answer': 'public evidence',
                   'sources': ['https://www.python.org/downloads/'],
                   'checked_at': dt.datetime.now(dt.timezone.utc).isoformat()}
        monkeypatch.setattr('fri.fri_actions.lookup', AsyncMock(return_value=(receipt, {}, 'completed')))
        model(fri, tmp_path, [{'action': {'kind': 'web_search', 'query': question}}, answer])
        texts = delivered(event)
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            assert len(texts) == 1 and texts[0].startswith('it’s this one') and 'discord.com' not in texts[0]
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_source_the_member_asked_for_stays_when_it_is_a_post_link(monkeypatch, tmp_path):
    """A verified web source is a link the model was shown (fri #223), so filtering again after citations keeps it."""
    import datetime as dt

    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        question = 'what did the python discord say about the release, with sources'
        event = message('<@99> '+question)
        receipt = {'kind': 'web_search', 'query': question, 'answer': 'public evidence', 'sources': [FOUND],
                   'checked_at': dt.datetime.now(dt.timezone.utc).isoformat()}
        monkeypatch.setattr('fri.fri_actions.lookup', AsyncMock(return_value=(receipt, {}, 'completed')))
        model(fri, tmp_path, [{'action': {'kind': 'web_search', 'query': question}}, f'they posted it [here]({FOUND})'])
        texts = delivered(event)
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            assert texts == [f'they posted it [here](<{FOUND}>)']
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['web_search', 'web_refine', 'web_sources'])
def test_a_reply_built_from_a_web_receipt_keeps_a_post_link_it_cites(kind):
    """fri #223: a web receipt lists its verified sources as strings. Those count as shown; links in its prose don't."""
    from fri.fri_outcomes import fallback, receipt
    assert fallback([receipt(kind, {'answer': f'it is {FOUND}', 'sources': [FOUND]})]) == f'it is <{FOUND}>'
    assert fallback([receipt(kind, {'answer': f'it is {MADE_UP}', 'sources': [FOUND]})]) == 'it is '
    assert fallback([receipt(kind, {'answer': f'it is {FOUND} or {MADE_UP}', 'sources': [FOUND, MADE_UP+'/']})]) == (
        f'it is <{FOUND}> or ')
    assert fallback([receipt('gene_command', {'output': f'queued {FOUND}', 'sources': [FOUND]})]) == 'gene returned:\nqueued '


def units(text):
    return len(text.encode('utf-16-le'))//2


@pytest.mark.parametrize('value', [{'output': '🙂'*1000}, {'output': '🙂'*600+'x'*500}, {'output': 'x'*1600}],
                         ids=['emoji', 'mixed', 'ascii'])
def test_a_reply_built_from_receipts_fits_discord_in_utf_16_units(value):
    """fri #223: Discord counts UTF-16 units, so an emoji counts twice."""
    from fri.fri_outcomes import fallback, receipt
    sent = fallback([receipt('gene_command', value)])
    assert units(sent) <= 1800 and sent.startswith('gene returned:\n')


@pytest.mark.parametrize('before', ['🙂'*880+' ', '🙂'*890+' ', '🙂'*870+' ', '🙂'*860+'\n', '🙂'*885, '🙂'*870],
                         ids=['link-at-the-cut', 'link-past-the-cut', 'cut-after-the-link', 'fits', 'first-word-cut',
                              'first-word-fits'])
@pytest.mark.parametrize('source', [FOUND, {'url': FOUND}], ids=['string', 'record'])
def test_a_reply_cut_to_fit_keeps_a_shown_link_whole_or_leaves_it_out(before, source):
    """The cut falls between links, never through one (a cut post link names another post), and the filter runs again
    after it."""
    from fri.fri_outcomes import fallback, receipt
    sent = fallback([receipt('web_search', {'answer': before+FOUND+' and more', 'sources': [source]})])
    assert units(sent) <= 1800
    assert f'<{FOUND}>' in sent or 'discord' not in sent
    assert 'discord' not in sent.replace(f'<{FOUND}>', '')


@pytest.mark.parametrize('answer', ['🙂'*885+f'[clip]({FOUND})', '🙂'*885+FOUND, FOUND+'🙂'*900, f'[clip]({FOUND})'+'🙂'*900],
                         ids=['masked', 'bare', 'link-first', 'masked-first'])
def test_a_first_word_holding_a_link_goes_whole_and_the_reply_says_so_if_nothing_is_left(answer):
    """fri #231: the word holding the link starts the reply, so the cut can't fall after it. It goes whole too, and
    with nothing left the reply says it can't link it, as with a reply that was only a dropped link."""
    from fri.fri_links import EMPTY
    from fri.fri_outcomes import fallback, receipt
    assert fallback([receipt('web_search', {'answer': answer, 'sources': [{'url': FOUND}]})]) == EMPTY


@pytest.mark.parametrize('answer, sent', [
    ('🙂'*905+'https:', '🙂'*900), ('🙂'*905+'https://', '🙂'*900), ('a '+'🙂'*905+'https:', 'a '+'🙂'*899),
    ('🙂'*905+'https://example.com', 'can’t link that one.'),
], ids=['scheme', 'scheme-and-slashes', 'scheme-later', 'link'])
def test_a_scheme_alone_is_cut_as_text(answer, sent):
    """Review of fri #236: only a scheme with an address after it is a link the cut keeps whole."""
    from fri.fri_outcomes import fallback, receipt
    assert fallback([receipt('web_search', {'answer': answer, 'sources': []})]) == sent


@pytest.mark.parametrize('answer, sent', [
    ('🙂'*905+'https\\://example.com', '🙂'*900), ('🙂'*905+'[x](https\\://example.com)', 'can’t link that one.'),
], ids=['escaped-out-of-a-target', 'escaped-in-a-target'])
def test_an_escaped_scheme_is_a_link_to_the_cut_only_in_a_masked_target(answer, sent):
    """Review of fri #236, round 2: out of a masked link's target, "https\\://" is text, as the filter reads it."""
    from fri.fri_outcomes import fallback, receipt
    assert fallback([receipt('web_search', {'answer': answer, 'sources': []})]) == sent
