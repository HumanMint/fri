"""Conservative text minimization before storage or model input."""

import json
import re
import unicodedata

TOKEN_PATTERNS = [
    re.compile(r'(?i)\b(?:mfa\.[\w-]{20,}|[\w-]{23,28}\.[\w-]{6}\.[\w-]{25,})\b'),
    re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,})\b'),
    re.compile(r'\bsk-[A-Za-z0-9_-]{16,}\b'),
    re.compile(r'(?i)(?:authorization\s*[:=]\s*(?:bearer|bot)\s+)\S+'),
    re.compile(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', re.S),
]
SECRET_NAME = r'(?:password|passwd|token|api[_ -]?key|secret)'
ASSIGNMENT_PATTERN = re.compile(r'(?im)\b' + SECRET_NAME + r'\s*[:=]\s*[\'\"]?[^\s\'\"]{6,}')
PATTERNS = [*TOKEN_PATTERNS, ASSIGNMENT_PATTERN]


def source_has_credentials(text: str) -> bool:
    """Check code without treating environment lookups as hardcoded credentials.

    Text submitted by users still uses the more conservative redactor. In source,
    a credential assignment must contain a literal; ``token = os.getenv(...)``
    is a reference to a secret, not the secret itself. Known token formats are
    detected anywhere, including comments.
    """
    literal_assignment = re.compile(
        r'(?im)\b' + SECRET_NAME + r'[\'\"]?\s*(?::\s*str\s*)?[:=]\s*[rubf]*'
        r'([\'\"])([^\n\'\"]{6,})\1')
    if any(pattern.search(text) for pattern in TOKEN_PATTERNS):
        return True
    return literal_assignment.search(text) is not None


def redact(text: str) -> str:
    for pattern in PATTERNS:
        text = pattern.sub('[REDACTED]', text)
    return text.replace('\x00', '')


def clean(text: str, limit: int = 6000) -> str:
    return redact(text.strip())[:limit]


# Invisible (default-ignorable) letters and marks, blank or zero-width symbols, and every variation
# selector: even an emoji's U+FE0E/U+FE0F often changes nothing visible, so their presence could carry
# hidden bits. Non-ASCII spaces (category Zs) stay escaped too: their widths differ, so a gap could.
INVISIBLE = re.compile('[\u034f\u115f\u1160\u17b4\u17b5\u180b-\u180d\u180f\u2800\u303f\u3164\uffa0'
                       '\ufe00-\ufe0f\ufffc\U00013441\U00013442\U00016fe4\U0001d159\U0001da7f\U0001da80'
                       '\U000e0100-\U000e01ef]')
NOT_PRINTABLE_ASCII = re.compile(r'[^\x20-\x7e]')
CONJOINING = re.compile('[\u1160-\u11ff\ud7b0-\ud7ff]')  # Hangul vowel and final jamo join the syllable before.


def visible(char):
    return unicodedata.category(char)[0] in 'LMNPS' and not INVISIBLE.match(char)


def joins(char):
    return unicodedata.category(char)[0] == 'M' or bool(CONJOINING.match(char))


def redundant(text, index):
    """Only a character's first mark (or conjoining jamo) arrives raw, and only when it leaves the
    character normalized. A second mark can repeat the first or swap order with it, and é can arrive
    composed or not, all looking alike, so those differences could carry hidden bits."""
    if not joins(text[index]):
        return False
    return index == 0 or joins(text[index-1]) or not unicodedata.is_normalized('NFC', text[index-1:index+1])


def model_json(value):
    """JSON for model input with visible text as written: emoji, curly quotes and non-English letters
    stay raw instead of becoming \\u escapes. Controls, format and invisible characters (bidi, zero-width,
    tags, variation selectors), non-ASCII spaces, a character's second and later combining marks, line
    separators, surrogates, private-use and unassigned code points stay escaped. json.dumps does the traversal, key conversion and errors, and
    its output keeps every non-ASCII character inside a string, so ASCII-only output is exactly
    json.dumps'."""
    text = json.dumps(value, ensure_ascii=False)
    return NOT_PRINTABLE_ASCII.sub(lambda match: match.group() if visible(match.group()) and not
                                   redundant(text, match.start()) else json.dumps(match.group())[1:-1], text)
