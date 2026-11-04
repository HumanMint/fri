"""Conversation-quality regressions using synthetic in-process peers only."""

import asyncio
import base64
import json
import os
import socket
from dataclasses import replace

import pytest
from test_engine_config import environment
from test_engine_flow import answer_phone, dial, make_engine, settled
from test_engine_transport import EVENTS, ULAW, Media, attach_and_open, bridge

from fri.jpcall.config import CALL_LANGUAGES, JpcallConfig
from fri.jpcall.contract import Limits, QuestionAsked, Utterance
from fri.jpcall.hold import HoldController
from fri.jpcall.timeline import DebugTimeline


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Network connections are forbidden in engine tests')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)


@pytest.mark.parametrize('code,maximum,already,exception', [
    ('ja', 340, '自己紹介は済みました', '誰かと聞かれない限り'),
    ('ko', 375, '소개는 끝났어요', '누구냐고 묻지 않으면'),
])
def test_short_prompt_prevents_reintroduction(code, maximum, already, exception):
    text = CALL_LANGUAGES[code].instructions
    assert len(text) <= maximum
    assert already in text and exception in text
    assert 'ask_group' in text and 'end_call' in text
    assert 'declined to talk with an AI interpreter' in text
    assert 'Fri' not in text


@pytest.mark.parametrize('code', ['ja', 'ko'])
def test_original_goal_first_and_oob_context_contains_only_short_prompt_and_line(
        tmp_path, monkeypatch, code):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(
            tmp_path, monkeypatch, call_language=code)
        try:
            call, brief, audio, events = await dial(engine, provider)
            language = engine.config.language
            text = peer.sent[0]['session']['instructions']
            data, rules = text[len(language.plan_label):].split('\n', 1)
            plan = json.loads(data)
            # Original injection-like goal stays JSON data.
            assert plan['goal'] == call.request.goal
            assert plan['summary_en'] == brief.summary_en
            assert plan['share'] == list(brief.share) and plan['never'] == list(brief.never)
            assert rules == language.instructions
            await answer_phone(call)
            for line in (language.hold, language.apology, language.farewell):
                generated = await call._rt.say(line, out_of_band=True)
                assert await generated == 'completed'
                response = peer.responses[-1]
                assert response['conversation'] == 'none' and response['input'] == []
                assert response['tools'] == [] and response['tool_choice'] == 'none'
                assert response['instructions'] == (language.instructions + '\n'
                    + language.say_prefix + json.dumps(line, ensure_ascii=False))
                assert call.request.goal not in response['instructions']
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('effort', ['minimal', 'low', 'medium', 'high', 'xhigh', ''])
def test_realtime_reasoning_session_field(effort):
    client, *_ = bridge()
    client.config = JpcallConfig.from_env(environment() | {'JPCALL_REALTIME_REASONING': effort})
    assert client.config.enabled
    session = client.session_update()['session']
    if effort:
        assert session['reasoning'] == {'effort': effort}
    else:
        assert 'reasoning' not in session
        assert client.config.realtime_reasoning is None
    assert client.config.realtime_model == 'gpt-realtime-2.1'


@pytest.mark.parametrize('key,value', [
    ('JPCALL_REALTIME_REASONING', 'max'), ('JPCALL_REALTIME_REASONING', 'none'),
    ('JPCALL_VAD_THRESHOLD', '-0.1'), ('JPCALL_VAD_THRESHOLD', '1.1'),
    ('JPCALL_VAD_THRESHOLD', 'nan'), ('JPCALL_VAD_THRESHOLD', 'inf'),
    ('JPCALL_VAD_THRESHOLD', 'bad'), ('JPCALL_VAD_SILENCE_DURATION_MS', '0'),
    ('JPCALL_VAD_SILENCE_DURATION_MS', '1.5'), ('JPCALL_VAD_NOISE_REDUCTION', 'bad'),
])
def test_invalid_vad_or_reasoning_is_disabled_safely(key, value):
    config = JpcallConfig.from_env(environment() | {key: value})
    assert not config.enabled and any(key in reason for reason in config.disabled_reasons)


def test_vad_defaults_and_overrides_are_ga_fields():
    client, *_ = bridge()
    audio = client.session_update()['session']['audio']['input']
    assert audio['noise_reduction'] == {'type': 'near_field'}
    assert audio['turn_detection'] == {'type': 'server_vad', 'threshold': .7,
        'silence_duration_ms': 600, 'create_response': False, 'interrupt_response': False}
    for threshold, reduction in [('0', 'off'), ('1', 'far_field')]:
        client.config = JpcallConfig.from_env(environment() | {
            'JPCALL_VAD_THRESHOLD': threshold, 'JPCALL_VAD_NOISE_REDUCTION': reduction,
            'JPCALL_VAD_SILENCE_DURATION_MS': '800'})
        assert client.config.enabled
        audio = client.session_update()['session']['audio']['input']
        assert audio['turn_detection']['threshold'] == float(threshold)
        assert audio['turn_detection']['silence_duration_ms'] == 800
        assert audio['noise_reduction'] == (None if reduction == 'off' else {'type': reduction})


async def hold_fixture(*, now, spoken, timeline=None):
    async def emit(event):
        pass

    async def say(text, **kwargs):
        spoken.append(text)

    async def result(*args):
        pass

    async def state(held):
        pass

    async def sleep(seconds):
        await asyncio.Event().wait()

    return HoldController('synthetic', Limits(max_hold_s=180), emit, say, result, state,
        clock=lambda: now[0], sleep=sleep, timeline=timeline)


def test_hold_has_one_proactive_checkin_after_45_seconds_of_silence():
    async def run():
        now, spoken = [0], []
        hold = await hold_fixture(now=now, spoken=spoken)
        try:
            assert hold.limits.hold_apology_every_s == 45
            assert JpcallConfig.from_env(environment()).limits.hold_apology_every_s == 45
            assert await hold.ask('tool', 'Which time?', ())
            await asyncio.sleep(0)
            now[0] = 44
            await hold.tick()
            assert spoken == [hold.language.hold]
            now[0] = 45
            await hold.tick()
            await asyncio.sleep(0)
            for now[0] in (90, 135, 179):
                await hold.tick()
                await asyncio.sleep(0)
            assert spoken == [hold.language.hold, hold.language.apology]
        finally:
            await hold.close()
    asyncio.run(run())


def test_callee_speech_consumes_checkin_and_resets_interval():
    async def run():
        now, spoken = [0], []
        hold = await hold_fixture(now=now, spoken=spoken)
        try:
            await hold.ask('tool', 'Which time?', ())
            await asyncio.sleep(0)
            now[0] = 44
            hold.callee_speech(True, 'callee')
            now[0] = 46
            hold.callee_speech(False, 'callee')
            assert hold.question.next_apology == 91
            for now[0] in (91, 135, 179):
                await hold.tick()
                await asyncio.sleep(0)
            assert spoken == [hold.language.hold]
        finally:
            await hold.close()
    asyncio.run(run())


def test_callee_can_get_natural_answer_while_group_question_stays_open(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            call, brief, audio, events = await dial(engine, provider)
            await answer_phone(call)
            peer.tool('ask_group', {'question_en': 'Which time?', 'options': []})
            await settled(lambda: len(peer.responses) == 2 and call._rt._idle.is_set())
            question = call._hold.question
            peer.feed({'type': EVENTS['speech_started'], 'item_id': 'hold-callee',
                       'audio_start_ms': 1000})
            peer.feed({'type': EVENTS['speech_stopped'], 'item_id': 'hold-callee'})
            peer.feed({'type': EVENTS['input_transcript'], 'item_id': 'hold-callee',
                       'transcript': '何を確認していますか？'})
            await settled(lambda: len(peer.responses) == 3 and call._rt._idle.is_set())
            assert 'instructions' not in peer.responses[-1]  # Natural response uses full context.
            assert call._hold.question is question
            assert any(isinstance(e, Utterance) and e.item_id == 'hold-callee'
                       for e in events.items)
            clock.advance(45)
            for _ in range(20):
                await asyncio.sleep(0)
            assert len(peer.responses) == 3
            assert await call.answer(question.question_id, 'noon', 34)
            await settled(lambda: len(peer.responses) == 4)
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('spoken,echo', [
    ('네, 프리입니다. 예약 문의드려요.', '네 프리입니다 예약 문의드려요'),
    ('お客様は１２時の予約をご希望です。', 'お客様は12時の予約をご希望です'),
    ('예약은 내일 저녁 여섯 시에 가능할까요?', '예약은 내일 저녁 일곱 시에 가능할까요'),
])
def test_echo_is_deleted_without_reply_utterance_or_barge_in(tmp_path, caplog, spoken, echo):
    async def run():
        path = tmp_path / 'debug.jsonl'
        client, audio, events, meter, peer = bridge()
        client.timeline = DebugTimeline(path)
        media = Media()
        await attach_and_open(client, media)
        try:
            await client.handle_event({'type': EVENTS['output_audio'], 'item_id': 'fri1',
                'content_index': 0, 'delta': base64.b64encode(ULAW).decode()})
            await client.handle_event({'type': EVENTS['output_transcript'], 'item_id': 'fri1',
                'content_index': 0, 'transcript': spoken})
            before = len([e for e in peer.sent if e['type'] == EVENTS['response_create']])
            await client.handle_event({'type': EVENTS['speech_started'], 'item_id': 'echo',
                                       'audio_start_ms': 100})
            await client.handle_event({'type': EVENTS['speech_stopped'], 'item_id': 'echo'})
            event = {'type': EVENTS['input_transcript'], 'item_id': 'echo', 'transcript': echo}
            await client.handle_event(event)
            await client.handle_event(event)
            for _ in range(5):
                await asyncio.sleep(0)
            assert [e for e in peer.sent if e['type'] == EVENTS['item_delete']] == [
                {'type': EVENTS['item_delete'], 'item_id': 'echo'}]
            assert len([e for e in peer.sent if e['type'] == EVENTS['response_create']]) == before
            assert not audio.cleared and not any(e.speaker == 'callee' for e in events.items)
            assert not any(e['type'] == EVENTS['truncate'] for e in peer.sent)
            timeline = [json.loads(line) for line in path.read_text().splitlines()]
            drops = [e for e in timeline if e['event'] == 'echo_drop']
            assert len(drops) == 1 and drops[0]['text'] == echo
            assert echo not in caplog.text and spoken not in caplog.text
        finally:
            await client.stop()
    asyncio.run(run())


def test_partial_transcript_echo_and_short_answers_and_expiry():
    async def run():
        now = [100]
        client, audio, events, meter, peer = bridge(clock=lambda: now[0])
        await attach_and_open(client, Media())
        try:
            text = '予約人数は四名様でよろしいですか'
            await client.handle_event({'type': EVENTS['output_transcript_delta'], 'item_id': 'fri1',
                                       'content_index': 0, 'delta': text})
            await client.handle_event({'type': EVENTS['input_transcript'], 'item_id': 'echo',
                                       'transcript': text})
            assert not any(e.speaker == 'callee' for e in events.items)
            now[0] += 11
            await client.handle_event({'type': EVENTS['input_transcript'], 'item_id': 'real1',
                                       'transcript': text})
            await client.handle_event({'type': EVENTS['input_transcript'], 'item_id': 'real2',
                                       'transcript': 'はい'})
            assert [(e.item_id, e.text_ja) for e in events.items] == [
                ('real1', text), ('real2', 'はい')]
            assert not any(e.get('item_id') in ('real1', 'real2')
                           for e in peer.sent if e['type'] == EVENTS['item_delete'])
        finally:
            await client.stop()
    asyncio.run(run())


def test_timeline_is_opt_in_private_appending_and_contains_no_credentials(tmp_path, monkeypatch):
    config = JpcallConfig.from_env(environment())
    assert config.debug_timeline_path is None
    disabled = DebugTimeline()
    with monkeypatch.context() as patch:
        patch.setattr(os, 'open', lambda *a, **kw: pytest.fail('Disabled timeline accessed disk'))
        disabled.record('transcript', text='synthetic')
    path = tmp_path / 'timeline.jsonl'
    path.write_text('')
    path.chmod(0o644)
    for _ in range(2):
        timeline = DebugTimeline(path)
        timeline.record('transcript', speaker='callee', text='synthetic')
        timeline.close()
        timeline.record('transcript', text='must not appear')
    assert path.stat().st_mode & 0o777 == 0o600
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(lines) == 2 and len({line['call'] for line in lines}) == 2
    assert all(line['at'] and line['elapsed_s'] >= 0 for line in lines)
    assert config.openai_api_key not in path.read_text()
    env_config = JpcallConfig.from_env(environment() | {'JPCALL_DEBUG_TIMELINE_PATH': str(path)})
    assert env_config.debug_timeline_path == path and str(path) not in repr(env_config)


def test_timeline_write_failure_and_symlink_do_not_affect_call(tmp_path, caplog):
    target = tmp_path / 'target'
    target.write_text('unchanged')
    link = tmp_path / 'link'
    link.symlink_to(target)
    DebugTimeline(link).record('transcript', text='private synthetic dialogue')
    assert target.read_text() == 'unchanged'
    assert 'timeline unavailable' in caplog.text
    assert str(link) not in caplog.text and 'private synthetic' not in caplog.text


def test_full_flow_uses_one_timeline_for_transport_hold_and_tools(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        path = tmp_path / 'private-timeline.jsonl'
        engine.config = replace(engine.config, debug_timeline_path=path)
        try:
            call, brief, audio, events = await dial(engine, provider)
            await answer_phone(call)
            peer.tool('ask_group', {'question_en': 'Which time?', 'options': []})
            await settled(lambda: any(isinstance(e, QuestionAsked) for e in events.items)
                          and len(peer.responses) == 2)
            clock.advance(45)
            await settled(lambda: len(peer.responses) == 3 and call._rt._idle.is_set())
            assert await call.answer(call._hold.question.question_id, 'noon', 34)
            await settled(lambda: len(peer.responses) == 4 and call._rt._idle.is_set())
            await call.hang_up('test')
            data = [json.loads(line) for line in path.read_text().splitlines()]
            assert len({e['call'] for e in data}) == 1
            kinds = {e['event'] for e in data}
            assert {'speech_started', 'speech_stopped', 'response_created', 'response_done',
                    'transcript', 'tool_call', 'tool_result', 'hold_start', 'hold_check_in',
                    'hold_answer'} <= kinds
            assert any(e['event'] == 'response_done' and e['status'] == 'completed' for e in data)
            assert path.stat().st_mode & 0o777 == 0o600
            assert not any('audio' in e or 'delta' in e for e in data)
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_non_echo_interrupt_logs_played_truncation_cancel_and_late_transcript(tmp_path):
    async def run():
        client, audio, events, meter, peer = bridge()
        path = tmp_path / 'timeline.jsonl'
        client.timeline = DebugTimeline(path)
        media = Media()
        await attach_and_open(client, media)
        try:
            await client.handle_event({'type': EVENTS['response_created'], 'response': {
                'id': 'active', 'status': 'in_progress'}})
            delta = {'type': EVENTS['output_audio'], 'response_id': 'active', 'item_id': 'fri1',
                     'content_index': 0, 'delta': base64.b64encode(ULAW).decode()}
            await client.handle_event(delta)
            await client.mark('1')
            await client.handle_event(delta)
            await client.handle_event({'type': EVENTS['speech_started'], 'item_id': 'callee1',
                                       'audio_start_ms': 100})
            assert not audio.cleared
            await client.handle_event({'type': EVENTS['input_transcript'], 'item_id': 'callee1',
                                       'transcript': '予約できますか。'})
            # The transcript can finish after cancellation. Preserve what was interrupted.
            await client.handle_event({'type': EVENTS['output_transcript'], 'response_id': 'active',
                'item_id': 'fri1', 'content_index': 0, 'transcript': '途中の文章'})
            assert next(e for e in events.items if e.speaker == 'fri').text_ja == '途中の文章 …'
            frames = len(audio.frames)
            await client.handle_event(delta | {'item_id': 'late-new-item'})
            assert len(audio.frames) == frames
            await settled(lambda: any(json.loads(line).get('status') == 'cancelled'
                                      for line in path.read_text().splitlines()))
            data = [json.loads(line) for line in path.read_text().splitlines()]
            truncate = next(e for e in data if e['event'] == 'truncate')
            assert truncate['audio_end_ms'] == 20
            assert any(e['event'] == 'response_cancel' and e['response_id'] == 'active'
                       for e in data)
            assert any(e['event'] == 'response_done' and e['status'] == 'cancelled'
                       and e['truncated'] for e in data)
        finally:
            await client.stop()
    asyncio.run(run())


def test_echo_window_tracks_playout_rather_than_only_generation():
    async def run():
        now = [100]
        client, audio, events, meter, peer = bridge(clock=lambda: now[0])
        await attach_and_open(client, Media())
        try:
            text = '예약은 내일 저녁 여섯 시에 가능할까요?'
            await client.handle_event({'type': EVENTS['output_audio'], 'item_id': 'fri1',
                'content_index': 0, 'delta': base64.b64encode(ULAW).decode()})
            await client.handle_event({'type': EVENTS['output_transcript'], 'item_id': 'fri1',
                                       'content_index': 0, 'transcript': text})
            await client.handle_event({'type': EVENTS['output_audio_done'], 'item_id': 'fri1',
                                       'content_index': 0})
            now[0] += 12  # Queued phone playback may finish well after model generation.
            await client.mark('1')
            now[0] += 1
            await client.handle_event({'type': EVENTS['input_transcript'], 'item_id': 'echo',
                                       'transcript': text})
            assert not any(e.speaker == 'callee' for e in events.items)
            assert any(e['type'] == EVENTS['item_delete'] for e in peer.sent)
        finally:
            await client.stop()
    asyncio.run(run())


def test_speech_cancels_a_queued_checkin_before_it_is_sent():
    async def run():
        now, spoken = [0], []
        hold = await hold_fixture(now=now, spoken=spoken)
        try:
            await hold.ask('tool', 'Which time?', ())
            await asyncio.sleep(0)
            now[0] = 45
            await hold.tick()
            hold.callee_speech(True, 'callee')
            await asyncio.sleep(0)
            assert spoken == [hold.language.hold]
        finally:
            await hold.close()
    asyncio.run(run())
