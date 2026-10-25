"""Conservative text minimization before storage or model input."""

import json
import re

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


# The only characters that reach the model raw: none looks like another, or like ASCII, alone or in a
# row. Emoji are Emoji_Presentation in Unicode 14.0 (Python 3.11's version; Discord draws each as its own
# image) without the regional indicators, since pairs of them make identical flags (🇫🇷 and Clipperton's
# 🇨🇵), plus U+2764; then Hangul syllables, Hangul compatibility jamo (ㅋㅋ) without the blank filler,
# and curly quotes. Other scripts and symbols have pixel-identical pairs (Latin A and Cyrillic А, the
# Kelvin sign and K, combining marks), so a row of them could spell bits a person can't see; they stay
# escaped.
RAW = re.compile(
    '[\u2018\u2019\u201c\u201d\u3131-\u3163\u3165-\u318e\uac00-\ud7a3\u2764'
    '\u231a\u231b\u23e9-\u23ec\u23f0\u23f3\u25fd\u25fe\u2614\u2615\u2648-\u2653\u267f\u2693\u26a1'
    '\u26aa\u26ab\u26bd\u26be\u26c4\u26c5\u26ce\u26d4\u26ea\u26f2\u26f3\u26f5\u26fa\u26fd\u2705'
    '\u270a\u270b\u2728\u274c\u274e\u2753-\u2755\u2757\u2795-\u2797\u27b0\u27bf\u2b1b\u2b1c\u2b50'
    '\u2b55\U0001f004\U0001f0cf\U0001f18e\U0001f191-\U0001f19a\U0001f201'
    '\U0001f21a\U0001f22f\U0001f232-\U0001f236\U0001f238-\U0001f23a\U0001f250\U0001f251'
    '\U0001f300-\U0001f320\U0001f32d-\U0001f335\U0001f337-\U0001f37c\U0001f37e-\U0001f393'
    '\U0001f3a0-\U0001f3ca\U0001f3cf-\U0001f3d3\U0001f3e0-\U0001f3f0\U0001f3f4\U0001f3f8-\U0001f43e'
    '\U0001f440\U0001f442-\U0001f4fc\U0001f4ff-\U0001f53d\U0001f54b-\U0001f54e\U0001f550-\U0001f567'
    '\U0001f57a\U0001f595\U0001f596\U0001f5a4\U0001f5fb-\U0001f64f\U0001f680-\U0001f6c5\U0001f6cc'
    '\U0001f6d0-\U0001f6d2\U0001f6d5-\U0001f6d7\U0001f6dd-\U0001f6df\U0001f6eb\U0001f6ec'
    '\U0001f6f4-\U0001f6fc\U0001f7e0-\U0001f7eb\U0001f7f0\U0001f90c-\U0001f93a\U0001f93c-\U0001f945'
    '\U0001f947-\U0001f9ff\U0001fa70-\U0001fa74\U0001fa78-\U0001fa7c\U0001fa80-\U0001fa86'
    '\U0001fa90-\U0001faac\U0001fab0-\U0001faba\U0001fac0-\U0001fac5\U0001fad0-\U0001fad9'
    '\U0001fae0-\U0001fae7\U0001faf0-\U0001faf6]')
NOT_PRINTABLE_ASCII = re.compile(r'[^\x20-\x7e]')


def model_json(value):
    """JSON for model input with emoji, Korean and curly quotes as written, so an emoji is one character
    instead of a 12-character \\u escape. Everything else outside printable ASCII stays the escape
    json.dumps writes: other scripts and symbols (they have lookalikes), combining marks, variation
    selectors, controls, format and invisible characters, spaces and separators. json.dumps does the
    traversal, key conversion and errors, and its output keeps every non-ASCII character inside a string,
    so ASCII-only output is exactly json.dumps'."""
    text = json.dumps(value, ensure_ascii=False)
    return NOT_PRINTABLE_ASCII.sub(lambda match: match.group() if RAW.match(match.group()) else
                                   json.dumps(match.group())[1:-1], text)
