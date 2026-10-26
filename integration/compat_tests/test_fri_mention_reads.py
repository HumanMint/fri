"""How many Discord reads an @mention waits on (roadmap item 5): every check that guards something
stays, in its one-by-one order, and a check repeated with nothing in between is gone."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from compat_tests.test_fri_mentions import attachment, environment
from fri.fri_chat import ChatResult

REAL_SLEEP = asyncio.sleep
PIXELS = 'data:image/png;base64,aGVsbG8='


class Reads:
    """Counts Discord REST reads, and the rounds a member waits on: a round starts when a read goes out
    with none in flight, so reads sent together count once."""

    def __init__(self):
        self.reads = self.rounds = self.inflight = 0

    def wrap(self, read):
        async def counted(*args, **kwargs):
            self.reads += 1
            self.inflight += 1
            if self.inflight == 1:
                self.rounds += 1
            try:
                await REAL_SLEEP(0)  # A real request yields, so reads sent together overlap.
                result = read(*args, **kwargs)
                return await result if asyncio.iscoroutine(result) else result
            finally:
                self.inflight -= 1
        return AsyncMock(side_effect=counted)

    def snapshot(self):
        return self.reads, self.rounds


def counted_environment(monkeypatch, tmp_path):
    fri, agent, store, controller, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
    reads, guild = Reads(), channel.guild
    guild.fetch_channel = reads.wrap(lambda cid: channel)
    guild.fetch_member = reads.wrap(lambda user: SimpleNamespace(id=user, bot=user == 99))
    guild.fetch_roles = reads.wrap(lambda: [])
    channel.fetch_message = reads.wrap(lambda mid: rows[mid])
    history = channel.history
    def counted_history(**kwargs):  # One page read, however many messages it yields.
        reads.reads += 1
        reads.rounds += reads.inflight == 0
        return history(**kwargs)
    channel.history = counted_history
    return fri, agent, store, channel, message, calls, rows, reads


# Before the model: main's reads plus a reread of the ping after the privacy read, and for an image ping one
# more after the image post's privacy read.
BEFORE = {'plain': (19, 19), 'image': (24, 24), 'image and tool': (24, 24)}
# After the model: the ping's last check (its access, the ping before and after the privacy read), which
# covers its image. The tool adds its own checks before acting, its read, and one before the model hears
# the turn again.
AFTER = {'plain': (6, 6), 'image': (6, 6), 'image and tool': (47, 47)}


@pytest.mark.parametrize('case', ['plain', 'image', 'image and tool'])
def test_an_at_mention_waits_on_few_discord_rounds(monkeypatch, tmp_path, case):
    async def scenario():
        fri, agent, _, channel, message, calls, _, reads = counted_environment(monkeypatch, tmp_path)
        event = message('<@99> what do you think?', attachments=[attachment(channel.id)] if case != 'plain' else ())
        event.reply.return_value = SimpleNamespace(id=event.id+1, content='sent')
        monkeypatch.setattr('fri.fri_images.load_image', AsyncMock(return_value=PIXELS))
        seen = {}
        async def respond(_, context):
            seen.setdefault('model', reads.snapshot())
            if case == 'image and tool' and not context.get('action_result'):
                return ChatResult(status='completed', action={'kind': 'read_recent', 'channel_id': str(channel.id)})
            return ChatResult(status='completed', reply='that meme is doing a lot of work')
        fri.chat_once.side_effect = respond
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            event.reply.assert_awaited_once()
            before, total = seen['model'], reads.snapshot()
            after = (total[0]-before[0], total[1]-before[1])
            # (reads, rounds) on main d41a93a: before the model (18, 18), (22, 22), (22, 22); after it
            # (10, 10), (16, 16), (97, 97). The last check before the model also rereads the ping.
            assert before <= BEFORE[case] and after <= AFTER[case], (before, after)
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('failing', ['channel', 'roles', 'author', 'fri', 'message'])
@pytest.mark.parametrize('moved', [False, True])
def test_a_failed_read_behind_an_earlier_refusal_keeps_that_refusal(monkeypatch, tmp_path, failing, moved):
    # Why the ping can't be answered stays the first reason its reads give.
    from fri.fri_mentions import Invitation
    async def scenario():
        fri, agent, _, channel, message, _, rows, _ = counted_environment(monkeypatch, tmp_path)
        event = message()
        invitation = Invitation(agent, event)
        guild, lost = channel.guild, discord_error()
        moved_channel = SimpleNamespace(**{**vars(channel), 'id': 2})
        guild.fetch_channel.side_effect = (lambda cid: (_ for _ in ()).throw(lost) if failing == 'channel'
                                           else moved_channel if moved else channel)
        if failing == 'roles':
            guild.fetch_roles.side_effect = lost
        if failing in ('author', 'fri'):
            target = 20 if failing == 'author' else 99
            guild.fetch_member.side_effect = lambda user: (_ for _ in ()).throw(lost) if user == target else (
                SimpleNamespace(id=user, bot=user == 99))
        if failing == 'message':
            channel.fetch_message.side_effect = lost
        try:
            assert not await invitation.current(check_evidence=False)
            if failing == 'channel':
                assert invitation.refusal == 'check failed: NotFound'
            elif moved:
                assert invitation.refusal == 'channel changed'
            else:
                assert invitation.refusal == 'check failed: NotFound'
        finally:
            await fri.close()
    asyncio.run(scenario())


def discord_error():
    import discord
    return discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'Unknown')
