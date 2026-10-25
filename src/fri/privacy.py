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
# The 359 bases of Unicode's emoji-variation-sequences.txt (Emoji 15.1, keycaps aside): the only
# characters U+FE0E/U+FE0F visibly switch between text and emoji style.
EMOJI_BASES = re.compile(
    '[\u00a9\u00ae\u203c\u2049\u2122\u2139\u2194-\u2199\u21a9-\u21aa\u231a-\u231b\u2328\u23cf'
    '\u23e9-\u23f3\u23f8-\u23fa\u24c2\u25aa-\u25ab\u25b6\u25c0\u25fb-\u25fe\u2600-\u2604\u260e'
    '\u2611\u2614-\u2615\u2618\u261d\u2620\u2622-\u2623\u2626\u262a\u262e-\u262f\u2638-\u263a'
    '\u2640\u2642\u2648-\u2653\u265f-\u2660\u2663\u2665-\u2666\u2668\u267b\u267e-\u267f'
    '\u2692-\u2697\u2699\u269b-\u269c\u26a0-\u26a1\u26a7\u26aa-\u26ab\u26b0-\u26b1\u26bd-\u26be'
    '\u26c4-\u26c5\u26c8\u26ce-\u26cf\u26d1\u26d3-\u26d4\u26e9-\u26ea\u26f0-\u26f5\u26f7-\u26fa'
    '\u26fd\u2702\u2705\u2708-\u270d\u270f\u2712\u2714\u2716\u271d\u2721\u2728\u2733-\u2734\u2744'
    '\u2747\u274c\u274e\u2753-\u2755\u2757\u2763-\u2764\u2795-\u2797\u27a1\u27b0\u27bf'
    '\u2934-\u2935\u2b05-\u2b07\u2b1b-\u2b1c\u2b50\u2b55\u3030\u303d\u3297\u3299\U0001f004'
    '\U0001f170-\U0001f171\U0001f17e-\U0001f17f\U0001f202\U0001f21a\U0001f22f\U0001f237'
    '\U0001f30d-\U0001f30f\U0001f315\U0001f31c\U0001f321\U0001f324-\U0001f32c\U0001f336\U0001f378'
    '\U0001f37d\U0001f393\U0001f396-\U0001f397\U0001f399-\U0001f39b\U0001f39e-\U0001f39f'
    '\U0001f3a7\U0001f3ac-\U0001f3ae\U0001f3c2\U0001f3c4\U0001f3c6\U0001f3ca-\U0001f3ce'
    '\U0001f3d4-\U0001f3e0\U0001f3ed\U0001f3f3\U0001f3f5\U0001f3f7\U0001f408\U0001f415\U0001f41f'
    '\U0001f426\U0001f43f\U0001f441-\U0001f442\U0001f446-\U0001f449\U0001f44d-\U0001f44e'
    '\U0001f453\U0001f46a\U0001f47d\U0001f4a3\U0001f4b0\U0001f4b3\U0001f4bb\U0001f4bf\U0001f4cb'
    '\U0001f4da\U0001f4df\U0001f4e4-\U0001f4e6\U0001f4ea-\U0001f4ed\U0001f4f7'
    '\U0001f4f9-\U0001f4fb\U0001f4fd\U0001f508\U0001f50d\U0001f512-\U0001f513'
    '\U0001f549-\U0001f54a\U0001f550-\U0001f567\U0001f56f-\U0001f570\U0001f573-\U0001f579'
    '\U0001f587\U0001f58a-\U0001f58d\U0001f590\U0001f5a5\U0001f5a8\U0001f5b1-\U0001f5b2\U0001f5bc'
    '\U0001f5c2-\U0001f5c4\U0001f5d1-\U0001f5d3\U0001f5dc-\U0001f5de\U0001f5e1\U0001f5e3'
    '\U0001f5e8\U0001f5ef\U0001f5f3\U0001f5fa\U0001f610\U0001f687\U0001f68d\U0001f691\U0001f694'
    '\U0001f698\U0001f6ad\U0001f6b2\U0001f6b9-\U0001f6ba\U0001f6bc\U0001f6cb\U0001f6cd-\U0001f6cf'
    '\U0001f6e0-\U0001f6e5\U0001f6e9\U0001f6f0\U0001f6f3]')
NOT_PRINTABLE_ASCII = re.compile(r'[^\x20-\x7e]')
STRING = re.compile(r'"(?:[^"\\]|\\.)*"', re.S)  # A string literal in json.dumps output.


def visible(text, at):
    char = text[at]
    if char in '\ufe0e\ufe0f':
        # Kept only straight after a character it visibly restyles, so selectors anywhere else
        # (after other characters, after one another, at the start) can't carry hidden text.
        before = text[at-1] if at else ''
        keycap = before in tuple('0123456789#*') and text[at+1:at+2] == '\u20e3'  # 1️⃣
        return keycap or bool(EMOJI_BASES.fullmatch(before))
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
    tags), line separators, surrogates, private-use and unassigned code points stay escaped.
    json.dumps does the traversal, key conversion and errors; each string literal is then rewritten
    from its own decoded text, so ASCII-only output is exactly json.dumps'."""
    return STRING.sub(lambda match: model_text(json.loads(match.group())), json.dumps(value, ensure_ascii=False))
