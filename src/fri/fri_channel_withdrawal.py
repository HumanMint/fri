"""Channels and threads deleted in Discord (fri #358 D1).

Deleting a channel or a thread deletes every message in it, and Discord sends no event for each one, so nothing Fri
withdrew by message ID reached what it saved from there: a server-profile note learned in a deleted channel still
reached a later private-panel request once the channel had left the cache. The deletion's own event now queues the
channel (discord.py sends one for each thread under it the cache holds) for the controller to withdraw: everything it
holds from it, and from the threads it knows are under it (see Gene's fri_channel_withdrawal). The queue is written to disk first,
and until the controller has taken all of it every saved context is held back (see holding, which SavedTurns.closed
reads), so nothing from the channel reaches a request meanwhile. A controller that doesn't take channels (CAPABILITY),
an older one in a rollback window, takes nothing, so everything stays held back until one that does is back, at most
as long as anything withdrawn could be kept (KEPT_SECONDS).

A deletion Fri missed (it was down, say) is found by a later read (see sweep): the controller lists every channel and
thread it holds anything from, and one Discord says is gone is queued as its event would have queued it. Gone means
Discord answers a read of the channel itself with Unknown Channel, and the server's channel list doesn't have it. A
channel Fri can no longer see, its permissions changed, stays in that list and answers Missing Access: nothing is
withdrawn for that.
"""

import asyncio
import json
import logging
import math
import os
import time

import aiohttp
import discord

from .fri_errors import ControllerRejected, ControllerUnavailable
from .subscription import private_json

CAPABILITY = 'channel-withdrawals'
RECORD = 'channel-withdrawals.json'
SAVED = 'channel-withdrawals.saved'  # See ChannelWithdrawals.marker.
KEPT_SECONDS = 90*86400  # As long as a note, the longest anything withdrawn here could otherwise be kept.
SWEEP_SECONDS = 3600  # How often the controller's channels are read again for deletions Fri missed.
SWEEP_MOST = 25  # Reads of channels missing from the cache in one sweep; the rest wait for the next.
MOST = 100  # Channels and threads the controller takes at once.
UNKNOWN_CHANNEL = 10003
LOG = logging.getLogger('fri.agent')


class ChannelWithdrawals:
    def __init__(self, agent):
        self.agent = agent
        self.loaded = None  # Channel ID -> {'guild_id', 'threads', 'queued_at'}, read from disk on first use.
        # Recovering: a queue that couldn't be read, or wasn't written down: everything is held back until a sweep has
        # had every held channel answered, and the controller has taken whatever it found gone (see settle).
        self.recovering = False
        self.resolved = set()  # Channels a sweep since recovery began had answered, either way.
        self.cursor = 0  # Where the next sweep starts reading channels missing from the cache (see sweep).
        self.swept = 0.0  # When a sweep last ran through; never, after a start.
        self.sending = None  # The task sending the queue now, if one is.
        self.written = 0  # Which write of the queue is on disk, as its marker must say too (see marker).
        self.held = set()  # The channels the last sweep found the controller holding anything from.
        # Moved by every withdrawal the controller accepted: a request's context read before it is dropped whole at
        # its last check (see moved, Sol round 2 of #372, B3).
        self.generation = 0

    @property
    def pending(self):
        if self.loaded is None:
            self.loaded = self.read()
        return self.loaded

    def record(self):
        data = getattr(self.agent.fri, 'data', None)
        return None if data is None else data/RECORD

    def marker(self):
        """Beside the queue while what's on disk is all of it, naming that write of it: removed, durably, before any
        new write starts, and written again only once that write has landed, so a start finds the queue may be missing
        something whenever a write was cut short (Sol round 1 of #372, F2; round 2, B4)."""
        path = self.record()
        return None if path is None else path.with_name(SAVED)

    def read(self):
        """The queue as written down. One that can't be read, says it's recovering, or wasn't written down in full
        (see marker) is never read as all there is: what it named would come back. Every saved context is held back
        instead, until a sweep has found, and the controller withdrawn, whatever is gone (see settle). A queue never
        written, with no marker either, is a first start: written down now, empty, if it can be (a disk full from the
        very first start, before anything could be written, can't be told from one). Logged without its contents."""
        path, marker = self.record(), self.marker()
        if path is None:
            return {}
        if not path.exists():
            self.loaded = {}
            if marker.exists():  # Removed since it was written.
                self.recover('was not written down')
            else:  # A first start: nothing was queued to lose, whether or not it can be written now.
                self.persist()
            return self.loaded
        try:
            data = json.loads(path.read_text())
            written = data.get('written', 0)
            if type(written) is not int:
                raise ValueError('not a write number')
            now, pending, unread = time.time(), {}, 0
            for channel, entry in data['pending'].items():
                if (valid := entry_of(channel, entry, now)) is None:
                    unread += 1  # Not as this writes them: what it named is found again by recovery's sweep.
                else:
                    pending[channel] = valid
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            self.recover(f'could not be read ({type(exc).__name__})')
            return {}
        stamp = self.stamp(marker)
        self.written = max(written, stamp or 0)
        if unread:  # Sol round 3 of #372: nothing in it is discarded unaccounted for.
            self.recover(f'had {unread} entries that could not be read')
        elif data.get('recovering') is True:
            self.recover('was recovering')
        elif stamp != written:  # No marker, or one for another write: this one may not have landed whole.
            self.recover('was not written down in full')
        return pending

    @staticmethod
    def stamp(marker):
        """Which write the marker says landed, or None without one that can be read."""
        try:
            value = json.loads(marker.read_text()).get('written')
        except (OSError, ValueError, AttributeError):
            return None
        return value if type(value) is int else None

    def recover(self, why):
        if not self.recovering:
            LOG.warning('Pending channel withdrawals %s, so every saved context is held back until the controller has '
                        'withdrawn what was deleted', why)
        self.recovering, self.resolved = True, set()

    def persist(self):
        """The queue to disk, with whether it's recovering and which write it is: its marker removed, durably, first,
        then the queue, then a marker naming that write. A write that fails, or stops part way, leaves no marker for it
        (Sol round 2 of #372, B4)."""
        path, marker = self.record(), self.marker()
        if path is None:
            return True
        pending = self.pending  # Read first: reading is what finds a queue recovering.
        try:
            marker.unlink(missing_ok=True)
            synced(marker.parent)
            self.written += 1
            private_json(path, {'pending': pending, 'recovering': self.recovering, 'written': self.written})
            private_json(marker, {'written': self.written})
        except OSError as exc:  # Still queued, and held back, in memory; a start finds it unwritten (no marker).
            LOG.warning('Pending channel withdrawals could not be saved: %s', type(exc).__name__)
            return False
        return True

    def holding(self):
        """Whether every saved context is held back: a channel or thread deleted that the controller hasn't withdrawn
        yet, or a queue recovering."""
        now = time.time()
        if expired := [channel for channel, entry in self.pending.items() if entry['queued_at'] < now-KEPT_SECONDS]:
            for channel in expired:  # Nothing it protects can still be kept.
                del self.pending[channel]
            self.persist()
        return self.recovering or bool(self.pending)

    def withdraw(self, guild_id, channel_id, threads=(), send=True):
        """A channel or thread deleted in Discord, with the threads under it Fri knows of: queued, written down, then
        sent (see send). Only Fri's own server's."""
        if guild_id is None or int(guild_id) != int(self.agent.fri.guild_id):
            return
        entry = self.pending.setdefault(str(channel_id), {'guild_id': str(guild_id), 'threads': [],
                                                          'queued_at': time.time()})
        entry['threads'] = sorted({*entry['threads'], *(str(thread) for thread in threads)}, key=int)
        self.persist()
        if send:
            self.kick()

    def kick(self):
        if self.sending is None or self.sending.done():
            try:
                self.sending = asyncio.get_running_loop().create_task(self.send())
            except RuntimeError:  # No loop running: the heartbeat sends it.
                self.sending = None

    def offered(self):
        return CAPABILITY in getattr(self.agent.fri, 'capabilities', ())

    async def send(self):
        """Each queued channel, with its threads, to the controller; what it took leaves the queue, and the messages it
        says it took are taken back here too (see taken). Nothing without a controller that takes them. A failure
        leaves the rest queued, for the heartbeat to send again."""
        for channel, entry in sorted(self.pending.items()):
            if not self.offered():
                return
            try:
                answer = await self.agent.fri.api('POST', '/chat/channels/withdraw', {
                    'guild_id': entry['guild_id'], 'channel_ids': [channel, *entry['threads']][:MOST]})
            except ControllerRejected as exc:
                if exc.status == 404:  # Rolled back past it since Fri started: held back until it's offered again.
                    self.agent.fri.capabilities = frozenset(self.agent.fri.capabilities) - {CAPABILITY}
                LOG.warning('A channel withdrawal was refused; it stays queued and saved context stays held back')
                return
            except (ControllerUnavailable, discord.DiscordException, aiohttp.ClientError, asyncio.TimeoutError,
                    OSError) as exc:
                LOG.warning('A channel withdrawal was not sent: %s; it stays queued', type(exc).__name__)
                return
            self.taken((answer or {}).get('messages') or ())
            if self.pending.get(channel) is entry:
                del self.pending[channel]
                self.persist()

    def taken(self, messages):
        """A withdrawal the controller accepted, naming the messages it took (none, for one it had taken already, its
        first answer lost). Any request's context read before it is dropped whole at its last check (see moved), and
        a memory view reads again, since the withdrawal queue's generation moved (Sol round 2 of #372, B3). The
        messages are marked as taken back here too, as a deleted message is (Sol round 1, F1)."""
        self.generation += 1
        turns, now = self.agent.saved_turns, time.time()
        for message in messages:
            if str(message).isdigit():
                turns.marked.pop(str(message), None)
                turns.marked[str(message)] = now
        turns.generation += 1
        turns.prune(now)

    async def sweep(self):
        """Deletions Fri missed: every channel and thread the controller holds anything from that isn't in the cache, or
        in the server's channel list, is read from Discord, at most SWEEP_MOST a sweep, taking turns (Sol round 1 of
        #372, F4). One that answers Unknown Channel is queued (see the module's doc); Missing Access, Fri can't see it
        now, is an answer too, and withdraws nothing. Anything else leaves it unanswered, for the next sweep. Threads
        found are named to the controller with the channel they're under, for those it saved before it kept that.
        True once every channel the controller holds was answered since recovery began (Sol round 1 of #372, F3)."""
        fri = self.agent.fri
        guild = fri.get_guild(fri.guild_id)
        if guild is None:
            return False
        held = (await fri.api('POST', '/chat/channels/held', {'guild_id': str(guild.id)}))['channels']
        self.held = {row['id'] for row in held}
        missing, parents = [], {}
        for row in held:
            channel = fri.get_channel(int(row['id']))
            if channel is None:
                missing.append(row)  # Answered only once read, or once the server's list has it (round 2, B2).
                continue
            self.resolved.add(row['id'])  # Discord holds it.
            if row.get('parent') is None and getattr(channel, 'parent_id', None):
                parents[row['id']] = str(channel.parent_id)
        listed = set()
        if missing:
            listed = {str(channel.id) for channel in await guild.fetch_channels()}
            if not listed:  # A server with no channels while the cache holds some: nothing it says can be trusted.
                return False
        unread = [row for row in missing if row['id'] not in listed]
        self.resolved |= {row['id'] for row in missing if row['id'] in listed}  # The server still has them.
        start = self.cursor % len(unread) if unread else 0
        for row in (unread[start:] + unread[:start])[:SWEEP_MOST]:
            self.cursor += 1
            try:
                found = await guild.fetch_channel(int(row['id']))
            except discord.NotFound as exc:
                if getattr(exc, 'code', None) == UNKNOWN_CHANNEL:
                    self.withdraw(guild.id, row['id'], send=False)
                    self.resolved.add(row['id'])
                else:  # Another 404 says nothing of the channel.
                    self.resolved.discard(row['id'])
                continue
            except discord.Forbidden:
                self.resolved.add(row['id'])  # Missing Access: it's there; Fri can't see it.
                continue
            except (discord.DiscordException, aiohttp.ClientError, asyncio.TimeoutError, OSError):
                self.resolved.discard(row['id'])
                continue
            self.resolved.add(row['id'])
            if row.get('parent') is None and getattr(found, 'parent_id', None):
                parents[row['id']] = str(found.parent_id)  # An archived thread, say.
        if parents:
            await fri.api('POST', '/chat/channels/parents', {'guild_id': str(guild.id),
                                                             'parents': dict(sorted(parents.items())[:500])})
            self.taken(())  # A thread named under a withdrawn channel is withdrawn with it there (gene #243 G2).
        return all(row['id'] in self.resolved for row in held)

    async def settle(self):
        """The heartbeat's stage: what's queued is sent, and, first thing after a start and hourly after that, the
        sweep, whose finds are sent too; while recovering, every tick. Recovery ends once a sweep had every held channel
        answered and the controller took all it found (Sol round 1 of #372, F3). A controller read as not taking
        channels is read again, at most a minute apart, while the queue holds (Sol round 1 of #372, F6)."""
        if not self.offered():
            if not (self.pending or self.recovering) or not await self.agent.saved_turns.reread() or not self.offered():
                return
        await self.send()
        if self.recovering or time.time()-self.swept >= SWEEP_SECONDS:
            through = await self.sweep()
            await self.send()
            self.swept = time.time()
            if through and self.recovering and not self.pending:
                self.recovering = False
                if not self.persist():
                    self.recover('could not be written down')

    def status(self):
        """For fri health: how many deletions wait for the controller, whether saved context is held back for them or
        for recovery, how many held channels recovery has had answered of those the last sweep found, whether the
        controller takes channels, and when the last sweep ran. No IDs."""
        return {'pending': len(self.pending), 'holding': self.holding(), 'recovering': self.recovering,
                'answered': len(self.held & self.resolved) if self.recovering else None,
                'held': len(self.held) if self.swept else None, 'offered': self.offered(),
                'swept_at': self.swept or None}

    def moved(self, since):
        """Whether a withdrawal was accepted since `since` (see generation): a context read before it is dropped."""
        return since is not None and self.generation != since

    @staticmethod
    def drop(context):
        """Every saved context of a request read before a channel's withdrawal (see moved): what it held of the
        channel can't be told from the rest, so none of it goes to the model, as when a withdrawals record can't be
        read (SavedTurns.withhold)."""
        context['turns'], context['pending'], context['elsewhere'] = [], None, []
        if context.get('personal_memory'):
            context['personal_memory']['notes'] = []
        for key in ('note_sources', 'profile_origins'):
            if context.get(key) is not None:
                context[key] = {}
        context['member_summary'] = None


def synced(directory):
    """The directory's entries durably on disk: a marker removed stays removed whatever happens next."""
    handle = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def entry_of(channel, entry, now):
    """A queued entry as this writes it, or None: a channel ID, its server's ID, a list of thread IDs and a time, each
    of its own type exactly, so a boolean (an int to Python) is none of them (Sol round 3 of #372)."""
    if not (type(channel) is str and channel.isdigit() and type(entry) is dict):
        return None
    guild, threads, queued = entry.get('guild_id'), entry.get('threads', []), entry.get('queued_at')
    if not (type(guild) is str and guild.isdigit() and type(threads) is list and type(queued) in (int, float) and
            all(type(thread) is str and thread.isdigit() for thread in threads)):
        return None
    return {'guild_id': guild, 'queued_at': when(queued, now), 'threads': threads}


def when(value, now):
    """A queued time as written down; one that isn't a finite time, or is ahead of now, counts from now, so it ends
    when one queued now would (Sol round 1 of #372, F5)."""
    value = float(value)
    return value if math.isfinite(value) and value <= now + 60 else now
