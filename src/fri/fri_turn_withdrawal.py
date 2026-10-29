"""A member's message withdrawn after Fri saved their exchange with it: the controller drops that exchange (fri #129).

Fri's live view and turns already refuse a message edited or deleted while handled or carried (see Agent.withdraw,
Agent.revise and Agent.unsend), but the member's saved exchange holding it would come back as history on any later
turn. A controller that offers turn-withdrawal drops it. Fri asks about messages whose exchanges it saved itself,
for as long as the controller keeps them, so it knows whose they are and where they were said. It keeps that in
memory only, so any other message edited or deleted where Fri saves exchanges, young enough to be in one still (one
saved before a restart, say), is asked about by ID alone from a controller that also offers saved-turn-withdrawal:
it drops only an exchange that holds it (fri #174).

Each withdrawal waits in one queue until the controller takes it (fri #215): on disk, so a timeout, an outage or a
restart sends it again later, and sent by one worker, one call at a time, ten IDs a call by ID, each ID once however
many events name it. Meanwhile, and for an hour after (past any turn still holding a copy), what holds a taken-back
message stays out of every request: saved history, a pending question, an exchange elsewhere and a note learned from
it (see withhold), and a reply of Fri's in the channel's view that carried it (see Agent.withdrawn_from).
"""

import asyncio
import json
import logging
import time

from .fri_errors import ControllerRejected
from .private_files import private_json

CAPABILITY = 'turn-withdrawal'
SAVED_CAPABILITY = 'saved-turn-withdrawal'
# The controller also drops notes learned from it and retires replies that carried it (gene #141), keeps the messages
# a turn carried (drew_on), and names where notes and exchanges elsewhere came from when asked.
SOURCES = 'withdrawal-sources'
KEPT_SECONDS = 7*86400  # As long as the controller keeps a saved exchange.
NOTED_SECONDS = 90*86400  # As long as it keeps a note, which a controller offering SOURCES drops with its message.
MOST = 20000  # Bounds memory only: the controller keeps six exchanges per member and conversation.
MARKED_SECONDS = 3600  # As Agent.withdrawn: longer than any turn that could still hold a copy runs.
BATCH = 10  # As many IDs as /chat/turn/withdraw/saved takes at once.
RETRY_SECONDS = (2, 15, 60, 300)  # After each failure, then every five minutes while it stays pending.
RECORD = 'withdrawals.json'

LOG = logging.getLogger('fri.agent')


class SavedTurns:
    def __init__(self, agent):
        self.agent = agent
        self.saved = {}  # Discord message ID -> (when saved, the scope it was saved under), oldest first.
        self.tasks = set()  # The worker sending withdrawals, while it runs.
        self.marked = {}  # Message ID (str) -> when it was taken back, oldest first.
        self._pending = None  # Message ID (str) -> what to send for it; read from disk on first use.
        self.timer = None

    def offered(self):
        return CAPABILITY in getattr(self.agent.fri, 'capabilities', ())

    def capable(self, name):
        return name in getattr(self.agent.fri, 'capabilities', ())

    @property
    def pending(self):
        """Withdrawals the controller hasn't taken yet: message ID -> {'scope': ...} for one Fri saved an exchange with,
        {'server': ...} for one asked about by ID, with 'tries' and 'due' (time.time())."""
        if self._pending is None:
            self._pending = {}
            path = self.record()
            try:
                stored = json.loads(path.read_text())['pending'] if path and path.exists() else {}
                horizon = time.time()-NOTED_SECONDS
                for i, entry in stored.items():
                    if not (str(i).isdigit() and isinstance(entry, dict) and sent_at(int(i)) > horizon):
                        continue
                    if isinstance(entry.get('scope'), dict):
                        self._pending[str(i)] = {'scope': {key: str(entry['scope'].get(key, '')) for key in (
                            'guild_id', 'channel_id', 'conversation_id', 'user_id')}, 'tries': 0, 'due': 0}
                    elif str(entry.get('server', '')).isdigit():
                        self._pending[str(i)] = {'server': str(entry['server']), 'tries': 0, 'due': 0}
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                LOG.warning('Pending withdrawals could not be read, so none are resent: %s', type(exc).__name__)
            now = time.time()
            for i in self._pending:  # Taken back before the restart: still held back until they land.
                self.marked.setdefault(i, now)
        return self._pending

    def record(self):
        data = getattr(self.agent.fri, 'data', None)
        return None if data is None else data/RECORD

    def persist(self):
        if path := self.record():
            try:
                private_json(path, {'pending': self.pending})
            except OSError as exc:  # Still queued in memory; resent while Fri runs.
                LOG.warning('Pending withdrawals could not be saved: %s', type(exc).__name__)

    def kept(self, payload, saved, began):
        """The controller saved this turn, or may have (`saved` None: no answer came). `began`: time.time() and
        time.monotonic() just before it was sent. One of its messages withdrawn since went while the save was on its
        way, before anything here knew of it, so its exchange goes now."""
        if not self.offered() or payload.get('mention_grant'):  # A mention keeps only a placeholder.
            return
        if saved is not None and saved.get('kept', True) is False:  # Memory off, or a removal since: nothing kept.
            return
        scope = {key: str(payload.get(key) or '') for key in ('guild_id', 'channel_id', 'conversation_id', 'user_id')}
        if not all(scope.values()):  # Not a turn this could name again; the save it follows still stands.
            return
        ids = {int(item['id']) for item in [payload.get('source') or {}, *(
            row.get('source') or {} for row in payload.get('burst') or ())]  # A burst's earlier messages too.
               if item.get('kind') == 'discord_message' and str(item.get('id', '')).isdigit()}
        now = time.time()
        for i in ids:
            self.saved.pop(i, None)
            self.saved[i] = (now, scope)
        while self.saved and (next(iter(self.saved.values()))[0] < now-KEPT_SECONDS or len(self.saved) > MOST):
            del self.saved[next(iter(self.saved))]
        wall, monotonic = began
        agent = self.agent
        self.withdraw(i for i in ids if agent.withdrawn.get(str(i), 0) >= wall or agent.revised.get(i, 0) >= wall
                      or agent.unsent.get(i, -1) >= monotonic)

    def withdraw(self, ids, server=None):
        """Messages taken back: each exchange Fri saved with one goes, asked for once. server: the one Fri saves
        exchanges said where they were taken back under (see Agent.saved_in); any other one young enough to be in an
        exchange still (or in a note, from a controller offering SOURCES) is then asked about by ID, ten at a time,
        from a controller offering it. Each is held back from requests from now on (see withhold)."""
        ids = [int(i) for i in ids]
        horizon = NOTED_SECONDS if self.capable(SOURCES) else KEPT_SECONDS
        queued, now = False, time.time()
        for i in ids:
            entry = self.saved.pop(i, None)
            if entry is not None:
                if self.offered():
                    self.pending[str(i)] = {'scope': entry[1], 'tries': 0, 'due': 0}
                    queued = True
            elif server is not None and sent_at(i) > now-horizon:
                if self.offered() and self.capable(SAVED_CAPABILITY) and 'scope' not in self.pending.get(str(i), {}):
                    self.pending[str(i)] = {'server': str(server), 'tries': 0, 'due': 0}
                    queued = True
            else:
                continue  # Nothing Fri saves could hold it.
            self.marked.pop(str(i), None)
            self.marked[str(i)] = now
        # Fri's replies in view that carried them go too (gene #141), and count as taken back for a turn holding one.
        for mid in getattr(self.agent, 'withdrawn_from', lambda ids: ())(ids):
            self.marked.pop(mid, None)
            self.marked[mid] = now
        self.prune(now)
        if queued:
            self.persist()
            self.kick()

    def prune(self, now):
        while self.marked and len(self.marked) > MOST*5:
            del self.marked[next(iter(self.marked))]
        for i, at in list(self.marked.items()):
            if at >= now-MARKED_SECONDS:
                break  # The rest are newer.
            if i not in self.pending:
                del self.marked[i]

    def withheld(self, ids):
        """Of these message IDs, those taken back while a turn may still hold a copy, or whose withdrawal the
        controller hasn't taken yet."""
        now = time.time()
        return {str(i) for i in ids if str(i) in self.pending or self.marked.get(str(i), 0) >= now-MARKED_SECONDS}

    def withhold(self, context):
        """Right before a request: what the turn read that holds a message withheld (see withheld) leaves its context.
        An exchange of its history, a pending question, an exchange elsewhere, a note learned from it, and one whose
        source the controller doesn't know but whose words a withheld exchange holds; and the member's summary, which
        may have read it (the controller drops it once it may have). Returns the IDs of what's left, which delivery and
        the save then check again (see Agent.reply_once)."""
        found = held(context)
        gone = self.withheld(found)
        if not gone:
            return set(found)
        said, history = [], []
        for turn in context.get('turns', []):
            ids = {str((item or {}).get('id', '')) for item in
                   [turn.get('source'), *((row.get('source') or {}) for row in turn.get('burst') or ())]}
            if ids & gone:
                said += [turn.get('input', ''), *(row.get('text', '') for row in turn.get('burst') or ())]
            else:
                history.append(turn)
        context['turns'] = history
        if str(((context.get('pending') or {}).get('source') or {}).get('id', '')) in gone:
            context['pending'] = None
        context['elsewhere'] = [row for row in context.get('elsewhere', [])
                                if not {str(i) for i in row.get('messages') or ()} & gone]
        sources, memory = context.get('note_sources'), context.get('personal_memory') or {}
        if sources is not None and memory.get('notes'):
            from .fri_memory import note_key
            def stays(note):
                ids = sources.get(note_key(note)) or ()
                if {str(i) for i in ids if i is not None} & gone:
                    return False
                # A copy of unknown source: the words decide, as the controller's do.
                return None not in ids or not any(note.get('evidence', '\0') in text for text in said if text)
            memory['notes'] = [note for note in memory['notes'] if stays(note)]
            kept = {note_key(note) for note in memory['notes']}
            context['note_sources'] = {key: ids for key, ids in sources.items() if key in kept}
            if context.get('profile_origins') is not None:
                context['profile_origins'] = {key: at for key, at in context['profile_origins'].items() if key in kept}
        context['member_summary'] = None
        return set(held(context))

    def retry(self):
        """Send everything still pending now, as a start does."""
        for entry in self.pending.values():
            entry['due'] = 0
        self.kick()

    def kick(self):
        if any(not task.done() for task in self.tasks) or not self.pending:
            return  # The running worker picks up what was just queued.
        try:
            task = asyncio.get_running_loop().create_task(self.drain())
        except RuntimeError:  # No loop: kept pending, sent once one runs.
            return
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def drain(self):
        """One call at a time: each withdrawal Fri saved an exchange for on its own, those by ID ten a call. A failure
        waits (RETRY_SECONDS) and is sent again; a refusal can never succeed, so it's logged and dropped."""
        tried = set()
        while True:
            now = time.time()
            due = sorted(i for i, entry in self.pending.items() if entry['due'] <= now and i not in tried)
            if not due:
                break
            placed = [i for i in due if 'scope' in self.pending[i]]
            if placed:
                batch, call = placed[:1], self.send(int(placed[0]), self.pending[placed[0]]['scope'])
            else:
                server = self.pending[due[0]]['server']
                batch = [i for i in due if self.pending[i].get('server') == server][:BATCH]
                call = self.send_unplaced(server, batch)
            tried.update(batch)
            outcome = await call
            for i in batch:
                if i not in self.pending:
                    continue
                if outcome == 'sent' or outcome == 'refused':
                    del self.pending[i]
                else:
                    entry = self.pending[i]
                    entry['tries'] += 1
                    entry['due'] = time.time()+RETRY_SECONDS[min(entry['tries'], len(RETRY_SECONDS))-1]
            self.persist()
        self.schedule()

    def schedule(self):
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        if not self.pending:
            return
        wait = max(0.0, min(entry['due'] for entry in self.pending.values())-time.time())
        try:
            self.timer = asyncio.get_running_loop().call_later(wait, self.kick)
        except RuntimeError:
            pass

    async def send(self, message_id, scope):
        try:
            await self.agent.fri.api('POST', '/chat/turn/withdraw', {**scope, 'message_id': str(message_id)})
        except Exception as exc:  # Logged without the words; whatever withdrew it goes on.
            return failed('Withdrawn message %s was not dropped from saved turns: %s', str(message_id), exc)
        return 'sent'

    async def send_unplaced(self, server, message_ids):
        try:
            await self.agent.fri.api('POST', '/chat/turn/withdraw/saved', {'guild_id': server, 'message_ids': message_ids})
        except Exception as exc:  # As above.
            return failed('Withdrawn messages %s were not dropped from saved turns: %s', ', '.join(message_ids), exc)
        return 'sent'


def failed(text, ids, exc):
    refused = isinstance(exc, ControllerRejected) and exc.status is not None
    LOG.warning(text+(' (refused, not sent again)' if refused else ' (sent again later)'), ids, type(exc).__name__)
    return 'refused' if refused else 'failed'


def sent_at(message_id):
    """When a Discord message was sent, from its ID."""
    return ((message_id >> 22)+1420070400000)/1000


def saved_lately(message_id):
    """Sent recently enough that the controller may keep an exchange holding it, from the time in its Discord ID."""
    return sent_at(message_id) > time.time()-KEPT_SECONDS


def held(context):
    """Message ID -> where it is, for each message the saved context a turn read holds: its history (each exchange's
    own message and burst), a pending question's message, its exchanges elsewhere and the notes it carries (from a
    controller offering SOURCES)."""
    found = {}
    for turn in context.get('turns', []):
        for item in [turn.get('source') or {}, *((row.get('source') or {}) for row in turn.get('burst') or ())]:
            if str(item.get('id', '')).isdigit():
                found.setdefault(str(item['id']), 'turns')
    if str(((context.get('pending') or {}).get('source') or {}).get('id', '')).isdigit():
        found.setdefault(str(context['pending']['source']['id']), 'pending')
    for row in context.get('elsewhere', []):
        found.update((str(i), 'elsewhere') for i in row.get('messages') or () if str(i).isdigit())
    sources = context.get('note_sources') or {}
    from .fri_memory import note_key
    for note in (context.get('personal_memory') or {}).get('notes', []):
        found.update((str(i), 'notes') for i in sources.get(note_key(note)) or () if str(i).isdigit())
    return found
