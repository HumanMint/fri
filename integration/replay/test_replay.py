"""Offline replay checks: real handler and controller, scripted provider, synthetic Discord."""

import datetime as dt
import json
from unittest.mock import patch

import httpx
import pytest
from starlette.testclient import TestClient

import fri.fri_image_share
from replay.harness import (
    Inspect,
    Replay,
    Say,
    Scenario,
    UnscriptedCall,
    Wait,
    failures,
    reply,
    run,
)
from replay.scenarios import CLOSING, DELIVERY_FAILURE, ERASURE, SCENARIOS


def preconditions(report):
    """Raise a non-assertion error so a broken run can never pass as a reproduced known gap."""
    problems = failures([report])
    for key in ('unused_script', 'controller_errors'):
        if report[key]:
            problems.append(f'{key}: {report[key]}')
    if problems:
        raise RuntimeError(problems)


@pytest.mark.parametrize('scenario', SCENARIOS, ids=lambda s: s.name)
def test_scenario_routes_and_checks(scenario, tmp_path):
    report = run(scenario, tmp_path)
    preconditions(report)
    json.dumps(report)


GAPS = [pytest.param(scenario, check.name, id=f'{scenario.name}:{check.name}',
                     marks=pytest.mark.xfail(strict=True, raises=AssertionError, reason=check.known_gap))
        for scenario in SCENARIOS for check in scenario.checks if check.known_gap]


@pytest.mark.parametrize('scenario,name', GAPS)
def test_known_gap_still_reproduces(scenario, name, tmp_path):
    """Strict xfail on AssertionError only: fixing the gap fails the suite until the marker is removed."""
    report = run(scenario, tmp_path)
    preconditions(report)
    result = next(c for c in report['checks'] if c['name'] == name)
    if result['detail']:
        raise RuntimeError(result['detail'])  # An erroring check is not a reproduction.
    assert result['passed'], result


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
    original = TestClient.request

    def rejecting(self, method, url, *args, **kwargs):
        if url == '/chat/turn':
            request = httpx.Request(method, 'http://testserver'+url)
            return httpx.Response(403, json={'detail': 'synthetic rejection'}, request=request)
        return original(self, method, url, *args, **kwargs)

    with patch.object(TestClient, 'request', rejecting):
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
