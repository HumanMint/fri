"""Offline replay checks: real handler and controller, scripted provider, synthetic Discord."""

import datetime as dt
import json
from unittest.mock import patch

import httpx
import pytest
from starlette.testclient import TestClient

import fri.fri_image_share
from replay.harness import (
    Check,
    Inspect,
    Replay,
    Say,
    Scenario,
    UnscriptedCall,
    Wait,
    act,
    failures,
    reply,
    run,
)
from replay.scenarios import (
    CLOSING,
    DELIVERY_FAILURE,
    ERASURE,
    OUTSIDE_MENTION,
    SCENARIOS,
    WEB_CLARIFICATION,
)


def rejecting(path):
    """Make the synthetic controller answer one route with HTTP 403."""
    original = TestClient.request

    def request(self, method, url, *args, **kwargs):
        if url == path:
            return httpx.Response(403, json={'detail': 'synthetic rejection'},
                                  request=httpx.Request(method, 'http://testserver'+url))
        return original(self, method, url, *args, **kwargs)
    return patch.object(TestClient, 'request', request)


class KnownGapPresent(Exception):
    """Raised only by the final gap assertion; no other failure can satisfy a known-gap xfail."""


def preconditions(report):
    """Raise a non-gap error so a broken run can never pass as a reproduced known gap."""
    problems = failures([report])
    if problems:
        raise RuntimeError(problems)


@pytest.mark.parametrize('scenario', SCENARIOS, ids=lambda s: s.name)
def test_scenario_routes_and_checks(scenario, tmp_path):
    report = run(scenario, tmp_path)
    preconditions(report)
    json.dumps(report)


GAPS = [pytest.param(scenario, check.name, id=f'{scenario.name}:{check.name}',
                     marks=pytest.mark.xfail(strict=True, raises=KnownGapPresent, reason=check.known_gap))
        for scenario in SCENARIOS for check in scenario.checks if check.known_gap]


@pytest.mark.parametrize('scenario,name', GAPS)
def test_known_gap_still_reproduces(scenario, name, tmp_path):
    """Strict xfail on KnownGapPresent only: fixing the gap fails the suite until the marker is removed."""
    report = run(scenario, tmp_path)
    preconditions(report)
    result = next(c for c in report['checks'] if c['name'] == name)
    if result['detail']:
        raise RuntimeError(result['detail'])  # An erroring check is not a reproduction.
    if not result['passed']:
        raise KnownGapPresent(result)


def test_manifests_and_routes_never_copy_member_text(tmp_path):
    report = run(ERASURE, tmp_path)
    recorded = json.dumps([{key: step[key] for key in ('model', 'route', 'controller_paths')}
                           for step in report['steps']])
    assert 'denver' not in recorded and 'moving' not in recorded
    assert report['prompt_versions'] and report['tokens']['input_tokens'] > 0


def test_unscripted_model_call_fails_loudly(tmp_path):
    scenario = Scenario('unscripted', 'A model call nobody scripted.', [Say(20, 'fri yo', label='yo')])
    with pytest.raises(UnscriptedCall, match='unscripted conversation model call'):
        run(scenario, tmp_path)


def live_reply(text):
    def provider(request):
        event = {'type': 'response.completed', 'response': {'usage': {'input_tokens': 7, 'output_tokens': 3},
                 'output': [{'type': 'message', 'content': [
                     {'type': 'output_text', 'text': json.dumps(reply(text))}]}]}}
        return httpx.Response(200, text='data: '+json.dumps(event)+'\n\n')
    return provider


def test_live_mode_uses_the_given_transport_and_enforces_budget(tmp_path):
    scenario = Scenario('budget', 'Two turns, budget for one.',
                        [Say(20, 'fri yo', label='one'), Say(20, 'fri you there', label='two')],
                        {'conversation': [reply('script must be ignored in live mode')]})
    report = run(scenario, tmp_path, live=httpx.MockTransport(live_reply('live yo')), max_calls=1)
    assert report['model_calls'] == 1 and report['mode'] == 'live'
    one, two = report['steps'][1], report['steps'][2]
    assert one['deliveries'][0]['text'] == 'live yo' and one['route']['inference'] == ['completed']
    assert two['route']['model_calls'] == 0 and two['route']['kind'] == 'notice'
    assert two['route']['inference'] and two['route']['inference'] != ['completed']
    assert failures([report])


def test_provider_error_notice_never_counts_as_an_answer(tmp_path):
    """Review finding: a live HTTP 500 must fail answer checks and the CLI result."""
    report = run(CLOSING, tmp_path, live=httpx.MockTransport(lambda request: httpx.Response(500, json={})),
                 max_calls=5)
    ask = next(c for c in report['checks'] if c['name'] == 'question gets a model text reply')
    assert not ask['passed'] and report['inference_failures'] and failures([report])


def test_rejected_save_is_not_reported_as_stored(tmp_path):
    """Review finding: only controller-accepted saves count, and the gap check reads the real store."""
    with rejecting('/chat/turn'):
        report = run(DELIVERY_FAILURE, tmp_path)
    assert report['rejected_saves'] == ['yo'] and report['steps'][1]['route']['stored'] == []
    gap = next(c for c in report['checks'] if c['known_gap'])
    assert gap['passed']  # The real store is empty, so this run does not reproduce the gap.
    with pytest.raises(RuntimeError):
        preconditions(report)


def test_production_datetime_reads_follow_the_virtual_clock(tmp_path):
    """Review finding: image/social freshness checks use datetime.now(), not time.time()."""
    def skew(replay):
        event = replay.world.rows[replay.world.labels['yo']]
        now = fri.fri_image_share.dt.datetime.now(dt.timezone.utc)
        assert isinstance(now, fri.fri_image_share.dt.datetime) and isinstance(now, dt.datetime)
        return (now-event.created_at).total_seconds()

    scenario = Scenario('clock', 'Fresh message after virtual waits.',
                        [Wait(600), Say(20, 'fri yo', label='yo'), Inspect('skew', skew)],
                        {'conversation': [reply('yo')]})
    replay = Replay(scenario, tmp_path).run()
    assert -5 <= replay.inspections['skew'] <= 5
    preconditions(replay.report())


def test_rejected_erasure_can_never_reproduce_the_privacy_gap(tmp_path):
    """Re-review finding: a refused "forget me" must fail the run before the gap check counts."""
    with rejecting('/chat/memory/control'):
        report = run(ERASURE, tmp_path)
    assert report['unexpected_rejections'] == [['erase', '/chat/memory/control', 403]]
    persisted = next(c for c in report['checks'] if c['name'].startswith('forget me persisted'))
    assert not persisted['passed'] and failures([report])
    with pytest.raises(RuntimeError):
        preconditions(report)


def test_stored_reflects_what_the_controller_retained(tmp_path):
    """Re-review finding: storage is read back from the store, not inferred from the request."""
    ping = run(OUTSIDE_MENTION, tmp_path/'mention')['steps'][2]['route']
    assert ping['stored'] == ['breadcrumb']  # The controller keeps a neutral mention placeholder.
    scenario = Scenario('memory-off', 'Turns are accepted but not retained after forget me.',
                        [Say(20, 'fri forget me', label='erase'), Say(20, 'fri yo', label='yo')],
                        {'conversation': [reply('yo')]})
    replay = Replay(scenario, tmp_path/'off').run()
    assert replay.step('yo').route()['stored'] == ['not-retained'] and not replay.stored(20)['turns']


def test_live_factory_transport_is_created_and_closed_per_scenario(tmp_path):
    """Re-review finding: a live transport must not outlive its scenario's event loop."""
    made = []

    class Tracked(httpx.MockTransport):
        closed = False

        async def aclose(self):
            self.closed = True

    def factory():
        made.append(Tracked(live_reply('live yo')))
        return made[-1]

    scenario = Scenario('factory', 'One live turn.', [Say(20, 'fri yo', label='yo')])
    for index in range(2):
        report = run(scenario, tmp_path/str(index), live=factory, max_calls=1)
        preconditions(report)
    assert len(made) == 2 and all(transport.closed for transport in made)


def test_live_delivery_gap_does_not_depend_on_scripted_wording(tmp_path):
    """Round-3 finding: any retained turn for the failed send reproduces the gap, whatever it says."""
    report = run(DELIVERY_FAILURE, tmp_path, live=httpx.MockTransport(live_reply('hey there')), max_calls=1)
    preconditions(report)
    gap = next(c for c in report['checks'] if c['known_gap'])
    assert not gap['passed'] and not gap['detail']


def test_known_gap_xfail_cannot_be_satisfied_by_other_assertions(tmp_path):
    """Round-3 finding: an unrelated AssertionError surfaces instead of counting as the gap."""
    from replay.harness import ModelTransport

    async def broken(self, request):
        raise AssertionError('synthetic provider invariant failure')

    name = next(c.name for c in ERASURE.checks if c.known_gap)
    with patch.object(ModelTransport, 'handle_async_request', broken), \
            pytest.raises(AssertionError, match='synthetic provider invariant'):
        test_known_gap_still_reproduces(ERASURE, name, tmp_path)


def test_controller_errors_fail_cli_and_pytest_alike(tmp_path):
    """Round-3 finding: failures() is the one validity rule, including controller 5xx."""
    original = TestClient.request

    def request(self, method, url, *args, **kwargs):
        if url == '/chat/usage/report':
            return httpx.Response(500, text='synthetic', request=httpx.Request(method, 'http://testserver'+url))
        return original(self, method, url, *args, **kwargs)

    with patch.object(TestClient, 'request', request):
        report = run(CLOSING, tmp_path)
    assert report['controller_errors'] and any('controller error' in item for item in failures([report]))


def test_checks_are_judged_on_the_virtual_clock(tmp_path):
    """Round-3 finding: TTL-sensitive store reads happen before real time is restored."""
    scenario = Scenario('ttl', 'A clarification left for 1000 virtual seconds expires.',
                        WEB_CLARIFICATION.steps[:1] + [Wait(1000)],
                        {'conversation': WEB_CLARIFICATION.script['conversation'][:1]},
                        [Check('pending expired', lambda r: r.stored(20)['pending'] is None)])
    report = run(scenario, tmp_path)
    preconditions(report)


def test_subscription_expiry_stays_on_real_time(tmp_path):
    """Round-3 finding: refreshed token expiry must not inherit the virtual clock's offset."""
    import time

    from fri.subscription import PLAN_SCOPE, Subscription
    auth = tmp_path/'session.json'
    auth.write_text(json.dumps({'access_token': 'synthetic-old', 'refresh_token': 'synthetic-refresh',
                                'client_id': 'eval', 'ext_agent_host_id': 'eval', 'scopes': [PLAN_SCOPE],
                                'expires_at': time.time()+60}))
    refresh = httpx.MockTransport(lambda request: httpx.Response(200, json={
        'access_token': 'synthetic-new', 'refresh_token': 'synthetic-rotated', 'token_type': 'bearer',
        'expires_in': 3600, 'scope': PLAN_SCOPE}))
    scenario = Scenario('auth', 'A refresh after virtual waits.', [Wait(600), Say(20, 'fri hi', label='hi')])
    run(scenario, tmp_path/'run', live=httpx.MockTransport(live_reply('hi')),
        subscription=Subscription(auth, refresh), max_calls=1)
    saved = json.loads(auth.read_text())
    assert saved['access_token'] == 'synthetic-new'
    assert abs(saved['expires_at'] - (time.time()+3600)) < 60


@pytest.mark.parametrize('text', ['Earlier ash said denver, next month', '[ash said denver, next month]'])
def test_placeholder_lookalikes_still_reproduce_the_privacy_gap(text, tmp_path):
    """Round-4 finding: only exact neutral placeholders count, not reply prefixes."""
    report = run(ERASURE, tmp_path, live=httpx.MockTransport(live_reply(text)), max_calls=1)
    preconditions(report)
    gap = next(c for c in report['checks'] if c['known_gap'])
    assert not gap['passed'] and report['steps'][2]['route']['stored'] == ['reply-text']


def test_erroring_known_gap_check_fails_the_run(tmp_path):
    """Round-4 finding: a crashing gap check is a failure for the CLI too, not a documented gap."""
    from dataclasses import replace

    def broken(replay):
        raise ValueError('synthetic gap evaluation error')

    checks = [replace(c, test=broken) if c.known_gap else c for c in ERASURE.checks]
    report = run(replace(ERASURE, checks=checks), tmp_path)
    assert any(c['detail'] for c in report['checks']) and failures([report])


def test_budget_skips_and_nonpositive_budgets_never_report_success(tmp_path):
    """Round-5 nit: skipped scenarios fail the CLI result; non-positive budgets are rejected."""
    from replay.__main__ import main
    assert main(['--max-calls', '1', '--report', str(tmp_path/'one.json')]) == 1
    skipped = json.loads((tmp_path/'one.json').read_text())['reports']
    assert any('skipped' in r for r in skipped) and failures(skipped)
    with pytest.raises(SystemExit):
        main(['--max-calls', '0'])


def test_early_return_tools_are_traced(tmp_path):
    """Round-5 nit: a clarification returns without another model call but is still a tool run."""
    ask = run(WEB_CLARIFICATION, tmp_path)['steps'][1]['route']
    assert ask['tools'] == ['clarify'] and ask['tool_outcomes'] == [{'kind': 'clarify', 'outcome': 'returned'}]
    assert ask['receipts'] == []


def test_tool_cancelled_by_the_runner_timeout_is_traced(tmp_path):
    """Final-review nit: CancelledError is a BaseException; the spy must still record the tool."""
    import asyncio
    import types

    import fri.fri_runner
    from fri.fri_actions import Actions

    async def hang(self, action):
        await asyncio.get_running_loop().create_future()

    fast = types.ModuleType('asyncio')
    fast.__dict__.update(vars(asyncio))
    fast.timeout = lambda delay: asyncio.timeout(0.005 if delay == 300 else delay)  # Runner deadline only.
    question = 'what is the current python release'
    scenario = Scenario('timeout', 'A tool still running when the turn deadline passes.',
                        [Say(20, 'fri '+question, label='ask')],
                        {'conversation': [act('web_search', query=question)]})
    with patch.object(Actions, 'execute', hang), patch.object(fri.fri_runner, 'asyncio', fast):
        report = run(scenario, tmp_path)
    preconditions(report)
    ask = report['steps'][1]
    assert ask['route']['tool_outcomes'] == [{'kind': 'web_search', 'outcome': 'CancelledError'}]
    assert ask['route']['outcome'] == 'reply' and ask['deliveries'][0]['text']  # Honest timeout fallback.
