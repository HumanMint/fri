"""Per-call, content-free accounting. No raw API events are retained or written."""

from __future__ import annotations

import json
import math
import os
import re
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

from .contract import UsageLine
from .pricing import PriceTable

COMPONENTS = frozenset({'realtime', 'transcription', 'translation', 'brief', 'summary'})


def _count(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('Usage counts must be finite nonnegative numbers')
    if not math.isfinite(value) or value < 0:
        raise ValueError('Usage counts must be finite nonnegative numbers')
    return float(value)


def _tokens(usage: dict, *, text_only: bool = False, output_text: bool = False) -> dict[str, float]:
    incoming = usage.get('input_token_details') or usage.get('input_tokens_details') or {}
    outgoing = usage.get('output_token_details') or {}
    cached = incoming.get('cached_tokens_details') or {}
    units = {}
    input_total = _count(usage.get('input_tokens', 0))
    output_total = _count(usage.get('output_tokens', 0))
    if text_only:
        cache = _count(incoming.get('cached_tokens', 0))
        if cache > input_total:
            raise ValueError('Cached usage exceeds input usage')
        return {'text_in_tokens': input_total - cache, 'cached_text_in_tokens': cache,
                'text_out_tokens': output_total}
    for kind in ('text', 'audio', 'image'):
        total = _count(incoming.get(f'{kind}_tokens', 0))
        cache = _count(cached.get(f'{kind}_tokens', 0))
        if cache > total:
            raise ValueError('Cached usage exceeds input usage')
        units[f'{kind}_in_tokens'] = total - cache
        units[f'cached_{kind}_in_tokens'] = cache
        units[f'{kind}_out_tokens'] = _count(outgoing.get(f'{kind}_tokens', 0))
    # Transcription reports a text-only output total without an output breakdown.
    if not outgoing and output_text:
        units['text_out_tokens'] = output_total
    split_in = sum(_count(incoming.get(f'{k}_tokens', 0)) for k in ('text', 'audio', 'image'))
    split_out = sum(v for k, v in units.items() if k.endswith('_out_tokens'))
    if split_in > input_total or split_out > output_total:
        raise ValueError('Usage breakdown exceeds total usage')
    if input_total > split_in:
        units['unknown_in_tokens'] = input_total - split_in
    if output_total > split_out:
        units['unknown_out_tokens'] = output_total - split_out
    cache_total = _count(incoming.get('cached_tokens', sum(cached.values())))
    if cache_total != sum(_count(v) for v in cached.values()):
        # An unsplit cache cannot safely be billed as regular text/audio input.
        units['unknown_cached_in_tokens'] = cache_total
    return {kind: value for kind, value in units.items() if value}


class UsageMeter:
    def __init__(self, prices: PriceTable | None = None):
        self.prices = prices or PriceTable()
        self._units: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
        self._unknown: set[tuple[str, str]] = set()
        self._seen: set[tuple[str, str]] = set()
        self._twilio: UsageLine | None = None
        self._streams: UsageLine | None = None
        self._twilio_pending = False

    def _add(self, component: str, model: str, units: dict[str, float],
             event_id: str = '', unknown: bool = False) -> None:
        if component not in COMPONENTS or not re.fullmatch(r'[a-zA-Z0-9._-]{1,100}', model):
            raise ValueError('Unknown usage component or invalid model identifier')
        if event_id and (component, event_id) in self._seen:
            return
        target = self._units[component, model]
        for kind, count in units.items():
            target[kind] = target.get(kind, 0) + _count(count)
        if unknown:
            self._unknown.add((component, model))
        if event_id:
            self._seen.add((component, event_id))

    def realtime(self, event: dict, model: str) -> None:
        response = event.get('response') or {}
        usage = response.get('usage')
        self._add('realtime', model, _tokens(usage) if usage else {},
                  response.get('id', ''), unknown=not usage)

    def transcription(self, usage: dict | None, model: str, item_id: str = '') -> None:
        if usage and usage.get('type') == 'duration':
            units = {'minutes': _count(usage.get('seconds', 0)) / 60}
        else:
            units = _tokens(usage, output_text=True) if usage else {}
        self._add('transcription', model, units, item_id, unknown=not usage)

    def text(self, component: str, model: str, response: dict) -> None:
        usage = response.get('usage')
        self._add(component, model, _tokens(usage, text_only=True) if usage else {},
                  response.get('id', ''), unknown=not usage)

    def twilio(self, duration_s: float, *, posted_usd: float | None = None,
               exact: bool = False, pending: bool = True,
               streams_duration_s: float | None = None) -> None:
        """Replace a cumulative estimate with the posted USD charge; never add it twice.

        ``None`` while pending is deliberate: an estimate can be requested with
        pending=False. Billing increments are whole minutes, including Media Streams.
        Media Streams remains an estimate independent of the posted voice-leg price.
        """
        minutes = math.ceil(_count(duration_s) / 60)
        streams = math.ceil(_count(duration_s if streams_duration_s is None
                                   else streams_duration_s) / 60)
        if exact and posted_usd is None:
            raise ValueError('An exact Twilio charge requires a posted USD price')
        if self._twilio is not None and self._twilio.exact and not exact:
            return
        cost = (_count(posted_usd) if posted_usd is not None else
                None if pending else minutes * self.prices.twilio_per_minute)
        self._twilio = UsageLine('twilio', 'twilio-voice', {'minutes': float(minutes)},
                                 cost, exact)
        self._twilio_pending = pending and not exact
        self._streams = UsageLine('media_streams', 'twilio-media-streams',
                                  {'minutes': float(streams)},
                                  streams * self.prices.media_streams_per_minute)

    def lines(self) -> tuple[UsageLine, ...]:
        lines = []
        for (component, model), units in sorted(self._units.items()):
            cost = self.prices.token_cost(model, units)
            if set(units) == {'minutes'}:
                rate = self.prices.transcription_per_minute.get(model)
                cost = None if rate is None else units['minutes'] * rate
            if (component, model) in self._unknown:
                cost = None
            lines.append(UsageLine(component, model, dict(units), cost))
        return tuple(lines + [line for line in (self._twilio, self._streams) if line is not None])

    @property
    def usage_final(self) -> bool:
        return not self._twilio_pending and all(line.usd is not None for line in self.lines())

    def write_ledger(self, data_dir: Path) -> None:
        """Append an anonymous aggregate snapshot, without request/user/call IDs."""
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        record = {'prices_as_of': self.prices.as_of, 'usage_final': self.usage_final,
                  'usage': [asdict(line) for line in self.lines()]}
        fd = os.open(data_dir / 'jpcall-usage.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as ledger:
            ledger.write(json.dumps(record, allow_nan=False) + '\n')
