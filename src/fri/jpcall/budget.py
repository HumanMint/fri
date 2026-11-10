"""Prepaid upper bounds, never refunded on uncertain completion. No call content."""

import json
import math
import os
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

from .contract import CallRejected
from .recovery import AttemptJournal

CONTEXT_TOKENS = 4096
OUTPUT_TOKENS = 512
TEXT_OUTPUT_TOKENS = 1200
MAX_INPUT_BYTES = 32768
RATES = {'text_input', 'text_output', 'realtime_input', 'realtime_output',
         'phone_minute', 'transcription_minute', 'phone_attempt'}
MODELS = {'text_model', 'realtime_model', 'transcribe_model'}


def micros(value):
    value = Decimal(str(value))
    if not value.is_finite() or value < 0:
        raise ValueError('invalid dollar amount')
    return int((value * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


class Budget:
    def __init__(self, journal):
        self.journal = journal
        self.path = journal.directory / 'budget.json'

    def read(self):
        try:
            marker = self.journal.directory / 'budget.initialized'
            if not marker.is_file() or marker.is_symlink():
                raise ValueError
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd) as source:
                data = json.load(source, object_pairs_hook=AttemptJournal._unique)
            self._validate(data)
            return data
        except (OSError, ValueError, TypeError, KeyError):
            raise CallRejected('Calling budget is missing, corrupt or incompatible.') from None

    @staticmethod
    def _validate(data):
        if (set(data) != {'version', 'limit', 'reserved', 'tariff', 'seeded_spent', 'reservations'} or type(data['version']) is not int or data['version'] != 1
                or type(data['limit']) is not int or not 0 < data['limit'] <= 5_000_000
                or type(data['reserved']) is not int or not 0 <= data['reserved'] <= data['limit']
                or set(data['tariff']) != RATES | MODELS
                or any(type(data['tariff'][k]) is not int or data['tariff'][k] <= 0 for k in RATES)
                or any(not isinstance(data['tariff'][k], str) or not data['tariff'][k] for k in MODELS)):
            raise ValueError
        if (type(data['seeded_spent']) is not int or not 0 <= data['seeded_spent'] <= data['reserved']
                or not isinstance(data['reservations'], dict)
                or set(data['reservations']) != {'call', 'text', 'realtime', 'other'}):
            raise ValueError
        for value in data['reservations'].values():
            if (not isinstance(value, dict) or set(value) != {'count', 'amount'}
                    or any(type(value[k]) is not int or value[k] < 0 for k in ('count', 'amount'))):
                raise ValueError
        if sum(v['amount'] for v in data['reservations'].values()) != data['reserved'] - data['seeded_spent']:
            raise ValueError

    def _write(self, data):
        # The engine's recovery lock serializes all read/reserve/write operations, including restart.
        writer = AttemptJournal(self.journal.directory)
        writer.path = self.path
        writer._lock = self.journal._lock
        writer._write(data)

    def reserve(self, amount, component='other'):
        data = self.read()
        if type(amount) is not int or amount < 0 or data['reserved'] + amount > data['limit']:
            raise CallRejected('Calling budget exhausted; no paid request was sent.')
        if component not in data['reservations']:
            raise CallRejected('Unknown budget reservation component.')
        data['reserved'] += amount
        data['reservations'][component]['count'] += 1
        data['reservations'][component]['amount'] += amount
        self._write(data)

    def tariff(self, config):
        data = self.read()
        if any(data['tariff'][k] != getattr(config, k) for k in MODELS):
            raise CallRejected('Calling model has no approved budget tariff.')
        return data['tariff']

    def call(self, config):
        t = self.tariff(config)
        minutes = math.ceil(config.limits.attempt_s / 60) + 1
        self.reserve(minutes * (t['phone_minute'] + t['transcription_minute']) + t['phone_attempt'], 'call')

    def tokens(self, config, component, input_tokens, output_tokens):
        t = self.tariff(config)
        kind = 'text' if component == 'text' else 'realtime'
        amount = (((input_tokens + 1024) * t[kind + '_input'] + output_tokens * t[kind + '_output'])
                  + 999_999) // 1_000_000
        self.reserve(amount, kind)

    def status(self, config=None):
        try:
            data = self.read()
        except CallRejected:
            unseeded = not any(p.exists() or p.is_symlink() for p in (
                self.path, self.journal.directory / 'budget.initialized'))
            return {'admission': 'blocked', 'ledger_state': 'unseeded' if unseeded else 'unreadable',
                    'limit_usd': 5, 'remaining_usd': None, 'reserved_usd': None,
                    'open_reservations': None}
        result = {'admission': 'ready' if data['reserved'] < data['limit'] else 'blocked',
                  'ledger_state': 'seeded', 'limit_usd': data['limit'] / 1_000_000,
                  'remaining_usd': (data['limit'] - data['reserved']) / 1_000_000,
                  'reserved_usd': data['reserved'] / 1_000_000,
                  'open_reservations': {k: {'count': v['count'], 'upper_usd': v['amount'] / 1_000_000}
                                        for k, v in data['reservations'].items()}}
        try:
            if config is not None:
                self.tariff(config)
        except CallRejected:
            result.update(admission='blocked', reason='unapproved model tariff')
        return result

    @classmethod
    def initialize(cls, directory: Path, *, spent_usd, tariff):
        record = {'version': 1, 'limit': 5_000_000, 'reserved': micros(spent_usd),
                  'tariff': {k: micros(v) if k in RATES else v for k, v in tariff.items()}}
        record['seeded_spent'] = record['reserved']
        record['reservations'] = {k: {'count': 0, 'amount': 0}
                                  for k in ('call', 'text', 'realtime', 'other')}
        cls._validate(record)
        journal = AttemptJournal(directory)
        journal.open()  # Also excludes an active engine or concurrent initialization.
        try:
            budget = cls(journal)
            if budget.path.exists() or budget.path.is_symlink():
                raise CallRejected('Calling budget already exists; never reset it.')
            marker = directory / 'budget.initialized'
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as out:
                out.write('1\n')
                out.flush()
                os.fsync(out.fileno())
            budget._write(record)
            budget.read()
        finally:
            journal.close()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Initialize a non-resettable $5 calling budget with approved rates.')
    parser.add_argument('directory', type=Path)
    parser.add_argument('--spent-usd', required=True)
    parser.add_argument('--tariff', required=True, type=Path)
    args = parser.parse_args()
    Budget.initialize(args.directory, spent_usd=args.spent_usd,
                      tariff=json.loads(args.tariff.read_text()))
