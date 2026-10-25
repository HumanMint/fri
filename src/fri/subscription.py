"""Official Sign in with ChatGPT plan credentials; no API key or Codex-token copying."""

import fcntl
import json
import logging
import os
import re
import time
from pathlib import Path

import httpx
import jwt

from .private_files import private_json

ISSUER = 'https://auth.openai.com'
TOKEN_URL = ISSUER + '/api/accounts/oauth/token'
RESOURCE = 'https://api.openai.com/v1'
PLAN_SCOPE = 'chatgpt.tokens.use.direct'
LOG = logging.getLogger('fri.subscription')


class SubscriptionError(RuntimeError):
    """The saved sign-in can't be used until the owner reconnects it."""


class SubscriptionUnavailable(SubscriptionError):
    """Renewal failed in a way a later call may not: network, rate limit or provider outage."""


def refusal(exc):
    """Only an explicit OAuth refusal of the grant needs the owner."""
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (400, 401)


def plan_withdrawn(tokens):
    """A renewal that names its scopes without the plan scope withdrew plan access."""
    return isinstance(tokens, dict) and isinstance(tokens.get('scope'), str) and PLAN_SCOPE not in tokens['scope'].split()


def cause(exc):
    """Class, HTTP status and OAuth error code only; never the response body or credentials."""
    if isinstance(exc, SubscriptionError):
        return 'unusable token response'
    if not isinstance(exc, httpx.HTTPStatusError):
        return type(exc).__name__
    try:
        code = exc.response.json().get('error')
    except (ValueError, AttributeError):
        code = None
    code = code if isinstance(code, str) and re.fullmatch(r'[a-z_]{1,40}', code) else ''
    return f'HTTP {exc.response.status_code} {code}'.rstrip()


def validate_identity(token, client_id, nonce, jwks, previous=None):
    header = jwt.get_unverified_header(token)
    if header.get('alg') != 'RS256':
        raise ValueError('Unexpected identity signature algorithm')
    key = next((key for key in jwks['keys'] if key.get('kid') == header.get('kid')), None)
    if key is None:
        raise ValueError('Unknown identity signing key')
    claims = jwt.decode(token, jwt.PyJWK.from_dict(key).key, algorithms=['RS256'],
                        audience=client_id, issuer=ISSUER, leeway=5,
                        options={'require': ['sub', 'exp', 'iat', 'nonce']})
    if claims['nonce'] != nonce or not claims['sub']:
        raise ValueError('Identity nonce mismatch')
    if previous and previous['subject'] != claims['sub']:
        raise ValueError('Sign in with the same ChatGPT account as the saved registration')
    return claims


def token_record(tokens, previous, *, client_id, host_id, identity=None):
    scopes = tokens.get('scope', ' '.join(previous.get('scopes', []))).split()
    if PLAN_SCOPE not in scopes or not tokens.get('access_token'):
        raise SubscriptionError('ChatGPT plan access was not granted. Reconnect Fri with ChatGPT.')
    if tokens.get('token_type', '').lower() != 'bearer' or int(tokens.get('expires_in', 0)) <= 0:
        raise SubscriptionError('The subscription sign-in returned invalid credentials.')
    return {**previous, **tokens, 'client_id': client_id, 'ext_agent_host_id': host_id,
            'scopes': scopes, 'expires_at': time.time() + int(tokens['expires_in']),
            **({'subject': identity['sub'], 'email': identity.get('email'), 'issuer': ISSUER}
               if identity else {})}


def incomplete():
    LOG.warning('ChatGPT sign-in rejected: saved sign-in is unreadable or incomplete')
    return SubscriptionError('Fri’s saved ChatGPT sign-in is incomplete. Reconnect the subscription.')


class Subscription:
    def __init__(self, path, transport=None):
        self.path = Path(path)
        self.transport = transport
        self.rejected = False  # Set by a refusal that needs the owner, cleared by the next usable token.

    def access_token(self):
        if not self.path.exists():
            self.rejected = True
            raise SubscriptionError('Fri needs a ChatGPT subscription sign-in. Ask the owner to connect them.')
        with self.path.with_suffix('.lock').open('a') as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            # Set under the lock, so an older refusal can't land after a newer success.
            try:
                token = self.locked_token()
            except SubscriptionUnavailable:
                raise
            except SubscriptionError:
                self.rejected = True
                raise
            self.rejected = False
            return token

    def locked_token(self):
        try:
            saved = json.loads(self.path.read_text())
        except ValueError:
            saved = None
        if (not isinstance(saved, dict) or not saved.get('access_token') or
                not isinstance(saved.get('expires_at'), (int, float))):
            raise incomplete()
        if PLAN_SCOPE not in saved.get('scopes', []):
            raise SubscriptionError('Fri has no ChatGPT plan permission. Reconnect their subscription.')
        if saved['expires_at'] > time.time() + 90:
            return saved['access_token']
        if not all(saved.get(key) for key in ('refresh_token', 'client_id', 'ext_agent_host_id')):
            raise incomplete()
        tokens = None
        try:
            with httpx.Client(timeout=30, trust_env=False, transport=self.transport) as client:
                response = client.post(TOKEN_URL, data={'grant_type': 'refresh_token',
                    'client_id': saved['client_id'], 'refresh_token': saved['refresh_token'],
                    'resource': RESOURCE})
                response.raise_for_status()
            tokens = response.json()
            renewed = token_record(tokens, saved, client_id=saved['client_id'],
                                   host_id=saved['ext_agent_host_id'])
        except (httpx.HTTPError, SubscriptionError, KeyError, ValueError, TypeError, AttributeError) as exc:
            # A malformed success is the provider's fault, not the grant's; only a refusal needs the owner.
            withdrawn = plan_withdrawn(tokens)
            refused = withdrawn or refusal(exc)
            LOG.warning('ChatGPT sign-in renewal %s: %s', 'rejected' if refused else 'failed',
                        'plan scope missing' if withdrawn else cause(exc))
            if refused:
                raise SubscriptionError('Fri could not renew their ChatGPT connection. Reconnect the subscription.') from None
            raise SubscriptionUnavailable('Fri couldn’t reach ChatGPT to renew their sign-in. Try again in a minute.') from None
        private_json(self.path, renewed)
        return renewed['access_token']
