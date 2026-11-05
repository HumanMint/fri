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
import time

import aiohttp
import discord

from .fri_errors import ControllerRejected, ControllerUnavailable
from .subscription import private_json

CAPABILITY = 'channel-withdrawals'
RECORD = 'channel-withdrawals.json'
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
        self.unreadable = False  # A record that couldn't be read: everything held back until a sweep (see read).
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

    def read(self):
        """The queue as written down. One that can't be read is never read as empty: what it named would come back.
        Every saved context is held back instead until a sweep has found, and the controller withdrawn, whatever is
        gone (see settle). Logged without its contents."""
        path = self.record()
        if path is None or not path.exists():
            return {}
        try:
            return {str(channel): {'guild_id': str(entry['guild_id']), 'queued_at': float(entry['queued_at']),
                                   'threads': [str(thread) for thread in entry.get('threads', []) if str(thread).isdigit()]}
                    for channel, entry in json.loads(path.read_text())['pending'].items() if str(channel).isdigit()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            LOG.warning('Pending channel withdrawals could not be read (%s), so every saved context is held back until '
                        'the controller has withdrawn what was deleted', type(exc).__name__)
            self.unreadable = True
            return {}

    def persist(self):
        if path := self.record():
            try:
                private_json(path, {'pending': self.pending})
            except OSError as exc:  # Still queued, and held back, in memory.
                LOG.warning('Pending channel withdrawals could not be saved: %s', type(exc).__name__)
                return False
        return True

    def holding(self):
        """Whether every saved context is held back: a channel or thread deleted that the controller hasn't withdrawn
        yet, or a queue that couldn't be read."""
        now = time.time()
        if expired := [channel for channel, entry in self.pending.items() if entry['queued_at'] < now-KEPT_SECONDS]:
            for channel in expired:  # Nothing it protects can still be kept.
                del self.pending[channel]
            self.persist()
        return self.unreadable or bool(self.pending)

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
        """Each queued channel, with its threads, to the controller; what it took leaves the queue. Nothing without a
        controller that takes them. A failure leaves the rest queued, for the heartbeat to send again."""
        for channel, entry in sorted(self.pending.items()):
            if not self.offered():
                return
            try:
                await self.agent.fri.api('POST', '/chat/channels/withdraw', {
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
            if self.pending.get(channel) is entry:
                del self.pending[channel]
                self.persist()

    async def sweep(self):
        """Deletions Fri missed: every channel and thread the controller holds anything from that isn't in the cache is
        read from Discord; one that answers Unknown Channel and isn't in the server's channel list is queued (see the
        module's doc). Threads found are named to the controller with the channel they're under, for those it saved
        before it kept that. At most SWEEP_MOST reads; any other answer leaves it for the next sweep."""
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
        listed = {str(channel.id) for channel in await guild.fetch_channels()} if missing else set()
        for row in missing[:SWEEP_MOST]:
            if row['id'] in listed:  # The server still has it, whatever the cache says.
                continue
            try:
                found = await guild.fetch_channel(int(row['id']))
            except discord.NotFound as exc:
                if getattr(exc, 'code', None) == UNKNOWN_CHANNEL:
                    self.withdraw(guild.id, row['id'], send=False)
                continue
            except (discord.DiscordException, aiohttp.ClientError, asyncio.TimeoutError, OSError):
                continue  # Missing Access (Fri can't see it now) withdraws nothing; anything else waits.
            if row.get('parent') is None and getattr(found, 'parent_id', None):
                parents[row['id']] = str(found.parent_id)  # An archived thread, say.
        if parents:
            await fri.api('POST', '/chat/channels/parents', {'guild_id': str(guild.id),
                                                             'parents': dict(sorted(parents.items())[:500])})
        return len(missing) <= SWEEP_MOST

    async def settle(self):
        """The heartbeat's stage: what's queued is sent, and, first thing after a start and hourly after that, the
        sweep, whose finds are sent too. A queue that couldn't be read is held back until a sweep has run through."""
        if not self.offered():
            return
        await self.send()
        if self.unreadable or time.time()-self.swept >= SWEEP_SECONDS:
            through = await self.sweep()
            await self.send()
            self.swept = time.time()
            if through and self.unreadable and not self.pending and self.persist():
                self.unreadable = False

    def status(self):
        """For fri health: how many deletions wait for the controller, whether saved context is held back for them,
        and when the last sweep ran. No IDs."""
        return {'pending': len(self.pending), 'holding': self.holding(), 'unreadable': self.unreadable,
                'swept_at': self.swept or None}

