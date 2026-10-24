"""Explicit usage requests and compact, deterministic reports without inference."""

import re
from collections import defaultdict

SUBJECT = r'(?:(?:my|our|your|the|current|fri\'s)\s+)?(?:token\s+usage|usage|tokens\s+used|token\s+counts?|token\s+totals?)'
TAIL = r'(?:\s+(?:so far|right now|rn|please|pls|by task|by model|breakdown))*'
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
          'evaluation': 'isolated tests'}


def usage_requested(text):
    text = ' '.join(text.casefold().replace('’', "'").split()).strip(' .!?')
    text = re.sub(r'^(?:(?:hey|yo)\s+)?fri\b[,:]?\s*', '', text)
    text = re.sub(r'^(?:please|pls)\s+', '', text)
    return bool(REQUEST.fullmatch(text))


def describe_usage(rows, *, by_model=False):
    if not rows:
        return 'nothing recorded for fri yet. this tracker covers fri activity, not the whole subscription.'
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
    lines = [f"fri has logged **{qualifier}{total:,} tokens** across **{attempts:,} calls** so far:"]
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
    lines.append("cumulative fri activity, not your remaining plan allowance. gene's coding usage is separate.")
    return '\n'.join(lines)


def add(group, **counts):
    for key, value in counts.items():
        if value is not None:
            group[key] = (group[key] or 0) + value
