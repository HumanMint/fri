"""Explicit opt-in configuration; importing Fri never reads credentials."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .contract import CallRejected, Limits
from .numbers import normalize_number


@dataclass(frozen=True)
class JpcallConfig:
    openai_api_key: str = field(default='', repr=False)
    twilio_account_sid: str = field(default='', repr=False)
    twilio_api_key_sid: str = field(default='', repr=False)
    twilio_api_key_secret: str = field(default='', repr=False)
    # Webhooks use the account Auth Token, never the REST API key secret.
    twilio_auth_token: str = field(default='', repr=False)
    twilio_from_number: str = field(default='', repr=False)
    public_base_url: str = ''
    listen_host: str = '127.0.0.1'
    listen_port: int = 8790
    realtime_model: str = 'gpt-realtime-2.1'
    realtime_voice: str = ''
    transcribe_model: str = 'gpt-realtime-whisper'
    text_model: str = 'gpt-6.1-terra'
    text_effort: str = 'low'
    limits: Limits = field(default_factory=Limits, repr=False)
    disabled_reasons: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return not self.disabled_reasons and all((
            self.openai_api_key, self.twilio_account_sid, self.twilio_api_key_sid,
            self.twilio_api_key_secret, self.twilio_auth_token, self.twilio_from_number,
            self.public_base_url, self.realtime_voice, self.transcribe_model, self.text_model,
        ))

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> JpcallConfig:
        env = os.environ if environ is None else environ
        errors = []
        values = {}
        required = {
            'openai_api_key': 'JPCALL_OPENAI_API_KEY',
            'twilio_account_sid': 'TWILIO_ACCOUNT_SID',
            'twilio_api_key_sid': 'TWILIO_API_KEY_SID',
            'twilio_api_key_secret': 'TWILIO_API_KEY_SECRET',
            'twilio_auth_token': 'TWILIO_AUTH_TOKEN',
            'twilio_from_number': 'TWILIO_FROM_NUMBER',
            'public_base_url': 'JPCALL_PUBLIC_BASE_URL',
            'realtime_voice': 'JPCALL_REALTIME_VOICE',
        }
        for field_name, name in required.items():
            values[field_name] = env.get(name, '').strip()
            if not values[field_name]:
                errors.append(f'Missing {name}')
        try:
            url = urlsplit(values['public_base_url'])
            if (url.scheme != 'https' or not url.hostname or url.username or url.password
                    or url.query or url.fragment or url.path not in ('', '/')):
                raise ValueError
            url.port
        except ValueError:
            errors.append('JPCALL_PUBLIC_BASE_URL must be an https origin')
            values['public_base_url'] = ''
        values['public_base_url'] = values['public_base_url'].rstrip('/')
        if not re.fullmatch(r'\+[1-9][0-9]{7,14}', values['twilio_from_number']):
            errors.append('TWILIO_FROM_NUMBER must be E.164')
        for field_name, name, default in (
            ('listen_host', 'JPCALL_LISTEN_HOST', '127.0.0.1'),
            ('realtime_model', 'JPCALL_REALTIME_MODEL', 'gpt-realtime-2.1'),
            ('text_effort', 'JPCALL_TEXT_EFFORT', 'low'),
            ('transcribe_model', 'JPCALL_TRANSCRIBE_MODEL', 'gpt-realtime-whisper'),
            ('text_model', 'JPCALL_TEXT_MODEL', 'gpt-6.1-terra'),
        ):
            values[field_name] = env.get(name, default).strip()
            if not values[field_name]:
                errors.append(f'{name} must not be empty')

        def positive(name: str, default: int, maximum: int | None = None) -> int:
            try:
                value = int(env.get(name, str(default)))
                if value <= 0 or (maximum is not None and value > maximum):
                    raise ValueError
                return value
            except ValueError:
                errors.append(f'{name} must be a positive integer'
                              + (f' at most {maximum}' if maximum else ''))
                return default

        values['listen_port'] = positive('JPCALL_LISTEN_PORT', 8790, 65535)
        try:
            ids = frozenset(int(x) for x in re.split(r'[,\s]+',
                            env.get('JPCALL_ALLOWED_USER_IDS', '').strip()) if x)
            if any(x <= 0 for x in ids):
                raise ValueError
        except ValueError:
            errors.append('JPCALL_ALLOWED_USER_IDS must contain positive integer IDs')
            ids = frozenset()
        try:
            tests = frozenset(x.strip() for x in
                              env.get('JPCALL_TEST_NUMBERS', '').split(',') if x.strip())
            tests = frozenset(normalize_number(x, tests) for x in tests)
        except (ValueError, CallRejected):
            errors.append('JPCALL_TEST_NUMBERS must contain safe E.164 numbers')
            tests = frozenset()
        values['limits'] = Limits(
            max_call_s=positive('JPCALL_MAX_CALL_S', 900),
            max_hold_s=positive('JPCALL_MAX_HOLD_S', 60),
            hold_apology_every_s=positive('JPCALL_HOLD_APOLOGY_EVERY_S', 15),
            max_calls_per_user_per_day=positive('JPCALL_MAX_CALLS_PER_USER_PER_DAY', 5),
            allowed_user_ids=ids, test_numbers=tests,
        )
        return cls(**values, disabled_reasons=tuple(errors))
