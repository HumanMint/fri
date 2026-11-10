"""Durable conservative dollar reservations."""
import asyncio

import pytest
from test_engine_flow import make_engine

from fri.jpcall.budget import RATES, Budget
from fri.jpcall.contract import CallRejected
from fri.jpcall.recovery import AttemptJournal


def ledger(tmp_path):
    AttemptJournal.initialize(tmp_path)
    Budget.initialize(tmp_path, spent_usd=0, tariff={
        **{k: '1' for k in RATES}, 'text_model': 'text', 'realtime_model': 'voice', 'transcribe_model': 'asr'})
    journal = AttemptJournal(tmp_path)
    journal.open()
    return journal, Budget(journal)


def test_reservations_survive_restart_and_block_at_cap(tmp_path):
    journal, budget = ledger(tmp_path)
    budget.reserve(5_000_000)
    journal.close()
    journal = AttemptJournal(tmp_path)
    journal.open()
    try:
        budget = Budget(journal)
        assert budget.status()['reserved_usd'] == 5
        with pytest.raises(CallRejected, match='exhausted'):
            budget.reserve(1)
    finally:
        journal.close()


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'duplicate'])
def test_unknown_ledger_blocks_requests(tmp_path, damage):
    journal, budget = ledger(tmp_path)
    try:
        if damage == 'missing':
            budget.path.unlink()
        else:
            budget.path.write_text('{' if damage == 'corrupt' else '{"version":1,"version":1}')
        assert budget.status()['admission'] == 'blocked'
        with pytest.raises(CallRejected):
            budget.reserve(1)
    finally:
        journal.close()


@pytest.mark.parametrize('state', ['exhausted', 'unseeded', 'unreadable'])
def test_budget_failure_prevents_dial_and_text_api(tmp_path, monkeypatch, state):
    async def run():
        engine, provider, peer, http, *_ = await make_engine(tmp_path, monkeypatch)
        try:
            from test_engine_flow import request
            req = request()
            brief = await engine.plan(req)
            if state == 'exhausted':
                engine._budget.reserve(5_000_000)
            elif state == 'unseeded':
                engine._budget.path.unlink()
                (engine._directory / 'budget.initialized').unlink()
            else:
                engine._budget.path.write_text('{')
            with pytest.raises(CallRejected):
                await engine.start(brief, req, None, None)
            from fri.jpcall.translate import TextError
            from fri.jpcall.usage import UsageMeter
            with pytest.raises(TextError):
                await engine._text.generate('summary', UsageMeter(engine._prices), 'summarize', {'text': 'secret'})
            assert not provider.requests and not peer.sent
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_midcall_voice_request_is_reserved_before_send(tmp_path, monkeypatch):
    async def run():
        from fri.jpcall.realtime import EVENTS, RealtimeError
        engine, provider, peer, http, *_ = await make_engine(tmp_path, monkeypatch)
        try:
            from test_engine_flow import answer_phone, dial
            call, *_ = await dial(engine, provider)
            await answer_phone(call)
            update = next(e['session'] for e in peer.sent if e['type'] == EVENTS['session_update'])
            assert update['max_output_tokens'] == 512
            assert update['truncation']['token_limits']['post_instructions'] == 4096
            engine._budget.reserve(5_000_000 - engine._budget.read()['reserved'])
            before = len([r for r in peer.sent if r['type'] == EVENTS['response_create']])
            with pytest.raises(RealtimeError):
                await call._rt.say('追加の有料発言')
            assert len([r for r in peer.sent if r['type'] == EVENTS['response_create']]) == before
            assert engine.budget_status()['admission'] == 'blocked'
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_write_failure_cannot_admit_paid_request(tmp_path, monkeypatch):
    journal, budget = ledger(tmp_path)
    try:
        def fail(*args):
            raise CallRejected('disk full')
        monkeypatch.setattr(budget, '_write', fail)
        with pytest.raises(CallRejected):
            budget.reserve(1)
    finally:
        journal.close()


def test_deleted_budget_cannot_be_reinitialized(tmp_path):
    journal, budget = ledger(tmp_path)
    budget.path.unlink()
    journal.close()
    with pytest.raises(FileExistsError):
        Budget.initialize(tmp_path, spent_usd=0, tariff={
            **{k: '1' for k in RATES}, 'text_model': 'text', 'realtime_model': 'voice', 'transcribe_model': 'asr'})


def test_oversize_text_cannot_reach_provider(tmp_path, monkeypatch):
    async def run():
        from fri.jpcall.translate import TextError
        from fri.jpcall.usage import UsageMeter
        engine, provider, peer, http, *_ = await make_engine(tmp_path, monkeypatch)
        try:
            before = engine._budget.read()['reserved']
            with pytest.raises(TextError):
                await engine._text.generate('translation', UsageMeter(engine._prices), 'translate', {'text': '秘' * 32768})
            assert not provider.requests and engine._budget.read()['reserved'] == before
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_changed_model_is_unknown_to_the_approved_tariff(tmp_path, monkeypatch):
    async def run():
        from dataclasses import replace
        engine, provider, peer, http, *_ = await make_engine(tmp_path, monkeypatch)
        try:
            changed = replace(engine.config, text_model='unpriced-model')
            assert engine._budget.status(changed)['admission'] == 'blocked'
            with pytest.raises(CallRejected, match='tariff'):
                engine._budget.call(changed)
            assert not provider.requests
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_health_distinguishes_unseeded_unreadable_and_open_reservations(tmp_path):
    from types import SimpleNamespace

    from fri.fri import Fri

    def health():
        client = SimpleNamespace(agent=None, guild_id=1, data=tmp_path, get_guild=lambda _: None,
            is_ready=lambda: False, _connection=SimpleNamespace(_status='offline'),
            jpcall_engine=SimpleNamespace(budget_status=budget.status))
        asyncio.run(Fri.health.coro(client))
        import json
        return json.loads((tmp_path / 'fri-health.json').read_text())['jpcall_budget']

    journal = AttemptJournal(tmp_path)
    budget = Budget(journal)
    assert budget.status() == {'admission': 'blocked', 'ledger_state': 'unseeded',
                               'limit_usd': 5, 'remaining_usd': None,
                               'reserved_usd': None, 'open_reservations': None}
    assert health() == budget.status()
    journal, budget = ledger(tmp_path)
    try:
        budget.reserve(250_000, 'text')
        status = budget.status()
        assert health() == status
        assert status['ledger_state'] == 'seeded' and status['remaining_usd'] == 4.75
        assert status['open_reservations']['text'] == {'count': 1, 'upper_usd': .25}
        assert status['open_reservations']['call'] == {'count': 0, 'upper_usd': 0}
    finally:
        journal.close()
    journal = AttemptJournal(tmp_path)
    journal.open()
    try:
        budget = Budget(journal)
        assert budget.status() == status
        budget.path.write_text('{')
        assert budget.status()['ledger_state'] == 'unreadable'
        assert budget.status()['remaining_usd'] is None
        assert health() == budget.status()
    finally:
        journal.close()


@pytest.mark.parametrize('payload', ['[' * 2000 + ']' * 2000, '[]', 'null', '1', ' ' * 1000000],
                         ids=['deep', 'array', 'null', 'scalar', 'oversize'])
def test_bad_budget_cannot_stop_health_or_admit_spend(tmp_path, payload):
    import json
    from types import SimpleNamespace

    from fri.fri import Fri

    journal, budget = ledger(tmp_path)
    try:
        budget.path.write_text(payload)
        client = SimpleNamespace(agent=None, guild_id=1, data=tmp_path, get_guild=lambda _: None,
            is_ready=lambda: False, _connection=SimpleNamespace(_status='offline'),
            jpcall_engine=SimpleNamespace(budget_status=budget.status))
        for _ in range(2):  # The real health coroutine still runs and updates after corrupt input.
            asyncio.run(Fri.health.coro(client))
            status = json.loads((tmp_path / 'fri-health.json').read_text())['jpcall_budget']
            assert status['ledger_state'] == 'unreadable' and status['admission'] == 'blocked'
        with pytest.raises(CallRejected):
            budget.reserve(1)
    finally:
        journal.close()


def test_health_survives_an_unexpected_budget_contribution_failure(tmp_path):
    import json
    from types import SimpleNamespace

    from fri.fri import Fri

    def broken():
        raise RecursionError('synthetic budget implementation failure')
    client = SimpleNamespace(agent=None, guild_id=1, data=tmp_path, get_guild=lambda _: None,
        is_ready=lambda: False, _connection=SimpleNamespace(_status='offline'),
        jpcall_engine=SimpleNamespace(budget_status=broken))
    asyncio.run(Fri.health.coro(client))
    assert json.loads((tmp_path / 'fri-health.json').read_text())['jpcall_budget'] == {
        'admission': 'blocked', 'ledger_state': 'unreadable',
        'remaining_usd': None, 'open_reservations': None}


def test_ledger_reader_never_requests_unbounded_bytes(tmp_path, monkeypatch):
    from contextlib import contextmanager

    import fri.jpcall.budget as module

    journal, budget = ledger(tmp_path)
    original = module.os.fdopen
    reads = []
    try:
        budget.path.write_bytes(b' ' * 1000000)

        @contextmanager
        def bounded(fd, *args, **kwargs):
            with original(fd, *args, **kwargs) as source:
                class Reader:
                    def fileno(self):
                        return source.fileno()

                    def read(self, size=-1):
                        reads.append(size)
                        assert 0 < size <= 65537
                        return source.read(size)
                yield Reader()
        monkeypatch.setattr(module.os, 'fdopen', bounded)
        with pytest.raises(CallRejected):
            budget.read()
        assert reads == [65537]
    finally:
        journal.close()
