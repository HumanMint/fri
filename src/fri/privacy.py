"""Conservative text minimization before storage or model input."""

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
