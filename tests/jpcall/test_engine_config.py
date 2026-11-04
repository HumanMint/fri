import importlib

import pytest

from fri.jpcall.config import JpcallConfig
from fri.jpcall.contract import CallRejected
from fri.jpcall.numbers import normalize_number


def environment():
    return {
        'JPCALL_OPENAI_API_KEY': 'synthetic-openai-secret',
        'TWILIO_ACCOUNT_SID': 'AC' + '1' * 32,
        'TWILIO_API_KEY_SID': 'SK' + '2' * 32,
        'TWILIO_API_KEY_SECRET': 'synthetic-rest-secret',
        'TWILIO_AUTH_TOKEN': 'synthetic-webhook-secret',
        'TWILIO_FROM_NUMBER': '+81312345678',
        'JPCALL_PUBLIC_BASE_URL': 'https://calls.example.test',
        'JPCALL_REALTIME_VOICE': 'marin',
        'JPCALL_TRANSCRIBE_MODEL': 'gpt-4o-transcribe',
        'JPCALL_TEXT_MODEL': 'gpt-5-mini',
    }


def test_config_is_explicit_and_defaults_are_deny_all(monkeypatch):
    monkeypatch.setenv('JPCALL_LISTEN_PORT', 'invalid')
    import fri.jpcall.config
    importlib.reload(fri.jpcall.config)
    disabled = JpcallConfig.from_env({})
    assert not disabled.enabled
    assert 'Missing JPCALL_OPENAI_API_KEY' in disabled.disabled_reasons
    config = JpcallConfig.from_env(environment())
    assert config.enabled
    assert config.listen_host == '127.0.0.1' and config.listen_port == 8790
    assert config.realtime_model == 'gpt-realtime-2.1' and config.text_effort == 'low'
    defaults_env = environment()
    del defaults_env['JPCALL_TRANSCRIBE_MODEL'], defaults_env['JPCALL_TEXT_MODEL']
    defaults = JpcallConfig.from_env(defaults_env)
    assert defaults.transcribe_model == 'gpt-realtime-whisper'
    assert defaults.text_model == 'gpt-6.1-terra' and defaults.enabled
    assert not config.limits.allowed_user_ids and not config.limits.test_numbers
    for name, value in environment().items():
        if 'SECRET' in name or 'TOKEN' in name or 'KEY' in name:
            assert value not in repr(config)


@pytest.mark.parametrize('name,value', [
    ('JPCALL_LISTEN_PORT', '0'), ('JPCALL_LISTEN_PORT', '65536'),
    ('JPCALL_MAX_HOLD_S', '-2'), ('JPCALL_ALLOWED_USER_IDS', '123,no'),
    ('JPCALL_TEST_NUMBERS', '119'), ('JPCALL_TEST_NUMBERS', '+819901234567'),
    ('JPCALL_PUBLIC_BASE_URL', 'http://calls.example.test'),
    ('JPCALL_PUBLIC_BASE_URL', 'https://secret:password@calls.example.test'),
    ('JPCALL_PUBLIC_BASE_URL', 'https://calls.example.test/path'),
    ('JPCALL_PUBLIC_BASE_URL', 'https://calls.example.test?secret=value'),
    ('JPCALL_PUBLIC_BASE_URL', 'https://calls.example.test:bad'),
    ('TWILIO_FROM_NUMBER', '03-1234-5678'),
])
def test_invalid_config_disables_without_echoing_values(name, value):
    env = environment() | {name: value}
    config = JpcallConfig.from_env(env)
    assert not config.enabled
    assert all(value not in reason for reason in config.disabled_reasons)


def test_limits_and_test_numbers_are_configured():
    config = JpcallConfig.from_env(environment() | {
        'JPCALL_ALLOWED_USER_IDS': '12, 34 56', 'JPCALL_TEST_NUMBERS': '+12025550123',
        'JPCALL_MAX_CALL_S': '300', 'JPCALL_MAX_HOLD_S': '30',
        'JPCALL_HOLD_APOLOGY_EVERY_S': '10', 'JPCALL_MAX_CALLS_PER_USER_PER_DAY': '2',
    })
    assert config.enabled
    assert config.limits.allowed_user_ids == {12, 34, 56}
    assert config.limits.test_numbers == {'+12025550123'}
    assert config.limits.max_call_s == 300 and config.limits.max_hold_s == 30
    assert config.limits.hold_apology_every_s == 10
    assert config.limits.max_calls_per_user_per_day == 2


@pytest.mark.parametrize('number,expected', [
    ('03-1234-5678', '+81312345678'), ('090 1234 5678', '+819012345678'),
    ('+81 3 1234 5678', '+81312345678'), ('０３－１２３４－５６７８', '+81312345678'),
    ('（０９０）１２３４　５６７８', '+819012345678'), ('0120-123-456', '+81120123456'),
])
def test_japanese_normalization(number, expected):
    assert normalize_number(number) == expected


@pytest.mark.parametrize('number', [
    '110', '118', '119', '１１９', '112', '171', '104', '117', '999', '1234',
    '+81119', '0990-123-456', '+81990123456', '+12025550123', '+810312345678',
    '03123', '03-1234-5678 ext 12', '03/1234/5678', '00312345678', '++81312345678',
])
def test_unsafe_numbers_are_member_safe_rejections(number):
    with pytest.raises(CallRejected) as error:
        normalize_number(number)
    assert number not in str(error.value)


def test_non_japanese_test_exception_does_not_bypass_service_restrictions():
    assert normalize_number('+1 (202) 555-0123', {'+12025550123'}) == '+12025550123'
    for number in ('119', '+81119', '+81990123456'):
        with pytest.raises(CallRejected):
            normalize_number(number, {number})
