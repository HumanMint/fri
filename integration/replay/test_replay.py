"""Offline replay checks: real handler and controller, scripted provider, synthetic Discord."""

import json

import httpx
import pytest

from replay.harness import Say, Scenario, reply, run
from replay.scenarios import ERASURE, SCENARIOS


@pytest.mark.parametrize('scenario', SCENARIOS, ids=lambda s: s.name)
def test_scenario_routes_and_checks(scenario, tmp_path):
    report = run(scenario, tmp_path)
    assert not report['unused_script'], report['unused_script']
    assert not report['controller_errors'], report['controller_errors']
    failed = [c for c in report['checks'] if not c['passed'] and not c['known_gap']]
    assert not failed, failed
    json.dumps(report)


GAPS = [pytest.param(scenario, check.name, id=f'{scenario.name}:{check.name}',
                     marks=pytest.mark.xfail(strict=True, reason=check.known_gap))
        for scenario in SCENARIOS for check in scenario.checks if check.known_gap]


@pytest.mark.parametrize('scenario,name', GAPS)
def test_known_gap_still_reproduces(scenario, name, tmp_path):
    """Strict xfail: fixing the gap makes this pass, which fails the suite until the marker is removed."""
    result = next(c for c in run(scenario, tmp_path)['checks'] if c['name'] == name)
    assert result['passed'], result


def test_manifests_and_routes_never_copy_member_text(tmp_path):
    report = run(ERASURE, tmp_path)
    recorded = json.dumps([{key: step[key] for key in ('model', 'route', 'controller_paths')}
                           for step in report['steps']])
    assert 'denver' not in recorded and 'moving' not in recorded
    assert report['prompt_versions'] and report['tokens']['input_tokens'] > 0


def test_unscripted_model_call_fails_loudly(tmp_path):
    scenario = Scenario('unscripted', 'A model call nobody scripted.', [Say(20, 'fri yo', label='yo')])
    with pytest.raises(AssertionError, match='unscripted conversation model call'):
        run(scenario, tmp_path)


def test_live_mode_uses_the_given_transport_and_enforces_budget(tmp_path):
    calls = []

    def provider(request):
        calls.append(json.loads(request.content))
        event = {'type': 'response.completed', 'response': {'usage': {'input_tokens': 7, 'output_tokens': 3},
                 'output': [{'type': 'message', 'content': [
                     {'type': 'output_text', 'text': json.dumps(reply('live yo'))}]}]}}
        return httpx.Response(200, text='data: '+json.dumps(event)+'\n\n')

    scenario = Scenario('budget', 'Two turns, budget for one.',
                        [Say(20, 'fri yo', label='one'), Say(20, 'fri you there', label='two')],
                        {'conversation': [reply('script must be ignored in live mode')]})
    report = run(scenario, tmp_path, live=httpx.MockTransport(provider), max_calls=1)
    assert len(calls) == 1 and report['model_calls'] == 1 and report['mode'] == 'live'
    one, two = report['steps'][1], report['steps'][2]
    assert one['deliveries'][0]['text'] == 'live yo'
    assert two['route']['model_calls'] == 0 and two['route']['kind'] == 'notice'
