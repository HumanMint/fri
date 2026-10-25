import errno
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from fri.subscription import (
    ISSUER,
    PLAN_SCOPE,
    Subscription,
    SubscriptionError,
    SubscriptionUnavailable,
    private_json,
    token_record,
    validate_identity,
)


def test_identity_requires_signed_matching_audience_nonce_and_subject():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk['kid'] = 'test'
    claims = {'sub': 'owner', 'aud': 'fri-client', 'iss': ISSUER, 'iat': time.time(),
              'exp': time.time() + 60, 'nonce': 'fresh'}
    token = jwt.encode(claims, key, algorithm='RS256', headers={'kid': 'test'})
    assert validate_identity(token, 'fri-client', 'fresh', {'keys': [jwk]})['sub'] == 'owner'
    for client, nonce, previous in [('other', 'fresh', None), ('fri-client', 'replay', None),
                                     ('fri-client', 'fresh', {'subject': 'another'})]:
        with pytest.raises((ValueError, jwt.PyJWTError)):
            validate_identity(token, client, nonce, {'keys': [jwk]}, previous)
    with pytest.raises((ValueError, jwt.PyJWTError)):
        validate_identity(jwt.encode(claims, 'untrusted' * 8, algorithm='HS256'), 'fri-client', 'fresh', {'keys': [jwk]})


def test_refresh_rotates_once_across_concurrent_callers_and_keeps_private_permissions(tmp_path):
    path = tmp_path / 'session.json'
    private_json(path, {'access_token': 'expired', 'refresh_token': 'old-refresh', 'client_id': 'client',
        'ext_agent_host_id': 'vm', 'scopes': [PLAN_SCOPE], 'expires_at': 0})
    requests = []
    def refresh(request):
        requests.append(request)
        assert b'refresh_token=old-refresh' in request.content
        assert 'auth.openai.com' == request.url.host
        return httpx.Response(200, json={'access_token': 'renewed', 'refresh_token': 'rotated',
                                        'token_type': 'Bearer', 'expires_in': 3600})
    session = Subscription(path, httpx.MockTransport(refresh))
    with ThreadPoolExecutor(4) as pool:
        assert list(pool.map(lambda _: session.access_token(), range(4))) == ['renewed'] * 4
    assert len(requests) == 1
    assert json.loads(path.read_text())['refresh_token'] == 'rotated'
    assert path.stat().st_mode & 0o777 == 0o600


def test_failed_refresh_does_not_destroy_existing_credential_or_expose_response(tmp_path):
    path = tmp_path / 'session.json'
    saved = {'access_token': 'old', 'refresh_token': 'private', 'client_id': 'client',
             'ext_agent_host_id': 'vm', 'scopes': [PLAN_SCOPE], 'expires_at': 0}
    private_json(path, saved)
    session = Subscription(path, httpx.MockTransport(lambda _: httpx.Response(400, text='private')))
    with pytest.raises(SubscriptionError, match='could not renew') as exc:
        session.access_token()
    assert 'private' not in str(exc.value)
    assert json.loads(path.read_text()) == saved
    with pytest.raises(SubscriptionError, match='not granted'):
        token_record({'access_token': 'a', 'scope': 'openid', 'token_type': 'Bearer', 'expires_in': 60},
                     {}, client_id='client', host_id='vm')


def expired(path, **changes):
    saved = {'access_token': 'old', 'refresh_token': 'private-refresh', 'client_id': 'client',
             'ext_agent_host_id': 'vm', 'scopes': [PLAN_SCOPE], 'expires_at': 0, **changes}
    private_json(path, saved)
    return saved


def test_refused_refresh_needs_the_owner_and_logs_only_its_cause(tmp_path, caplog):
    path = tmp_path / 'session.json'
    saved = expired(path)
    refused = httpx.Response(400, json={'error': 'invalid_grant', 'error_description': 'private detail'})
    session = Subscription(path, httpx.MockTransport(lambda _: refused))
    with pytest.raises(SubscriptionError, match='Reconnect') as exc:
        session.access_token()
    assert not isinstance(exc.value, SubscriptionUnavailable) and session.rejected
    assert 'renewal rejected: HTTP 400 invalid_grant' in caplog.text
    assert 'private' not in caplog.text and json.loads(path.read_text()) == saved


NESTED = '[' * 1100 + '0' + ']' * 1100  # Valid JSON that Python's parser gives up on with RecursionError.
MALFORMED = [{}, [], {'access_token': 'private-a', 'token_type': 'Bearer', 'expires_in': 0},
             {'access_token': 'private-a', 'token_type': 'Bearer', 'expires_in': None},
             {'access_token': 'private-a', 'token_type': None, 'expires_in': 60},
             {'access_token': 123, 'token_type': 'Bearer', 'expires_in': 60},
             {'access_token': 'private-a', 'token_type': 'Bearer', 'expires_in': 60, 'scope': 7}]


@pytest.mark.parametrize('outcome', [httpx.Response(503, text='private outage'), httpx.Response(429, json={'error': 'slow down'}),
                                     httpx.Response(200, text='not json'), httpx.Response(200, text=NESTED),
                                     httpx.ConnectError('private host'),
                                     httpx.Response(200, text='{"access_token": "private-a", "token_type": "Bearer", "expires_in": 1e999}'),
                                     httpx.Response(200, json={'access_token': 'private-a', 'token_type': 'Bearer', 'expires_in': 10**400}),
                                     *(httpx.Response(200, json=body) for body in MALFORMED)])
def test_transient_refresh_failure_does_not_tell_anyone_to_reconnect(tmp_path, caplog, outcome):
    path = tmp_path / 'session.json'
    saved = expired(path)
    def refresh(request):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    session = Subscription(path, httpx.MockTransport(refresh))
    with pytest.raises(SubscriptionUnavailable) as exc:
        session.access_token()
    assert 'reconnect' not in str(exc.value).lower() and not session.rejected
    assert 'renewal failed:' in caplog.text and 'private' not in caplog.text and 'slow down' not in caplog.text
    assert json.loads(path.read_text()) == saved


@pytest.mark.parametrize('body', [NESTED, 'private, not json'], ids=['deeply nested', 'not json'])
def test_a_refusal_with_an_unreadable_body_is_still_a_refusal(tmp_path, caplog, body):
    path = tmp_path / 'session.json'
    saved = expired(path)
    session = Subscription(path, httpx.MockTransport(lambda _: httpx.Response(400, text=body)))
    with pytest.raises(SubscriptionError, match='Reconnect') as exc:
        session.access_token()
    assert not isinstance(exc.value, SubscriptionUnavailable) and session.rejected
    assert 'renewal rejected: HTTP 400\n' in caplog.text and 'private' not in caplog.text
    assert json.loads(path.read_text()) == saved


class Corrupt(httpx.SyncByteStream):
    """A gzip-labelled body that isn't gzip. httpx asks for gzip by default, so error replies often are."""

    def __iter__(self):
        yield b'private, not gzip'


def corrupt(status):
    return httpx.Response(status, headers={'Content-Encoding': 'gzip'}, stream=Corrupt())


@pytest.mark.parametrize('status', [400, 401])
def test_a_refusal_whose_body_cannot_be_decoded_is_still_a_refusal(tmp_path, caplog, status):
    path = tmp_path / 'session.json'
    saved = expired(path)
    session = Subscription(path, httpx.MockTransport(lambda _: corrupt(status)))
    with pytest.raises(SubscriptionError, match='Reconnect') as exc:
        session.access_token()
    assert not isinstance(exc.value, SubscriptionUnavailable) and session.rejected
    assert f'renewal rejected: HTTP {status}\n' in caplog.text and 'private' not in caplog.text
    assert json.loads(path.read_text()) == saved


@pytest.mark.parametrize('status', [200, 503])
def test_any_other_reply_whose_body_cannot_be_decoded_is_transient(tmp_path, caplog, status):
    path = tmp_path / 'session.json'
    saved = expired(path)
    session = Subscription(path, httpx.MockTransport(lambda _: corrupt(status)))
    with pytest.raises(SubscriptionUnavailable):
        session.access_token()
    assert not session.rejected and 'renewal failed: DecodingError' in caplog.text and 'private' not in caplog.text
    assert json.loads(path.read_text()) == saved


def test_renewal_without_the_plan_scope_is_a_refusal(tmp_path, caplog):
    path = tmp_path / 'session.json'
    expired(path)
    withdrawn = httpx.Response(200, json={'access_token': 'private-a', 'refresh_token': 'private-r', 'scope': 'openid',
                                          'token_type': 'Bearer', 'expires_in': 60})
    session = Subscription(path, httpx.MockTransport(lambda _: withdrawn))
    with pytest.raises(SubscriptionError, match='Reconnect') as exc:
        session.access_token()
    assert not isinstance(exc.value, SubscriptionUnavailable) and session.rejected
    assert 'renewal rejected: plan scope missing' in caplog.text and 'private' not in caplog.text


@pytest.mark.parametrize('damage', ['expires_at', 'access_token', 'refresh_token', 'client_id', 'ext_agent_host_id',
                                    'not json', 'not an object', 'null scopes', 'numeric access token', 'string expiry',
                                    'huge expiry', 'deeply nested'])
def test_incomplete_saved_sign_in_is_a_refusal_and_a_usable_token_clears_it(tmp_path, caplog, damage):
    path = tmp_path / 'session.json'
    saved = expired(path, expires_at=0 if damage in ('refresh_token', 'client_id', 'ext_agent_host_id') else time.time() + 3600)
    if damage == 'not json':
        path.write_text('{"access_token": "private')
    elif damage == 'not an object':
        path.write_text('["private"]')
    elif damage == 'deeply nested':
        path.write_text(NESTED)
    elif damage in ('null scopes', 'numeric access token', 'string expiry', 'huge expiry'):
        key, value = {'null scopes': ('scopes', None), 'numeric access token': ('access_token', 7),
                      'string expiry': ('expires_at', 'soon'), 'huge expiry': ('expires_at', 10**400)}[damage]
        private_json(path, {**saved, key: value})
    else:
        private_json(path, {k: v for k, v in saved.items() if k != damage})
    session = Subscription(path, httpx.MockTransport(lambda _: pytest.fail('no refresh from an incomplete sign-in')))
    with pytest.raises(SubscriptionError, match='Reconnect') as exc:
        session.access_token()
    assert not isinstance(exc.value, SubscriptionUnavailable) and session.rejected
    assert 'ChatGPT sign-in rejected: saved sign-in' in caplog.text and 'private' not in caplog.text
    private_json(path, {**saved, 'expires_at': time.time() + 3600})
    assert session.access_token() == 'old' and not session.rejected


def test_a_withdrawn_plan_stays_refused_and_a_reconnect_still_works(tmp_path):
    path = tmp_path / 'session.json'
    saved = expired(path)
    spent = []

    def refresh(request):
        token = parse_qs(request.content.decode())['refresh_token'][0]
        if token in spent:
            return httpx.Response(400, json={'error': 'invalid_grant'})
        spent.append(token)
        return httpx.Response(200, json={'access_token': 'private-a', 'refresh_token': 'rotated', 'scope': 'openid',
                                         'token_type': 'Bearer', 'expires_in': 60})
    session = Subscription(path, httpx.MockTransport(refresh))
    for _ in range(2):
        with pytest.raises(SubscriptionError, match='Reconnect') as exc:
            session.access_token()
        assert not isinstance(exc.value, SubscriptionUnavailable) and session.rejected
    assert session.refusals == 1
    # Neither the plan-less token nor its scopes are kept, so a renewal that leaves out scope can't revive the
    # plan, and scripts/connect_chatgpt.py builds the new record on the one it had.
    assert json.loads(path.read_text()) == saved
    tokens = {'access_token': 'new', 'refresh_token': 'new-refresh', 'token_type': 'Bearer', 'expires_in': 3600}
    private_json(path, token_record(tokens, json.loads(path.read_text()), client_id='client', host_id='vm'))
    assert session.access_token() == 'new' and not session.rejected


@pytest.mark.parametrize('expires_in', [None, 10**400], ids=['missing expiry', 'huge expiry'])
def test_rotated_refresh_token_is_kept_even_when_the_rest_of_the_renewal_is_unusable(tmp_path, expires_in):
    path = tmp_path / 'session.json'
    saved = expired(path)
    rotated = httpx.Response(200, json={'access_token': 'private-a', 'refresh_token': 'rotated',
                                        'token_type': 'Bearer', 'expires_in': expires_in})
    with pytest.raises(SubscriptionUnavailable):
        Subscription(path, httpx.MockTransport(lambda _: rotated)).access_token()
    assert json.loads(path.read_text()) == {**saved, 'refresh_token': 'rotated'}  # Still expired, so the next call renews.


@pytest.mark.parametrize('error', [OSError(errno.ENOSPC, 'private'), RecursionError()], ids=['disk full', 'too deep to save'])
def test_a_renewal_that_cannot_be_saved_still_keeps_its_rotated_refresh_token(tmp_path, caplog, monkeypatch, error):
    path = tmp_path / 'session.json'
    saved = expired(path)
    renewed = httpx.Response(200, json={'access_token': 'private-a', 'refresh_token': 'rotated',
                                        'token_type': 'Bearer', 'expires_in': 3600})
    import fri.subscription as subscription
    writes = []

    def full_once(target, data):
        writes.append(data)
        if len(writes) == 1:
            raise error
        private_json(target, data)
    monkeypatch.setattr(subscription, 'private_json', full_once)
    session = Subscription(path, httpx.MockTransport(lambda _: renewed))
    with pytest.raises(SubscriptionUnavailable):
        session.access_token()
    assert json.loads(path.read_text()) == {**saved, 'refresh_token': 'rotated'} and not session.rejected
    assert f'renewal failed: {type(error).__name__}' in caplog.text and 'private' not in caplog.text


def test_a_failed_save_of_the_rotation_itself_is_still_not_raw(tmp_path, caplog, monkeypatch):
    path = tmp_path / 'session.json'
    saved = expired(path)
    rotated = httpx.Response(200, json={'access_token': 'private-a', 'refresh_token': 'rotated',
                                        'token_type': 'Bearer', 'expires_in': None})

    def full(*_):
        raise OSError(errno.ENOSPC, 'private')
    monkeypatch.setattr('fri.subscription.private_json', full)
    with pytest.raises(SubscriptionUnavailable):
        Subscription(path, httpx.MockTransport(lambda _: rotated)).access_token()
    assert json.loads(path.read_text()) == saved
    assert 'rotated refresh token not saved: OSError' in caplog.text and 'private' not in caplog.text


def test_renewal_without_a_refresh_token_keeps_the_saved_one(tmp_path):
    path = tmp_path / 'session.json'
    expired(path)
    renewed = httpx.Response(200, json={'access_token': 'new', 'refresh_token': None, 'token_type': 'Bearer',
                                        'expires_in': 3600})
    assert Subscription(path, httpx.MockTransport(lambda _: renewed)).access_token() == 'new'
    record = json.loads(path.read_text())
    assert record['refresh_token'] == 'private-refresh' and record['access_token'] == 'new'


class Unreadable:
    """The session path, except that reading it fails after the existence check passed."""

    def __init__(self, path, error):
        self.path, self.error = path, error

    def read_text(self):
        raise self.error

    def __getattr__(self, name):
        return getattr(self.path, name)


def test_a_saved_sign_in_gone_after_the_check_is_a_refusal(tmp_path, caplog):
    path = tmp_path / 'session.json'
    expired(path, expires_at=time.time() + 3600)
    session = Subscription(path)
    session.path = Unreadable(path, FileNotFoundError(2, 'private'))
    with pytest.raises(SubscriptionError, match='Reconnect') as exc:
        session.access_token()
    assert not isinstance(exc.value, SubscriptionUnavailable) and session.rejected
    assert 'saved sign-in is unreadable' in caplog.text and 'private' not in caplog.text
    session.path = path
    assert session.access_token() == 'old' and not session.rejected


@pytest.mark.parametrize('error', [OSError(errno.EIO, 'private'), PermissionError(13, 'private')],
                         ids=['disk error', 'permission denied'])
@pytest.mark.parametrize('where', ['read', 'lock'])
def test_a_file_error_is_not_a_refusal(tmp_path, caplog, monkeypatch, error, where):
    # A reconnect wouldn't fix the disk, and the token on it may be fine: members retry, the log has the cause.
    path = tmp_path / 'session.json'
    expired(path, expires_at=time.time() + 3600)
    session = Subscription(path)
    if where == 'read':
        session.path = Unreadable(path, error)
    else:
        def flock(*_):
            raise error
        monkeypatch.setattr('fri.subscription.fcntl.flock', flock)
    with pytest.raises(SubscriptionUnavailable) as exc:
        session.access_token()
    assert 'reconnect' not in str(exc.value).lower() and not session.rejected and session.refusals == 0
    assert f'sign-in unavailable: {type(error).__name__}' in caplog.text and 'private' not in caplog.text
    monkeypatch.undo()
    session.path = path
    assert session.access_token() == 'old'


class Paused:
    """The session path, except that one thread's existence check stalls and then sees no file."""

    def __init__(self, path):
        self.path, self.checking, self.release = path, threading.Event(), threading.Event()

    def exists(self):
        if threading.current_thread().name == 'stale':
            self.checking.set()
            self.release.wait(5)
            return False
        return self.path.exists()

    def __getattr__(self, name):
        return getattr(self.path, name)


def test_an_older_missing_file_check_cannot_override_a_newer_success(tmp_path):
    path = tmp_path / 'session.json'
    expired(path, expires_at=time.time() + 3600)
    session = Subscription(path)
    session.path = Paused(path)
    errors = []

    def stale():
        try:
            session.access_token()
        except SubscriptionError as exc:
            errors.append(exc)
    worker = threading.Thread(target=stale, name='stale')
    worker.start()
    assert session.path.checking.wait(5)
    assert session.access_token() == 'old' and not session.rejected
    session.path.release.set()
    worker.join(5)
    assert errors and not session.rejected  # The stale refusal finished last but observed first.
