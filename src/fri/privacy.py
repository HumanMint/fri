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


# Invisible (default-ignorable) letters and marks, blank symbols, and every variation selector.
INVISIBLE = re.compile('[\u034f\u115f\u1160\u17b4\u17b5\u180b-\u180d\u180f\u2800\u3164\uffa0'
                       '\ufe00-\ufe0f\U0001d159\U000e0100-\U000e01ef]')
# Emoji bases outside the So category that take U+FE0E/U+FE0F: double exclamation, info, arrows, wavy dash.
EMOJI_BASES = '\u203c\u2049\u2139\u2194\u2195\u2196\u2197\u2198\u2199\u21a9\u21aa\u3030\u303d'
NOT_PRINTABLE_ASCII = re.compile(r'[^\x20-\x7e]')


def visible(text, at):
    char = text[at]
    if char in '\ufe0e\ufe0f':
        # Kept only as an emoji's text or emoji presentation, so selectors after letters, after one
        # another or after nothing can't carry hidden text.
        before = text[at-1] if at else ''
        return bool(before) and (
            (unicodedata.category(before) == 'So' or before in EMOJI_BASES) and not INVISIBLE.match(before) or
            before in '0123456789#*' and text[at+1:at+2] == '\u20e3')  # A keycap: 1️⃣.
    category = unicodedata.category(char)
    return (category[0] in 'LMNPS' or category == 'Zs') and not INVISIBLE.match(char)


def model_text(text):
    """A JSON string literal: visible characters raw, everything else escaped as json.dumps does."""
    parts, start = [], 0
    for match in NOT_PRINTABLE_ASCII.finditer(text):
        at = match.start()
        if not visible(text, at):
            parts += [json.dumps(text[start:at], ensure_ascii=False)[1:-1], json.dumps(text[at])[1:-1]]
            start = at+1
    return '"'+''.join(parts)+json.dumps(text[start:], ensure_ascii=False)[1:]


def model_json(value):
    """JSON for model input with visible text as written: emoji, curly quotes and non-English letters
    stay raw instead of becoming \\u escapes. Controls, format and invisible characters (bidi, zero-width,
    tags), line separators, surrogates, private-use and unassigned code points stay escaped. The output
    matches json.dumps for ASCII-only values."""
    if isinstance(value, str):
        return model_text(value)
    if isinstance(value, dict):
        return '{'+', '.join(model_text(k if isinstance(k, str) else json.dumps(k))+': '+model_json(v)
                             for k, v in value.items())+'}'
    if isinstance(value, (list, tuple)):
        return '['+', '.join(model_json(v) for v in value)+']'
    return json.dumps(value)
