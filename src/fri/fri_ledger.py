"""What happened to the messages addressed to Fri: a ping, its name or a reply to it in a channel it watches,
or a DM. Counts only, in memory since Fri started, for the health file and the owner's "fri health". Reasons are
the gate and exception names the agent already logs, never content, names or IDs."""

import math
import time
from collections import Counter, deque

from .fri_addressing import named, pinged

OUTAGE = frozenset({'controller unreachable', 'ControllerUnavailable', 'check failed: ControllerUnavailable'})
COOLDOWN = 'social cooldown'
CHOSEN = frozenset({'chose quiet'})  # The model's own call to stay quiet, not a reply held back.
AGAIN = 'message edited'  # Answered again as it reads now (see Agent.rerun), which counts on its own.
SAMPLES = 1000  # Answer latencies kept for the percentiles, newest last.
PENDING = 10000  # Messages still being handled; past this the oldest are let go uncounted.
REASONS = 40  # Distinct reasons kept per outcome; any after those count as "other".


def addressed(agent, message):
    """Which kind of message to Fri this is, from what Discord shows before any read: a ping (anywhere, as
    Agent.watch has it), a DM, or Fri's name or a reply to Fri in a channel it watches. None otherwise."""
    me = getattr(getattr(agent.fri, 'user', None), 'id', None)
    author = message.author
    if not me or author.bot or getattr(message, 'webhook_id', None) or author.id == me:
        return None
    if message.guild is None:
        return 'dm'
    if pinged(message, me):
        return 'ping'
    if not agent.allowed(message.channel):
        return None
    content = getattr(message, 'content', None)
    resolved = getattr(getattr(message, 'reference', None), 'resolved', None)
    if (isinstance(content, str) and named(content) or
            getattr(getattr(resolved, 'author', None), 'id', None) == me):
        return 'name'
    return None


def turn_ids(context):
    """The messages a model request answers, as Fri.chat_once's last checks list them: its own and its burst."""
    source = context.get('source') or {}
    ids = [*([source.get('id')] if source.get('kind') == 'discord_message' else []),
           *((row.get('source') or {}).get('id') for row in context.get('message_burst', []))]
    return [int(i) for i in ids if str(i).isdigit()]


def notice_reason(exc):
    """A fixed name for the notice a refusal's text gives, never the text: it may carry the controller's words."""
    from . import feedback as commands
    from .fri import CHANGED, CLOSED, CONVERSATION_CHANGED, READ_CHANGED, WITHDRAWN
    from .fri_images import CHANGED as IMAGES_CHANGED
    names = {CONVERSATION_CHANGED: 'conversation changed', READ_CHANGED: 'read changed', IMAGES_CHANGED: 'images changed',
             WITHDRAWN: 'message withdrawn', CHANGED: 'privacy changed', CLOSED: 'channel closed'}
    if name := names.get(str(exc)):
        return name
    return 'other notice' if type(exc) is commands.UserFeedbackCheckFailure else type(exc).__name__


class Ledger:
    def __init__(self):
        self.since = time.time()
        self.pending = {}  # Message ID -> what's known of it so far, until its outcome is counted.
        self.kinds = Counter()
        self.answers = 0
        self.outage = self.cooldown = 0
        self.told, self.quiet, self.withheld = Counter(), Counter(), Counter()
        self.latency = deque(maxlen=SAMPLES)

    def track(self, message, kind):
        self.pending.pop(message.id, None)
        self.pending[message.id] = {'kind': kind, 'at': time.monotonic(), 'reason': None, 'composed': False,
                                    'notice': None}
        while len(self.pending) > PENDING:
            del self.pending[next(iter(self.pending))]

    def miss(self, i, reason):
        """Why it may go unanswered, the first reason kept, as Agent.miss keeps a ping's."""
        if (entry := self.pending.get(i)) and entry['reason'] is None:
            entry['reason'] = reason

    def composed(self, context):
        """The model answered this request: from here a reply that doesn't go out was withheld after composing."""
        for i in turn_ids(context):
            if entry := self.pending.get(i):
                entry['composed'] = True

    def notice(self, ids, reason):
        """What Fri says next for these messages, if anything, is a notice about `reason`, not an answer."""
        for i in ids:
            if entry := self.pending.get(i):
                entry['notice'] = reason

    def answered(self, ids, told=None):
        """Something went out for these: their answer, or a notice saying why not (`told`, or one set earlier)."""
        for i in ids:
            if (entry := self.pending.pop(i, None)) is None:
                continue
            if reason := told or entry['notice']:
                self.close(entry, reason, told=True)
            else:
                self.kinds[entry['kind']] += 1
                self.answers += 1
                self.latency.append(time.monotonic()-entry['at'])

    def explained(self, ids, reason):
        for i in ids:
            if entry := self.pending.pop(i, None):
                self.close(entry, reason, told=True)

    def settle(self, i, reason=None):
        """Its handling ended with nothing said: quiet, for the first reason given."""
        if entry := self.pending.pop(i, None):
            self.close(entry, entry['reason'] or reason or 'no reply', told=False)

    def close(self, entry, reason, told):
        if reason == AGAIN:
            return
        self.kinds[entry['kind']] += 1
        if reason in OUTAGE:
            self.outage += 1
        elif reason == COOLDOWN:
            self.cooldown += 1
        elif entry['composed'] and reason not in CHOSEN:
            self.add(self.withheld, reason)
        else:
            self.add(self.told if told else self.quiet, reason)

    @staticmethod
    def add(counts, reason):
        reason = str(reason)[:80]
        counts[reason if reason in counts or len(counts) < REASONS else 'other'] += 1

    def snapshot(self):
        latency = sorted(self.latency)
        def rank(q):  # Nearest rank.
            return round(latency[math.ceil(q*len(latency))-1], 1) if latency else None
        return {'since': self.since, 'addressed': dict(self.kinds), 'in_flight': len(self.pending),
                'answered': self.answers, 'told_why': dict(self.told), 'quiet': dict(self.quiet),
                'withheld': dict(self.withheld), 'outage': self.outage, 'cooldown': self.cooldown,
                'latency': {'p50': rank(0.5), 'p90': rank(0.9), 'samples': len(latency)}}


def describe(outcomes):
    """The owner's one line, e.g. "41 (12 pings, 20 by name, 9 dms), 37 answered (p50 4s, p90 19s), ..."."""
    from .fri_watch import span
    kinds = outcomes['addressed']
    shown = [f'{kinds[kind]} {label}' + ('s' if plural and kinds[kind] > 1 else '')
             for kind, label, plural in (('ping', 'ping', True), ('name', 'by name', False), ('dm', 'dm', True))
             if kinds.get(kind)]
    parts = [f'{sum(kinds.values())}' + (f" ({', '.join(shown)})" if shown else '')]
    latency = outcomes['latency']
    answered = f"{outcomes['answered']} answered"
    if latency['samples']:
        answered += f" (p50 {span(latency['p50'])}, p90 {span(latency['p90'])})"
    parts.append(answered)
    for key, label in (('told_why', 'told why'), ('withheld', 'withheld after composing'), ('quiet', 'quiet')):
        if counts := outcomes[key]:
            top = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            reasons = ', '.join(reason + (f' ×{n}' if n > 1 else '') for reason, n in top[:3])
            parts.append(f'{sum(counts.values())} {label} ({reasons}' + (', …)' if len(top) > 3 else ')'))
    for key in ('outage', 'cooldown'):
        if outcomes[key]:
            parts.append(f'{outcomes[key]} {key}')
    return f"- addressed to me since <t:{int(outcomes['since'])}:R>: " + ', '.join(parts)
