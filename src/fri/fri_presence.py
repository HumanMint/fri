"""Daily public status batches from coarse topics, never private quotes or profiles."""

import asyncio
import datetime as dt
import json
import logging
import math
import re
import time

import discord

from . import feedback as commands
from .fri_agent_model import STRING, obj
from .private_files import private_json

LOG = logging.getLogger('fri.presence')
SCHEMA = obj({'lines': {'type': 'array', 'items': STRING}})
FALLBACKS = ['probably lurking', 'gene has the aux again', 'still deciding what to play', 'meme queue looking questionable']
INSTRUCTION = '''Write four different short public Discord status lines for Fri (they/them).
Fri is a dry, cheeky regular in the Fricking Fricks friend server; Gene handles music/utilities.
Lowercase, offhand, 3-9 words each and at most 70 characters. Ordinary chat, no polished slogans,
quests, leveling up, uwu, forced gamer slang, emojis, em dashes or "as an AI".
Use the supplied broad topics for some variety, but do not claim someone said/played/did something.
No member names, personal facts, quotations, links, mentions, insults at a person, or claims to be
reading private chats, doing actual work, listening to a live track, or fixing/deploying anything.
Generic server-flavor jokes about the aux, indecisive gaming, meme piles and Gene are fine.
These are public playful lines, not telemetry. No human-life stories or made-up server incidents.
Do not repeat the supplied previous lines. All four should be distinct, not rewrites of one joke.
Only output the requested JSON; no tools or other actions.'''

# Only these canonical labels can cross from channel text to the public-status task.
# New Discord read permissions do not expand the separate status source allowlist.
TOPICS = {
    'minecraft': ('minecraft', 'redstone'),
    'zelda': ('zelda', 'hyrule'),
    'warframe': ('warframe', 'tenno'),
    'helldivers': ('helldivers',),
    'counter-strike': ('counter strike', 'counter-strike', 'cs2'),
    'valorant': ('valorant',),
    'overwatch': ('overwatch',),
    'league of legends': ('league of legends', 'league ranked'),
    'dota': ('dota',),
    'rocket league': ('rocket league',),
    'elden ring': ('elden ring',),
    'final fantasy': ('final fantasy', 'ffxiv', 'ff14'),
    'terraria': ('terraria',),
    'pokemon': ('pokemon', 'pokémon'),
    'trance music': ('trance', 'nanobii'),
    'electronic music': ('edm', 'porter robinson', 'daft punk', 'electronic music'),
    'music queues': ('playlist', 'music', 'song', 'gene'),
    'memes': ('meme', 'memes', 'shitpost', 'brainrot'),
}


def topics_in(text):
    return {topic for topic, words in TOPICS.items()
            if any(re.search(r'(?<!\w)'+re.escape(word)+r'(?!\w)', text.casefold()) for word in words)}


def valid_line(line):
    return (isinstance(line, str) and 3 <= len(line) <= 70 and line == line.strip()
            and re.fullmatch(r"[a-z0-9 ,.!?'():-]+", line) is not None
            and not re.search(r'https?|www|discord\.|(?:[a-z0-9-]+\.)+[a-z]{2,}|\d{5,}', line))


class Presence:
    def __init__(self, agent):
        self.agent, self.fri = agent, agent.fri
        self.path = self.fri.data/'rotating-status.json'
        self.lock = asyncio.Lock()
        self.applied = None
        self.receipt = {}

    def load(self):
        if self.path.exists():
            state = json.loads(self.path.read_text())
        else:
            state = {'version': 1, 'next_generation': 0, 'batch_at': 0, 'lines': FALLBACKS[:],
                     'recent': [], 'next_publish': 0, 'last_sent': '', 'last_result': 'not_started'}
        if (state.get('version') != 1 or not isinstance(state.get('lines'), list)
                or not 1 <= len(state['lines']) <= 4 or not all(valid_line(x) for x in state['lines'])
                or not isinstance(state.get('recent'), list) or len(state['recent']) > 12
                or not all(valid_line(x) for x in state['recent'])
                or any(not isinstance(state.get(k), (int, float)) or not math.isfinite(state[k])
                       for k in ('next_generation', 'batch_at', 'next_publish'))):
            raise ValueError('Invalid presence state; do not reset its generation reservation')
        return state

    def save(self, state):
        private_json(self.path, state)
        self.receipt = {k: state[k] for k in ('next_generation', 'last_sent', 'last_result')}

    def enabled(self):
        return self.agent.config['enabled'] and self.agent.config['status_enabled'] and self.fri.status == discord.Status.online

    def readable(self, channel):
        if (not isinstance(channel, discord.TextChannel) or channel.guild.id != self.fri.guild_id
                or channel.is_nsfw() or str(channel.id) not in self.agent.state['status_context_channels']):
            return False
        permissions = channel.permissions_for(channel.guild.me)
        return permissions.view_channel and permissions.read_message_history

    async def privacy(self, channel, user):
        return await self.fri.api('POST', '/agent/status-context',
            {**self.agent.identity(user), 'channel_id': str(channel)})

    async def sample(self):
        topics, profiles = set(), {}
        after = dt.datetime.fromtimestamp(time.time()-7*86400, dt.timezone.utc)
        for cid in self.agent.state['status_context_channels'][:2]:
            channel = self.fri.get_channel(int(cid))
            if not self.readable(channel):
                continue
            async for message in channel.history(limit=20, after=after, oldest_first=False):
                if message.author.bot or message.webhook_id or not message.content:
                    continue
                if not await self.agent.member(message.author.id):
                    continue
                key = (channel.id, message.author.id)
                if key not in profiles:
                    profiles[key] = await self.privacy(*key)
                if profiles[key]['enabled']:
                    topics.update(topics_in(message.content[:1000]))
        return sorted(topics)[:8], profiles

    async def still_allowed(self, profiles):
        for (cid, user), before in profiles.items():
            if not self.readable(self.fri.get_channel(cid)):
                return False
            after = await self.privacy(cid, user)
            if after != before or not await self.agent.member(user, fresh=True):
                return False
        return True

    async def generate(self, state):
        now = time.time()
        # Reserve before even reading context: restart, errors and crashes cannot spend again today.
        state.update(next_generation=now+86400, last_result='reserved')
        self.save(state)
        topics, profiles = await self.sample()
        result, _, status = await self.agent.run('status', INSTRUCTION,
            {'topics': topics, 'previous_lines': state['recent'],
             'day': dt.datetime.fromtimestamp(now, dt.timezone.utc).date().isoformat()},
            SCHEMA)
        self.agent.state = await self.fri.api('GET', '/agent/state')
        if not self.enabled():
            state['last_result'] = 'disabled'
        elif not await self.still_allowed(profiles):
            state['last_result'] = 'context_changed'
        else:
            lines = result.get('lines', []) if status == 'completed' else []
            lines = list(dict.fromkeys(line for line in lines if valid_line(line) and line not in state['recent']))[:4]
            if len(lines) >= 2:
                state.update(lines=lines, batch_at=now, recent=(state['recent']+lines)[-12:], last_result='generated')
            else:
                state['last_result'] = 'generation_failed'
        self.save(state)

    async def publish(self, text):
        activity = discord.CustomActivity(text) if text else None
        await self.fri.change_presence(status=self.fri.status, activity=activity)
        self.fri.activity = activity  # discord.py's public setter preserves it for a fresh Identify.
        self.applied = text

    async def check(self):
        async with self.lock:
            try:
                if not self.enabled():
                    if self.fri.activity is not None:
                        await self.publish('')
                    return
                state = self.load()
                if time.time() >= state['next_generation']:
                    try:
                        await self.generate(state)
                    except (discord.DiscordException, commands.UserFeedbackCheckFailure, TimeoutError, ValueError):
                        state['last_result'] = 'generation_failed'
                        self.save(state)
                if not self.enabled():
                    if self.fri.activity is not None:
                        await self.publish('')
                    return
                now = time.time()
                index = int(max(0, now-state['batch_at'])//(60*self.agent.config['status_rotation_minutes'])) % len(state['lines'])
                line = state['lines'][index]
                if self.applied == line or now < state['next_publish']:
                    return
                state['next_publish'] = now+300  # Bound gateway retries independently of model calls.
                self.save(state)
                await self.publish(line)
                state.update(next_publish=0, last_sent=line)
                self.save(state)
            except (discord.DiscordException, commands.UserFeedbackCheckFailure, OSError, TimeoutError, ValueError, KeyError):
                self.receipt['error'] = 'deferred'
                LOG.warning('Fri status update deferred; generation reservation retained')
