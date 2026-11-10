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


def test_upgrade_breadcrumb_is_not_reported_as_carried_answer(monkeypatch, tmp_path):
    async def scenario():
        from gene.fri_member_summary import advance
        from gene.store import Store
        w = await world(monkeypatch, tmp_path)
        captured = []
        original_api = w.fri.api
        async def api(method, path, payload=None):
            if path == '/chat/turn':
                captured.append(payload.copy())
            return await original_api(method, path, payload)
        w.fri.api = api
        try:
            first = w.said('fri explain the synthetic bicycle')
            await w.agent.handle(first)
            with w.store.connection() as db:
                db.execute('UPDATE chat_turns SET summary_generation=0,drew_on=? WHERE reply_message_id=?',
                           ('["900"]', str(first.id+100)))
                db.execute('DROP TRIGGER chat_answer_retired')
                db.execute('ALTER TABLE chat_turns DROP COLUMN answer_retired')
                advance(db, '123', '20')
            Store(w.store.path)
            w.agent.recent[1].clear()  # C sees saved history and the harmless live question, not the old live answer.
            await w.agent.handle(w.said('fri explain a different synthetic bicycle'))
            assert captured[-1]['carried_answers'] == []
            w.store.withdraw_saved('123', ['900'], deleted=True)
            assert w.store.chat_context('123', 'public:1', '20')['turns'][-1]['reply'] == captured[-1]['reply']
        finally:
            await w.fri.close()
    asyncio.run(scenario())
