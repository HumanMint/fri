"""chat_once's side of the provider seam: its recheck before a second provider (fri #418), one receipt per attempt with
provider fields only for a controller that keeps them, usage delivered off the reply's path, and the Claude latch in
fri health."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fri import fri as runtime
from fri.fri_chat import ChatResult
from fri.fri_provider import CAPABILITY
from fri.fri_watch import Watch


@pytest.fixture
def bot(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime.os, 'environ', {
        'GENE_GUILD_ID': '1', 'GENE_OWNER_ID': '10', 'GENE_WORKSHOP_CHANNEL_ID': '3',
        'GENE_FRI_TOKEN': 'synthetic-token', 'FRI_DATA': str(tmp_path / 'data'),
        'FRI_CHATGPT_AUTH': str(tmp_path / 'unused-auth.json')})
    subject = runtime.Fri()
    subject.api = AsyncMock(return_value={'ok': True})
    return subject


def context(**extra):
    return {'model': 'gpt-6.1-sol', 'effort': 'high', 'facts': [], 'turns': [], **extra}


def receipts(bot):
    return sorted((json.loads(path.read_text()) for path in (bot.data / 'usage').glob('*.json')),
                  key=lambda report: report['status'])


def fake_respond(bot, seen):
    """A plan attempt that failed over, then the recheck, then Claude's answer: as Chat.respond runs them."""
    async def respond(text, context, *, ready=None):
        seen['context'] = dict(context)
        recheck, record = context['fence']['recheck'], context['fence']['record']
        record('chatgpt', 'gpt-6.1-sol', 'high', ChatResult(status='failed', counters={'input_tokens': 0}), 'rate_limited')
        await recheck()
        answer = ChatResult(status='completed', reply='ok', model='claude-opus-5-5', effort='high', provider='claude',
                            cost_micros=42, counters={'input_tokens': 5})
        record('claude', 'claude-opus-5-5', 'high', answer, None)
        return answer
    bot.chat.respond = respond


@pytest.mark.parametrize('capable', [True, False])
def test_one_receipt_per_attempt_with_provider_fields_only_when_the_controller_keeps_them(bot, capable):
    bot.capabilities = frozenset({CAPABILITY} if capable else ())
    seen = {}
    fake_respond(bot, seen)

    async def run():
        result = await bot.chat_once('hi', context(provider='claude', claude_chat_model='', fence={}))
        await asyncio.gather(*bot.usage_deliveries)  # Delivered in the background, not before the reply returned.
        return result
    bot.flush_usage = AsyncMock()
    assert asyncio.run(run()).reply == 'ok'
    bot.flush_usage.assert_awaited()
    answered, failed = receipts(bot)
    base = {'failed': {'model': 'gpt-6.1-sol', 'effort': 'high', 'status': 'failed', 'counters': {'input_tokens': 0}},
            'completed': {'model': 'claude-opus-5-5', 'effort': 'high', 'status': 'completed',
                          'counters': {'input_tokens': 5}}}
    assert {k: v for k, v in failed.items() if k != 'attempt_id'} == {**base['failed'], **(
        {'provider': 'chatgpt', 'cost_micros': None, 'failover': 'rate_limited'} if capable else {})}
    assert {k: v for k, v in answered.items() if k != 'attempt_id'} == {**base['completed'], **(
        {'provider': 'claude', 'cost_micros': 42} if capable else {})}
    # An older controller never hears of the provider: the plan stays primary there.
    assert ('provider' in seen['context']) == capable


@pytest.mark.parametrize('fence', [
    {'erased': AsyncMock(return_value=True)},
    {'dms': lambda: False},
    {'commit': lambda: False}])
def test_the_recheck_before_a_second_provider_refuses_what_changed(bot, fence):
    """fri #418: an erasure, the DM switch or a continuation during the first attempt stops the second one, and the
    first attempt's receipt is still kept."""
    bot.capabilities = frozenset({CAPABILITY})
    fake_respond(bot, {})
    bot.flush_usage = AsyncMock()

    async def run():
        try:
            await bot.chat_once('hi', context(fence=fence))
        finally:
            await asyncio.gather(*bot.usage_deliveries)
    with pytest.raises((runtime.AuthorErased, runtime.GateClosed, runtime.Continued)):
        asyncio.run(run())
    assert [report['status'] for report in receipts(bot)] == ['failed']


def test_a_message_deleted_during_the_first_attempt_stops_the_second(bot):
    bot.capabilities = frozenset()
    bot.agent = SimpleNamespace(unsent={5})
    fake_respond(bot, {})
    bot.flush_usage = AsyncMock()
    bot.depend = lambda context: None

    async def run():
        ready_context = context(source={'kind': 'discord_message', 'id': '5'}, fence={})
        await bot.chat_once('hi', ready_context)
    with pytest.raises(runtime.GateClosed):
        asyncio.run(run())


def test_fri_health_shows_the_claude_latch(bot):
    bot.chat.claude_off = (runtime.time.monotonic() + 600, 'billing', runtime.time.time())
    bot.agent = None
    bot.is_ready = lambda: False
    bot.watching = None
    bot.owner_reviews = SimpleNamespace(is_running=lambda: True, failed=lambda: False)
    bot.health = SimpleNamespace(is_running=lambda: True, failed=lambda: False)
    watch = Watch(bot)
    watch.probe = AsyncMock(return_value='unreachable')
    report = asyncio.run(watch.report())
    assert '- claude: skipped after a billing failure, until <t:' in report
    bot.chat.claude_off = None
    assert '- claude:' not in asyncio.run(watch.report())
