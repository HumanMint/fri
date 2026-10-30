"""A member's message withdrawn after Fri saved their exchange with it: the controller drops that exchange (fri #129).

Fri's live view and turns already refuse a message edited or deleted while handled or carried (see Agent.withdraw,
Agent.revise and Agent.unsend), but the member's saved exchange holding it would come back as history on any later
turn. A controller that offers turn-withdrawal drops it. Fri asks about messages whose exchanges it saved itself,
for as long as the controller keeps them, so it knows whose they are and where they were said. It keeps that in
memory only, so any other message edited or deleted where Fri saves exchanges, young enough to be in one still (one
saved before a restart, say), is asked about by ID alone from a controller that also offers saved-turn-withdrawal:
it drops only an exchange that holds it (fri #174).

Each withdrawal waits in one queue until the controller takes it (fri #215), so a timeout or an outage sends it again
later, and sent by one worker, one call at a time, ten IDs a call by ID, each ID once however
many events name it. Meanwhile, and for an hour after (past any turn still holding a copy), what holds a taken-back
message stays out of every request and memory view: saved history, a pending question, an exchange elsewhere, a note
learned from it and the member's summary (see withhold and screen); once deleted, a reply of Fri's in the channel's
view that carried it too (see Agent.withdrawn_from). A member's "forget me" takes their queued withdrawals with it.
Only one for an exchange this process saved (under the erasure guard, see kept) is kept on disk, so a restart sends it
again; one by ID is retried in memory only, so a restart loses it, as before this queue (see lasting).
"""

import asyncio
import json
import logging
import time

from .fri_errors import ControllerRejected
from .fri_protocol import compatible
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
REREAD_SECONDS = 60  # At most this often, the controller's contract read again for a refusal (see reread).
RECORD = 'withdrawals.json'

LOG = logging.getLogger('fri.agent')


class SavedTurns:
    def __init__(self, agent):
        self.agent = agent
        # Discord message ID -> (when saved, the scope it was saved under, the member's revision then), oldest first.
        self.saved = {}
        self.tasks = set()  # The worker sending withdrawals, while it runs.
        self.marked = {}  # Message ID (str) -> when it was taken back, oldest first.
        self._pending = None  # Message ID (str) -> what to send for it; read from disk on first use.
        self.timer = None
        self.generation = 0  # Each queued entry's own number: a reply to an older one never settles a newer one.
        self.erasures = {}  # Member (str) -> how many times they said "forget me" (see forget, kept).
        self.reread_at = 0.0  # time.monotonic() as the contract was last read again (see reread).

    def offered(self):
        return CAPABILITY in getattr(self.agent.fri, 'capabilities', ())

    def capable(self, name):
        return name in getattr(self.agent.fri, 'capabilities', ())

    def entry(self, **what):
        self.generation += 1
        return {**what, 'tries': 0, 'due': 0, 'gen': self.generation}

    @property
    def pending(self):
        """Withdrawals the controller hasn't taken yet: message ID -> {'scope', 'revision'} for one Fri saved an exchange
        with, {'server'} for one asked about by ID, each with 'deleted' (not an edit), 'tries', 'due' (time.time())
        and 'gen' (see drain)."""
        if self._pending is None:
            self._pending = {}
            path = self.record()
            try:
                stored = json.loads(path.read_text())['pending'] if path and path.exists() else {}
                for i, entry in stored.items():
                    if not (str(i).isdigit() and isinstance(entry, dict)):
                        continue
                    deleted = entry.get('deleted') is True
                    if isinstance(entry.get('scope'), dict):
                        revision = entry.get('revision')
                        # From an earlier process: replayed only where its revision is checked (see drain).
                        self._pending[str(i)] = self.entry(scope={key: str(entry['scope'].get(key, '')) for key in (
                            'guild_id', 'channel_id', 'conversation_id', 'user_id')}, deleted=deleted,
                            revision=revision if type(revision) is int and revision >= 0 else None, replayed=True)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                LOG.warning('Pending withdrawals could not be read, so none are resent: %s', type(exc).__name__)
            self.prune(time.time())
            now = time.time()
            for i in self._pending:  # Taken back before the restart: still held back until they land.
                self.marked.setdefault(i, now)
        return self._pending

    def record(self):
        data = getattr(self.agent.fri, 'data', None)
        return None if data is None else data/RECORD

    @staticmethod
    def lasting(entry):
        """Whether a queued withdrawal goes to disk: only one for an exchange this process saved, which names its member
        so their "forget me" finds it, and which a save answered after one never records (see kept). Never one by ID:
        Fri may not know whose it is, and a restart forgets any "forget me" before it (fri #224 round 3)."""
        return 'scope' in entry

    def persist(self):
        if path := self.record():
            try:
                private_json(path, {'pending': {i: {key: value for key, value in entry.items()
                                                    if key not in ('gen', 'replayed')}
                                                for i, entry in self.pending.items() if self.lasting(entry)}})
            except OSError as exc:  # Still queued in memory; resent while Fri runs.
                LOG.warning('Pending withdrawals could not be saved: %s', type(exc).__name__)

    def erasure(self, user):
        """How many times the member said "forget me": a save sent before one answers too late to record (see kept)."""
        return self.erasures.get(str(user), 0)

    def kept(self, payload, saved, began, erasure=None):
        """The controller saved this turn, or may have (`saved` None: no answer came). `began`: time.time() and
        time.monotonic() just before it was sent. One of its messages withdrawn since went while the save was on its
        way, before anything here knew of it, so its exchange goes now. erasure: the member's (see erasure) as it was
        sent; once they've said "forget me" since, the controller erased it, so nothing of it is recorded here."""
        if not self.offered() or payload.get('mention_grant'):  # A mention keeps only a placeholder.
            return
        if erasure is not None and self.erasure(payload.get('user_id')) != erasure:
            return
        if saved is not None and saved.get('kept', True) is False:  # Memory off, or a removal since: nothing kept.
            return
        scope = {key: str(payload.get(key) or '') for key in ('guild_id', 'channel_id', 'conversation_id', 'user_id')}
        if not all(scope.values()):  # Not a turn this could name again; the save it follows still stands.
            return
        ids = {int(item['id']) for item in [payload.get('source') or {}, *(
            row.get('source') or {} for row in payload.get('burst') or ())]  # A burst's earlier messages too.
               if item.get('kind') == 'discord_message' and str(item.get('id', '')).isdigit()}
        revision = payload.get('revision') if type(payload.get('revision')) is int else None
        now = time.time()
        for i in ids:
            self.saved.pop(i, None)
            self.saved[i] = (now, scope, revision)
        while self.saved and (next(iter(self.saved.values()))[0] < now-KEPT_SECONDS or len(self.saved) > MOST):
            del self.saved[next(iter(self.saved))]
        wall, monotonic = began
        agent = self.agent
        deleted = {i for i in ids if agent.unsent.get(i, -1) >= monotonic}
        self.withdraw(deleted, deleted=True)
        self.withdraw(i for i in ids if i not in deleted and (agent.withdrawn.get(str(i), 0) >= wall or
                                                              agent.revised.get(i, 0) >= wall))

    def withdraw(self, ids, server=None, deleted=False, authors=None):
        """Messages taken back: each exchange Fri saved with one goes, asked for once. server: the one Fri saves
        exchanges said where they were taken back under (see Agent.saved_in); any other one young enough to be in an
        exchange still (or in a note, from a controller offering SOURCES) is then asked about by ID, ten at a time,
        from a controller offering it. deleted: not edited, so what carried them goes too (gene #141, #150). authors:
        message ID -> its author, where Fri knows it, so that one by ID can be kept on disk (see lasting). Each is
        held back from requests from now on (see withhold). A change to a queued one is a new entry (see drain)."""
        authors = {int(i): str(user) for i, user in (authors or {}).items() if str(user).isdigit()}
        ids = [int(i) for i in ids]
        horizon = NOTED_SECONDS if self.capable(SOURCES) else KEPT_SECONDS
        queued, now = False, time.time()
        for i in ids:
            known = self.pending.get(str(i), {})
            entry = self.saved.pop(i, None)
            if entry is not None:
                if self.offered():
                    self.pending[str(i)] = self.entry(scope=entry[1], revision=entry[2],
                                                      deleted=deleted or known.get('deleted') is True)
                    queued = True
            elif server is not None and sent_at(i) > now-horizon:
                if self.offered() and self.capable(SAVED_CAPABILITY):
                    if 'scope' in known:  # Fri knows where it was saved: that one stands, deleted or not.
                        if deleted and not known.get('deleted'):
                            self.pending[str(i)] = self.entry(**{key: known[key] for key in ('scope', 'revision')},
                                                              deleted=True)
                            queued = True
                    elif not known or (deleted and not known.get('deleted')) or (i in authors and 'user_id' not in known):
                        author = authors.get(i, known.get('user_id'))
                        self.pending[str(i)] = self.entry(server=str(server), deleted=deleted or known.get('deleted')
                                                          is True, **({'user_id': author} if author else {}))
                        queued = True
            else:
                continue  # Nothing Fri saves could hold it.
            self.marked.pop(str(i), None)
            self.marked[str(i)] = now
        if deleted:  # Fri's replies in view that carried them go too (gene #141), and count as taken back.
            for mid in getattr(self.agent, 'withdrawn_from', lambda ids: ())(ids):
                self.marked.pop(mid, None)
                self.marked[mid] = now
        self.prune(now)
        if queued:
            self.persist()
            self.kick()

    def forget(self, user):
        """The member's "forget me": the controller erased everything their withdrawals could still name, so nothing
        about them stays queued, on disk or here, to bring their IDs back. One already on its way carries the revision
        it was saved under, which the erasure moved, so the controller writes nothing for it (gene #141)."""
        user = str(user)
        self.erasures[user] = self.erasures.get(user, 0)+1
        for i in [i for i, entry in self.saved.items() if entry[1].get('user_id') == user]:
            del self.saved[i]
        mine = [i for i, entry in self.pending.items()
                if (entry.get('scope') or {}).get('user_id') == user or entry.get('user_id') == user]
        for i in mine:
            del self.pending[i]
        self.persist()

    def prune(self, now):
        """Marks an hour, and queued withdrawals as long as anything they name can be kept (a week, or a note's 90 days
        from a controller offering SOURCES), at most MOST of them: past that, the oldest messages' go first."""
        while self.marked and len(self.marked) > MOST*5:
            del self.marked[next(iter(self.marked))]
        for i, at in list(self.marked.items()):
            if at >= now-MARKED_SECONDS:
                break  # The rest are newer.
            if self._pending is None or i not in self._pending:
                del self.marked[i]
        if self._pending is None:
            return
        horizon = now-(NOTED_SECONDS if self.capable(SOURCES) else KEPT_SECONDS)
        old = [i for i in self._pending if sent_at(int(i)) <= horizon]
        for i in old:
            del self._pending[i]
        extra = sorted(self._pending, key=int)[:max(0, len(self._pending)-MOST)]
        for i in extra:
            del self._pending[i]
        if extra:
            LOG.warning('%s pending withdrawals dropped, the oldest messages first: more than %s queued', len(extra), MOST)

    def withholding(self):
        """Whether anything is held back now, so a memory view should be screened (see screen)."""
        now = time.time()
        return bool(self.pending) or any(at >= now-MARKED_SECONDS for at in self.marked.values())

    def withheld(self, ids):
        """Of these message IDs, those taken back while a turn may still hold a copy, or whose withdrawal the
        controller hasn't taken yet."""
        now = time.time()
        return {str(i) for i in ids if str(i) in self.pending or self.marked.get(str(i), 0) >= now-MARKED_SECONDS}

    def withhold(self, context, said=None):
        """Right before a request: what the turn read that holds a message withheld (see withheld) leaves its context.
        An exchange of its history, a pending question, an exchange elsewhere, a note learned from it, and one whose
        source the controller doesn't know but whose words a withheld message of its history holds; and the member's
        summary, which may have read it (the controller drops it once it may have). Returns the IDs of what's left,
        which delivery and the save then check again (see Agent.reply_once). said: gets the withheld messages' words,
        for a memory view to judge notes by (see screen); kept nowhere else."""
        found = held(context)
        gone = self.withheld(found)
        if not gone:
            return set(found)
        said, history = ([] if said is None else said), []
        for turn in context.get('turns', []):
            rows = turn.get('burst') or ()
            if not exchange(turn) & gone:
                history.append(turn)
                continue
            said += [row.get('text', '') for row in rows if str((row.get('source') or {}).get('id')) in gone]
            if not rows and str((turn.get('source') or {}).get('id')) in gone:
                said.append(turn.get('input', ''))
        context['turns'] = history
        if str(((context.get('pending') or {}).get('source') or {}).get('id', '')) in gone:
            context['pending'] = None
        context['elsewhere'] = [row for row in context.get('elsewhere', [])
                                if not {str(i) for i in row.get('messages') or ()} & gone]
        sources, memory = context.get('note_sources'), context.get('personal_memory') or {}
        if sources is not None and memory.get('notes'):
            from .fri_memory import note_key
            memory['notes'] = [note for note in memory['notes']
                               if stays(sources.get(note_key(note)), note.get('evidence'), gone, said)]
            kept = {note_key(note) for note in memory['notes']}
            context['note_sources'] = {key: ids for key, ids in sources.items() if key in kept}
            if context.get('profile_origins') is not None:
                context['profile_origins'] = {key: at for key, at in context['profile_origins'].items() if key in kept}
        context['member_summary'] = None
        return set(held(context))

    def screen(self, context, state=None):
        """A memory view (what do you remember, My memory) holds back what a request would (see withhold): `context`,
        the member's chat context, is screened as a request's is, and `state`, their notes as My memory lists them
        (with the messages each came from), loses those a withheld message taught. The summary goes too once anything
        of theirs is held back. Returns the screened state, or None without one."""
        said = []
        gone = bool(context is not None and self.withheld(held(context)))
        if context is not None:
            self.withhold(context, said)
        if state is None:
            return None
        withheld = self.withheld(i for entry in state.get('entries', []) for i in entry.get('sources') or ()
                                 if i is not None)
        entries = [entry for entry in state.get('entries', [])
                   if stays(entry.get('sources', ()), entry.get('evidence'), withheld, said)]
        gone = gone or len(entries) != len(state.get('entries', []))
        return {**state, 'entries': entries, **({'summary': None} if gone and 'summary' in state else {})}

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
        """One call at a time: each withdrawal Fri saved an exchange for on its own, those by ID ten a call (edits and
        deletions apart). A failure or a refusal waits (RETRY_SECONDS) and is sent again; none is dropped for one, so it
        stays held back until the controller takes it (a Gene rolled back alone refuses fields it doesn't know: see
        send). One the controller can't take now (no such request since a rollback) waits too. Each answer settles only
        the entries it was sent for: one queued for the same message meanwhile (its save landed and named it, say, or it
        was deleted after an edit) is newer, so it stands and goes next. One left on disk by an earlier process goes
        only to a controller that checks its revision (SOURCES): any other may have outlived the member's "forget me"
        through a rolled-back Fri, which left the record, so it's dropped, as a failed withdrawal was before this queue."""
        tried = set()
        while True:
            now = time.time()
            self.prune(now)
            dropped = [i for i, entry in self.pending.items() if entry.get('replayed') and not self.capable(SOURCES)]
            for i in dropped:
                del self.pending[i]
                LOG.warning('Withdrawn message %s from an earlier process was dropped: this controller can\'t check it '
                            'against a privacy change since', i)
            if dropped:
                self.persist()
            due = sorted(i for i, entry in self.pending.items() if entry['due'] <= now and entry['gen'] not in tried)
            waiting = [i for i in due if not self.takes(self.pending[i])]
            if waiting and await self.reread():  # A controller rolled back may have come forward again.
                waiting = [i for i in waiting if not self.takes(self.pending[i])]
            for i in waiting:
                self.wait(self.pending[i])
                tried.add(self.pending[i]['gen'])
            due = [i for i in due if i not in waiting]
            if not due:
                if waiting:
                    self.persist()
                    continue
                break
            placed = [i for i in due if 'scope' in self.pending[i]]
            if placed:
                batch = [(placed[0], self.pending[placed[0]])]
                call = self.send(int(placed[0]), batch[0][1])
            else:
                first = self.pending[due[0]]
                batch = [(i, self.pending[i]) for i in due if self.pending[i].get('server') == first['server']
                         and self.pending[i].get('deleted') is first.get('deleted')][:BATCH]
                call = self.send_unplaced(first['server'], [i for i, _ in batch], first.get('deleted') is True)
            tried.update(entry['gen'] for _, entry in batch)
            outcome = await call
            for i, entry in batch:
                if self.pending.get(i) is not entry:
                    continue  # Replaced or forgotten while it was sent: the newer one stands.
                if outcome == 'sent':
                    del self.pending[i]
                else:
                    self.wait(entry)
            self.persist()
        self.schedule()

    @staticmethod
    def wait(entry):
        entry['tries'] += 1
        entry['due'] = time.time()+RETRY_SECONDS[min(entry['tries'], len(RETRY_SECONDS))-1]

    def takes(self, entry):
        """Whether the controller offers the request this withdrawal needs, as it last said."""
        return self.offered() and ('scope' in entry or self.capable(SAVED_CAPABILITY))

    async def reread(self):
        """The controller's contract again, at most every REREAD_SECONDS: one rolled back (or forward) while Fri runs
        offers what it offers now, and Fri asks in that shape (see send). Fri's capabilities follow it everywhere. True
        if it was read."""
        now = time.monotonic()
        if now-self.reread_at < REREAD_SECONDS:
            return False
        self.reread_at = now
        try:
            contract = await self.agent.fri.api('GET', '/fri/contract')
        except Exception as exc:
            LOG.warning('The controller\'s contract could not be read again: %s', type(exc).__name__)
            return False
        if not compatible(contract):
            LOG.warning('The controller\'s contract read again is not one Fri runs with; keeping what it read before')
            return False
        self.agent.fri.capabilities = frozenset(contract['capabilities'])
        return True

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

    def extra(self, entry):
        """What a controller offering SOURCES also takes: a deletion, and the revision the exchange was saved under."""
        if not self.capable(SOURCES):
            return {}
        return {**({'deleted': True} if entry.get('deleted') else {}),
                **({'revision': entry['revision']} if entry.get('revision') is not None else {})}

    async def send(self, message_id, entry):
        return await self.asking(lambda: ('/chat/turn/withdraw', {**entry['scope'], 'message_id': str(message_id),
                                                                  **self.extra(entry)}), entry,
                                 'Withdrawn message %s was not dropped from saved turns: %s', str(message_id))

    async def send_unplaced(self, server, message_ids, deleted=False):
        return await self.asking(lambda: ('/chat/turn/withdraw/saved', {
            'guild_id': server, 'message_ids': message_ids, **self.extra({'deleted': deleted})}), {'deleted': deleted},
            'Withdrawn messages %s were not dropped from saved turns: %s', ', '.join(message_ids))

    async def asking(self, request, entry, text, ids):
        """Sent as the controller takes it. A refusal may be a controller rolled back alone, which doesn't know a field
        or a request Fri learned at its start: Fri reads its contract again and, if the request it would make now
        differs, sends that once more right away. Otherwise, and on any failure, it waits (see drain). Logged without
        the words."""
        path, payload = request()
        try:
            await self.agent.fri.api('POST', path, payload)
            return 'sent'
        except ControllerRejected as exc:
            if exc.status is None or not await self.reread():
                return failed(text, ids, exc)
            if not self.takes({'scope': None} if path == '/chat/turn/withdraw' else {}):
                return failed(text, ids, exc)
            again = request()
            if again == (path, payload):
                return failed(text, ids, exc)
            try:
                await self.agent.fri.api('POST', *again)
                return 'sent'
            except Exception as retried:
                return failed(text, ids, retried)
        except Exception as exc:
            return failed(text, ids, exc)


def stays(sources, evidence, gone, said):
    """Whether a note stays in view: none of the messages it came from is withheld, and, for a copy of unknown source
    (None among them), no withheld message's words hold its words, as the controller would judge it."""
    sources = sources if isinstance(sources, list) else [None] if sources is None else list(sources)
    if {str(i) for i in sources if i is not None} & set(gone):
        return False
    return None not in sources or not any(evidence and evidence in text for text in said if text)


def failed(text, ids, exc):
    """Kept and sent again later, refused or not: a refusal can come from a controller rolled back alone."""
    refused = isinstance(exc, ControllerRejected) and exc.status is not None
    LOG.warning(text+(f' (refused, HTTP {exc.status}; sent again later)' if refused else ' (sent again later)'), ids,
                type(exc).__name__)
    return 'failed'


def sent_at(message_id):
    """When a Discord message was sent, from its ID."""
    return ((message_id >> 22)+1420070400000)/1000


def saved_lately(message_id):
    """Sent recently enough that the controller may keep an exchange holding it, from the time in its Discord ID."""
    return sent_at(message_id) > time.time()-KEPT_SECONDS


def exchange(turn):
    """The messages a saved exchange answered: its own and its burst's, as strings."""
    rows = turn.get('burst') or ()
    return {str((item or {}).get('id', '')) for item in [turn.get('source'), *(row.get('source') for row in rows)]}


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
