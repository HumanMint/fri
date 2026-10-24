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
    keys = ('tokens', 'calls', 'input', 'cached', 'output', 'reasoning')
    groups = defaultdict(lambda: dict.fromkeys(keys, 0))
    missing = 0
    for row in rows:
        label = (f"{row['model']} / {row['effort'] or 'default'}" if by_model
                 else LABELS.get(row['role'], 'other activity'))
        group = groups[label]
        group['tokens'] += (row['input_tokens'] or 0) + (row['output_tokens'] or 0)
        group['calls'] += row['attempts']
        group['input'] += row['input_tokens'] or 0
        group['cached'] += row.get('cached_tokens') or 0
        group['output'] += row['output_tokens'] or 0
        group['reasoning'] += row.get('reasoning_tokens') or 0
        missing += row.get('unknown_attempts', 0) or int(
            row['input_tokens'] is None or row['output_tokens'] is None)
    total = sum(value['tokens'] for value in groups.values())
    attempts = sum(value['calls'] for value in groups.values())
    qualifier = 'at least ' if missing else ''
    lines = [f"fri has logged **{qualifier}{total:,} tokens** across **{attempts:,} calls** so far:"]
    ranked = sorted(groups.items(), key=lambda item: item[1]['tokens'], reverse=True)
    if len(ranked) > 6:
        rest = {key: sum(value[key] for _, value in ranked[6:]) for key in keys}
        ranked = ranked[:6] + [('other models' if by_model else 'other tasks', rest)]
    for name, group in ranked:
        # Cached input and reasoning output are reported shares, so they show what prompt caching and
        # reasoning effort are doing to each task without claiming what the plan bills for them.
        shares = [f"{group[part] / group[whole]:.0%} {label}" for part, whole, label in (
            ('cached', 'input', 'of input cached'), ('reasoning', 'output', 'of output reasoning'))
            if group[part] and group[whole]]
        lines.append(f"- {name}: {group['tokens']:,} tokens · {group['calls']:,} calls"
                     + (f" ({', '.join(shares)})" if shares else ''))
    if missing:
        lines.append('some calls have missing token counts, so these totals are incomplete.')
    lines.append("cumulative fri activity, not your remaining plan allowance. gene's coding usage is separate.")
    return '\n'.join(lines)
