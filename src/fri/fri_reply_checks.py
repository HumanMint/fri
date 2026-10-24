"""Background reply checks: did a member's next message say Fri's last reply failed them?

The controller offers recent reply/follow-up pairs without identities and keeps only labels from
fixed lists, fenced by privacy revisions. Nothing here posts to Discord or stores chat text.
"""

import logging

from . import feedback as commands
from .fri_agent_model import STRING, analyze, obj
from .fri_errors import ControllerRejected
from .subscription import SubscriptionError

LOG = logging.getLogger('fri.reply_checks')
CAPABILITY = 'reply-checks'
CATEGORIES = ['wrong_info', 'misunderstood', 'ignored_request', 'failed_action', 'too_long', 'vague',
              'repetitive', 'tone', 'refused', 'other']
TOPICS = ['games', 'music_playback', 'media_and_shows', 'current_events_research', 'reminders_and_plans',
          'images', 'gene_commands', 'memory_and_privacy', 'settings', 'banter', 'advice', 'tech_help',
          'server_info', 'other']
FIXES = ['verify_facts', 'ask_clarifying_question', 'answer_the_actual_question', 'follow_through_on_action',
         'report_failures_honestly', 'be_shorter', 'be_more_specific', 'avoid_repeating', 'adjust_tone',
         'dont_refuse', 'use_context', 'other']
SCHEMA = obj({'findings': {'type': 'array', 'items': obj({
    'pair_id': STRING, 'category': {'type': 'string', 'enum': CATEGORIES},
    'severity': {'type': 'string', 'enum': ['low', 'medium', 'high']},
    'topic': {'type': 'string', 'enum': TOPICS}, 'fix': {'type': 'string', 'enum': FIXES}})}})
INSTRUCTION = '''Each pair is a member's message to Fri, Fri's reply, then the same member's next message
(and Fri's answer to it, when kept). Decide whether that next message shows the member was genuinely
unsatisfied with Fri's earlier reply: correcting it, saying it's wrong, re-asking the same thing,
pointing out Fri ignored or misread the request, complaining that an action didn't happen, or real
frustration aimed at Fri.
Fri is deliberately deadpan and teasing in a friend server. Banter, jokes, roasting Fri back, freaky
or playful insults, a new topic, thanks, or a normal follow-up question are not dissatisfaction unless
the member also signals the reply failed them. Flag only clear cases; an empty list is normal.
For each flagged pair give its pair_id and four labels: category (what went wrong), severity (high for
wrong facts or a broken action, medium for a misunderstood or ignored request, low for tone, length or
vagueness), topic (what the member's request was about) and fix (what Fri should do differently next
time). Pick the closest label; use other only when nothing fits.'''


async def check(agent):
    """Run at most one claimed batch. Failures are reported so the controller can retry or skip."""
    # Claim only once the model is free and Fri is signed in, so nothing waits between fetching the
    # chat and sending it: anything erased before the claim is never sent.
    async with agent.slots:
        try:
            headers = await agent.fri.chat.headers()
        except SubscriptionError:
            LOG.warning('Reply checks wait for the subscription sign-in')
            return None
        try:
            claim = await agent.fri.api('POST', '/agent/reply-checks/claim')
        except ControllerRejected:
            # A controller rolled back past reply checks; stop asking until Fri restarts against one.
            agent.fri.capabilities = agent.fri.capabilities - {CAPABILITY}
            LOG.warning('The controller refused reply checks; they stay off until Fri restarts')
            return None
        if not claim:
            return None
        result, counters, status = await analyze(agent.fri.chat, INSTRUCTION, {'pairs': claim['pairs']},
                                                 claim['model'], claim['effort'], SCHEMA, headers=headers)
    # An existing role, so a receipt still queued when Fri and the controller roll back is accepted.
    # Saved, not delivered here: a slow usage backlog mustn't outlast the claim and waste the call.
    await agent.record('attention', claim['model'], claim['effort'], counters, status, 'reply_check', deliver=False)
    findings = result.get('findings', [])[:20] if status == 'completed' else []
    try:
        return await agent.fri.api('POST', f"/agent/reply-checks/{claim['id']}/finish",
                                   {'findings': findings, 'status': status})
    except commands.UserFeedbackCheckFailure:
        LOG.warning('Reply check %s was not recorded; the controller retries it later', claim['id'][:8])
        return None


def words(name):
    return name.replace('_', ' ')


def describe(overview):
    """Owner-only summary: counts and recurring themes, never who, where or what was said."""
    if not overview['enabled'] and not overview['flagged']:
        return 'reply checks are off. ask me to enable them and i’ll show a settings preview.'
    days, flagged, checked = overview['days'], overview['flagged'], overview['checked_pairs']
    if not flagged:
        return f'no unhappy follow-ups in the last {days} days ({checked} checked). suspicious, but fine.'
    counts = ', '.join(f'{count} {words(name)}' for name, count in overview['categories'].items())
    lines = [f'last {days} days: {flagged} unhappy follow-up'+('s' if flagged != 1 else '')+
             f' out of {checked} checked. {counts}.']
    for theme in overview['themes']:
        lines.append(f"- {theme['count']}× {words(theme['topic'])}: {words(theme['category'])} → "
                     f"{words(theme['fix'])}")
    return '\n'.join(lines)[:1900]
