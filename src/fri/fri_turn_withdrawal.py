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
One for an exchange this process saved (under the erasure guard, see kept) is kept on disk whole, so a restart sends it
again; one by ID is retried in memory, and kept on disk only as a hold of its message's ID, so a restart holds it back
without sending it (fri #252, see lasting). Past the queue's capacity, the oldest go, but every saved context is held
back until nothing they name can be kept (fri #256, see prune), past what a start reads from disk too, and each lasts
from when it was queued, after anything saved from its message (fri #329, see lasts and overflowed).
"""

import asyncio
import io
import json
import json.decoder
import logging
import math
import re
import time

from .fri_edit_fingerprint import MOST as MOST_HASHES
from .fri_edit_fingerprint import salt
from .fri_errors import ControllerRejected
from .fri_protocol import compatible
from .private_files import private_json

CAPABILITY = 'turn-withdrawal'
SAVED_CAPABILITY = 'saved-turn-withdrawal'
# The controller also drops notes learned from it and retires replies that carried it (gene #141), keeps the messages
# a turn carried (drew_on), and names where notes and exchanges elsewhere came from when asked.
SOURCES = 'withdrawal-sources'
# And with an edit, what it took out, hashed (see fri_edit_fingerprint): replies that carried it and hold that retire too
# (gene #150, #246).
FINGERPRINTS = 'edit-fingerprints'
KEPT_SECONDS = 7*86400  # As long as the controller keeps a saved exchange.
NOTED_SECONDS = 90*86400  # As long as it keeps a note, which a controller offering SOURCES drops with its message.
MOST = 20000  # Bounds memory only: the controller keeps six exchanges per member and conversation.
MARKED_SECONDS = 3600  # As Agent.withdrawn: longer than any turn that could still hold a copy runs.
FRI = 'fri'  # The author marked for one of Fri's own replies (see marked_by): never a member's.
# A summary of the member's that saved context quoted but no request carried: when it was made isn't known, so any
# message of theirs taken back may be one of its sources (see summary_sources; fri #383 item 2).
UNDATED = {'refreshed': None}
# Seconds before Fri's clock an edit's own time (Discord's) still counts as just made: clock skew and gateway delay.
FRESH = 15*60
BATCH = 10  # As many IDs as /chat/turn/withdraw/saved takes at once.
RETRY_SECONDS = (2, 15, 60, 300)  # After each failure, then every five minutes while it stays pending.
REREAD_SECONDS = 60  # At most this often, the controller's contract read again for a refusal (see reread).
RECORD = 'withdrawals.json'
CLOSED_RECORD = 'withdrawals.closed.json'  # Until when every saved context is held back (see fail_closed).
# Why, as the closed record says (see mark_closed): a record that couldn't be read, or more queued than the queue keeps.
UNREADABLE_CAUSE, CAPACITY_CAUSE = 'unreadable', 'capacity'
# How long saved context stays held back after a record that couldn't be read: as long as anything a withdrawal
# protects can be kept, by any controller (an exchange, or a note).
CLOSED_SECONDS = max(KEPT_SECONDS, NOTED_SECONDS)
SET_ASIDE = 3  # At most this many unreadable records kept aside, the newest, none past CLOSED_SECONDS.
UNREADABLE = re.compile(r'withdrawals\.unreadable-([0-9]{1,12})(?:-[0-9]{1,3})?\.json')
CHUNK = 2**20  # Characters read at a time: a record of any size costs time to read, not memory (see read_record).
LONGEST = 64  # Characters of a string kept, past any message ID, scope value or key a record uses.
DEEPEST = 2**20  # Containers open at once in a value passed over; json.loads gives up near a thousand.

LOG = logging.getLogger('fri.agent')


class SavedTurns:
    def __init__(self, agent):
        self.agent = agent
        # Discord message ID -> (when saved, the scope it was saved under, the member's revision then), oldest first.
        self.saved = {}
        # Each message of an exchange this Fri saved, and its reply's, as message ID -> (when saved, whose exchange,
        # the messages its reply drew on, as strings), oldest first: what saved context drew on, which no model reads
        # (see dependencies; fri #332). Bounded as `saved` is. Nothing for one saved before this started.
        self.drawn = {}
        self.tasks = set()  # The worker sending withdrawals, while it runs.
        self.marked = {}  # Message ID (str) -> when it was taken back, oldest first.
        # Message ID (str) -> its author (str; FRI for one of Fri's own replies), where known, as long as it's marked,
        # after its withdrawal has left the queue too: so a mark of someone else's message never holds a member's
        # summary back (see summary_sources; fri #382's round 1, S2). Gone with their "forget me".
        self.marked_by = {}
        self._pending = None  # Message ID (str) -> what to send for it; read from disk on first use.
        # When the first of those can no longer be kept, and the horizon that was worked out with (see expire).
        self.expiry = (0.0, None)
        self.timer = None
        self.generation = 0  # Each queued entry's own number: a reply to an older one never settles a newer one.
        self.erasures = {}  # Member (str) -> how many times they said "forget me" (see forget, kept).
        # What every edit's fingerprint is salted with, while this process runs, so two of a message's merge (see
        # fingerprinted). Never written down, as fingerprints aren't (see lasting).
        self.salt = salt()
        self.reread_at = 0.0  # time.monotonic() as the contract was last read again (see reread).
        self.closed_until = 0.0  # time.time() until which every saved context is held back (see fail_closed).
        self.closed_for = set()  # Why (UNREADABLE_CAUSE, CAPACITY_CAUSE), as far as it's known (see mark_closed).
        self.blocked = False  # An unreadable record not yet set aside: no queue write replaces it (see persist).
        # What the reader let go held back, but not yet written down: no queue write replaces the record (see persist).
        self.unwritten = False

    def offered(self):
        return CAPABILITY in getattr(self.agent.fri, 'capabilities', ())

    def capable(self, name):
        return name in getattr(self.agent.fri, 'capabilities', ())

    def entry(self, restored=False, queued_at=None, since=None, **what):
        """A queued withdrawal, with its own number and its member's erasure count now (see current). restored: loaded
        from what an earlier process left on disk, or rebuilt from one that was, which every entry rebuilt for that
        message keeps (see withdraw). Set only as it's read, never from what the file says, so one an earlier process
        left can't pass as this one's, and one queued here never as an earlier one's, whatever the clock does.

        queued_at: when it was queued, a whole second, rounded up (fri #329): after anything Fri saved with its message
        before the message was taken back, so what that saved lasts at most a horizon from then (see lasts). One read
        from disk keeps what it says, whatever the clock says now (Sol round 1 of #339): a clock set back since mustn't
        end it early. Now where it says nothing (main's record), so it's never earlier than the truth, and for one queued
        here; queued again (`since`, what the one it replaces said), now, but never earlier than before."""
        self.generation += 1
        user = (what.get('scope') or {}).get('user_id') or what.get('user_id')
        now = math.ceil(time.time())
        if restored and queued(queued_at):
            stamped = queued_at
        else:
            stamped = max(since, now) if queued(since) else now
        return {**what, 'queued_at': stamped, 'tries': 0, 'due': 0, 'gen': self.generation,
                **({'restored': True} if restored else {}), **({'erasure': self.erasure(user)} if user else {})}

    def horizon(self):
        """How long the controller, as it last said, keeps what a withdrawal names: a week, or a note's 90 days from one
        offering SOURCES."""
        return NOTED_SECONDS if self.capable(SOURCES) else KEPT_SECONDS

    def lasts(self, i, entry):
        """Until when what a queued withdrawal names can be kept: a horizon (see horizon) from the later of its message's
        time and when it was queued (fri #329). Its message's time alone isn't enough: an exchange or a note drawn from
        it is saved later, and kept from then."""
        return max(sent_at(int(i)), entry.get('queued_at', 0))+self.horizon()

    def expire(self):
        """What can no longer be kept goes as soon as anything asks what's held back, not at the next withdrawal, save
        or start (see prune): a hold, never sent, would otherwise go on holding its message back, and what waits on the
        queue (a member's summary), past its time (fri #358). Only once the first of them is due, or the horizon has
        changed with the controller, so a check stays cheap."""
        pending, now = self.pending, time.time()  # Read first: a start's read prunes too.
        if pending and (now >= self.expiry[0] or self.expiry[1] != self.horizon()):
            self.prune(now)

    @property
    def pending(self):
        """Withdrawals the controller hasn't taken yet: message ID -> {'scope', 'revision'} for one Fri saved an exchange
        with, {'server'} for one asked about by ID, each with 'deleted' (not an edit), 'tries', 'due' (time.time())
        and 'gen' (see drain); or {'hold'} for one read from disk that can't be sent but whose message stays held back.

        The record is read in pieces, without recursion: any size main reads, in bounded memory, building only what an
        entry uses and checking the rest as JSON (see read_record). Each entry is checked on its own. A record that still
        can't be trusted at all is never read as empty: every saved context is held back (see fail_closed). Nothing
        here stops Fri starting."""
        if self._pending is None:
            self._pending = {}
            self.closed_until = self.read_closed()
            path, found, overflow = self.record(), {}, {}
            if path is not None:
                try:
                    with path.open() as stream:
                        found = read_record(stream, overflow)
                except FileNotFoundError:
                    pass  # Nothing pending.
                except (OSError, ValueError) as exc:  # Unreadable and UnicodeDecodeError are ValueErrors.
                    overflow.clear()  # Nothing of it is read: all of it is held back (see fail_closed).
                    self.fail_closed(path, exc)
            held, dated = 0, 0
            for i, facts in found.items():
                if facts is not None:
                    # From an earlier process, whatever it says: replayed only where its revision is checked (sendable).
                    self._pending[i] = self.entry(**facts, restored=True)
                else:  # Its message is known, the rest isn't one Fri could send: held back, never released (round 4).
                    held += 1
                    self._pending[i] = self.entry(hold=True, restored=True)
                dated += not queued((facts or {}).get('queued_at'))
            if held:
                LOG.warning('%s pending withdrawals on disk were malformed, so their messages are held back unsent', held)
            if overflow.get('dropped'):
                self.overflowed(overflow)
            self.tidy()
            self.prune(time.time())
            now = time.time()
            for i, entry in self._pending.items():  # Taken back before the restart: still held back until they land.
                self.marked.setdefault(i, now)
                if author := (entry.get('scope') or {}).get('user_id') or entry.get('user_id'):
                    self.marked_by.setdefault(i, str(author))
            if dated or overflow.get('dropped'):
                # Dated by this start, or cut by the reader: saved now, not at the next withdrawal, so later starts keep
                # those dates instead of dating them again, each later than the last (fri #343). As any save, never over
                # a record whose cut isn't held back on disk yet (see persist): a failed write leaves the record as it
                # was, read and dated again, never earlier, by the next start.
                self.persist()
        return self._pending

    def overflowed(self, overflow):
        """The reader let the oldest go past twice the queue's capacity, to bound memory (see read_record), as the queue
        does past it (see prune), so the same holds (fri #329): every saved context is held back until nothing they named
        can be kept, from now on, and written down. Until that's on disk, the record isn't replaced (see persist), so a
        restart reads all of it again. Their own times count however the clock stands, one ahead of it too, and one
        that says no time it was queued counts from now (Sol round 1 of #339)."""
        now = math.ceil(time.time())
        latest = max(overflow['latest'] or 0, now if overflow['unknown'] or overflow['latest'] is None else 0)
        kept = NOTED_SECONDS if self.capable(SOURCES) else KEPT_SECONDS
        self.closed_until = max(self.closed_until, latest+kept)
        self.closed_for.add(CAPACITY_CAUSE)
        self.unwritten = not self.mark_closed(self.closed_until)
        LOG.warning('%s pending withdrawals on disk were past what the queue keeps, so every saved context is held back '
                    'for %s days%s', overflow['dropped'], max(1, int((self.closed_until-now)//86400)),
                    '; that could not be written down, so the record stays as it is' if self.unwritten else '')

    def fail_closed(self, path, exc):
        """A record that can't be trusted at all is never read as empty: what it held back would come back. Every saved
        context is held back instead, for as long as anything a withdrawal protects can be kept (CLOSED_SECONDS), across
        restarts. That is written down first, beside the record (CLOSED_RECORD); only then is the record set aside
        unread (renamed, for someone to recover). Until both are done it stays where it is and no queue write replaces it
        (see persist), so the next start finds it unreadable again. Fri still starts and answers, without saved context.
        Logged without its contents."""
        LOG.warning('Pending withdrawals could not be read (%s), so every saved context is held back for %s days',
                    type(exc).__name__, CLOSED_SECONDS//86400)
        self.blocked = True
        self.set_aside(path)

    def set_aside(self, path):
        """Closed until the longest horizon from now, on disk first, and then the unreadable record renamed out of the
        way. True once no queue write can replace it."""
        now = time.time()
        self.closed_until = max(self.closed_until, now+CLOSED_SECONDS)
        self.closed_for.add(UNREADABLE_CAUSE)
        if not self.mark_closed(self.closed_until):
            return False
        for n in range(100):
            aside = path.with_name(f'withdrawals.unreadable-{int(now)}{"" if n == 0 else f"-{n}"}.json')
            try:
                if aside.exists():
                    continue
                path.rename(aside)
            except FileNotFoundError:  # Gone already: nothing left to replace.
                pass
            except OSError as exc:
                LOG.warning('The unreadable withdrawals record could not be set aside: %s', type(exc).__name__)
                return False
            else:
                LOG.warning('The unreadable withdrawals record was set aside as %s', aside.name)
            self.blocked = False
            self.tidy()
            return True
        return False

    def marker(self):
        data = getattr(self.agent.fri, 'data', None)
        return None if data is None else data/CLOSED_RECORD

    def mark_closed(self, until):
        """Writes until when every saved context is held back, and why, as far as it's known (closed_for, for fri
        health; main reads only the time). True if it's on disk."""
        path = self.marker()
        if path is None:
            return False
        try:
            private_json(path, {'closed_until': until, 'why': sorted(self.closed_for)})
            if until >= self.closed_until:
                self.unwritten = False  # What the reader let go is held back on disk now too (see overflowed).
            return True
        except OSError as exc:
            LOG.warning('Until when saved context is held back could not be saved: %s', type(exc).__name__)
            return False

    def read_closed(self):
        """Until when every saved context is held back, as written down (see fail_closed). A deadline once written down
        is never cut short (Sol round 1 of #339): one past the longest horizon from now, from a clock set back since,
        stands as it is, and every write after it is at least as late (see mark_closed's callers). One that can't be
        read closes for the whole horizon from now, written back. One that's over is removed. Removing it by hand ends
        it early (see docs/operations.md). Why it was closed is read with it (see mark_closed); one that can't be read
        counts as an unreadable record."""
        path = self.marker()
        if path is None:
            return 0.0
        now = time.time()
        try:
            with path.open() as stream:
                text = stream.read(256)
            until, self.closed_for = deadline(text), causes(text)
        except FileNotFoundError:
            return 0.0
        except (OSError, ValueError):  # UnicodeDecodeError too.
            until = None
        if until is None:
            self.closed_for = {UNREADABLE_CAUSE}
        if until is not None and until <= now:
            try:
                path.unlink()
            except OSError:
                pass
            return 0.0
        if until is None:
            LOG.warning('Until when saved context is held back was unreadable, so it is %s days from now',
                        CLOSED_SECONDS//86400)
            until = now+CLOSED_SECONDS
            self.mark_closed(until)
        return until  # One past the longest horizon from now (a clock set back since) stands (Sol round 1 of #339).

    def tidy(self):
        """Unreadable records set aside, kept for someone to recover: the newest SET_ASIDE, none past CLOSED_SECONDS,
        when nothing they held can matter. One dated in the future counts as set aside now."""
        path = self.record()
        if path is None:
            return
        now = time.time()
        try:
            found = sorted(((min(int(m.group(1)), now), entry) for entry in path.parent.iterdir()
                            if (m := UNREADABLE.fullmatch(entry.name))), key=lambda pair: pair[0], reverse=True)
        except OSError as exc:
            LOG.warning('Unreadable withdrawals records could not be looked for: %s', type(exc).__name__)
            return
        for _, entry in found[SET_ASIDE:]+[pair for pair in found[:SET_ASIDE] if pair[0] <= now-CLOSED_SECONDS]:
            try:
                entry.unlink()
            except OSError:
                pass

    def closed(self):
        """Whether every saved context is held back now: a record couldn't be read (see fail_closed), or a channel or
        thread deleted in Discord isn't withdrawn yet (fri #358; see fri_channel_withdrawal)."""
        channels = getattr(self.agent, 'channel_withdrawals', None)
        return self.closed_by_record() or bool(channels and channels.holding())

    def closed_by_record(self):
        """Whether a record couldn't be read (see fail_closed): the hold status reports."""
        self.pending  # Read first: that's what can close it.
        return time.time() < self.closed_until

    def status(self):
        """For fri health and its file: until when every saved context is held back (None if it isn't), why, as far as
        it's known ('unreadable', 'capacity'; none for a deadline an older Fri wrote), whether queued withdrawals are
        saved to disk meanwhile, and if not, why ('unsaved_for', as closed_for), and how many wait. No IDs, nothing of
        the record."""
        self.expire()
        closed = self.closed_by_record()
        unsaved =UNREADABLE_CAUSE if self.blocked else CAPACITY_CAUSE if self.unwritten else None
        return {'closed_until': self.closed_until if closed else None,
                'closed_for': sorted(self.closed_for) if closed else [], 'saved_to_disk': unsaved is None,
                'unsaved_for': unsaved, 'pending': len(self.pending)}

    def record(self):
        data = getattr(self.agent.fri, 'data', None)
        return None if data is None else data/RECORD

    @staticmethod
    def lasting(entry):
        """What of a queued withdrawal goes to disk, so a restart can't release it before the controller took it (fri
        #252). One for an exchange this process saved, whole: it names its member, so their "forget me" finds it, and a
        save answered after one never records it (see kept). One by ID only as a hold, the message's ID and nothing else:
        Fri may not know whose it is, and a restart forgets any "forget me" before it, so it's never sent again from
        there (fri #224 round 3) and names no member; it holds the message back until nothing it names can be kept (see
        prune). A hold stays one. Either keeps when it was queued (see entry), which names no one."""
        if 'scope' in entry:  # An edit's fingerprint never: what it took out, even hashed, stays in memory only.
            return {key: value for key, value in entry.items()
                    if key not in ('gen', 'erasure', 'restored', 'fingerprint')}
        return {'hold': True, 'queued_at': entry['queued_at']}

    def persist(self):
        """The lasting withdrawals to disk (see lasting). Never over an unreadable record not yet set aside: it's set
        aside first, or nothing is written and every saved context stays held back for the longest horizon from now, so
        what's queued only in memory is covered too (see fail_closed). Nor over one whose oldest the reader let go
        before what holds them back is written down: that's written first, or nothing is (see overflowed)."""
        if path := self.record():
            pending = self.pending  # Read first: reading is what finds a record unreadable.
            if self.blocked and not self.set_aside(path):
                LOG.warning('Pending withdrawals were not saved: the unreadable record could not be set aside, so it '
                            'stays as it was and every saved context stays held back')
                return
            if self.unwritten and not self.mark_closed(self.closed_until):
                LOG.warning('Pending withdrawals were not saved: holding back what the queue let go could not be '
                            'written down, so the record stays as it was and every saved context stays held back')
                return
            self.unwritten = False
            try:
                private_json(path, {'pending': {i: self.lasting(entry) for i, entry in pending.items()}})
            except OSError as exc:  # Still queued in memory; resent while Fri runs.
                LOG.warning('Pending withdrawals could not be saved: %s', type(exc).__name__)

    def erasure(self, user):
        """How many times the member said "forget me": a save sent before one answers too late to record (see kept)."""
        return self.erasures.get(str(user), 0)

    def kept(self, payload, saved, began, erasure=None, unknown=False):
        """The controller saved this turn, or may have (`saved` None: no answer came). `began`: time.time() and
        time.monotonic() just before it was sent. One of its messages withdrawn since went while the save was on its
        way, before anything here knew of it, so its exchange goes now. erasure: the member's (see erasure) as it was
        sent; once they've said "forget me" since, the controller erased it, so nothing of it is recorded here.
        unknown: the saved context its requests carried drew on what this can't tell (see dependencies), so what it
        drew on can't be told either, wherever it's read again (fri #383 item 1)."""
        if not self.offered() or payload.get('mention_grant'):  # A mention keeps only a placeholder.
            return
        if erasure is not None and self.erasure(payload.get('user_id')) != erasure:
            return
        if saved is not None and saved.get('kept', True) is False:  # Memory off, or a removal since: nothing kept.
            return
        scope = {key: str(payload.get(key) or '') for key in ('guild_id', 'channel_id', 'conversation_id', 'user_id')}
        if not all(scope.values()):  # Not a turn this could name again; the save it follows still stands.
            return
        sources = [payload.get('source') or {}, *(row.get('source') or {} for row in payload.get('burst') or ())]
        ids = {int(item['id']) for item in sources  # A burst's earlier messages too.
               if item.get('kind') == 'discord_message' and str(item.get('id', '')).isdigit()}
        revision = payload.get('revision') if type(payload.get('revision')) is int else None
        now = time.time()
        for i in ids:
            self.saved.pop(i, None)
            self.saved[i] = (now, scope, revision)
        while self.saved and (next(iter(self.saved.values()))[0] < now-KEPT_SECONDS or len(self.saved) > MOST):
            del self.saved[next(iter(self.saved))]
        # What its reply drew on, as the save named it (see dependencies), by each of its sources and its reply's
        # message: an interaction's too, a /fri panel turn's, which saved context names it by, though only a Discord
        # message is ever withdrawn, so only those are kept above (fri #382's round 1, S1).
        drew = payload.get('drew_on')
        if isinstance(drew, list):
            reply = str(payload.get('reply_message_id') or '')
            own = {int(item['id']) for item in sources if str(item.get('id', '')).isdigit()}
            for i in [*own, *([int(reply)] if reply.isdigit() else [])]:
                self.drawn.pop(i, None)
                self.drawn[i] = (now, scope['user_id'], frozenset(str(item) for item in drew if str(item).isdigit()),
                                 bool(unknown))
            while self.drawn and (next(iter(self.drawn.values()))[0] < now-KEPT_SECONDS or len(self.drawn) > MOST):
                del self.drawn[next(iter(self.drawn))]
        wall, monotonic = began
        agent = self.agent
        deleted = {i for i in ids if agent.unsent.get(i, -1) >= monotonic}
        self.withdraw(deleted, deleted=True)
        self.withdraw(i for i in ids if i not in deleted and (agent.withdrawn.get(str(i), 0) >= wall or
                                                              agent.revised.get(i, 0) >= wall))

    def fingerprinted(self, known, fingerprint):
        """An edit's fingerprint (see fri_edit_fingerprint), with the one already queued for its message: what each
        edit took out, less what it says after the last, which is kept (gene #246 round 1, B3: edited back to what it
        said, nothing of that was taken out). One salted otherwise (none is, in one process) replaces it."""
        before = known.get('fingerprint')
        if not fingerprint:
            return before
        if not before or before.get('salt') != fingerprint.get('salt'):
            return fingerprint
        taken = (set(before['removed']) | set(fingerprint['removed'])) - set(fingerprint['kept'])
        return {'salt': fingerprint['salt'], 'kept': fingerprint['kept'], 'removed': sorted(taken)[:MOST_HASHES]}

    def withdraw(self, ids, server=None, deleted=False, authors=None, edited_at=None, fingerprints=None):
        """Messages taken back: each exchange Fri saved with one goes, asked for once. server: the one Fri saves
        exchanges said where they were taken back under (see Agent.saved_in); any other one is then asked about by
        ID, ten at a time, from a controller offering it: a deletion whatever its message's age, since what drew on it
        can be saved long after it was sent (a note learned from an old message quoted in a newer conversation, fri
        #342), and its entry lasts from when it was queued (see lasts); an edit while its message is young enough to be
        in an exchange still (or in a note, from a controller offering SOURCES), as an update with no fresh edit, a
        pin of one edited long ago say, looks the same here; and an edit of an older message whose own time,
        edited_at (Discord's), is fresh (see fresh), as what drew on it can be saved long after it was sent too (fri
        #358 E1). That asks as main does, with no time, so what was saved from it in the minutes since goes as well,
        and a refresh repeating that edit's time inside the window asks again; both take more than they must, never
        less. deleted: not edited, so what carried them goes too (gene #141, #150). authors: message ID -> its author,
        where Fri knows it, so that one by ID can be kept on disk (see lasting). Each is held back from requests from
        now on (see withhold). A change to a queued one is a new entry (see drain)."""
        authors = {int(i): str(user) for i, user in (authors or {}).items() if str(user).isdigit()}
        ids = [int(i) for i in ids]
        horizon = NOTED_SECONDS if self.capable(SOURCES) else KEPT_SECONDS
        queued, now = False, time.time()
        just_edited = fresh(edited_at, now)
        for i in ids:
            known = self.pending.get(str(i), {})
            entry = self.saved.pop(i, None)
            # An edit's fingerprint (message ID -> what it took out), with the one queued for it: a deletion needs none,
            # and a new one is queued afresh where it adds something (gene #150, #246).
            printed = self.fingerprinted(known, None if deleted else (fingerprints or {}).get(i))
            gone = deleted or known.get('deleted') is True
            extra = {'fingerprint': printed} if printed and not gone else {}
            adds = bool(extra) and printed != known.get('fingerprint')
            if entry is not None:
                if self.offered():
                    self.pending[str(i)] = self.entry(scope=entry[1], revision=entry[2], restored=known.get('restored'),
                                                      deleted=gone, since=known.get('queued_at'), **extra)
                    queued = True
            elif server is not None and (deleted or just_edited or sent_at(i) > now-horizon):
                if self.offered() and self.capable(SAVED_CAPABILITY):
                    if known.get('hold'):  # Held from disk: it stays held, and on disk, whatever comes by ID.
                        pass
                    elif 'scope' in known:  # Fri knows where it was saved: that one stands, deleted or not.
                        if (deleted and not known.get('deleted')) or adds:
                            self.pending[str(i)] = self.entry(**{key: known[key] for key in ('scope', 'revision')},
                                                              deleted=gone, restored=known.get('restored'),
                                                              since=known.get('queued_at'), **extra)
                            queued = True
                    elif (not known or (deleted and not known.get('deleted')) or (i in authors and 'user_id' not in known)
                          or adds):
                        author = authors.get(i, known.get('user_id'))
                        self.pending[str(i)] = self.entry(server=str(server), deleted=gone,
                                                          restored=known.get('restored'),
                                                          since=known.get('queued_at'),
                                                          **({'user_id': author} if author else {}), **extra)
                        queued = True
            else:
                continue  # Nothing Fri saves could hold it.
            self.marked.pop(str(i), None)
            self.marked[str(i)] = now
            # Its author, where known: named, the member whose exchange it was, or as it was queued before.
            if author := (authors.get(i) or (entry[1].get('user_id') if entry is not None else None)
                          or (known.get('scope') or {}).get('user_id') or known.get('user_id')):
                self.marked_by[str(i)] = str(author)
        if deleted:  # Fri's replies in view that carried them go too (gene #141), and count as taken back.
            for mid in getattr(self.agent, 'withdrawn_from', lambda ids: ())(ids):
                self.marked.pop(mid, None)
                self.marked[mid] = now
                self.marked_by[mid] = FRI
        self.prune(now)
        if queued:
            self.persist()
            self.kick()

    def depends_on(self, messages):
        """What saved replies with these messages (strings) drew on, as far as this Fri saved them, and what those drew
        on in turn, through Fri's own replies among them (see drawn); and whether one of them drew on what it can't
        tell: a saved reply it didn't save itself, or one it saved whose own requests carried such a reply (see kept's
        unknown), however far back (fri #383 item 1). What's known stands either way, so a reply known to draw on a
        message taken back goes with it (Sol round 1 of #386, B1). Each read once, so a cycle ends."""
        found, todo, unknown = set(), [str(i) for i in messages if str(i).isdigit()], False
        known = [i for i in todo if int(i) in self.drawn]
        if todo and not known:
            unknown = True
        seen = set()
        while known:
            i = known.pop()
            if i in seen:
                continue
            seen.add(i)
            unknown = unknown or self.drawn[int(i)][3]
            for item in self.drawn[int(i)][2]:
                found.add(item)
                if int(item) in self.drawn and item not in seen:
                    known.append(item)
        return found, unknown

    @staticmethod
    def quotes(turn, member=None):
        """Whether a saved reply drew on the member's summary, any generation of it, as the controller names it (its
        `summaries`, a covered rebuild's too); with no member known, anyone's (fri #383 item 2). One whose `summaries`
        can't be read (see unreadable) may have quoted anyone's."""
        if unreadable(turn):
            return True
        return any(member is None or str(item['user_id']) == str(member) for item in turn.get('summaries') or ())

    def dependencies(self, context):
        """What the saved context a request carried drew on (fri #332): each saved exchange's reply, as its messages
        name it here (see depends_on), as message IDs (strings); and whether any reply among them drew on what this
        can't tell. Exchanges elsewhere hold only the member's own words, never a reply. Never what a model reads."""
        found, unknown = set(), False
        for messages in (exchange(turn) for turn in context.get('turns', []) if turn.get('reply')):
            drew, cant_tell = self.depends_on(messages - {''})
            found |= drew
            unknown = unknown or cant_tell
        return found, unknown

    def summary_sources(self, summary, member=None, taken=None):
        """Whether one of the messages taken back here (`taken`; by default, those queued and not yet taken, and those
        marked within the hour) may be a source of the member's summary (fri #332, #324): one of the member's own
        messages, sent before the summary was made. One sent since, or one whose author is known and someone else,
        can't be. member: the summary's, if known."""
        if not summary:
            return False
        made = summary.get('refreshed') if isinstance(summary, dict) else None
        now = time.time()
        authors = dict(self.marked_by)  # Kept with the mark once the withdrawal has left the queue (see marked_by).
        for i, entry in self.pending.items():
            if author := (entry.get('scope') or {}).get('user_id') or entry.get('user_id'):
                authors[i] = str(author)
        if taken is None:
            taken = set(self.pending) | {i for i, at in self.marked.items() if at >= now-MARKED_SECONDS}
        taken = {str(i) for i in taken}
        return any(i.isdigit() and (type(made) not in (int, float) or sent_at(int(i)) <= made)  # Not known: any.
                   and (member is None or authors.get(i) in (None, str(member))) for i in taken)

    def taken_since(self, depends, recent, member=None):
        """Whether the saved context a reply's requests carried may hold a message taken back since (fri #332), so the
        reply is held back: `depends`, what that context drew on (see dependencies), as chat_once kept it; `recent`, the
        messages taken back here since the turn began. One of what it drew on taken back since; any taken back since,
        where a saved reply it carried drew on what this can't tell (one saved before this started); or one that may be
        a source of the member's summary it carried (see summary_sources). One taken back before is withheld from the
        request already (see withhold)."""
        if not depends:
            return False
        recent = {str(i) for i in recent}
        if (recent & depends['messages']) or (depends['unknown'] and recent):
            return True
        return self.summary_sources(depends.get('summary'), member, recent)

    def forget(self, user):
        """The member's "forget me": the controller erased everything their withdrawals could still name, so nothing
        about them stays queued, on disk or here, to bring their IDs back. One already on its way carries the revision
        it was saved under, which the erasure moved, so the controller writes nothing for it (gene #141)."""
        user = str(user)
        self.erasures[user] = self.erasures.get(user, 0)+1
        for i in [i for i, entry in self.saved.items() if entry[1].get('user_id') == user]:
            del self.saved[i]
        for i in [i for i, entry in self.drawn.items() if entry[1] == user]:
            del self.drawn[i]
        for i in [i for i, author in self.marked_by.items() if author == user]:
            del self.marked_by[i]
        mine = [i for i, entry in self.pending.items()
                if (entry.get('scope') or {}).get('user_id') == user or entry.get('user_id') == user]
        for i in mine:
            del self.pending[i]
        self.persist()

    def prune(self, now):
        """Marks an hour, and queued withdrawals as long as anything they name can be kept (see lasts), at most MOST of
        them. Past that, those whose names can be kept least long go first, but never released (fri #256): every saved
        context is held back instead, until nothing they name can be kept, written down first (see mark_closed), so a
        restart keeps it too. If that can't be written, none goes."""
        while self.marked and len(self.marked) > MOST*5:
            del self.marked[next(iter(self.marked))]
        for i, at in list(self.marked.items()):
            if at >= now-MARKED_SECONDS:
                break  # The rest are newer.
            if self._pending is None or i not in self._pending:
                del self.marked[i]
        for i in [i for i in self.marked_by if i not in self.marked]:  # An author lasts as long as its mark.
            del self.marked_by[i]
        if self._pending is None:
            return
        lasting = {i: self.lasts(i, entry) for i, entry in self._pending.items()}
        for i in [i for i, until in lasting.items() if until <= now]:
            del self._pending[i], lasting[i]
        self.expiry = (min(lasting.values(), default=math.inf), self.horizon())
        extra =sorted(lasting, key=lambda i: (lasting[i], int(i)))[:max(0, len(lasting)-MOST)]
        if not extra:
            return
        until, causes_then = max(self.closed_until, max(lasting[i] for i in extra)), set(self.closed_for)
        self.closed_for.add(CAPACITY_CAUSE)
        if not self.mark_closed(until):
            self.closed_for = causes_then
            LOG.warning('More than %s withdrawals are queued, and holding saved context back could not be written down, '
                        'so none was dropped', MOST)
            return
        self.closed_until = until
        for i in extra:
            del self._pending[i]
        LOG.warning('%s pending withdrawals dropped, those whose names can be kept least long first: more than %s '
                    'queued, so every saved context is held back for %s days', len(extra), MOST,
                    max(1, int((until-now)//86400)))

    def withholding(self):
        """Whether anything is held back now, so a memory view should be screened (see screen)."""
        self.expire()
        now = time.time()
        return self.closed() or bool(self.pending) or any(at >= now-MARKED_SECONDS for at in self.marked.values())

    def withheld(self, ids):
        """Of these message IDs, those taken back while a turn may still hold a copy, or whose withdrawal the
        controller hasn't taken yet; all of them while a record couldn't be read (see fail_closed)."""
        self.expire()
        if self.closed():
            return {str(i) for i in ids}
        now = time.time()
        return {str(i) for i in ids if str(i) in self.pending or self.marked.get(str(i), 0) >= now-MARKED_SECONDS}

    def withhold(self, context, said=None, member=None):
        """Right before a request: what the turn read that holds a message withheld (see withheld) leaves its context.
        An exchange of its history, a pending question, an exchange elsewhere, a note learned from it, and one whose
        source the controller doesn't know but whose words a withheld message of its history holds; and the member's
        summary, which may have read it (the controller drops it once it may have). Returns the IDs of what's left,
        which delivery and the save then check again (see Agent.reply_once). said: gets the withheld messages' words,
        for a memory view to judge notes by (see screen); kept nowhere else. While a record couldn't be read, every
        saved context goes, notes of unknown source too (see fail_closed).

        A saved reply whose words drew on a withheld message, as far as this Fri knows (see dependencies), goes too, as
        one of its own messages would; and the member's summary while a message taken back may be one of its sources
        (see summary_sources: fri #332, #324). member: the summary's, where the caller knows it."""
        if self.closed():
            context['turns'], context['pending'], context['elsewhere'] = [], None, []
            if context.get('personal_memory'):
                context['personal_memory']['notes'] = []
            for key in ('note_sources', 'profile_origins'):
                if context.get(key) is not None:
                    context[key] = {}
            context['member_summary'] = None
            return set()
        found = held(context)
        gone = self.withheld(found)
        drew = [self.depends_on(exchange(turn) - {''})[0] for turn in context.get('turns', [])]
        taken = self.withheld({i for ids in drew for i in ids})
        summary = context.get('member_summary')
        if summary and self.summary_sources(summary, member):
            context['member_summary'] = None
        # A saved reply that quoted the member's summary, any generation of it, goes with it, while a message taken back
        # may be one of its sources; with no summary here to date it by, any of theirs (fri #383 item 2). So do the
        # replies that drew on that one, as on a message taken back.
        quoting = [self.quotes(turn, member) for turn in context.get('turns', [])]
        if any(quoting) and self.summary_sources(summary or UNDATED, member):
            taken |= {i for turn, quotes in zip(context.get('turns', []), quoting) if quotes for i in exchange(turn) - {''}}
        else:
            # One whose summaries can't be read can't be dated or tied to anyone: it goes, and what drew on it (S4).
            quoting = [unreadable(turn) for turn in context.get('turns', [])]
            taken |= {i for turn, bad in zip(context.get('turns', []), quoting) if bad for i in exchange(turn) - {''}}
        if not gone and not taken and not any(quoting):
            return set(found)
        said, history = ([] if said is None else said), []
        for turn, depends, quotes in zip(context.get('turns', []), drew, quoting):
            rows = turn.get('burst') or ()
            if not exchange(turn) & gone and not depends & taken and not quotes:
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

    def screen(self, context, state=None, member=None):
        """A memory view (what do you remember, My memory) holds back what a request would (see withhold): `context`,
        the member's chat context, is screened as a request's is, and `state`, their notes as My memory lists them
        (with the messages each came from), loses those a withheld message taught. The summary goes too once anything
        of theirs is held back, or while a message taken back may be one of its sources, its exchange gone from those
        Gene keeps or not (see summary_sources; fri #324). member: whose memory it is. Returns the screened state, or
        None without one."""
        said = []
        gone = bool(context is not None and self.withheld(held(context)))
        gone = gone or self.summary_sources((state or {}).get('summary'), member)
        if context is not None:
            self.withhold(context, said, member)
        if state is None:
            return None
        if self.closed():
            return {**state, 'entries': [], **({'summary': None} if 'summary' in state else {})}
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
        post). One the controller can't safely take now waits too, held back: a request it no longer offers since a
        rollback, or one left on disk by an earlier process, for a controller without the revision guard (SOURCES),
        which a rolled-back Fri's "forget me" may have outlived. The contract is read again on the retry schedule, so
        one rolled forward is used. Such an entry goes only once it can no longer matter: older than the controller
        keeps what it names (prune), or its member erased here (forget). Each answer settles only the entries it was
        sent for: one queued for the same message meanwhile is newer, so it stands and goes next. Everything is worked
        out again after each wait, and whatever stays is scheduled however the worker ends."""
        tried = set()
        try:
            while True:
                if not self.due(tried):
                    break
                if [i for i in self.due(tried) if not self.sendable(self.pending[i])]:
                    await self.reread()  # A controller rolled back may have come forward again.
                due = self.due(tried)  # Again: anything may have changed while it read.
                for i in [i for i in due if not self.sendable(self.pending[i])]:
                    self.wait(self.pending[i])
                    tried.add(self.pending[i]['gen'])
                due = [i for i in due if self.sendable(self.pending[i])]
                if not due:
                    self.persist()
                    continue
                placed = [i for i in due if 'scope' in self.pending[i]]
                if placed:
                    batch = [(placed[0], self.pending[placed[0]])]
                else:
                    first = self.pending[due[0]]
                    batch = [(i, self.pending[i]) for i in due if self.pending[i].get('server') == first['server']
                             and self.pending[i].get('deleted') is first.get('deleted')][:BATCH]
                tried.update(entry['gen'] for _, entry in batch)
                sent = await self.post(batch)
                for i, entry in batch:
                    if self.pending.get(i) is not entry:
                        continue  # Replaced or forgotten while it was sent: the newer one stands, or none.
                    if (i, entry['gen']) in sent:
                        del self.pending[i]
                    else:
                        self.wait(entry)
                self.persist()
        finally:
            self.schedule()

    def due(self, tried):
        now = time.time()
        self.prune(now)
        return sorted(i for i, entry in self.pending.items()
                      if not entry.get('hold') and entry['due'] <= now and entry['gen'] not in tried)

    def current(self, i, entry):
        """Right before any request for it: still this entry, queued, for a member who hasn't said "forget me" since
        it was, and one the controller can safely take as it last said (see sendable)."""
        if self.pending.get(i) is not entry:
            return False
        user = (entry.get('scope') or {}).get('user_id') or entry.get('user_id')
        return (not user or self.erasure(user) == entry.get('erasure', 0)) and self.sendable(entry)

    def sendable(self, entry):
        """Whether the controller, as it last said, offers the request this withdrawal needs, and can take it safely:
        one from an earlier process (read from disk, however it was rebuilt since: see entry) only with the revision
        guard (SOURCES), and only with the revision it was saved under, so a member erased since through another Fri
        gets nothing written."""
        if entry.get('hold') or not self.offered() or ('scope' not in entry and not self.capable(SAVED_CAPABILITY)):
            return False
        return not entry.get('restored') or (self.capable(SOURCES) and entry.get('revision') is not None)

    @staticmethod
    def wait(entry):
        entry['tries'] += 1
        entry['due'] = time.time()+RETRY_SECONDS[min(entry['tries'], len(RETRY_SECONDS))-1]

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
        due = [entry['due'] for entry in self.pending.values() if not entry.get('hold')]  # A hold is never sent.
        if not due:
            return
        wait = max(0.0, min(due)-time.time())
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

    def request(self, batch):
        """The request for these, in the shape the controller takes as it last said (see extra)."""
        (first, entry), ids = batch[0], [i for i, _ in batch]
        if 'scope' in entry:
            return '/chat/turn/withdraw', {**entry['scope'], 'message_id': str(first), **self.extra(entry),
                                           **self.printed(entry)}
        printed = {i: self.printed(each)['fingerprint'] for i, each in batch if self.printed(each)}
        return '/chat/turn/withdraw/saved', {'guild_id': entry['server'], 'message_ids': ids,
                                             **self.extra({'deleted': entry.get('deleted')}),
                                             **({'fingerprints': printed} if printed else {})}

    def printed(self, entry):
        """An edit's fingerprint, for a controller offering FINGERPRINTS: an older one would refuse the field."""
        if entry.get('deleted') or not entry.get('fingerprint') or not self.capable(FINGERPRINTS):
            return {}
        return {'fingerprint': entry['fingerprint']}

    async def post(self, batch):
        """Sent as the controller takes it. A refusal may come from a controller rolled back alone, which doesn't know a
        field or a request Fri learned at its start: Fri reads its contract again and, if the request it would make now
        differs, sends that once more right away. Before each request (the first and that one), only the entries still
        queued as they were, for members who haven't erased since, and safe to send as the controller now stands, go
        (see current): any other waits, or was dropped meanwhile. Returns (message ID, generation) of those taken.
        Logged without the words."""
        made = None
        for attempt in (1, 2):
            live = [(i, entry) for i, entry in batch if self.current(i, entry)]
            if not live:
                return set()
            path, payload = self.request(live)
            if (path, payload) == made:
                return set()  # The same request again: nothing changed, so it waits.
            made, ids = (path, payload), ', '.join(i for i, _ in live)
            text = (f'Withdrawn message{"s" if len(live) > 1 else ""} %s {"were" if len(live) > 1 else "was"} not '
                    'dropped from saved turns: %s')
            try:
                await self.agent.fri.api('POST', path, payload)
                return {(i, entry['gen']) for i, entry in live}
            except ControllerRejected as exc:
                failed(text, ids, exc)
                if attempt == 2 or exc.status is None or not await self.reread():
                    return set()
            except Exception as exc:
                failed(text, ids, exc)
                return set()
        return set()


class Unreadable(ValueError):
    """The record isn't JSON Fri could have written; nothing in it can be trusted."""


LONG = type('Long', (), {'__repr__': lambda self: 'LONG', '__slots__': ()})()  # A string past LONGEST: checked, not kept.
OTHER = type('Other', (), {'__repr__': lambda self: 'OTHER', '__slots__': ()})()  # Any other value, checked, not kept.
WHITESPACE = re.compile(r'[ \t\n\r]*')
PLAIN = re.compile(r'[^"\\\x00-\x1f]*')  # What a JSON string holds as itself.
DIGITS = re.compile(r'[0-9]*')
HEX = re.compile(r'[0-9a-fA-F]{4}')
LITERALS = (('true', True), ('false', False), ('null', None), ('NaN', OTHER), ('Infinity', OTHER))
ID = re.compile(r'[0-9]{1,25}')
# A run of plain values each followed by a comma: numbers, literals, strings without escapes, and arrays and objects of
# only those. Passed over a window at a time, as the value-by-value reading would, so long arrays and objects cost C
# time, not a Python step per value.
SPACES = r'[ \t\n\r]*'
TEXT = r'"[^"\\\x00-\x1f]*"'
PLAIN_VALUE = rf'(?:-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?|true|false|null|NaN|-?Infinity|{TEXT})'
PAIR = rf'{TEXT}{SPACES}:{SPACES}{PLAIN_VALUE}'
FLAT = (rf'(?:{PLAIN_VALUE}|\[{SPACES}(?:{PLAIN_VALUE}(?:{SPACES},{SPACES}{PLAIN_VALUE})*+)?{SPACES}\]|'
        rf'\{{{SPACES}(?:{PAIR}(?:{SPACES},{SPACES}{PAIR})*+)?{SPACES}\}})')
ITEMS = re.compile(rf'(?:{SPACES}{FLAT}{SPACES},)*+')
MEMBERS = re.compile(rf'(?:{SPACES}{TEXT}{SPACES}:{SPACES}{FLAT}{SPACES},)*+')
WINDOW = 2**16
SCOPE = {'guild_id': r'[0-9]{1,25}', 'channel_id': r'[0-9]{1,25}', 'conversation_id': r'(private|public):[0-9]{1,25}',
         'user_id': r'[0-9]{1,25}'}


class Reader:
    """JSON from a stream, a piece at a time, accepted where json.loads accepts it (short of its own nesting and digit
    limits, which it doesn't share). Only what's asked for is built, and at most LONGEST characters of a string; the
    rest is checked and passed over. So a record of any size or nesting costs bounded memory, and no recursion."""

    def __init__(self, stream):
        self.stream, self.text, self.at, self.ended = stream, '', 0, False

    def ahead(self, n=1):
        """The next n characters (fewer only where the record ends), read in as needed."""
        while len(self.text)-self.at < n and not self.ended:
            more = self.stream.read(CHUNK)
            self.ended = not more
            self.text, self.at = self.text[self.at:]+more, 0
        return self.text[self.at:self.at+n]

    def run(self, pattern, keep=0):
        """Past a run of what `pattern` matches, across pieces: how long it was, and its first `keep` characters."""
        count, kept = 0, ''
        while True:
            self.ahead()
            end = pattern.match(self.text, self.at).end()
            if len(kept) < keep:
                kept += self.text[self.at:min(end, self.at+keep-len(kept))]
            count, self.at = count+end-self.at, end
            if end < len(self.text) or self.ended:
                return count, kept

    def space(self):
        self.run(WHITESPACE)

    def expect(self, mark):
        if self.ahead() != mark:
            raise Unreadable(f'expected {mark!r}')
        self.at += 1

    def string(self):
        """A string, past its closing quote: itself, or LONG past LONGEST characters."""
        self.expect('"')
        raw, room, long = '', 12*LONGEST, False  # A character takes at most twelve in JSON (a surrogate pair).
        while True:
            count, part = self.run(PLAIN, keep=0 if long else room+1-len(raw))
            raw += part
            long = long or count > len(part) or len(raw) > room
            mark = self.ahead()
            if mark == '"':
                self.at += 1
                return LONG if long else json.decoder.scanstring(raw+'"', 0)[0]
            if mark != '\\':
                raise Unreadable('a string never ends or holds something JSON doesn\'t')
            escape = self.ahead(6)
            size = (6 if escape[1:2] == 'u' and HEX.fullmatch(escape, 2) else
                    2 if escape[1:2] and escape[1] in '"\\/bfnrt' else 0)
            if not size:
                raise Unreadable('an escape JSON doesn\'t have')
            if not long:
                raw += escape[:size]
            self.at += size

    def number(self):
        """A number, past it: an integer of at most 25 digits as itself, any other as OTHER."""
        sign = ''
        if self.ahead() == '-':
            sign, self.at = '-', self.at+1
        first = self.ahead()
        if first == '0':
            count, digits, self.at = 1, '0', self.at+1
        elif '1' <= first <= '9':
            count, digits = self.run(DIGITS, keep=26)
        else:
            raise Unreadable('expected a value')
        whole = True
        if self.ahead() == '.':
            self.at += 1
            if not self.run(DIGITS)[0]:
                raise Unreadable('expected a digit')
            whole = False
        if self.ahead() in ('e', 'E'):
            self.at += 1
            if self.ahead() in ('+', '-'):
                self.at += 1
            if not self.run(DIGITS)[0]:
                raise Unreadable('expected a digit')
            whole = False
        return int(sign+digits) if whole and count <= 25 else OTHER

    def value(self):
        """A value, past it: a string, an integer, true, false or null as itself; anything else checked, as OTHER."""
        mark = self.ahead()
        if mark == '"':
            return self.string()
        if mark in ('{', '['):
            self.skip()
            return OTHER
        if mark == '-' and self.ahead(9) == '-Infinity':
            self.at += 9
            return OTHER
        if mark == '-' or '0' <= mark <= '9':
            return self.number()
        for word, value in LITERALS:
            if self.ahead(len(word)) == word:
                self.at += len(word)
                return value
        raise Unreadable('expected a value')

    def plain(self, pattern):
        """Past a run of plain values each followed by a comma (ITEMS or MEMBERS), a window at a time. One cut off where
        the text read so far ends is left for the value-by-value reading."""
        while (end := pattern.match(self.text, self.at, min(len(self.text), self.at+WINDOW)).end()) > self.at:
            self.at = end

    def key(self):
        """An object's key and its colon."""
        name = self.string()
        self.space()
        self.expect(':')
        self.space()
        return name

    def skip(self):
        """Past one value, checked as json.loads checks it, none of it built. Returns None."""
        closes = bytearray()  # What each container open ends with.
        while True:
            self.space()
            mark = self.ahead()
            if mark in ('{', '['):
                if len(closes) >= DEEPEST:
                    raise Unreadable('nested deeper than any record')
                end = '}' if mark == '{' else ']'
                self.at += 1
                self.space()
                if self.ahead() != end:
                    closes.append(ord(end))
                    if mark == '{':
                        self.key()
                    continue
                self.at += 1
            else:
                self.value()
            while closes:  # The value is read: past each container that ends here.
                self.space()
                mark = self.ahead()
                if mark == ',':
                    self.at += 1
                    if closes[-1] == ord('}'):
                        self.plain(MEMBERS)
                        self.space()
                        self.key()
                    else:
                        self.plain(ITEMS)
                    break
                if mark != chr(closes[-1]):
                    raise Unreadable('expected a comma or a close')
                self.at += 1
                closes.pop()
            else:
                return None

    def members(self):
        """Each key of the object here, with the reader at its value, which the caller reads before asking for more."""
        self.expect('{')
        self.space()
        if self.ahead() == '}':
            self.at += 1
            return
        while True:
            yield self.key()
            self.space()
            mark = self.ahead()
            if mark == '}':
                self.at += 1
                return
            if mark != ',':
                raise Unreadable('expected a comma or a close')
            self.at += 1
            self.space()


def read_record(record, overflow=None):
    """The pending withdrawals a record holds (a stream, or its text): message ID -> its 'scope', 'revision' and
    'deleted', or None for one whose message ID is readable but whose rest isn't an entry Fri could send, each with
    'queued_at' where it says one (see entry). Accepted where json.loads accepts it, and read as it reads (a key given
    twice counts as given last). Only those fields are built; anything else is checked as JSON and passed over, so
    neither size nor nesting can fail the read or exhaust memory. At most MOST*2 entries are held while it reads, the
    newest messages', as the queue keeps MOST (see prune). overflow, a dict: gets how many it let go ('dropped'), the
    latest time any of them was sent or queued ('latest', None if none), and whether any said no time it was queued
    ('unknown'), for the queue to hold back what they named (fri #329, see SavedTurns.overflowed). Raises Unreadable if
    it isn't JSON, or isn't a record of pending withdrawals."""
    overflow = {} if overflow is None else overflow
    overflow.update(dropped=0, latest=None, unknown=False)
    reader = Reader(io.StringIO(record) if isinstance(record, str) else record)
    reader.space()
    if reader.ahead() != '{':
        raise Unreadable('not a record of pending withdrawals')
    pending, nameless = None, 0
    for name in reader.members():
        if name == 'pending' and reader.ahead() == '{':
            pending, nameless = entries(reader, overflow)
        else:
            reader.skip()
            if name == 'pending':
                pending = None
    reader.space()
    if reader.ahead():
        raise Unreadable('more after the record')
    if pending is None:
        raise Unreadable('no record of pending withdrawals in it')
    if nameless:
        LOG.warning('%s pending withdrawals on disk named no message, so they were left out', nameless)
    return pending


def entries(reader, overflow):
    found, nameless = {}, 0
    for i in reader.members():
        facts = entry(reader) if reader.ahead() == '{' else reader.skip()
        if not (isinstance(i, str) and ID.fullmatch(i)):
            nameless += 1
            continue
        found[i] = facts
        if len(found) > 2*MOST:
            for old in sorted(found, key=int)[:len(found)-MOST]:
                queued = (found.pop(old) or {}).get('queued_at')
                at = max(sent_at(int(old)), queued or 0)
                overflow.update(dropped=overflow['dropped']+1, unknown=overflow['unknown'] or queued is None,
                                latest=at if overflow['latest'] is None else max(overflow['latest'], at))
    return found, nameless


def entry(reader):
    """An entry's scope, revision and whether it was a deletion; {'hold': True} for a hold (see SavedTurns.lasting);
    None if neither, its scope not one /chat/turn/withdraw takes. Either with 'queued_at', where it says a whole second
    (see SavedTurns.entry). Nothing else of it decides anything."""
    scope, revision, deleted, hold, stamp = None, None, False, False, {}
    for name in reader.members():
        if name == 'scope':
            scope = place(reader) if reader.ahead() == '{' else reader.skip()
        elif name == 'revision':
            value = reader.value()
            revision = value if type(value) is int and 0 <= value < 2**63 else None
        elif name == 'deleted':
            deleted = reader.value() is True
        elif name == 'hold':
            hold = reader.value() is True
        elif name == 'queued_at':
            value = reader.value()
            stamp = {'queued_at': value} if queued(value) else {}
        else:
            reader.skip()
    if scope is None:
        return {'hold': True, **stamp} if hold else None
    return {'scope': scope, 'revision': revision, 'deleted': deleted, **stamp}


def place(reader):
    """Where an entry's exchange was saved, as /chat/turn/withdraw takes it, or None."""
    found = {}
    for name in reader.members():
        value = reader.value()
        if name in SCOPE:
            found[name] = value
    if all(isinstance(found.get(key), str) and re.fullmatch(pattern, found[key]) for key, pattern in SCOPE.items()):
        return {key: found[key] for key in SCOPE}
    return None


def deadline(text):
    """The time a closed marker holds (see SavedTurns.read_closed), or None if it holds none Fri could have written."""
    try:
        value = json.loads(text)
    except ValueError:
        return None
    value = value.get('closed_until') if isinstance(value, dict) else None
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def causes(text):
    """Why a closed marker says saved context is held back (see SavedTurns.mark_closed): those of UNREADABLE_CAUSE and
    CAPACITY_CAUSE it names, none for one an older Fri wrote or one that can't be read."""
    try:
        value = json.loads(text)
    except ValueError:
        return set()
    why = value.get('why') if isinstance(value, dict) else None
    return {cause for cause in why if cause in (UNREADABLE_CAUSE, CAPACITY_CAUSE)} if isinstance(why, list) else set()


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


def queued(value):
    """Whether it's a time a withdrawal was queued, as Fri writes one down (see SavedTurns.entry): whole seconds."""
    return type(value) is int and 0 <= value < 2**63


def sent_at(message_id):
    """When a Discord message was sent, from its ID."""
    return ((message_id >> 22)+1420070400000)/1000


def fresh(edited_at, now):
    """Whether an edit was just made, by its own time (Discord's) against Fri's clock `now`: no more than FRESH before
    it, or any time after it (a clock behind Discord's). A pin or an embed refresh repeats an earlier edit's time,
    which isn't, unless that edit was itself just made (fri #358 E1)."""
    return edited_at is not None and edited_at >= now-FRESH


def saved_lately(message_id):
    """Sent recently enough that the controller may keep an exchange holding it, from the time in its Discord ID."""
    return sent_at(message_id) > time.time()-KEPT_SECONDS


def unreadable(turn):
    """Whether a saved reply's `summaries` aren't as the controller writes them, a list of {'user_id', 'generation'}: whose
    summary it quoted, or when, can't be told then (Sol round 1 of #386, S4)."""
    items = turn.get('summaries')
    return items is not None and not (isinstance(items, list) and all(
        isinstance(item, dict) and str(item.get('user_id', '')).isdigit() for item in items))


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
