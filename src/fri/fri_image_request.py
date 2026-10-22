"""Keep a full current image brief separate from a short research-receipt label."""

import re

from .web_policy import public_query

MAX_REQUEST = 4000


def image_request(query, message):
    # Empty query asks the harness to bind the real current request. A supplied
    # excerpt must still belong to that request; never accept inferred/private text.
    if not isinstance(query, str) or (query and not public_query(
            query, message, max_length=MAX_REQUEST, allow_newlines=True)):
        return ''
    if not public_query(message, message, max_length=MAX_REQUEST, allow_newlines=True):
        return ''
    return message  # A short excerpt cannot discard the user's later constraints.


def receipt_query(message):
    # Receipts retain the existing bounded exact-excerpt contract. This is only a
    # lookup label: planning and visual inspection receive the COMPLETE request.
    for line in sorted(message.splitlines(), key=len, reverse=True):
        # End at complete words so a public URL cannot become an invalid host.
        ends = [part.end() for part in re.finditer(r'\S+', line) if part.end() <= 240]
        for end in reversed(ends):
            label = line[:end].strip()
            if public_query(label, message):
                return label
        # Long URLs or languages without spaces can have no complete first word.
        # A prefix is still a label, never a download address; validate it normally.
        label = line[:240].strip()
        if public_query(label, message):
            return label
    raise ValueError('No public image request label')
