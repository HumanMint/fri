"""Official Sign in with ChatGPT plan credentials; no API key or Codex-token copying."""

import fcntl
import json
import logging
import math
import os
import re
import threading
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


REFUSED = (400, 401)


def refusal(exc):
    """Only an explicit OAuth refusal of the grant needs the owner."""
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in REFUSED


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
    except Exception:  # Only for the log: a body that's unread, undecodable or nested too deep names no code.
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


def usable(value):
    return isinstance(value, str) and bool(value)


def lifetime(value):
    """Whole seconds from a token response, or None unless finite, positive and under a year."""
    if isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):  # JSON integers can be too big for a float.
        return None
    return int(seconds) if math.isfinite(seconds) and 1 <= seconds <= 365 * 86400 else None


def moment(value):
    """A saved timestamp: a number a float can hold, so not a bool, inf, nan or a huge integer."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def token_record(tokens, previous, *, client_id, host_id, identity=None):
    if not isinstance(tokens, dict):
        raise SubscriptionError('The subscription sign-in returned invalid credentials.')
    saved_scopes = previous.get('scopes')
    scope = tokens.get('scope', ' '.join(saved_scopes) if isinstance(saved_scopes, list) else '')
    scopes = scope.split() if isinstance(scope, str) else []
    if PLAN_SCOPE not in scopes or not usable(tokens.get('access_token')):
        raise SubscriptionError('ChatGPT plan access was not granted. Reconnect Fri with ChatGPT.')
    expires = lifetime(tokens.get('expires_in'))
    if not isinstance(tokens.get('token_type'), str) or tokens['token_type'].lower() != 'bearer' or expires is None:
        raise SubscriptionError('The subscription sign-in returned invalid credentials.')
    # A response without a usable refresh token keeps the one already saved.
    kept = {} if usable(tokens.get('refresh_token')) else {'refresh_token': previous.get('refresh_token')}
    return {**previous, **tokens, **kept, 'client_id': client_id, 'ext_agent_host_id': host_id,
            'scopes': scopes, 'expires_at': time.time() + expires,
            **({'subject': identity['sub'], 'email': identity.get('email'), 'issuer': ISSUER}
               if identity else {})}


def incomplete():
    LOG.warning('ChatGPT sign-in rejected: saved sign-in is unreadable or incomplete')
    return SubscriptionError('Fri’s saved ChatGPT sign-in is incomplete. Reconnect the subscription.')


class Subscription:
    def __init__(self, path, transport=None):
        self.path = Path(path)
        self.transport = transport
        # The current refusal's number while one needs the owner; a usable token in between starts a new one.
        self.refused = None
        self.refusals = 0
        self.guard = threading.Lock()
        self.observed = self.settled = 0

    @property
    def rejected(self):
        return self.refused is not None

    def observation(self):
        with self.guard:
            self.observed += 1
            return self.observed

    def settle(self, observation, rejected):
        """Calls finish out of order across threads; only a newer observation may change the flag."""
        with self.guard:
            if observation <= self.settled:
                return
            self.settled = observation
            if not rejected:
                self.refused = None
            elif self.refused is None:
                self.refusals += 1
                self.refused = self.refusals

    def access_token(self):
        observation = self.observation()
        try:
            if not self.path.exists():
                self.settle(observation, True)
                raise SubscriptionError('Fri needs a ChatGPT subscription sign-in. Ask the owner to connect them.')
            with self.path.with_suffix('.lock').open('a') as lock:
                os.chmod(lock.name, 0o600)
                fcntl.flock(lock, fcntl.LOCK_EX)
                observation = self.observation()
                try:
                    token = self.locked_token()
                except SubscriptionUnavailable:
                    raise
                except SubscriptionError:
                    self.settle(observation, True)
                    raise
                self.settle(observation, False)
                return token
        except OSError as exc:  # A reconnect can't fix the disk, and the saved token may be fine.
            LOG.warning('ChatGPT sign-in unavailable: %s', type(exc).__name__)
            raise SubscriptionUnavailable('Fri couldn’t read their ChatGPT sign-in. Try again in a minute.') from None

    def locked_token(self):
        try:
            saved = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError, RecursionError):  # Gone since the check, or not JSON we can read.
            saved = None
        expires_at = saved.get('expires_at') if isinstance(saved, dict) else None
        if not isinstance(saved, dict) or not usable(saved.get('access_token')) or not moment(expires_at):
            raise incomplete()
        scopes = saved.get('scopes')
        if not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes) or PLAN_SCOPE not in scopes:
            LOG.warning('ChatGPT sign-in rejected: saved sign-in has no plan scope')
            raise SubscriptionError('Fri has no ChatGPT plan permission. Reconnect their subscription.')
        if expires_at > time.time() + 90:
            return saved['access_token']
        if not all(usable(saved.get(key)) for key in ('refresh_token', 'client_id', 'ext_agent_host_id')):
            raise incomplete()
        tokens = None
        try:
            with httpx.Client(timeout=30, trust_env=False, transport=self.transport) as client:
                with client.stream('POST', TOKEN_URL, data={'grant_type': 'refresh_token',
                        'client_id': saved['client_id'], 'refresh_token': saved['refresh_token'],
                        'resource': RESOURCE}) as response:
                    try:
                        response.read()
                    except httpx.HTTPError:
                        if response.status_code not in REFUSED:
                            raise  # A refusal is one whatever its body, which only names the OAuth error.
                    response.raise_for_status()
            tokens = response.json()
            renewed = token_record(tokens, saved, client_id=saved['client_id'],
                                   host_id=saved['ext_agent_host_id'])
            private_json(self.path, renewed)
        except (httpx.HTTPError, SubscriptionError, KeyError, ValueError, TypeError, AttributeError,
                RecursionError, OSError) as exc:
            rotated = tokens.get('refresh_token') if isinstance(tokens, dict) else None
            if usable(rotated) and rotated != saved['refresh_token']:
                try:
                    private_json(self.path, {**saved, 'refresh_token': rotated})  # The old one may be spent already.
                except (OSError, ValueError, RecursionError) as failed:
                    LOG.warning('ChatGPT sign-in renewal: rotated refresh token not saved: %s', type(failed).__name__)
            # A malformed success is the provider's fault, not the grant's; only a refusal needs the owner.
            withdrawn = plan_withdrawn(tokens)
            refused = withdrawn or refusal(exc)
            LOG.warning('ChatGPT sign-in renewal %s: %s', 'rejected' if refused else 'failed',
                        'plan scope missing' if withdrawn else cause(exc))
            if refused:
                raise SubscriptionError('Fri could not renew their ChatGPT connection. Reconnect the subscription.') from None
            raise SubscriptionUnavailable('Fri couldn’t reach ChatGPT to renew their sign-in. Try again in a minute.') from None
        return renewed['access_token']
