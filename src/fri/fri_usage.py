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
    groups = defaultdict(lambda: [0, 0])
    missing = 0
    for row in rows:
        label = (f"{row['model']} / {row['effort'] or 'default'}" if by_model
                 else LABELS.get(row['role'], 'other activity'))
        groups[label][0] += (row['input_tokens'] or 0) + (row['output_tokens'] or 0)
        groups[label][1] += row['attempts']
        missing += row.get('unknown_attempts', 0) or int(
            row['input_tokens'] is None or row['output_tokens'] is None)
    total = sum(value[0] for value in groups.values())
    attempts = sum(value[1] for value in groups.values())
    qualifier = 'at least ' if missing else ''
    lines = [f"fri has logged **{qualifier}{total:,} tokens** across **{attempts:,} calls** so far:"]
    ranked = sorted(groups.items(), key=lambda item: item[1][0], reverse=True)
    if len(ranked) > 6:
        rest = [sum(value[index] for _, value in ranked[6:]) for index in (0, 1)]
        ranked = ranked[:6] + [('other models' if by_model else 'other tasks', rest)]
    for name, (tokens, calls) in ranked:
        lines.append(f'- {name}: {tokens:,} tokens · {calls:,} calls')
    if missing:
        lines.append('some calls have missing token counts, so these totals are incomplete.')
    lines.append("cumulative fri activity, not your remaining plan allowance. gene's coding usage is separate.")
    return '\n'.join(lines)
