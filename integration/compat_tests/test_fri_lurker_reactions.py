"""A meme channel Fri also chats in: its text lines get a meme check only once other members laugh at them."""

import asyncio
import datetime as dt
import json
import random
import re
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from compat_tests.test_fri_learning import learning as learning  # noqa: F401
from compat_tests.test_fri_memes import attachment, build
from compat_tests.test_fri_memes import harness as harness  # noqa: F401
from fri.fri_agent import Agent
from fri.fri_errors import ControllerUnavailable

BIT = [(90, 12, 'who left the oven on'), (91, 13, 'not me'), (92, 12, 'the smoke alarm says otherwise'),
       (93, 13, 'that alarm lies'), (94, 14, 'it lied about the toast too'), (95, 12, 'ok but who')]
LINE = 'i was making toast for the alarm, it gets lonely'


@pytest.fixture
def chat(learning, tmp_path, monkeypatch):  # noqa: F811
    """Channel 3 is a meme channel and one of Fri's chat channels, like #fri-chat."""
    h = build(learning, tmp_path, monkeypatch, agent_channels='1,2,3')
    h.fri.user = SimpleNamespace(id=99)
    h.fri.hear = lambda message: None

    async def say(mid, user, text, *, bot=False, settle=True, **kwargs):
        """A message as Discord delivers it, through Fri's live handling. settle: wait for the checks it starts."""
        h.clock[0] += 1
        message = h.add(mid, at=h.clock[0], content=text, guild=SimpleNamespace(id=123), mentions=[],
                        role_mentions=[], stickers=[], reference=kwargs.pop('reference', None),
                        author=SimpleNamespace(id=user, bot=bot, display_name=f'member{user}'), **kwargs)
        await h.agent.handle(message)
        if settle:
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


def model(h, monkeypatch):
    """Agent.run as it runs: one model slot, sign-in, its erased check, then the model, mocked and returned."""
    analyze = AsyncMock(return_value=({'reaction': '💀', 'confidence': 'high', 'observation': 'A joke'}, {}, 'completed'))
    monkeypatch.setattr('fri.fri_agent.analyze', analyze)
    h.fri.chat = SimpleNamespace(headers=AsyncMock(return_value={}))
    h.agent.record = AsyncMock()
    h.agent.run = Agent.run.__get__(h.agent, Agent)
    return analyze


async def waiting(h, mid):
    """Until the check of message mid has reserved its turn and waits on the model slot."""
    for _ in range(100):
        with h.agent.memes.state(write=False) as state:
            if str(mid) in state['entries']:
                break
        await asyncio.sleep(0)
    for _ in range(10):
        await asyncio.sleep(0)
    assert h.agent.slots.locked()


def configure(h, version, **changes):
    h.scheduler.update(changes, version, '10')
    h.agent.state = h.scheduler.state()


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
        assert h.agent.memes.notes()[100]['note'] == ''  # Only the emoji: the context held others' lines.
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
                                  'left_server', 'other_reply', 'too_far', 'too_late', 'not_laughter'])
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
        elif case == 'left_server':
            h.agent.member.side_effect = lambda user, **_: None if user == 31 else object()
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


def test_a_reply_edited_into_a_laugh_still_laughs_at_what_it_replied_to(chat):
    async def scenario():
        h = chat
        await h.say(80, 12, 'the oven is self aware now')
        line = await h.say(81, 13, 'it asked me to stop opening it')
        typo = await h.say(82, 30, 'lmoa', reference=reply_to(80))  # Not a laugh when it came in.
        typo.content, typo.edited_at = 'lmao', dt.datetime.fromtimestamp(h.clock[0], dt.timezone.utc)
        h.agent.edit(3, typo)
        await h.say(83, 31, 'lol')  # One member laughing at the newer line, the other at the older one.
        h.agent.run.assert_not_awaited()
        line.add_reaction.assert_not_awaited()
        await h.say(84, 32, 'lmao', reference=reply_to(80))  # A second at the older one is what it needs.
        h.agent.run.assert_awaited_once()
        assert h.agent.run.await_args.args[2]['caption'] == 'the oven is self aware now'
    asyncio.run(scenario())


def test_the_timer_checks_the_daily_cap_again_where_it_reserves(chat):
    async def scenario():
        h = chat
        configure(h, 2, meme_daily_checks=2)
        with h.agent.memes.state() as state:  # One of today's two checks is used.
            state['entries']['70'] = {'at': time.time(), 'channel': '3', 'status': 'quiet'}
        meme = h.add(80, attachments=[attachment()], author=SimpleNamespace(id=50, bot=False))
        line = await h.bit()
        await h.say(101, 30, 'lol')
        paused, resume = asyncio.Event(), asyncio.Event()

        async def member(user, **_):
            if user == 50:  # The meme's author: the timer waits here, past its first look at the cap.
                paused.set()
                await resume.wait()
            return object()
        h.agent.member.side_effect = member
        timer = asyncio.create_task(h.agent.memes.check())
        await paused.wait()
        await h.say(102, 31, 'lmao')  # Meanwhile a laugh check takes the last one.
        line.add_reaction.assert_awaited_once()
        resume.set()
        await timer
        assert h.agent.run.await_count == 1
        meme.add_reaction.assert_not_awaited()
        with h.agent.memes.state(write=False) as state:
            assert set(state['entries']) == {'70', '100'}
    asyncio.run(scenario())


def test_one_laugh_check_runs_at_a_time_and_laughs_meanwhile_start_nothing(chat):
    async def scenario():
        h = chat
        configure(h, 2, meme_daily_checks=2)
        h.agent.run.return_value = ({'reaction': 'quiet', 'confidence': 'high', 'observation': ''}, {}, 'completed')
        gate, reads, read = asyncio.Event(), [], h.agent.context_messages

        async def stalled(*args, **kwargs):  # A slow controller: profile reads stall.
            reads.append(args)
            await gate.wait()
            return await read(*args, **kwargs)
        h.agent.context_messages = stalled
        lines, most = [], 0
        for i in range(6):
            lines.append(await h.say(200+10*i, 12, f'the toaster has a lawyer {i}', settle=False))
            await h.say(201+10*i, 30, 'lol', settle=False)
            await h.say(202+10*i, 31, 'lmao', settle=False)
            await asyncio.sleep(0)
            most = max(most, len(h.agent.memes.tasks))
        assert most == 1 and len(reads) == 1  # Dropped, not queued behind it.
        gate.set()
        await settled(h)
        assert h.agent.run.await_count == 1 and h.agent.run.await_args.args[2]['caption'] == lines[0].content
        await h.say(260, 32, 'lmao')  # Once it's done, a third member laughing at a line can start one.
        assert len(reads) == 2 and h.agent.run.await_args.args[2]['caption'] == lines[-1].content
    asyncio.run(scenario())


@pytest.mark.parametrize('spent', ['checks', 'reactions', 'gap'])
def test_a_spent_budget_stops_a_laugh_check_before_it_reads_anything(chat, spent):
    async def scenario():
        h = chat
        now = time.time()
        if spent == 'checks':
            configure(h, 2, meme_daily_checks=2)
        elif spent == 'reactions':
            configure(h, 2, meme_daily_reactions=1)
        with h.agent.memes.state() as state:
            if spent == 'checks':
                state['entries'] = {'70': {'at': now, 'channel': '3', 'status': 'quiet'},
                                    '71': {'at': now, 'channel': '3', 'status': 'quiet'}}
            else:  # A reaction two hours ago uses up a daily budget of one; ten minutes ago, the hourly gap.
                state['reactions'] = [now-7200 if spent == 'reactions' else now-600]
        reads, read = [], h.agent.context_messages

        async def counted(*args, **kwargs):
            reads.append(args)
            return await read(*args, **kwargs)
        h.agent.context_messages = counted
        line = await h.bit()
        await h.say(101, 30, 'lol')
        fetched = h.channel.fetch_message.await_count
        await h.say(102, 31, 'lmao')
        assert not reads and h.channel.fetch_message.await_count == fetched
        h.agent.run.assert_not_awaited()
        line.add_reaction.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['opted_out', 'forgotten', 'deleted', 'edited', 'line_deleted', 'line_edited',
                                    'author_opted_out', 'during_fetch', 'line_edited_past_the_live_view',
                                    'controller_down'])
def test_every_line_the_model_would_see_is_checked_again_after_the_slot_and_sign_in(chat, monkeypatch, change):
    async def scenario():
        h = chat
        analyze = model(h, monkeypatch)
        line = await h.bit()
        await h.say(101, 30, 'lol')
        message = {m.id: m for m in h.rows}

        def edit(mid):
            message[mid].content = 'nvm'
            message[mid].edited_at = dt.datetime.fromtimestamp(h.clock[0], dt.timezone.utc)
            h.agent.edit(3, message[mid])
        changes = {'opted_out': lambda: h.store.personal_control('14', 'disable'),  # Line 94's author.
                   'forgotten': lambda: h.agent.forget(14), 'deleted': lambda: h.agent.unsend(3, [94]),
                   'edited': lambda: edit(94), 'line_deleted': lambda: h.agent.unsend(3, [100]),
                   'line_edited': lambda: edit(100), 'author_opted_out': lambda: h.store.personal_control('11', 'disable'),
                   # Discord has the edit, past the live view's first 500 characters, so its row looks the same.
                   'line_edited_past_the_live_view': lambda: (
                       setattr(message[100], 'content', message[100].content + ' and my old address'),
                       setattr(message[100], 'edited_at', dt.datetime.fromtimestamp(h.clock[0], dt.timezone.utc))),
                   'controller_down': lambda: setattr(h.agent, 'profile', AsyncMock(side_effect=ControllerUnavailable('down')))}
        if change == 'during_fetch':  # After the lines were read, while the line itself is fetched.
            async def fetch(mid):
                h.store.personal_control('14', 'disable')
                return message[mid]
            h.channel.fetch_message.side_effect = fetch
            await h.say(102, 31, 'lmao')
        else:
            await h.agent.slots.acquire()  # Another model call is running.
            await h.say(102, 31, 'lmao', settle=False)
            await waiting(h, 100)
            changes[change]()
            h.agent.slots.release()
            await settled(h)
        analyze.assert_not_awaited()
        line.add_reaction.assert_not_awaited()
        with h.agent.memes.state(write=False) as state:
            assert state['entries']['100']['status'] == 'skipped'  # Considered, so not judged again later.
    asyncio.run(scenario())


def test_the_last_look_lets_an_unchanged_line_through(chat, monkeypatch):
    async def scenario():
        h = chat
        analyze = model(h, monkeypatch)
        line = await h.bit()
        await h.say(101, 30, 'lol')
        await h.agent.slots.acquire()
        await h.say(102, 31, 'lmao', settle=False)
        await waiting(h, 100)
        h.agent.slots.release()
        await settled(h)
        analyze.assert_awaited_once()
        data = analyze.await_args.args[2]
        assert data['caption'] == LINE and [row['text'] for row in data['preceding_lines']] == [t for _, _, t in BIT[-5:]]
        line.add_reaction.assert_awaited_once_with('💀')
    asyncio.run(scenario())


def test_no_model_call_once_the_reactions_are_spent_while_a_check_waits(chat, monkeypatch):
    async def scenario():
        h = chat
        analyze = model(h, monkeypatch)
        configure(h, 2, meme_daily_reactions=1)
        await h.agent.slots.acquire()  # Another model call is running while four lines get their laughs.
        lines = []
        for i in range(4):
            lines.append(await h.say(200+10*i, 12, f'the toaster has a lawyer {i}', settle=False))
            await h.say(201+10*i, 30, 'lol', settle=False)
            await h.say(202+10*i, 31, 'lmao', settle=False)
            await asyncio.sleep(0)
        await waiting(h, 200)
        with h.agent.memes.state() as state:  # The timer's meme check reacted meanwhile: today's one reaction.
            state['reactions'].append(time.time())
        h.agent.slots.release()
        await settled(h)
        analyze.assert_not_awaited()
        for line in lines:
            line.add_reaction.assert_not_awaited()
    asyncio.run(scenario())


# The pattern laughter() replaced: the same laughs, matched with backtracking that could take seconds.
OLD_LAUGH = r"(?:lo+l|lmf?ao+|ha(?:ha)+h?|ㅋ{2,}|i['’]?m\s+dead|crying|😂|🤣|💀|😭)"
OLD_LAUGHTER = re.compile(OLD_LAUGH + r'(?:\s*' + OLD_LAUGH + r')*[\s.!]*', re.I)


def test_laughter_takes_no_time_on_long_near_laughs_and_matches_the_same_laughs():
    from fri.fri_memes import laughter
    started = time.perf_counter()
    for text in ('ㅋ' * 39 + '?', 'ㅋㅋ ' * 13 + '?', 'haha' * 9 + 'ha?', 'lol' * 13 + '?', 'hahah' * 7 + 'x'):
        assert not laughter(text), text
    assert time.perf_counter()-started < 0.5  # The old pattern took seconds on the first alone.
    pieces = ['lol', 'lool', 'LOL', 'lmao', 'lmfao', 'lmaooo', 'haha', 'hahah', 'HaHaHa', 'ㅋ', 'ㅋㅋ', 'ㅋㅋㅋ',
              "i'm dead", 'im  dead', 'i’m dead', 'crying', '😂', '🤣', '💀', '😭', '😂️', 'h', 'a', 'l', 'o',
              'i', 'm', 'dead', 'cry', ' ', '  ', '\t', '.', '!', '?', 'x', 'ok', ' ']
    rng = random.Random(105)
    for _ in range(5000):
        text = ''.join(rng.choice(pieces) for _ in range(rng.randint(1, 6)))
        stripped = text.replace('️', '').strip()
        expected = 0 < len(stripped) <= 40 and OLD_LAUGHTER.fullmatch(stripped) is not None
        assert laughter(text) == expected, repr(text)


@pytest.mark.parametrize('change', ['delete', 'forget', 'edit', 'optout'])
def test_context_withdrawn_during_the_last_looks_reads_never_reaches_the_model(chat, monkeypatch, change):
    """The last look's Discord fetch comes first and its live-view check last, so a change while it reads still counts."""
    async def scenario():
        h = chat
        analyze = model(h, monkeypatch)
        line = await h.bit()
        await h.say(101, 30, 'lol')
        old_text = next(r for r in h.agent.recent[3] if r['id'] == '94')['text']
        changed, observed = [], []
        checked = h.agent.unwithdrawn

        def withdraw():
            if change == 'delete':
                h.agent.unsend(3, [94])
            elif change == 'forget':
                h.agent.forget(14)
            elif change == 'edit':
                message = next(m for m in h.rows if m.id == 94)
                message.content = 'I take that back'
                message.edited_at = dt.datetime.fromtimestamp(h.clock[0], dt.timezone.utc)
                h.agent.edit(3, message)
            else:
                h.store.personal_control('14', 'disable')
            changed.append(True)

        async def fetch(mid):  # Discord's HTTP read yields; a withdrawal can land meanwhile.
            await asyncio.sleep(0)
            return next(m for m in h.rows if m.id == mid)

        async def last_check(*args):
            allowed = await checked(*args)
            asyncio.get_running_loop().call_soon(withdraw)  # Lands right after the privacy read.
            return allowed

        def infer(*args, **kwargs):
            observed.append((bool(changed), any(r['text'] == old_text for r in args[2]['preceding_lines'])))
            return {'reaction': 'quiet', 'confidence': 'high', 'observation': ''}, {}, 'completed'

        h.agent.unwithdrawn = last_check
        h.channel.fetch_message.side_effect = fetch
        analyze.side_effect = infer
        await h.say(102, 31, 'lmao')
        await settled(h)
        line.add_reaction.assert_not_awaited()
        assert not any(withdrawn and sent for withdrawn, sent in observed)
    asyncio.run(scenario())
