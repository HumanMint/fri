import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import pytest
from discord.ext import commands as discord_commands
from gene.runtime_controls import ControlRunner
from redbot.core import commands
from redbot.core.bot import Red

from compat_tests.test_gene_controls import control_body


class OfflineRed(discord_commands.Bot):
    """Use installed Red's additive hook dispatcher, not discord.py's single hook."""
    _before_invoke = Red._before_invoke
    _red_before_invoke_method = Red._red_before_invoke_method
    before_invoke = Red.before_invoke
    remove_before_invoke_hook = Red.remove_before_invoke_hook

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._red_before_invoke_objs = set()
        self.wait_until_red_ready = AsyncMock()


async def runtime():
    """Real Red commands/checks/parser with an offline Discord transport and fictional members."""
    bot = OfflineRed(command_prefix='!', intents=discord.Intents.none(), help_command=None, owner_ids={10})
    bot._connection.user = discord.ClientUser(state=bot._connection,
        data={'id': '99', 'username': 'Gene', 'discriminator': '0', 'avatar': None})
    bot.dispatch = Mock()
    bot.verify_permissions_hooks = AsyncMock(return_value=None)
    bot.cog_disabled_in_guild = AsyncMock(return_value=False)
    member = SimpleNamespace(id=20, name='user', display_name='current nick', discriminator='0',
                             bot=False, voice=None, roles=[], is_timed_out=lambda: False)
    guild = SimpleNamespace(id=123, unavailable=False, owner_id=10, me=bot.user,
        fetch_member=AsyncMock(return_value=member), get_member=lambda _: member)
    channel = SimpleNamespace(id=1, guild=guild, category=None, type=discord.ChannelType.text,
        permissions_for=lambda _: discord.Permissions.all(), send=AsyncMock())
    bot.get_guild = lambda _: guild
    bot.get_channel = lambda _: channel
    calls = []

    class Audio(commands.Cog):
        @commands.command(name='play')
        @commands.guild_only()
        @commands.check(lambda ctx: ctx.author.voice is not None)
        @commands.cooldown(1, 60, commands.BucketType.user)
        async def play(self, ctx, *, query: str):
            calls.append((ctx.author, query, ctx.message.id))
            await ctx.send(embed=discord.Embed(title='Track Enqueued', description=query))

        @commands.command(name='pause')
        async def pause(self, ctx):
            calls.append('toggle')
            await ctx.send('Paused')

        @commands.command(name='queue')
        async def queue(self, ctx):
            raise AssertionError('Interactive reaction menu must not be opened')

    audio = Audio()
    audio.requires.ready_event.set()
    for cmd in audio.get_commands():
        cmd.requires.ready_event.set()
    await bot.add_cog(audio)
    cog = SimpleNamespace(guild_id=123, api=AsyncMock())
    claim = {**control_body(), 'id': 'request', 'lease': 'a'*32}
    message = discord.Message(state=bot._connection, channel=channel, data={
        'id': claim['source']['id'], 'type': 0, 'content': claim['request_text'],
        'author': {'id': '20', 'username': 'user', 'discriminator': '0', 'avatar': None},
        'attachments': [], 'embeds': [], 'mentions': [], 'mention_roles': [], 'pinned': False,
        'mention_everyone': False, 'tts': False, 'edited_timestamp': None})
    # An old cached member must not supply the execution identity/roles.
    message.author = SimpleNamespace(id=20, bot=False, name='old nick')
    channel.fetch_message = AsyncMock(return_value=message)
    return bot, ControlRunner(bot), cog, claim, member, channel, calls, message


def test_red_hook_dispatch_does_not_reenter_and_unload_preserves_other_hooks():
    async def scenario():
        bot, runner, cog, claim, member, channel, calls, message = await runtime()
        # Fail immediately on recursion rather than allocating tasks until OOM.
        bot.wait_until_red_ready.side_effect = [None, AssertionError('Recursive Red hook dispatcher')]
        other = AsyncMock()
        bot.before_invoke(other)
        member.voice = SimpleNamespace(channel=SimpleNamespace(id=8))
        try:
            result = await runner.execute(cog, claim)
            assert 'Track Enqueued' in result['output']
            assert len(calls) == 1
            other.assert_awaited_once()
            await runner.close()
            assert runner.before not in bot._red_before_invoke_objs
            assert other in bot._red_before_invoke_objs
        finally:
            await bot.close()
    asyncio.run(scenario())


def test_real_red_permission_and_cooldown_pipeline_and_current_member():
    async def scenario():
        bot, runner, cog, claim, member, channel, calls, message = await runtime()
        try:
            assert 'blocked' in await runner.execute(cog, claim)
            assert not calls
            member.voice = SimpleNamespace(channel=SimpleNamespace(id=8))
            result = await runner.execute(cog, claim)
            assert 'Track Enqueued' in result['output']
            assert calls == [(member, 'Porter Robinson Sad Machine', message.id)]
            assert 'blocked' in await runner.execute(cog, claim)  # Red cooldown still applies.
            assert len(calls) == 1
            channel.send.assert_not_awaited()
            assert message.content == claim['request_text'] and message.author.name == 'old nick'
            channel.guild.fetch_member.assert_awaited_with(20)
        finally:
            await bot.close()
    asyncio.run(scenario())


def test_normal_prefix_command_uses_red_hooks_without_dm_routing():
    async def scenario():
        bot, runner, _, _, member, channel, calls, message = await runtime()
        member.voice = SimpleNamespace(channel=SimpleNamespace(id=8))
        message.author = member
        message.content = '!play Sad Machine'
        bot.wait_until_red_ready.side_effect = [None, AssertionError('Recursive Red hook dispatcher')]
        try:
            ctx = await bot.get_context(message, cls=commands.Context)
            ctx.send = AsyncMock()
            with patch('lavalink.get_player') as player:
                await bot.invoke(ctx)
                player.assert_not_called()
            assert not ctx.command_failed and len(calls) == 1
            ctx.send.assert_awaited_once()
        finally:
            await runner.close()
            await bot.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('boundary', ['edited', 'webhook', 'different_author', 'channel_access', 'wrong_guild', 'no_member', 'wrong_source'])
def test_runtime_rechecks_discord_event_and_access_before_start(boundary):
    async def scenario():
        bot, runner, cog, claim, member, channel, calls, message = await runtime()
        member.voice = SimpleNamespace(channel=SimpleNamespace(id=8))
        if boundary == 'edited':
            message.content = 'never mind'
        elif boundary == 'webhook':
            message.webhook_id = 90
        elif boundary == 'different_author':
            message.author.id = 30
        elif boundary == 'channel_access':
            channel.permissions_for = lambda _: discord.Permissions.none()
        elif boundary == 'wrong_guild':
            claim['guild_id'] = '777'
        elif boundary == 'no_member':
            channel.guild.fetch_member.return_value = None
        else:
            claim['source']['channel_id'] = '2'
        try:
            assert 'blocked' in await runner.execute(cog, claim)
            assert not calls
            cog.api.assert_not_awaited()
        finally:
            await bot.close()
    asyncio.run(scenario())


def test_verified_dm_uses_same_red_permissions_without_posting_fake_messages():
    async def scenario():
        bot, runner, cog, claim, member, channel, calls, _ = await runtime()
        claim['source'].update(guild_id=None, channel_id='999')
        claim['conversation_id'] = 'private:999'
        member.voice = SimpleNamespace(channel=SimpleNamespace(id=8))
        try:
            result = await runner.execute(cog, claim)
            assert 'Track Enqueued' in result['output'] and calls[0][0] is member
            channel.fetch_message.assert_not_awaited()
            channel.send.assert_not_awaited()
            cog.api.assert_awaited_once_with('POST', '/runtime/controls/request/start', {'lease': 'a'*32})
        finally:
            await bot.close()
    asyncio.run(scenario())


def test_disabled_queue_never_opens_menu_and_pause_does_not_toggle_twice():
    async def scenario():
        bot, runner, cog, claim, member, channel, calls, message = await runtime()
        try:
            for action, text in [('queue', 'fri whats in the queue'), ('pause', 'fri pause')]:
                claim.update(action=action, argument='', request_text=text)
                message.content = text
                cmd = bot.get_command(action)
                cmd.disable_in(channel.guild)
                with patch('lavalink.get_player', return_value=SimpleNamespace(paused=True)):
                    if action == 'queue':
                        with pytest.raises(commands.DisabledCommand):
                            await runner.execute(cog, claim)
                    else:
                        assert 'blocked' in await runner.execute(cog, claim)
                cmd.enable_in(channel.guild)
            with patch('lavalink.get_player', return_value=SimpleNamespace(paused=True)):
                assert 'already paused' in (await runner.execute(cog, claim))['output']
            assert not calls
        finally:
            await bot.close()
    asyncio.run(scenario())



def test_queue_adapter_preserves_red_before_and_after_hooks():
    async def scenario():
        bot, runner, cog, claim, member, channel, calls, message = await runtime()
        claim.update(action='queue', argument='', request_text='fri whats queued')
        message.content = claim['request_text']
        cmd = bot.get_command('queue')
        before, after = AsyncMock(), AsyncMock()
        cmd.before_invoke(before)
        cmd.after_invoke(after)
        try:
            with patch.object(runner, 'queue', return_value='Nothing queued') as read:
                assert (await runner.execute(cog, claim))['output'] == 'Nothing queued'
                before.assert_awaited_once()
                after.assert_awaited_once()
                read.assert_called_once()
            before.side_effect = commands.CheckFailure('Channel restricted by Audio hook')
            with patch.object(runner, 'queue') as read:
                with pytest.raises(commands.CheckFailure):
                    await runner.execute(cog, claim)
                read.assert_not_called()
        finally:
            await bot.close()
    asyncio.run(scenario())


def test_slow_red_operation_keeps_its_lifecycle_and_no_second_claim_starts():
    async def scenario():
        bot, runner, cog, claim, _, _, _, _ = await runtime()
        pending, started = asyncio.Event(), asyncio.Event()
        bot.wait_until_ready = AsyncMock()
        bot.is_closed = lambda: False
        bot.get_cog = lambda _: cog
        cog.api.return_value = claim
        async def operation(*args):
            started.set()
            await pending.wait()
            bot.is_closed = lambda: True
            return {'output': 'Track enqueued'}
        runner.execute = operation
        runner.start()
        try:
            await started.wait()
            assert not runner.task.done() and cog.api.await_count == 1
            # Fri/controller can report an uncertain result independently; no cancellation or retry.
            pending.set()
            with patch('gene.runtime_controls.asyncio.sleep', new=AsyncMock()):
                await runner.task
            assert cog.api.await_count == 2
            assert cog.api.await_args.args[1].endswith('/finish')
            assert cog.api.await_args.args[2]['output'] == 'Track enqueued'
        finally:
            await runner.close()
            await bot.close()
    asyncio.run(scenario())


def test_dm_notification_routing_occurs_after_red_permission_checks():
    async def scenario():
        bot, runner, cog, claim, member, channel, calls, _ = await runtime()
        claim['source'].update(guild_id=None, channel_id='999')
        claim['conversation_id'] = 'private:999'
        player = SimpleNamespace(store=Mock())
        try:
            with patch('lavalink.get_player', return_value=player):
                await runner.execute(cog, claim)
                player.store.assert_not_called()  # Not in voice; denied by Red.
                member.voice = SimpleNamespace(channel=SimpleNamespace(id=8))
                await runner.execute(cog, claim)
                player.store.assert_called_once_with('notify_channel', channel.id)
        finally:
            await runner.close()
            await bot.close()
    asyncio.run(scenario())
