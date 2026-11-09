"""Import-only helpers for one explicitly approved owner Korean test; never dials.

Twilio endpoints: https://www.twilio.com/docs/voice/api/recording
The parent owns the later explicit-go gate, the 120-second call limit and $5 budget.
"""

import asyncio
import io
import os
import re
import tempfile
import wave
from dataclasses import dataclass, field, replace
from pathlib import Path

import httpx

from fri.jpcall.config import CALL_LANGUAGES, CallLanguage
from fri.jpcall.twilio import TwilioClient, TwilioError

TIMEOUT_S = 10
MAX_CALL_S = 120
MAX_AUDIO_BYTES = 4 * 1024 * 1024
UNAVAILABLE = 'Owner test recording is unavailable; capture was not verified.'


@dataclass(frozen=True)
class OwnerKoreanTestContext:
    destination: str = field(repr=False)
    call_sid: str = ''
    owner_only: bool = False
    explicit_go: bool = False
    recording_consent: bool = False


def _context(context):
    if (not isinstance(context, OwnerKoreanTestContext) or context.owner_only is not True
            or context.explicit_go is not True or context.recording_consent is not True
            or not isinstance(context.destination, str)
            or not re.fullmatch(r'\+1[0-9]{10}', context.destination)):
        raise TwilioError('An explicit owner-only Korean recording test context is required.')
    return context


def owner_recording_language(*, context=None) -> CallLanguage:
    """Owner-only test without Discord relay; use before plan/confirmation."""
    _context(context)
    base = CALL_LANGUAGES['ko']
    original = '녹음 없이 문자로 공유하고 일행도 통화를 들어요.'
    question = '부족한 정보는 ask_group으로 확인하세요.'
    if any(base.instructions.count(text) != 1 for text in (original, question)):
        raise TwilioError('The Korean test notice needs review before use.')
    return replace(base, instructions=base.instructions.replace(original,
        '소유자가 동의한 개인 한국어 테스트예요. 녹음 기능이 작동하면 통화 음성을 Twilio에 '
        '녹음하고 개인 평가용 파일로 저장해요. 녹음하지 않는다고 말하지 마세요. '
        '다른 사람에게 통화나 채팅을 공유하는 테스트가 아니에요.').replace(
            question, '부족한 정보는 ask_group으로 소유자에게 확인하세요.'),
        permission_description='Report the owner\'s explicit consent to this AI recording test, '
                               'a refusal or a transfer. Anything else is unclear and grants nothing.',
        question_description='Ask the owner by text for a missing fact; the app asks the callee to wait.',
        hold='잠시만요, 확인할게요.',
        opener='안녕하세요, AI 프리예요. 동의하신 녹음 테스트, 시작해도 될까요?')


def _guard(api, context):
    context = _context(context)
    if (api.config.call_language != 'ko' or not 0 < api.config.limits.max_call_s <= MAX_CALL_S
            or context.destination not in api.config.limits.test_numbers
            or not re.fullmatch(r'AC[0-9a-fA-F]{32}', api.config.twilio_account_sid)):
        raise TwilioError('Use only the dedicated, bounded Korean owner test client.')
    if not isinstance(context.call_sid, str):
        raise TwilioError('A known owner test CallSid is required.')
    TwilioClient._sid(context.call_sid)
    return context


async def _verify_call(api, context, *, starting=False):
    row = await api._request('GET', '/' + context.call_sid + '.json')
    if (row.get('sid') != context.call_sid or row.get('to') != context.destination
            or row.get('account_sid') != api.config.twilio_account_sid
            or row.get('direction') != 'outbound-api'
            or row.get('status') not in ({'in-progress'} if starting else {'in-progress', 'completed'})):
        raise TwilioError('The existing call does not match the approved owner test.')


def _recording_url(api, recording_sid, extension='.json'):
    if not isinstance(recording_sid, str) or not re.fullmatch(r'RE[0-9a-fA-F]{32}', recording_sid):
        raise TwilioError('A valid owner test recording identifier is required.')
    return ('https://api.twilio.com/2010-04-01/Accounts/' + api.config.twilio_account_sid
            + '/Recordings/' + recording_sid + extension)


def _auth(api):
    return httpx.BasicAuth(api.config.twilio_api_key_sid, api.config.twilio_api_key_secret)


async def _metadata(api, recording_sid, context):
    try:
        response = await api._client.get(_recording_url(api, recording_sid), auth=_auth(api),
                                         follow_redirects=False, timeout=TIMEOUT_S)
        response.raise_for_status()
        row = response.json()
        if (not isinstance(row, dict) or row.get('sid') != recording_sid
                or row.get('call_sid') != context.call_sid
                or row.get('account_sid') != api.config.twilio_account_sid
                or row.get('source') != 'StartCallRecordingAPI'):
            raise ValueError
        return row
    except (httpx.HTTPError, ValueError):
        raise TwilioError(UNAVAILABLE) from None


async def start_recording(api: TwilioClient, *, context=None) -> str:
    """One POST, no retry; a lost acknowledgement needs manual reconciliation."""
    context = _guard(api, context)
    if api.config.language != owner_recording_language(context=context):
        raise TwilioError('Prepare the reviewed Korean recording notice before capture.')
    try:
        async with asyncio.timeout(TIMEOUT_S):
            await _verify_call(api, context, starting=True)
            prior = await api._request('GET', '/' + context.call_sid + '/Recordings.json?PageSize=1')
            if prior.get('recordings') != [] or prior.get('next_page_uri'):
                raise TwilioError('This test call already has recording state; do not retry start.')
            row = await api._request('POST', '/' + context.call_sid + '/Recordings.json', data={
                'RecordingChannels': 'dual', 'RecordingTrack': 'both', 'Trim': 'do-not-trim',
            })
            recording_sid = row.get('sid', '')
            _recording_url(api, recording_sid)
            if (row.get('call_sid') != context.call_sid
                    or row.get('account_sid') != api.config.twilio_account_sid
                    or row.get('channels') != 2 or row.get('source') != 'StartCallRecordingAPI'
                    or row.get('status') not in {'in-progress', 'processing', 'completed'}):
                raise TwilioError(UNAVAILABLE)
            return recording_sid
    except TimeoutError:
        raise TwilioError(UNAVAILABLE) from None


async def stop_recording(api: TwilioClient, recording_sid: str, *, context=None) -> None:
    """Stop this recording only; this helper never ends or creates a telephone Call."""
    context = _guard(api, context)
    try:
        async with asyncio.timeout(TIMEOUT_S):
            await _verify_call(api, context)
            row = await _metadata(api, recording_sid, context)
            if row.get('status') not in {'processing', 'in-progress', 'paused', 'completed', 'stopped'}:
                raise TwilioError(UNAVAILABLE)
            if row.get('status') in {'processing', 'in-progress', 'paused'}:
                row = await api._request('POST', '/' + context.call_sid + '/Recordings/'
                                         + recording_sid + '.json', data={'Status': 'stopped'})
                if (row.get('sid') != recording_sid or row.get('call_sid') != context.call_sid
                        or row.get('account_sid') != api.config.twilio_account_sid
                        or row.get('status') not in {
                        'stopped', 'processing', 'completed'}):
                    raise TwilioError(UNAVAILABLE)
    except TimeoutError:
        raise TwilioError(UNAVAILABLE) from None


def _artifact_parent(parent):
    try:
        parent = Path(parent).resolve(strict=True)
    except OSError:
        raise TwilioError('Use an existing artifact directory outside every Git repository.') from None
    if not parent.is_dir() or any((path / '.git').exists() for path in (parent, *parent.parents)):
        raise TwilioError('Use an existing artifact directory outside every Git repository.')
    return parent


def _verify_wav(body, duration):
    try:
        with wave.open(io.BytesIO(body), 'rb') as audio:
            frames = audio.getnframes()
            if (audio.getnchannels() != 2 or audio.getsampwidth() != 2
                    or audio.getcomptype() != 'NONE' or audio.getframerate() != 8000
                    or not 0 < frames / audio.getframerate() <= MAX_CALL_S
                    or abs(frames / audio.getframerate() - duration) > 1):
                raise ValueError
            pcm = audio.readframes(frames)
            if len(pcm) != frames * 4 or any(
                    not (any(pcm[channel::4]) or any(pcm[channel + 1::4])) for channel in (0, 2)):
                # Channel activity is a basic presence check, not proof of intelligible speech.
                raise ValueError
    except (wave.Error, EOFError, ValueError):
        raise TwilioError('A complete two-channel owner test WAV was not verified.') from None


async def download_completed(api: TwilioClient, recording_sid: str, artifact_parent, *,
                             context=None) -> Path:
    """Fetch only this completed two-track recording into a new private directory."""
    context = _guard(api, context)
    parent = _artifact_parent(artifact_parent)
    try:
        async with asyncio.timeout(TIMEOUT_S):
            await _verify_call(api, context)
            row = await _metadata(api, recording_sid, context)
            duration = int(row.get('duration', '0'))
            if row.get('status') != 'completed' or row.get('channels') != 2 or not 0 < duration <= MAX_CALL_S:
                raise TwilioError(UNAVAILABLE)
            body = bytearray()
            url = _recording_url(api, recording_sid, '.wav') + '?RequestedChannels=2'
            async with api._client.stream('GET', url, auth=_auth(api),
                                          follow_redirects=False, timeout=TIMEOUT_S) as response:
                response.raise_for_status()
                async for block in response.aiter_bytes(chunk_size=65536):
                    if len(body) + len(block) > MAX_AUDIO_BYTES:
                        raise TwilioError('The owner test recording exceeds the download limit.')
                    body.extend(block)
            _verify_wav(body, duration)
    except (httpx.HTTPError, TimeoutError, TypeError, ValueError):
        raise TwilioError(UNAVAILABLE) from None
    directory = Path(tempfile.mkdtemp(prefix='jpcall-owner-ko-', dir=parent))
    path = directory / 'call.wav'
    try:
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as file:
            file.write(body)
            file.flush()
            os.fsync(file.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        directory.rmdir()
        raise
    return path


async def delete_after_review(api: TwilioClient, recording_sid: str, *, context=None,
                               owner_reviewed=False) -> None:
    """Explicit manual cleanup only, after the owner reviewed the saved artifact."""
    context = _guard(api, context)
    if owner_reviewed is not True:
        raise TwilioError('Review the saved owner test recording before manual provider deletion.')
    try:
        async with asyncio.timeout(TIMEOUT_S):
            await _verify_call(api, context)
            row = await _metadata(api, recording_sid, context)
            if row.get('status') != 'completed':
                raise TwilioError('Only a completed, owner-reviewed recording may be deleted.')
            response = await api._client.delete(_recording_url(api, recording_sid), auth=_auth(api),
                                                follow_redirects=False, timeout=TIMEOUT_S)
            if response.status_code != 204:
                raise TwilioError('Provider recording deletion was not confirmed.')
    except (httpx.HTTPError, TimeoutError):
        raise TwilioError('Provider recording deletion was not confirmed.') from None
