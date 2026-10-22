"""Red's audio client logs its connection headers; redact known service secrets first."""

from __future__ import annotations

import logging
import os


def install_secret_filter():
    original = logging.getLogRecordFactory()
    secrets = tuple(value for key, value in os.environ.items()
                    if len(value) >= 12 and (key.endswith('_TOKEN') or key.endswith('_PASSWORD')))

    def factory(*args, **kwargs):
        record = original(*args, **kwargs)
        text = record.getMessage()
        for secret in secrets:
            text = text.replace(secret, '[REDACTED]')
        record.msg = text
        record.args = ()
        return record

    logging.setLogRecordFactory(factory)

