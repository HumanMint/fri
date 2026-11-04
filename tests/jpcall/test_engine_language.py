import asyncio
import json
import socket

import pytest
from test_engine_config import environment
from test_engine_transport import Audio, Events, RealtimeHTTP, RealtimePeer

from fri.jpcall.config import CALL_LANGUAGES, JpcallConfig
from fri.jpcall.contract import CallBrief, CallRejected
from fri.jpcall.numbers import normalize_number
from fri.jpcall.realtime import RealtimeClient
from fri.jpcall.usage import UsageMeter


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Network connections are forbidden in engine tests')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)


def test_language_speed_and_voice_defaults():
    env = environment()
    env.pop('JPCALL_REALTIME_VOICE')
    config = JpcallConfig.from_env(env)
    assert config.enabled and config.call_language == 'ja' and config.realtime_speed == 1.15
    assert config.realtime_voice == 'coral'
    assert JpcallConfig.from_env(env | {'JPCALL_REALTIME_VOICE': 'cedar'}).realtime_voice == 'cedar'


@pytest.mark.parametrize('speed', ['0.25', '1', '1.15', '1.5'])
def test_valid_speed_range(speed):
    config = JpcallConfig.from_env(environment() | {'JPCALL_REALTIME_SPEED': speed})
    assert config.enabled and config.realtime_speed == float(speed)


@pytest.mark.parametrize('speed', ['0.2499', '1.5001', '0', '-1', 'nan', 'inf', '-inf', 'bad', ''])
def test_invalid_speed_disables_without_crashing(speed):
    config = JpcallConfig.from_env(environment() | {'JPCALL_REALTIME_SPEED': speed})
    assert not config.enabled
    assert config.disabled_reasons == (
        'JPCALL_REALTIME_SPEED must be a finite number from 0.25 to 1.5',)


@pytest.mark.parametrize('code', ['en', 'kr', 'KO', '', 'ja,ko'])
def test_invalid_language_disables_without_crashing(code):
    config = JpcallConfig.from_env(environment() | {'JPCALL_CALL_LANGUAGE': code})
    assert not config.enabled
    assert config.disabled_reasons == ('JPCALL_CALL_LANGUAGE must be ja or ko',)


@pytest.mark.parametrize('code,script,politeness,tone', [
    ('ja', 'フリー', '敬語', '明るく親しみやすい'),
    ('ko', '프리', '해요체', '밝고 친근'),
])
def test_session_selects_call_language_speed_and_phone_persona(code, script, politeness, tone):
    async def run():
        config = JpcallConfig.from_env(environment() | {
            'JPCALL_CALL_LANGUAGE': code, 'JPCALL_REALTIME_SPEED': '1.25',
            'JPCALL_REALTIME_VOICE': 'coral',
        })
        language = config.language
        brief = CallBrief('request1', 'Ask about lunch.', language.opener, (), ())
        peer = RealtimePeer()
        client = RealtimeClient(config, brief, Audio(), Events(), UsageMeter(),
                                session=RealtimeHTTP(peer))
        session = client.session_update()['session']
        assert session['audio']['input']['transcription']['language'] == code
        assert session['audio']['output']['speed'] == 1.25
        assert session['audio']['output']['voice'] == 'coral'
        assert script in session['instructions'] and politeness in session['instructions']
        assert tone in session['instructions'] and 'Fri' not in session['instructions']
        assert language.opener not in session['instructions']
        await client.connect()
        try:
            await client.say(language.opener, out_of_band=True)
            sent = peer.sent[-1]['response']['instructions']
            assert 'Fri' not in sent and script in sent
            assert json.loads(sent.rsplit(language.say_prefix, 1)[1]) == language.opener
        finally:
            await client.stop()
    asyncio.run(run())


def test_exact_openers_and_hold_phrases():
    ja, ko = CALL_LANGUAGES['ja'], CALL_LANGUAGES['ko']
    assert ja.opener == (
        'お忙しいところすみません。お客様の通訳をお手伝いしているAIのフリーです。'
        'お客様からお伺いしたいことがあるのですが、よろしいでしょうか。')
    assert ko.opener == (
        '안녕하세요! 손님 대신 통역 도와드리는 AI 프리입니다. '
        '손님분들이 질문이 있는데 괜찮으실까요?')
    assert ko.hold == '아, 잠시만요. 일행분들께 바로 여쭤볼게요.'
    assert ko.apology == '죄송해요, 조금만 더 기다려 주세요.'
    assert ko.farewell == '감사해요. 좋은 하루 보내세요.'
    assert ja.hold == '少々お待ちください、確認します。'
    assert ja.apology == 'お待たせしてすみません、もう少々お待ちください。'
    assert 'AI通訳' in ja.instructions and '人間を装わない' in ja.instructions
    assert 'AI 통역' in ko.instructions and '사람인 척하지 마세요' in ko.instructions
    for language in (ja, ko):
        for text in (language.instructions, language.opener, language.say_prefix,
                     language.hold, language.apology, language.farewell):
            assert 'Fri' not in text


def test_korean_call_language_does_not_change_number_permissions():
    config = JpcallConfig.from_env(environment() | {'JPCALL_CALL_LANGUAGE': 'ko'})
    assert normalize_number('03-1234-5678', config.limits.test_numbers) == '+81312345678'
    with pytest.raises(CallRejected):
        normalize_number('+821012345678', config.limits.test_numbers)


@pytest.mark.parametrize('code,phrases', [
    ('ja', ('一、二文', '一つずつ', '相づちは時々だけ', '棒読み')),
    ('ko', ('한두 문장', '질문은 하나씩', '해요체', '맞장구는 가끔만', '읽는 말투')),
])
def test_phone_rhythm_and_stop_after_opener(code, phrases):
    language = CALL_LANGUAGES[code]
    assert all(phrase in language.instructions for phrase in phrases)
    assert ('返事を待って' if code == 'ja' else '답을 기다리') in language.say_prefix


@pytest.mark.parametrize('code', ['ja', 'ko'])
def test_agreement_decline_and_recording_instructions(code):
    instructions = CALL_LANGUAGES[code].instructions
    assert 'end_call' in instructions and 'declined to talk with an AI interpreter' in instructions
    if code == 'ja':
        assert '了承後' in instructions and '断られたら' in instructions
        assert '録音はせず' in instructions and '文字を共有' in instructions
        assert 'グループも通話を聞きます' in instructions
    else:
        assert '동의하면' in instructions and '거절하면' in instructions
        assert '녹음 없이' in instructions and '문자로 공유' in instructions
        assert '일행도 통화를 들어요' in instructions
