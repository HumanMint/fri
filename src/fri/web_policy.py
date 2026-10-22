"""Pure validation for public web queries and URLs; no model or login dependencies."""

import ipaddress
import re
from urllib.parse import urlsplit


def public_url(value):
    if not isinstance(value, str) or len(value) > 350 or re.search(r'[\s<>\\()]', value):
        return False
    try:
        url = urlsplit(value)
        host = url.hostname or ''
        if (url.scheme not in ('http', 'https') or url.username or url.password or
                url.port not in (None, 80, 443) or '.' not in host or
                host.endswith(('.local', '.internal', '.localhost', '.test', '.invalid', '.onion'))):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return bool(re.fullmatch(r'[a-z0-9.-]+', host) and re.search('[a-z]', host))
    except ValueError:
        return False


def public_query(query, message, *, max_length=240, allow_newlines=False):
    # Exact current-message excerpts prevent a model from exporting saved history/notes.
    if not isinstance(query, str) or not 3 <= len(query) <= max_length or query not in message:
        return False
    for address in re.findall(r'\b\w+://\S+', query):
        if not public_url(address):
            return False
    if (not allow_newlines and re.search(r'[\n\r]', query)) or re.search(r'@|\d{15,}|\b(?:bearer|password|secret|token|api[_ -]?key)\s*[:=]|'
                 r'\b(?:sk-|gh[pousr]_|eyJ)[A-Za-z0-9_-]{12,}', query, re.I):
        return False
    # URL-shaped input is checked as a public address; no local hosts/IPs or URL credentials.
    for address in re.findall(r'\b(?:[\w.-]+\.[a-zA-Z]{2,}|(?:\d{1,3}\.){3}\d{1,3})(?::\d+)?(?:/[^\s]*)?', query):
        if not public_url('https://'+address):
            return False
    return not re.search(r'\b(?:localhost|127\.\d+\.\d+\.\d+|10\.\d+\.\d+\.\d+)\b', query, re.I)


