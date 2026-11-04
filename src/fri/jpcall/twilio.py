"""Small Twilio REST client and request authentication, without the Twilio SDK."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from xml.etree.ElementTree import Element, SubElement, tostring

import httpx

from .config import JpcallConfig
from .numbers import normalize_number


class TwilioError(RuntimeError):
    """Fixed, safe error text; upstream bodies and authenticated URLs stay private."""


def validate_signature(auth_token: str, url: str, params: Iterable[tuple[str, str]],
                       signature: str) -> bool:
    if not auth_token or not signature or not signature.isascii():
        return False
    grouped = defaultdict(set)
    for key, value in params:
        grouped[key].add(value)
    payload = url + ''.join(key + value for key in sorted(grouped)
                            for value in sorted(grouped[key]))
    expected = base64.b64encode(hmac.new(auth_token.encode(), payload.encode(),
                                        hashlib.sha1).digest()).decode()
    return hmac.compare_digest(expected, signature)


@dataclass(frozen=True)
class CallPrice:
    duration_s: float | None
    usd: float | None


class TwilioClient:
    def __init__(self, config: JpcallConfig, client: httpx.AsyncClient | None = None):
        self.config = config
        self._owned = client is None
        self._client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)

    async def close(self) -> None:
        if self._owned:
            await self._client.aclose()

    async def _request(self, method: str, suffix: str, data=None) -> dict:
        base = f'https://api.twilio.com/2010-04-01/Accounts/{self.config.twilio_account_sid}'
        try:
            response = await self._client.request(
                method, base + '/Calls' + suffix, data=data,
                auth=httpx.BasicAuth(self.config.twilio_api_key_sid,
                                    self.config.twilio_api_key_secret),
                follow_redirects=False,
            )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (httpx.HTTPError, ValueError):
            raise TwilioError('The phone service request failed.') from None

    @staticmethod
    def _sid(sid: str) -> str:
        if not re.fullmatch(r'CA[0-9a-fA-F]{32}', sid):
            raise TwilioError('The phone service returned an invalid call identifier.')
        return sid

    async def create_call(self, number: str, media_token: str, status_token: str) -> str:
        if not self.config.enabled:
            raise TwilioError('Calling is disabled until its configuration is complete.')
        number = normalize_number(number, self.config.limits.test_numbers)
        for token in (media_token, status_token):
            if not re.fullmatch(r'[a-zA-Z0-9_-]{32,128}', token):
                raise ValueError('Invalid call routing token')
        root = Element('Response')
        connect = SubElement(root, 'Connect')
        base = self.config.public_base_url
        SubElement(connect, 'Stream', url=base.replace('https://', 'wss://', 1)
                   + '/jpcall/media/' + media_token)
        SubElement(root, 'Hangup')
        result = await self._request('POST', '.json', data={
            'To': number, 'From': self.config.twilio_from_number,
            'Twiml': tostring(root, encoding='unicode'),
            'StatusCallback': base + '/jpcall/status/' + status_token,
            'StatusCallbackMethod': 'POST',
            'StatusCallbackEvent': ['initiated', 'ringing', 'answered', 'completed'],
            'TimeLimit': str(self.config.limits.max_call_s),
        })
        return self._sid(result.get('sid', ''))

    async def hang_up(self, call_sid: str) -> None:
        await self._request('POST', '/' + self._sid(call_sid) + '.json',
                            data={'Status': 'completed'})

    async def call_price(self, call_sid: str) -> CallPrice:
        result = await self._request('GET', '/' + self._sid(call_sid) + '.json')
        try:
            raw_duration = result.get('duration')
            duration = Decimal(str(raw_duration)) if raw_duration is not None else None
            raw_price = result.get('price')
            price = Decimal(str(raw_price)) if raw_price is not None else None
            if duration is not None and (not duration.is_finite() or duration < 0):
                raise ValueError
            if price is not None and not price.is_finite():
                raise ValueError
            return CallPrice(None if duration is None else float(duration),
                             float(abs(price)) if price is not None
                             and result.get('price_unit', '').upper() == 'USD' else None)
        except (InvalidOperation, ValueError):
            raise TwilioError('The phone service returned invalid billing data.') from None
