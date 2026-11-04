"""Conservative Japanese dialing validation, with no number in rejection text."""

import re
import unicodedata
from collections.abc import Collection

from .contract import CallRejected


def normalize_number(number: str, test_numbers: Collection[str] = ()) -> str:
    normalized = unicodedata.normalize('NFKC', number).strip()
    if not re.fullmatch(r'[+0-9\s()\-]+', normalized):
        raise CallRejected('Please enter a Japanese phone number without an extension.')
    compact = re.sub(r'[\s()\-]', '', normalized)
    if compact.startswith('+81'):
        national = compact[3:]
    elif compact.startswith('+'):
        if re.fullmatch(r'\+[1-9][0-9]{7,14}', compact) and compact in test_numbers:
            return compact
        raise CallRejected('Only Japanese numbers or approved test numbers can be called.')
    else:
        if compact.startswith('1') or len(compact) <= 3:
            raise CallRejected('Emergency and short service numbers cannot be called.')
        national = compact[1:] if compact.startswith('0') else ''
    if national.startswith('1') and not (national.startswith('120') and len(national) == 9):
        raise CallRejected('Emergency and short service numbers cannot be called.')
    if national.startswith('990'):
        raise CallRejected('Premium-rate numbers cannot be called.')
    if not re.fullmatch(r'[1-9][0-9]{8,9}', national):
        raise CallRejected('Please enter a complete Japanese phone number.')
    return '+81' + national
