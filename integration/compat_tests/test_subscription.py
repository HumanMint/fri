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
