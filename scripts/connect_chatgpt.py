#!/usr/bin/env python3
"""Continue with ChatGPT: authorize Fri to use the owner's subscription on Hostinger."""

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from fri.subscription import (
    ISSUER,
    RESOURCE,
    TOKEN_URL,
    private_json,
    token_record,
    validate_identity,
)

ROOT = Path(__file__).resolve().parents[1]


def main():
    destination = ROOT / '.secrets/fri-chatgpt.json'
    host_path = ROOT / '.secrets/fri-hostinger-id.json'
    if not host_path.exists():
        private_json(host_path, {'id': 'urn:uuid:' + str(uuid.uuid4())})
    host_id = json.loads(host_path.read_text())['id']
    registration = ROOT / '.secrets/fri-chatgpt-registration.json'
    previous_path = destination if destination.exists() else registration
    previous = json.loads(previous_path.read_text()) if previous_path.exists() else {}
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    callback = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Callback URLs contain an authorization code; never log them.

        def do_GET(self):
            parts = urlparse(self.path)
            values = {key: value[0] for key, value in parse_qs(parts.query).items() if len(value) == 1}
            valid = parts.path == '/auth/callback' and hmac.compare_digest(values.get('state', ''), state)
            self.send_response(200 if valid else 400)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            if valid:
                callback.update(values)
                self.wfile.write(b'Authorization received. Return to your terminal for verification.')
            else:
                self.wfile.write(b'Invalid callback. Return to the original sign-in window.')

    with HTTPServer(('127.0.0.1', 0), Handler) as server:
        server.timeout = 1
        redirect = f'http://127.0.0.1:{server.server_port}/auth/callback'
        params = {'client_id': previous.get('client_id', 'dynamic_agent_client'),
                  'ext_agent_host_id': host_id, 'response_type': 'code', 'redirect_uri': redirect,
                  'scope': 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct',
                  'resource': RESOURCE, 'state': state, 'nonce': nonce, 'code_challenge_method': 'S256',
                  'code_challenge': base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')}
        if not previous:
            params['agent_name_hint'] = 'Fri'
        # Omit ID-token/login hints so authorization URLs never contain stored credentials.
        url = ISSUER + '/api/accounts/authorize?' + urlencode(params)
        print('Continue with ChatGPT to authorize Fri on your personal Hostinger VM.')
        print('This uses your subscription, not an API key. Fri sets no token budget.')
        print('Opening the sign-in page. If it does not open, visit:\n' + url)
        webbrowser.open(url)
        deadline = time.monotonic() + 600
        while not callback and time.monotonic() < deadline:
            server.handle_request()
    if callback.get('error') or not callback.get('code'):
        raise SystemExit('Sign-in was declined or timed out. Your previous connection was kept.')
    client_id = callback.get('client_id', previous.get('client_id', ''))
    if not client_id or client_id == 'dynamic_agent_client' or (previous and client_id != previous['client_id']):
        raise SystemExit('The sign-in returned an unexpected registration. Nothing was saved.')
    try:
        with httpx.Client(timeout=30, trust_env=False) as client:
            response = client.post(TOKEN_URL, data={'grant_type': 'authorization_code', 'client_id': client_id,
                'code': callback['code'], 'code_verifier': verifier, 'redirect_uri': redirect, 'resource': RESOURCE})
            response.raise_for_status()
            tokens = response.json()
            discovery = client.get(ISSUER + '/.well-known/openid-configuration')
            discovery.raise_for_status()
            jwks_url = discovery.json()['jwks_uri']
            if not jwks_url.startswith(ISSUER + '/'):
                raise ValueError('Unexpected identity key source')
            keys = client.get(jwks_url)
            keys.raise_for_status()
            identity = validate_identity(tokens['id_token'], client_id, nonce, keys.json(), previous)
            record = token_record(tokens, previous, client_id=client_id, host_id=host_id, identity=identity)
            if not record.get('refresh_token'):
                raise ValueError('A renewable session is required')
            private_json(destination, record)
            private_json(registration, {key: record[key] for key in ('client_id', 'subject', 'ext_agent_host_id')})
    except Exception:
        raise SystemExit('Subscription verification did not complete. Nothing was replaced; retry sign-in.') from None
    print('Fri is connected to your ChatGPT plan. Credentials are saved privately for secure transfer.')
    print('You can manage Fri access and plan usage at https://chatgpt.com/settings/usage')


if __name__ == '__main__':
    main()
