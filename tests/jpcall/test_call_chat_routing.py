"""A call's group chat and chat Fri, in the channel where /jpcall runs.

The call's posts (brief, transcript, questions, report) are Fri's own messages there. Only the requester answers the
call, with a question's buttons; chat isn't passed on. So while it runs, a reply to one of its posts isn't chat with
Fri, Fri's own chat channel stops answering every line, and ambient attention leaves the channel alone. Explicit chat
(Fri's name, a mention, a reply to Fri's chat) is answered as always, so is a privacy control, and everything returns
once the call ends.
Real Agent.handle/attend and the real call manager's own send; the controller, model and Discord are synthetic."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fri.fri_agent import Agent
from fri.jpcall.contract import CallRequest, Limits
from fri.jpcall.discord_command import DiscordCall, DiscordCalls

BOT, OWNER, GUILD, WORKSHOP, DEDICATED, MEMBER, REQUESTER = 99, 10, 123, 456, 789, 11, 12
NOW = (int(time.time()*1000) - 1420070400000) << 22  # A snowflake from now: messages count as fresh.


def snowflake(n):
    return NOW + n


def message(n, channel, text, reference=None, author=MEMBER):
    return SimpleNamespace(id=snowflake(n), guild=SimpleNamespace(id=GUILD), channel=SimpleNamespace(id=channel),
                           author=SimpleNamespace(id=author, bot=False, display_name='friend'), content=text,
                           mentions=[], role_mentions=[], reference=reference, webhook_id=None, embeds=[],
                           attachments=[], stickers=[], edited_at=None, type=None, flags=None, message_snapshots=[])


def reply_to(post):
    return SimpleNamespace(message_id=post.id, channel_id=post.channel.id, resolved=post)


def agent_with_call(channel):
    """An agent whose Fri has /jpcall running in `channel`, with one question posted through the call's own send."""
    fri = SimpleNamespace(owner_id=OWNER, guild_id=GUILD, workshop_id=WORKSHOP, hear=lambda event: True,
                          user=SimpleNamespace(id=BOT), busy=set(), api=AsyncMock(return_value={'pending': False}),
                          get_channel=lambda channel_id: SimpleNamespace(id=channel_id))
    calls = DiscordCalls(fri, SimpleNamespace(), [GUILD], Limits(allowed_user_ids=frozenset({REQUESTER})))
    fri.jpcall_calls = calls
    sent = iter(range(5000, 6000))

    async def send(content, **kwargs):
        return SimpleNamespace(id=snowflake(next(sent)), author=SimpleNamespace(id=BOT),
                               channel=SimpleNamespace(id=channel), content=content or '')
    request = CallRequest('a'*32, GUILD, 1, REQUESTER, '+819000000000', 'opening hours')
    calls.active[GUILD] = call = DiscordCall(calls, request, SimpleNamespace(id=1),
                                             SimpleNamespace(id=channel, send=send))
    agent = Agent(fri)
    agent.state['config']['enabled'] = True
    agent.state['channels'] = [str(DEDICATED)]
    agent.dedicated = DEDICATED
    agent.profile = AsyncMock(return_value={'enabled': True, 'revision': 0})
    agent.respond = AsyncMock()
    agent.ensure_memes = lambda: SimpleNamespace(heard=lambda event: None, forget=lambda user: None)
    return agent, calls, call


async def answered(agent, event):
    before = agent.respond.await_count
    with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
        await agent.handle(event)
    return agent.respond.await_count > before


def test_replies_to_a_running_calls_posts_stay_with_the_call():
    async def run():
        agent, calls, call = agent_with_call(WORKSHOP)
        question = await call.send('a question for the requester · will you use the parking lot?')
        chat = SimpleNamespace(id=snowflake(6000), author=SimpleNamespace(id=BOT), channel=SimpleNamespace(id=WORKSHOP),
                               content='eh, sure')  # Fri's chat reply, not the call's.
        assert not await answered(agent, message(1, WORKSHOP, 'yes we have one car', reply_to(question)))
        assert not await answered(agent, message(2, WORKSHOP, 'one car', reply_to(question), author=REQUESTER))
        # A privacy control said to Fri's post keeps its usual path (erasure itself: compat_tests/test_fri_memory_control_aim).
        assert await answered(agent, message(6, WORKSHOP, 'what do you remember about me?', reply_to(question)))
        # Explicit chat with Fri still is: by name, even on a call post, or a reply to Fri's own chat.
        assert await answered(agent, message(3, WORKSHOP, 'fri what time is it there', reply_to(question)))
        assert await answered(agent, message(4, WORKSHOP, 'lol true', reply_to(chat)))
        calls.active.clear()  # The call ended: its posts are ordinary Fri messages again.
        assert await answered(agent, message(5, WORKSHOP, 'what did they say', reply_to(question)))
    asyncio.run(run())


def test_fris_chat_channel_stops_answering_every_line_only_while_a_call_posts_there():
    async def run():
        agent, calls, call = agent_with_call(DEDICATED)
        await call.send('before i call')
        assert not await answered(agent, message(1, DEDICATED, 'ask if they have parking'))
        assert await answered(agent, message(2, DEDICATED, 'fri you good?'))
        assert await answered(agent, message(5, DEDICATED, 'what do you remember about me?'))  # A privacy control.
        call.text_channel.id = WORKSHOP  # A call in another channel leaves Fri's chat channel as it was.
        assert await answered(agent, message(3, DEDICATED, 'ask if they have parking'))
        call.text_channel.id = DEDICATED
        calls.active.clear()
        assert await answered(agent, message(4, DEDICATED, 'anyway, how was your day'))
    asyncio.run(run())


def test_attention_leaves_a_calls_channel_to_the_call():
    async def run():
        agent, calls, call = agent_with_call(WORKSHOP)
        agent.state['channels'] = [str(DEDICATED), '321']
        agent.state['config']['heartbeat_seconds'] = 60
        agent.retired = AsyncMock(return_value=set())
        agent.run = AsyncMock(return_value=({}, {}, 'completed'))
        for n, channel in ((1, WORKSHOP), (2, 321)):
            agent.recent[channel].append({'id': str(snowflake(n)), 'user_id': str(MEMBER), 'name': 'friend',
                                          'text': 'should we ask if they are open sunday?', 'at': time.time(),
                                          'revision': 0, 'gene_error': False})
        await agent.attend()
        assert agent.run.await_count == 1  # Only the other channel was considered.
        assert agent.seen[WORKSHOP] == snowflake(1)  # And what was said during the call stays the call's.
    asyncio.run(run())
