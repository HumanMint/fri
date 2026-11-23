"""A reply the Claude fallback wrote is saved and counted under Claude, not the configured ChatGPT model."""

import asyncio
from unittest.mock import AsyncMock

from compat_tests.test_fri_reply_retention import reply_workflow
from compat_tests.test_gene_controls import controls_api  # noqa: F401
from fri.fri_chat import CLAUDE_EFFORT, CLAUDE_MODEL, ChatResult


def test_claude_answer_is_recorded_under_claude(monkeypatch, tmp_path, controls_api):  # noqa: F811
    async def scenario():
        fri, _, bot, runner, _, _, _, _, message, _, requests, controls = await reply_workflow(
            monkeypatch, tmp_path, controls_api)
        message.reference = None
        message.content = 'fri you awake'
        fri.chat.respond = AsyncMock(return_value=ChatResult(
            status='completed', reply='unfortunately', model=CLAUDE_MODEL, effort=CLAUDE_EFFORT,
            counters={'input_tokens': 12, 'output_tokens': 3, 'cached_input_tokens': 0,
                      'reasoning_output_tokens': None}))
        try:
            result = await fri.talk(123, 1, 20, message.content, 'public', event=message)
            assert (result.turn['model'], result.turn['effort']) == (CLAUDE_MODEL, CLAUDE_EFFORT)
            await fri.record_turn(result)
            reports = [body for path, body in requests if path == '/chat/usage/report']
            assert [(r['model'], r['effort']) for r in reports] == [(CLAUDE_MODEL, CLAUDE_EFFORT)]
            assert [(r['model'], r['effort']) for r in controls.store.chat_usage()] == [(CLAUDE_MODEL, CLAUDE_EFFORT)]
            with controls.store.connection() as db:
                saved = db.execute('SELECT model, effort, input_tokens FROM chat_turns').fetchall()
            assert [tuple(row) for row in saved] == [(CLAUDE_MODEL, CLAUDE_EFFORT, 12)]
        finally:
            await runner.close()
            await bot.close()
            await fri.close()
    asyncio.run(scenario())
