"""Explicit usage requests and compact, deterministic reports without inference."""

import re
from collections import defaultdict

SUBJECT = r'(?:(?:my|our|your|the|current|fri\'s)\s+)?(?:token\s+usage|usage|tokens\s+used|token\s+counts?|token\s+totals?)'
# Rolling windows only, each named exactly in the reply: "today" is the last 24 hours, not since midnight.
SPAN = (r'(?:(?:for|over|in|from|during)\s+)?(?:the\s+)?(?:last|past)\s+(?:[1-9][0-9]{0,2}\s*'
        r'(?:hours?|hrs?|h|days?|d|weeks?|wks?|w|months?)|hour|day|week|month)|today|this\s+(?:week|month)')
TAIL = rf'(?:\s+(?:so far|right now|rn|please|pls|by task|by model|breakdown|{SPAN}))*'
REQUEST = re.compile(
    rf'(?:{SUBJECT}|'
    rf'(?:(?:can|could|would)\s+(?:you|u)\s+)?(?:show|check|list|give|tell)\s+(?:(?:me|us)\s+)?{SUBJECT}|'
    rf'what(?:\'s| is| are)\s+{SUBJECT}|'
    r'how many tokens (?:have (?:we|you|i)|has fri) (?:used|spent))'
    + TAIL, re.IGNORECASE)

LABELS = {'conversation': 'chat', 'web_search': 'web lookups', 'attention': 'attention checks',
          'image_lookup': 'image searches and checks',
          'summary': 'learning summaries', 'exploration_plan': 'archive planning', 'meme': 'meme checks',
          'status': 'daily statuses',
          'investigation': 'investigations',
          'owner_settings': 'settings previews', 'history': 'history lookups',
          'evaluation': 'isolated tests', 'reply_check': 'reply checks'}


CAPABILITY = 'usage-window'
PERIOD = re.compile(r'(?:last|past)\s+(?:(?P<count>[1-9][0-9]{0,2})\s*)?(?P<unit>hours?|hrs?|h|days?|d|weeks?|wks?|w|months?)\b'
                    r'|(?P<today>today)|this\s+(?P<this>week|month)')
UNITS = {'h': ('hour', 3600), 'd': ('day', 86400), 'w': ('week', 7*86400), 'm': ('month', 30*86400)}


def normalized(text):
    text = ' '.join(text.casefold().replace('’', "'").split()).strip(' .!?')
    text = re.sub(r'^(?:(?:hey|yo)\s+)?fri\b[,:]?\s*', '', text)
    return re.sub(r'^(?:please|pls)\s+', '', text)


def usage_requested(text):
    return bool(REQUEST.fullmatch(normalized(text)))


def usage_window(text):
    """(seconds, label) for the rolling window an explicit request names, or None for all time."""
    match = PERIOD.search(normalized(text)) if usage_requested(text) else None
    if not match:
        return None
    if match['today']:
        return 86400, 'the last 24 hours'
    if match['this']:
        return (7*86400, 'the last 7 days') if match['this'] == 'week' else (30*86400, 'the last 30 days')
    name, seconds = UNITS['m' if match['unit'].startswith('mo') else match['unit'][0]]
    count = int(match['count'] or 1)
    return count*seconds, f'the last {name}' if count == 1 else f'the last {count} {name}s'


def usage_query(text, capabilities, now):
    """The window to ask for, its label, and whether one was asked for that this controller can't apply."""
    window = usage_window(text)
    if window is None or CAPABILITY not in capabilities:
        return {}, None, window is not None
    seconds, label = window
    return {'since': max(0.0, now-seconds)}, label, False


def describe_usage(rows, *, by_model=False, window=None, windowless=False):
    """window: the label of the period rows cover. windowless: one was asked for, but these are all-time totals."""
    if not rows:
        if window:
            return f'nothing recorded for fri in {window}. this tracker covers fri activity, not the whole subscription.'
        return (("the controller can't cut usage by time yet, and nothing's recorded for fri anyway." if windowless
                 else 'nothing recorded for fri yet.') + ' this tracker covers fri activity, not the whole subscription.')
    # Cached input and reasoning output are only summed over calls that reported them, so they show as
    # counts with a coverage note, never as shares of totals that include calls without them.
    groups = defaultdict(lambda: {'tokens': 0, 'calls': 0, 'cached': None, 'reasoning': None})
    missing = 0
    for row in rows:
        label = (f"{row['model']} / {row['effort'] or 'default'}" if by_model
                 else LABELS.get(row['role'], 'other activity'))
        add(groups[label], tokens=(row['input_tokens'] or 0) + (row['output_tokens'] or 0),
            calls=row['attempts'], cached=row.get('cached_tokens'), reasoning=row.get('reasoning_tokens'))
        missing += row.get('unknown_attempts', 0) or int(
            row['input_tokens'] is None or row['output_tokens'] is None)
    total = sum(value['tokens'] for value in groups.values())
    attempts = sum(value['calls'] for value in groups.values())
    qualifier = 'at least ' if missing else ''
    lines = ["the controller can't cut usage by time yet, so this is everything so far."] if windowless else []
    lines.append(f"fri has logged **{qualifier}{total:,} tokens** across **{attempts:,} calls** "
                 + (f'in {window}:' if window else 'so far:'))
    ranked = sorted(groups.items(), key=lambda item: item[1]['tokens'], reverse=True)
    if len(ranked) > 6:
        rest = {'tokens': 0, 'calls': 0, 'cached': None, 'reasoning': None}
        for _, value in ranked[6:]:
            add(rest, **value)
        ranked = ranked[:6] + [('other models' if by_model else 'other tasks', rest)]
    for name, group in ranked:
        parts = [f'{group[key]:,} {label}' for key, label in (('cached', 'cached input'), ('reasoning', 'reasoning'))
                 if group[key] is not None]
        lines.append(f"- {name}: {group['tokens']:,} tokens · {group['calls']:,} calls"
                     + (f" ({', '.join(parts)})" if parts else ''))
    if missing:
        lines.append('some calls have missing token counts, so these totals are incomplete.')
    if any(group['cached'] is not None or group['reasoning'] is not None for _, group in ranked):
        lines.append('cached input and reasoning only count calls that reported each one, '
                     'so they can cover different calls than the totals.')
    lines.append(("fri activity in that window" if window else "cumulative fri activity")
                 + ", not your remaining plan allowance. gene's coding usage is separate.")
    return '\n'.join(lines)


def add(group, **counts):
    for key, value in counts.items():
        if value is not None:
            group[key] = (group[key] or 0) + value
