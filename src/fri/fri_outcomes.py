"""Typed tool evidence and truthful replies that do not depend on another model."""

from enum import Enum

from .fri_notices import notice_text
from .privacy import clean


class Outcome(str, Enum):
    COMPLETED = 'completed'
    REJECTED = 'rejected'
    FAILED = 'failed'
    UNCERTAIN = 'uncertain'


def receipt(kind, value):
    """Keep existing evidence fields while making completion semantics explicit."""
    result = {**value, 'kind': kind}
    if kind == 'reminder_cancel' and result.get('state') not in (None, 'cancelled'):
        state = result['state']
        result['blocked'] = ("The reminder is already being sent; cancellation cannot undo that send."
                             if state == 'sending' else f'The reminder is already {state}; it was not cancelled.')
    if result.get('blocked'):
        outcome = Outcome.REJECTED
    elif result.get('uncertain'):
        outcome = Outcome.UNCERTAIN
    elif result.get('error'):
        outcome = Outcome.FAILED
    elif kind == 'gene_command' and not result.get('output'):
        outcome = Outcome.UNCERTAIN
        result['uncertain'] = 'Gene returned no confirmed result. Check the player before retrying.'
    else:
        outcome = Outcome.COMPLETED
    result['outcome'] = outcome.value
    return result


def fallback(receipts):
    """Never infer playback, publish a private read, or repeat a side effect."""
    earlier = [one(r) for r in receipts[:-1] if r['outcome'] == Outcome.COMPLETED
               and r['kind'] in ('work_save', 'reminder_cancel')]
    return '\n'.join([*earlier, one(receipts[-1])])[:1800]


def one(last):
    """Render only typed evidence, without the final model's proposed wording."""
    outcome = last['outcome']
    if outcome == Outcome.REJECTED:
        return 'that step was rejected: '+notice_text(clean(str(last['blocked']), 1200))
    if outcome == Outcome.FAILED:
        return 'that check failed: '+notice_text(clean(str(last['error']), 1200))
    if outcome == Outcome.UNCERTAIN:
        return ('completion is uncertain. '+notice_text(clean(str(last.get('uncertain', '')), 1000))+
                ' i have not retried it. check its current status before sending another request.')
    kind = last['kind']
    if kind == 'gene_command':
        return 'gene returned:\n'+clean(str(last['output']), 1600)
    if kind == 'work_save':
        return f"saved your work at version {last.get('version', 'confirmed')} ({last.get('state', 'saved')})."
    if kind == 'reminder_cancel' and last.get('state') == 'cancelled':
        return 'reminder cancelled.'
    if kind in ('web_search', 'web_refine', 'web_sources') and last.get('answer'):
        return clean(str(last['answer']), 1700)
    if kind == 'image_search' and last.get('image_status') == 'prepared':
        return 'found one that fits.'
    return 'that check completed, but i couldn’t finish explaining it. no action was repeated; ask for current details.'


def unrecorded(receipts, *, reason='i couldn’t save this chat record.'):
    """A failed privacy/revision write must not hide an already known side effect.

    Do not quote read results or controller details here: the failed write may be
    a concurrent privacy/access rejection, rather than a temporary outage.
    """
    lines = []
    for item in receipts:
        if item['outcome'] == Outcome.COMPLETED and item['kind'] == 'work_save':
            lines.append(f"your work was saved at version {item['version']}.")
        elif item['outcome'] == Outcome.COMPLETED and item['kind'] == 'reminder_cancel':
            lines.append('the reminder was cancelled.')
    last = receipts[-1]
    if last['outcome'] == Outcome.COMPLETED:
        if last['kind'] == 'gene_command':
            lines.append('gene returned a confirmed command result.')
        elif last['kind'] not in ('work_save', 'reminder_cancel'):
            lines.append('that check completed.')
    else:
        lines.append({'rejected': 'that step was rejected.', 'failed': 'that check failed.',
                      'uncertain': 'completion is uncertain.'}[last['outcome']])
    lines.append(notice_text(reason)+' no action was repeated; check current status before repeating an action.')
    return '\n'.join(lines)[:1800]
