import json
import time
from concurrent.futures import ThreadPoolExecutor

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


@pytest.mark.parametrize('outcome', [httpx.Response(503, text='private outage'), httpx.Response(429, json={'error': 'slow down'}),
                                     httpx.Response(200, text='not json'), httpx.ConnectError('private host')])
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


def test_incomplete_saved_sign_in_is_a_refusal_and_a_usable_token_clears_it(tmp_path):
    path = tmp_path / 'session.json'
    saved = expired(path)
    del saved['client_id']
    private_json(path, saved)
    session = Subscription(path, httpx.MockTransport(lambda _: pytest.fail('no refresh without a client id')))
    with pytest.raises(SubscriptionError, match='Reconnect'):
        session.access_token()
    assert session.rejected
    private_json(path, {**saved, 'client_id': 'client', 'expires_at': time.time() + 3600})
    assert session.access_token() == 'old' and not session.rejected
