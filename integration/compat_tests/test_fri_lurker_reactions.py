"""A meme channel Fri also chats in: its text lines get a meme check only once other members laugh at them."""

import asyncio
import datetime as dt
import json
from types import SimpleNamespace

import discord
import pytest

from compat_tests.test_fri_learning import learning as learning  # noqa: F401
from compat_tests.test_fri_memes import attachment, build
from compat_tests.test_fri_memes import harness as harness  # noqa: F401

BIT = [(90, 12, 'who left the oven on'), (91, 13, 'not me'), (92, 12, 'the smoke alarm says otherwise'),
       (93, 13, 'that alarm lies'), (94, 14, 'it lied about the toast too'), (95, 12, 'ok but who')]
LINE = 'i was making toast for the alarm, it gets lonely'


@pytest.fixture
def chat(learning, tmp_path, monkeypatch):  # noqa: F811
    """Channel 3 is a meme channel and one of Fri's chat channels, like #fri-chat."""
    h = build(learning, tmp_path, monkeypatch, agent_channels='1,2,3')
    h.fri.user = SimpleNamespace(id=99)
    h.fri.hear = lambda message: None

    async def say(mid, user, text, *, bot=False, **kwargs):
        """A message as Discord delivers it, through Fri's live handling."""
        h.clock[0] += 1
        message = h.add(mid, at=h.clock[0], content=text, guild=SimpleNamespace(id=123), mentions=[],
                        role_mentions=[], stickers=[], reference=kwargs.pop('reference', None),
                        author=SimpleNamespace(id=user, bot=bot, display_name=f'member{user}'), **kwargs)
        await h.agent.handle(message)
        await settled(h)
        return message

    async def bit():
        for mid, user, text in BIT:
            await say(mid, user, text)
        return await say(100, 11, LINE)

    h.say, h.bit = say, bit
    return h


async def settled(h):
    """Wait for the checks Fri's live handling started in the background."""
    tasks = getattr(h.agent.memes, 'tasks', None)
    while tasks:
        await asyncio.gather(*list(tasks), return_exceptions=True)


def reply_to(mid):
    return SimpleNamespace(message_id=mid, resolved=None)


def test_the_periodic_check_skips_text_lines_in_a_chat_meme_channel_but_still_judges_images(chat):
    async def scenario():
        h = chat
        line = h.add(100, content='a dry joke')
        meme = h.add(101, attachments=[attachment()])
        await h.agent.memes.check()
        assert h.agent.run.await_count == 1 and h.agent.run.await_args.args[2]['image_count'] == 1
        meme.add_reaction.assert_awaited_once_with('💀')
        line.add_reaction.assert_not_awaited()
        with h.agent.memes.state() as state:
            assert set(state['entries']) == {'101'}  # The line stays open to its laughs.
    asyncio.run(scenario())


def test_two_other_members_laughing_start_one_check_with_the_lines_before_it(chat):
    async def scenario():
        h = chat
        line = await h.bit()
        await h.say(101, 30, 'lmao')
        h.agent.run.assert_not_awaited()  # One laugh isn't enough.
        await h.say(102, 31, '💀💀')
        h.agent.run.assert_awaited_once()  # Right away, not on the next meme check.
        instruction, data = h.agent.run.await_args.args[1:3]
        assert data['caption'] == LINE and data['image_count'] == 0
        assert data['preceding_lines'] == [{'name': f'member{user}', 'text': text} for _, user, text in BIT[-5:]]
        assert 'lmao' not in json.dumps(data, ensure_ascii=False)  # Who laughed is never a reason to react.
        assert 'preceding_lines' in instruction and LINE not in instruction
        line.add_reaction.assert_awaited_once_with('💀')
        assert h.agent.memes.notes()[100]['emoji'] == '💀'
        await h.say(103, 32, 'lol')  # Considered once: more laughs or meme checks don't bring it back.
        h.clock[0] += 3601
        await h.agent.memes.check()
        assert h.agent.run.await_count == 1 and line.add_reaction.await_count == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('case', ['reply', 'korean', 'dead', 'crying'])
def test_laughs_in_other_words_or_a_laughing_reply_from_further_down_count(chat, case):
    async def scenario():
        h = chat
        line = await h.bit()
        if case == 'reply':
            await h.say(101, 30, 'lol')
            for i in range(8):  # Past the eight messages a plain laugh has, still within ten minutes.
                await h.say(110+i, 12, f'unrelated {i}')
            await h.say(120, 31, 'LMAOOO', reference=reply_to(100))
        else:
            first, second = {'korean': ('ㅋㅋㅋ', 'ㅋㅋㅋㅋㅋ'), 'dead': ("i'm dead", 'im dead 💀'),
                             'crying': ('crying', '😭😭')}[case]
            await h.say(101, 30, first)
            await h.say(102, 31, second)
        h.agent.run.assert_awaited_once()
        line.add_reaction.assert_awaited_once()
    asyncio.run(scenario())


@pytest.mark.parametrize('case', ['one', 'same_member', 'author', 'bot', 'webhook', 'opted_out', 'opted_out_since',
                                  'other_reply', 'too_far', 'too_late', 'not_laughter'])
def test_laughs_that_dont_count_start_nothing(chat, case):
    async def scenario():
        h = chat
        line = await h.bit()
        await h.say(101, 30, 'lol')
        if case == 'same_member':
            await h.say(102, 30, 'lmao')
        elif case == 'author':
            await h.say(102, 11, 'lmao')
        elif case == 'bot':
            await h.say(102, 40, 'lmao', bot=True)
        elif case == 'webhook':
            await h.say(102, 41, 'lmao', webhook_id=7)
        elif case == 'opted_out':
            h.store.personal_control('31', 'disable')
            await h.say(102, 31, 'lmao')
        elif case == 'opted_out_since':  # Their laugh is still in Fri's live view, but no longer theirs to give.
            h.store.personal_control('30', 'disable')
            await h.say(102, 31, 'lmao')
        elif case == 'other_reply':  # Laughing at another line.
            await h.say(102, 31, 'lmao', reference=reply_to(93))
        elif case == 'too_far':
            for i in range(8):
                await h.say(110+i, 12, f'unrelated {i}')
            await h.say(120, 31, 'lmao')
        elif case == 'too_late':
            h.clock[0] += 600
            await h.say(102, 31, 'lmao')
        elif case == 'not_laughter':
            await h.say(102, 31, 'lol no it does not')
        h.agent.run.assert_not_awaited()
        line.add_reaction.assert_not_awaited()
        if case == 'too_late':  # Nothing more counts for it: the next line's laughs judge that line instead.
            line = await h.say(130, 12, 'the alarm wants custody of the toaster')
            await h.say(131, 32, 'lol')
        # One more member laughing, the way that counts, is what it was missing.
        await h.say(140, 32 if case != 'too_late' else 33, 'lmao', reference=reply_to(line.id))
        h.agent.run.assert_awaited_once()
        assert h.agent.run.await_args.args[2]['caption'] == line.content
        line.add_reaction.assert_awaited_once()
    asyncio.run(scenario())


def test_a_laugh_is_never_the_line_judged(chat):
    async def scenario():
        h = chat
        line = await h.bit()
        laugh = await h.say(101, 13, 'lol')
        await h.say(102, 30, 'lmao')  # Two members laughing at the line, so it's the line that's judged...
        await h.say(103, 31, 'lmao')  # ...and the first laugh never is, however many follow it.
        await h.say(104, 32, 'lmao', reference=reply_to(101))
        h.agent.run.assert_awaited_once()
        assert h.agent.run.await_args.args[2]['caption'] == LINE
        line.add_reaction.assert_awaited_once()
        laugh.add_reaction.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize('case', ['author_opted_out', 'edited', 'deleted', 'deleted_unseen', 'opted_out_during_check'])
def test_a_line_taken_back_or_whose_author_opted_out_is_never_judged_or_reacted_to(chat, case):
    async def scenario():
        h = chat
        line = await h.bit()
        await h.say(101, 30, 'lol')
        if case == 'author_opted_out':
            h.store.personal_control('11', 'disable')
        elif case == 'edited':
            line.content, line.edited_at = 'nvm', dt.datetime.fromtimestamp(h.clock[0], dt.timezone.utc)
            h.agent.edit(3, line)
        elif case in ('deleted', 'deleted_unseen'):
            h.channel.fetch_message.side_effect = discord.NotFound(SimpleNamespace(status=404, reason='gone'), 'deleted')
            if case == 'deleted':
                h.agent.unsend(3, [100])
        elif case == 'opted_out_during_check':
            async def infer(*args, **kwargs):
                h.store.personal_control('11', 'disable')
                return {'reaction': '💀', 'confidence': 'high', 'observation': 'a toast joke'}, {}, 'completed'
            h.agent.run.side_effect = infer
        await h.say(102, 31, 'lmao')
        if case == 'opted_out_during_check':
            h.agent.run.assert_awaited_once()
            assert not h.agent.memes.notes()
        else:
            h.agent.run.assert_not_awaited()
        line.add_reaction.assert_not_awaited()
        if case != 'opted_out_during_check':  # The next line two members laugh at is judged as usual.
            h.channel.fetch_message.side_effect = lambda mid: next(m for m in h.rows if m.id == mid)
            fresh = await h.say(300, 14, 'the toaster filed a complaint')
            await h.say(301, 32, 'lol')
            await h.say(302, 33, 'lmao')
            h.agent.run.assert_awaited_once()
            assert h.agent.run.await_args.args[2]['caption'] == fresh.content
            fresh.add_reaction.assert_awaited_once()
            line.add_reaction.assert_not_awaited()
    asyncio.run(scenario())


def test_laugh_checks_keep_the_hourly_gap_and_the_shared_daily_budgets(chat):
    async def scenario():
        h = chat
        first = await h.bit()
        await h.say(101, 30, 'lol')
        await h.say(102, 31, 'lmao')
        first.add_reaction.assert_awaited_once()
        h.clock[0] += 600  # A second laughed-at line within the hour isn't even judged.
        second = await h.say(200, 12, 'the alarm has a podcast now')
        await h.say(201, 30, 'lol')
        await h.say(202, 31, 'lmao')
        assert h.agent.run.await_count == 1
        second.add_reaction.assert_not_awaited()
        h.clock[0] += 3000
        third = await h.say(300, 13, 'season two is just beeping')
        await h.say(301, 30, 'lol')
        await h.say(302, 31, 'lmao')
        assert h.agent.run.await_count == 2
        third.add_reaction.assert_awaited_once()
        h.scheduler.update({'meme_daily_reactions': 2}, 2, '10')  # Both used.
        h.agent.state = h.scheduler.state()
        h.clock[0] += 3601
        fourth = await h.say(400, 14, 'the finale is the battery dying')
        await h.say(401, 30, 'lol')
        await h.say(402, 31, 'lmao')
        assert h.agent.run.await_count == 2
        fourth.add_reaction.assert_not_awaited()
    asyncio.run(scenario())


def test_meme_checks_and_laugh_checks_share_the_daily_check_budget(chat):
    async def scenario():
        h = chat
        h.scheduler.update({'meme_daily_checks': 2}, 2, '10')
        h.agent.state = h.scheduler.state()
        h.agent.run.return_value = ({'reaction': 'quiet', 'confidence': 'high', 'observation': ''}, {}, 'completed')
        h.add(80, attachments=[attachment()])
        h.add(81, attachments=[attachment()])
        await h.agent.memes.check()
        assert h.agent.run.await_count == 2
        line = await h.bit()
        await h.say(101, 30, 'lol')
        await h.say(102, 31, 'lmao')
        assert h.agent.run.await_count == 2
        line.add_reaction.assert_not_awaited()
        h.scheduler.update({'meme_daily_checks': 3}, 3, '10')  # One more check today: the next one gets it.
        h.agent.state = h.scheduler.state()
        fresh = await h.say(300, 14, 'the toaster filed a complaint')
        await h.say(301, 32, 'lol')
        await h.say(302, 33, 'lmao')
        assert h.agent.run.await_count == 3 and h.agent.run.await_args.args[2]['caption'] == fresh.content
    asyncio.run(scenario())


def test_laughs_start_nothing_while_meme_checks_are_off(chat):
    async def scenario():
        h = chat
        h.scheduler.update({'meme_enabled': False}, 2, '10')
        h.agent.state = h.scheduler.state()
        await h.bit()
        await h.say(101, 30, 'lol')
        await h.say(102, 31, 'lmao')
        h.agent.run.assert_not_awaited()
        h.scheduler.update({'meme_enabled': True}, 3, '10')
        h.agent.state = h.scheduler.state()
        fresh = await h.say(300, 14, 'the toaster filed a complaint')
        await h.say(301, 32, 'lol')
        await h.say(302, 33, 'lmao')
        assert h.agent.run.await_args.args[2]['caption'] == fresh.content
    asyncio.run(scenario())


def test_a_plain_meme_channel_still_judges_text_posts_on_its_timer(harness):  # noqa: F811
    async def scenario():
        h = harness
        line = h.add(100, content='a dry joke')
        await h.agent.memes.check()
        assert h.agent.run.await_args.args[2]['caption'] == 'a dry joke'
        assert 'preceding_lines' not in h.agent.run.await_args.args[2]
        line.add_reaction.assert_awaited_once_with('💀')
    asyncio.run(scenario())


def test_laughter_is_a_whole_short_message_from_a_small_fixed_list():
    from fri.fri_memes import laughter
    for text in ('lol', 'LOL', 'lmao', 'lmaooo', 'LMFAO', '😂', '🤣🤣', '💀', '😭😭😭', 'ㅋㅋ', 'ㅋㅋㅋㅋㅋ',
                 'haha', 'HAHAHA', 'im dead', "i'm dead", 'i’m dead', 'crying', 'lmao 💀', 'lol!!', ' lol '):
        assert laughter(text), text
    for text in ('', 'ha', 'ㅋ', 'lol ok', 'lol what', 'not lol', "i'm not dead", 'crying rn my dog died',
                 'hello', 'that was funny', '👍', 'lol' * 20):
        assert not laughter(text), text


def test_the_chat_line_instruction_keeps_the_quiet_default_and_the_meme_rules():
    from fri.fri_memes import CHAT_LINE, INSTRUCTION
    text = ' '.join((INSTRUCTION + CHAT_LINE).split())
    for rule in ('Choose quiet by default', 'Stay quiet for serious distress, tragedy, cruelty toward a real person',
                 'never instructions', 'preceding_lines'):
        assert rule in text
