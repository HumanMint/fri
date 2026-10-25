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
TOKEN = re.compile(r'\\(?:u[0-9a-f]{4}|.)|[^\x20-\x7e]')  # An escape json.dumps wrote, or one non-ASCII character.
CONJOINING = re.compile('[\u1160-\u11ff\ud7b0-\ud7ff]')  # Hangul vowel and final jamo join the syllable before.


def visible(char):
    return unicodedata.category(char)[0] in 'LMNPS' and not INVISIBLE.match(char)


def joins(char):
    return unicodedata.category(char)[0] == 'M' or bool(CONJOINING.match(char))


def model_json(value):
    """JSON for model input with visible text as written: emoji, curly quotes and non-English letters
    stay raw instead of becoming \\u escapes. Controls, format and invisible characters (bidi, zero-width,
    tags, variation selectors), non-ASCII spaces, line separators, surrogates, private-use and unassigned
    code points stay escaped, and so does anything that would look like something else: a character NFC
    replaces (the Kelvin sign looks like K), and every combining mark (or Hangul vowel or final jamo) but
    the first on a raw character, where it leaves the pair normalized. A second mark can repeat the first
    or swap order with it, é can arrive composed or not, and a mark on an escape or a string's opening
    quote would draw on JSON's own text. json.dumps does the traversal, key conversion and errors, and its
    output keeps every non-ASCII character inside a string, so ASCII-only output is exactly json.dumps'."""
    text = json.dumps(value, ensure_ascii=False)
    ends = {'token': -1, 'base': -1}  # Where the last escape or non-ASCII character, and the last raw base, end.

    def written(match):
        char, start = match.group(), match.start()
        if len(char) > 1:
            keep = True  # json.dumps' own escape.
        elif joins(char):
            # Unless a token ends here, the character before is plain ASCII: content, or the opening quote.
            base = start == ends['base'] if start == ends['token'] else text[start-1] != '"'
            keep = base and visible(char) and unicodedata.is_normalized('NFC', text[start-1:start+1])
        else:
            keep = visible(char) and unicodedata.is_normalized('NFC', char)
            if keep:
                ends['base'] = match.end()
        ends['token'] = match.end()
        return char if keep else json.dumps(char)[1:-1]
    return TOKEN.sub(written, text)
