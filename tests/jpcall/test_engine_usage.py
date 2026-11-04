import json

import pytest

from fri.jpcall.pricing import PriceTable
from fri.jpcall.usage import UsageMeter


def realtime_usage():
    return {'input_tokens': 1000, 'output_tokens': 100,
            'input_token_details': {'text_tokens': 400, 'audio_tokens': 600,
                                   'cached_tokens': 200,
                                   'cached_tokens_details': {'text_tokens': 50,
                                                             'audio_tokens': 150}},
            'output_token_details': {'text_tokens': 20, 'audio_tokens': 80}}


def test_realtime_cache_is_disjoint_and_duplicate_response_is_not_double_billed():
    meter = UsageMeter()
    event = {'response': {'id': 'response-one', 'usage': realtime_usage()}}
    meter.realtime(event, 'gpt-realtime-2.1')
    meter.realtime(event, 'gpt-realtime-2.1')
    line, = meter.lines()
    assert line.units == {'text_in_tokens': 350, 'cached_text_in_tokens': 50,
                          'audio_in_tokens': 450, 'cached_audio_in_tokens': 150,
                          'text_out_tokens': 20, 'audio_out_tokens': 80}
    assert line.usd == pytest.approx((350 * 4 + 200 * .4 + 450 * 32 + 20 * 24 + 80 * 64) / 1e6)
    assert not line.exact


def test_transcription_responses_duration_and_price_overrides():
    prices = PriceTable(per_million={'named-text': {'text_in_tokens': 2,
                         'cached_text_in_tokens': .2, 'text_out_tokens': 8}})
    meter = UsageMeter(prices)
    meter.text('translation', 'named-text', {'id': 'text-one', 'usage': {
        'input_tokens': 100, 'input_tokens_details': {'cached_tokens': 25},
        'output_tokens': 30, 'output_tokens_details': {'reasoning_tokens': 20}}})
    meter.transcription({'input_tokens': 60, 'output_tokens': 10,
                         'input_token_details': {'audio_tokens': 60}}, 'gpt-4o-transcribe', 'item1')
    meter.transcription({'type': 'duration', 'seconds': 30}, 'gpt-transcribe', 'item2')
    lines = {line.model: line for line in meter.lines()}
    assert lines['named-text'].usd == pytest.approx((75 * 2 + 25 * .2 + 30 * 8) / 1e6)
    assert lines['named-text'].units['text_out_tokens'] == 30
    # An override replaces the table, so other models remain explicitly unknown.
    assert lines['gpt-4o-transcribe'].usd is None
    assert lines['gpt-transcribe'].usd == pytest.approx(.0045 / 2)


def test_twilio_estimate_pending_and_exact_replacement():
    meter = UsageMeter()
    meter.twilio(61, pending=False)
    assert meter.lines()[0].units == {'minutes': 2}
    assert meter.lines()[0].usd == pytest.approx(2 * .0746)
    meter.twilio(61)
    assert meter.lines()[0].usd is None and not meter.usage_final
    meter.twilio(61, posted_usd=.13, exact=True)
    assert meter.lines()[0].usd == .13 and meter.lines()[0].exact and meter.usage_final
    meter.twilio(500)  # A late pending callback cannot erase the exact result.
    assert meter.lines()[0].usd == .13
    assert meter.lines()[1].usd == pytest.approx(2 * .0044)


def test_unknown_model_missing_or_unsplit_usage_never_claims_zero_cost():
    for usage in (None, {}, {'input_tokens': 42, 'output_tokens': 2},
                  realtime_usage() | {'input_token_details': {
                      'text_tokens': 400, 'audio_tokens': 600, 'cached_tokens': 200}}):
        meter = UsageMeter()
        meter.realtime({'response': {'usage': usage}}, 'unknown-model')
        assert meter.lines()[0].usd is None and not meter.usage_final


def test_ledger_contains_only_aggregate_counts_and_costs(tmp_path):
    meter = UsageMeter()
    meter.text('summary', 'gpt-5-mini', {'id': 'private-request-id', 'output': 'secret-dialogue',
                                      'usage': {'input_tokens': 4, 'output_tokens': 5}})
    meter.twilio(42)
    meter.write_ledger(tmp_path)
    text = (tmp_path / 'jpcall-usage.jsonl').read_text()
    assert 'secret-dialogue' not in text and 'private-request-id' not in text
    assert json.loads(text)['usage'][0]['units']['text_in_tokens'] == 4
    assert (tmp_path / 'jpcall-usage.jsonl').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('bad', [-1, float('inf'), float('nan'), True, '10'])
def test_invalid_usage_is_rejected(bad):
    with pytest.raises(ValueError):
        UsageMeter().text('summary', 'gpt-5-mini', {'usage': {'input_tokens': bad}})


def test_missing_realtime_output_breakdown_is_not_billed_as_text():
    meter = UsageMeter()
    meter.realtime({'response': {'usage': {'input_tokens': 0, 'output_tokens': 100}}},
                   'gpt-realtime-2.1')
    assert meter.lines()[0].usd is None
    assert meter.lines()[0].units == {'unknown_out_tokens': 100}


def test_lead_default_models_have_rates():
    meter = UsageMeter()
    meter.text('translation', 'gpt-6.1-terra', {'usage': {'input_tokens': 100,
               'input_tokens_details': {'cached_tokens': 20}, 'output_tokens': 10}})
    meter.transcription({'type': 'duration', 'seconds': 30}, 'gpt-realtime-whisper')
    assert meter.lines()[1].usd == pytest.approx((80 + 20 * .05 + 10 * 5) / 1e6)
    assert meter.lines()[0].usd == pytest.approx(.017 / 2)


@pytest.mark.parametrize('number,rate', [('+817012345678', .185), ('+818012345678', .185),
    ('+819012345678', .185), ('+81312345678', .0746)])
def test_phone_estimate_uses_destination(number, rate):
    meter = UsageMeter(destination=number)
    meter.twilio(61, pending=False)
    assert meter.lines()[0].usd == pytest.approx(2 * rate)
    assert not meter.lines()[0].exact
    meter.twilio(61, posted_usd=.2, exact=True)
    assert meter.lines()[0].usd == .2 and meter.lines()[0].exact
