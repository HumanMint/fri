"""Background reply checks: did a member's next message say Fri's last reply failed them?

The controller offers recent reply/follow-up pairs without identities and keeps only
abstract, privacy-fenced findings. Nothing here posts to Discord or stores chat text.
"""

import logging

from . import feedback as commands
from .fri_agent_model import STRING, obj

LOG = logging.getLogger('fri.reply_checks')
CATEGORIES = ['wrong_info', 'misunderstood', 'ignored_request', 'failed_action', 'too_long', 'vague',
              'repetitive', 'tone', 'refused', 'other']
SCHEMA = obj({'findings': {'type': 'array', 'items': obj({
    'pair_id': STRING, 'category': {'type': 'string', 'enum': CATEGORIES},
    'severity': {'type': 'string', 'enum': ['low', 'medium', 'high']}, 'issue': STRING, 'fix': STRING})}})
INSTRUCTION = '''Each pair is a member's message to Fri, Fri's reply, then the same member's next message
(and Fri's answer to it, when kept). Decide whether that next message shows the member was genuinely
unsatisfied with Fri's earlier reply: correcting it, saying it's wrong, re-asking the same thing,
pointing out Fri ignored or misread the request, complaining that an action didn't happen, or real
frustration aimed at Fri.
Fri is deliberately deadpan and teasing in a friend server. Banter, jokes, roasting Fri back, freaky
or playful insults, a new topic, thanks, or a normal follow-up question are not dissatisfaction unless
the member also signals the reply failed them. Flag only clear cases; an empty list is normal.
For each flagged pair give its pair_id, a category, and a severity: high for wrong facts or a broken
action, medium for a misunderstood or ignored request, low for tone, length or vagueness.
issue: what went wrong in Fri's reply, at most 160 characters. fix: what Fri should do differently next
time, at most 160 characters. Describe the kind of request ("a game release date question"), never the
person. No quotes or phrases copied from the messages, names, mentions, links, numbers or personal details.'''


async def check(agent):
    """Run at most one claimed batch. Failures are reported so the controller can retry or skip."""
    claim = await agent.fri.api('POST', '/agent/reply-checks/claim')
    if not claim:
        return None
    result, _, status = await agent.run('reply_check', INSTRUCTION, {'pairs': claim['pairs']}, SCHEMA,
                                        claim['model'], claim['effort'])
    findings = result.get('findings', [])[:20] if status == 'completed' else []
    try:
        return await agent.fri.api('POST', f"/agent/reply-checks/{claim['id']}/finish",
                                   {'findings': findings, 'status': status})
    except commands.UserFeedbackCheckFailure:
        LOG.warning('Reply check %s was not recorded; the controller retries it later', claim['id'][:8])
        return None


def describe(overview):
    """Owner-only summary: counts and abstract descriptions, never who or where."""
    if not overview['enabled'] and not overview['flagged']:
        return 'reply checks are off. ask me to enable them and i’ll show a settings preview.'
    days, flagged, checked = overview['days'], overview['flagged'], overview['checked_pairs']
    if not flagged:
        return f'no unhappy follow-ups in the last {days} days ({checked} checked). suspicious, but fine.'
    counts = ', '.join(f"{count} {name.replace('_', ' ')}" for name, count in overview['categories'].items())
    lines = [f'last {days} days: {flagged} unhappy follow-up'+('s' if flagged != 1 else '')+
             f' out of {checked} checked. {counts}.']
    for row in overview['latest']:
        lines.append(f"- {row['day']} · {row['severity']} · {row['issue']} → {row['fix']}")
    return '\n'.join(lines)[:1900]
