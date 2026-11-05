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

    @property
    def pending(self):
        if self.loaded is None:
            self.loaded = self.read()
        return self.loaded

    def record(self):
        data = getattr(self.agent.fri, 'data', None)
        return None if data is None else data/RECORD

    def marker(self):
        """Beside the queue while what's on disk is all of it: a write that fails removes it, which needs no space, so
        a start finds the queue may be missing something (Sol round 1 of #372, F2)."""
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
            now = time.time()
            pending = {str(channel): {'guild_id': str(entry['guild_id']), 'queued_at': when(entry['queued_at'], now),
                                      'threads': [str(thread) for thread in entry.get('threads', [])
                                                  if str(thread).isdigit()]}
                       for channel, entry in data['pending'].items() if str(channel).isdigit()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            self.recover(f'could not be read ({type(exc).__name__})')
            return {}
        if data.get('recovering') is True:
            self.recover('was recovering')
        elif not marker.exists():
            self.recover('was not written down in full')
        return pending

    def recover(self, why):
        if not self.recovering:
            LOG.warning('Pending channel withdrawals %s, so every saved context is held back until the controller has '
                        'withdrawn what was deleted', why)
        self.recovering, self.resolved = True, set()

    def persist(self):
        """The queue to disk, with whether it's recovering, then its marker; a write that fails removes the marker."""
        path, marker = self.record(), self.marker()
        if path is None:
            return True
        try:
            private_json(path, {'pending': self.pending, 'recovering': self.recovering})
            marker.touch(mode=0o600)
        except OSError as exc:  # Still queued, and held back, in memory; a start finds it unwritten.
            LOG.warning('Pending channel withdrawals could not be saved: %s', type(exc).__name__)
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                pass
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
        """The messages the controller took with a channel, taken back here as a deleted message is: a copy of what
        they said that a request read before (its context, waiting on Discord or the model) is held back, by the same
        marks (see SavedTurns.withhold), and a memory view reads again, since its marks moved (Sol round 1 of #372,
        F1)."""
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
        missing, parents = [], {}
        for row in held:
            channel = fri.get_channel(int(row['id']))
            if channel is None:
                missing.append(row)
            elif row.get('parent') is None and getattr(channel, 'parent_id', None):
                parents[row['id']] = str(channel.parent_id)
            self.resolved.add(row['id'])  # Discord holds it.
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
        for recovery, and when the last sweep ran. No IDs."""
        return {'pending': len(self.pending), 'holding': self.holding(), 'recovering': self.recovering,
                'swept_at': self.swept or None}


def when(value, now):
    """A queued time as written down; one that isn't a finite time, or is ahead of now, counts from now, so it ends
    when one queued now would (Sol round 1 of #372, F5)."""
    value = float(value)
    return value if math.isfinite(value) and value <= now + 60 else now
