"""Measure actual save payloads after the final withholding boundary."""
import asyncio

import pytest

from compat_tests.test_fri_turn_withdrawal import world


@pytest.mark.parametrize('withheld', [False, True])
def test_model_boundary_reports_only_carried_saved_answers(monkeypatch, tmp_path, withheld):
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        first = w.said('fri explain the synthetic bicycle')
        captured = []
        original_api = w.fri.api
        async def api(method, path, payload=None):
            if path == '/chat/turn':
                captured.append(payload.copy())
            return await original_api(method, path, payload)
        w.fri.api = api
        try:
            await w.agent.handle(first)
            answer = str(first.id + 100)
            if withheld:
                original = w.agent.saved_turns.withhold
                def withhold(context, *args, **kwargs):
                    result = original(context, *args, **kwargs)
                    # A harmless question can remain; the withheld answer never reaches the request.
                    context['turns'] = [{**t, 'reply': '', 'answer_id': None} for t in context.get('turns', [])]
                    return result
                w.agent.saved_turns.withhold = withhold
            await w.agent.handle(w.said('fri explain the next synthetic bicycle'))
            assert captured[-1]['carried_answers'] == ([] if withheld else [answer])
        finally:
            await w.fri.close()
    asyncio.run(scenario())
