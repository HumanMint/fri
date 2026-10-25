"""Temporary notices follow actual reply delivery, without changing member text."""

import asyncio
import datetime as dt
import itertools
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from compat_tests.test_fri_actions import harness
from fri import feedback as commands
from fri.fri_chat import ChatResult
from fri.fri_notices import ReplyInProgress, WaitingNotices, notice_text

IDS = itertools.count(100000000000000001)


def event(user=10):
    return SimpleNamespace(id=next(IDS), author=SimpleNamespace(id=user, display_name='Member'),
        guild=SimpleNamespace(id=123), channel=SimpleNamespace(id=456, typing=Mock(return_value=AsyncMock())),
        reply=AsyncMock(return_value=SimpleNamespace(delete=AsyncMock())))


@pytest.mark.parametrize('outcome', ['reply', 'error', 'quiet', 'react', 'post', 'social'])
def test_wait_notice_is_deleted_only_after_completion(monkeypatch, tmp_path, outcome):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        first, followup = event(), event()
        started, finish = asyncio.Event(), asyncio.Event()
        notice = followup.reply.return_value
        other = event(20)
        fri.busy.add(20)
        await fri.waiting_notices.send(other)
        async def talk(*args, **kwargs):
            if fri.busy.__contains__(10):
                raise ReplyInProgress()
            fri.busy.add(10)
            started.set()
            try:
                await finish.wait()
                if outcome == 'error':
                    raise commands.UserFeedbackCheckFailure('The Image Could Not Be Loaded.')
                return ChatResult(status='completed', reply='Looks like a dog.',
                    response_kind=outcome if outcome in ('quiet', 'react') else 'reply',
                    reaction='👍' if outcome == 'react' else '',
                    post_preview={'id': 'post'} if outcome == 'post' else None,
                    social_preview={'id': 'social'} if outcome == 'social' else None)
            finally:
                fri.busy.discard(10)
        deliveries = []
        async def delivered(*args, **kwargs):
            deliveries.append(args)
            if len(deliveries) == 1:  # The first reply lands before the waiting notice goes.
                notice.delete.assert_not_awaited()
        first.reply.side_effect = delivered
        agent.acknowledge = AsyncMock(side_effect=delivered)
        for path in ('fri.fri_workspace.show_preview', 'fri.fri_social.show_preview'):
            monkeypatch.setattr(path, AsyncMock(side_effect=delivered))
        fri.talk = talk
        followup.id = discord.utils.time_snowflake(dt.datetime.now(dt.timezone.utc))  # A fresh message.
        task = asyncio.create_task(agent.respond(first, 'dog or rat'))
        waiting = None
        try:
            await started.wait()
            waiting = asyncio.create_task(agent.respond(followup, 'fri?'))
            while not followup.reply.await_count:
                await asyncio.sleep(0)
            assert followup.reply.await_args.args == ('hold on, still typing your last one.',)
            assert followup.reply.await_args.kwargs['delete_after'] == 300
            notice.delete.assert_not_awaited()
            finish.set()
            await task
            await waiting  # It waited its turn rather than being dropped.
            notice.delete.assert_awaited_once()
            other.reply.return_value.delete.assert_not_awaited()
            if outcome == 'reply':
                assert first.reply.await_args.args == ('Looks like a dog.',)  # Model text is unchanged.
                assert followup.reply.await_args.args == ('Looks like a dog.',)
            if outcome == 'error':
                assert first.reply.await_args.args == ('the image could not be loaded.',)
                assert followup.reply.await_args.args == ('the image could not be loaded.',)
        finally:
            finish.set()
            if waiting:
                await waiting
            await fri.close()
    asyncio.run(scenario())


def test_late_notice_cannot_survive_a_finished_reply():
    async def scenario():
        busy = {10}
        notices, message = WaitingNotices(busy), event()
        started, finish = asyncio.Event(), asyncio.Event()
        sent = SimpleNamespace(delete=AsyncMock())
        async def slow_send(*args, **kwargs):
            started.set()
            await finish.wait()
            return sent
        message.reply.side_effect = slow_send
        sending = asyncio.create_task(notices.send(message))
        await started.wait()
        busy.clear()
        clearing = asyncio.create_task(notices.clear(10))
        finish.set()
        await asyncio.gather(sending, clearing)
        sent.delete.assert_awaited_once()
        assert not notices.messages
        # A queued busy response arriving after completion does not post at all.
        await notices.send(message)
        assert message.reply.await_count == 1
    asyncio.run(scenario())


def test_notice_retention_is_bounded_and_delete_failure_is_nonfatal():
    async def scenario():
        busy = {10}
        notices, message = WaitingNotices(busy), event()
        sent = [SimpleNamespace(delete=AsyncMock()) for _ in range(4)]
        message.reply.side_effect = sent
        for _ in sent:
            await notices.send(message)
        sent[0].delete.assert_awaited_once()
        assert len(notices.messages[10]) == 3
        await notices.clear(10)  # Another response must not clear still-running work.
        sent[1].delete.assert_not_awaited()
        sent[1].delete.side_effect = discord.NotFound(SimpleNamespace(status=404, reason='gone'), 'gone')
        busy.clear()
        await notices.clear(10)
        assert not notices.messages
        for item in sent:
            item.delete.assert_awaited_once()
    asyncio.run(scenario())


def test_notice_casing_preserves_case_sensitive_literals():
    assert notice_text('Try `Gene.Play` at https://example.test/AbC?Key=X with <:GG:123>.\n```Python\nPrint(X)\n```') == (
        'try `Gene.Play` at https://example.test/AbC?Key=X with <:GG:123>.\n```Python\nPrint(X)\n```')


def test_panel_notice_is_lowercase_but_preview_stays_verbatim():
    from fri.fri_panel import reply
    async def scenario():
        panels = SimpleNamespace(show=AsyncMock(), current_view=lambda _: None)
        cog = SimpleNamespace(private_panels=panels)
        interaction = SimpleNamespace(user=SimpleNamespace(id=10))
        view = object()
        await reply(cog, interaction, 'Saved. Nothing Sent.', view=view)
        assert panels.show.await_args.kwargs['content'] == 'saved. nothing sent.'
        await reply(cog, interaction, 'Keep My Exact Draft', view=view, verbatim=True)
        assert panels.show.await_args.kwargs['content'] == 'Keep My Exact Draft'
    asyncio.run(scenario())
