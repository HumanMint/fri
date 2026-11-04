"""Lazy engine configuration for the standalone and main Discord identities."""

import re


def load_config():
    from .config import load_config as load
    return load()


def optional_config():
    try:
        return load_config()
    except ModuleNotFoundError as exc:
        # The Discord branch can run before the separately owned engine is integrated.
        # Never hide a missing dependency inside an installed config module.
        if exc.name != 'fri.jpcall.config':
            raise
        return None


def disabled_message(config):
    # Only setting names reach the terminal, even if a future validator includes values.
    names = sorted({name for reason in config.disabled_reasons
                    for name in re.findall(r'\b(?:JPCALL|TWILIO)_[A-Z0-9_]+\b', reason)})
    return ('jpcall is disabled. check ' + ', '.join(names) + '.' if names
            else 'jpcall is disabled. check the required call settings.')
