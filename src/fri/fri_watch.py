"""Fri keeps an eye on Gene and its controller: one owner DM per outage, plus an owner "fri health"."""

import asyncio
import json
import logging
import os
import re
import time

import discord

from .fri_errors import ControllerRejected, ControllerUnavailable
from .private_files import private_json

LOG = logging.getLogger('fri.watch')
NO_MENTIONS = discord.AllowedMentions.none()
INTERVAL = 120  # Fri.watch runs every two minutes.
CHECKS = 3  # Consecutive bad reads before the owner hears about it, so restarts and deploys stay quiet.
STALE = 60  # Gene's runtime reports every 10 s; a minute without one is several missed beats.
RELAPSE = 1800  # A new outage this soon after the last DM edits that DM instead of sending another.
READ = 10  # A watch read that hangs this long counts as an unreachable controller.
PROBE = 4  # Each live read in the owner's health report.
GENE = frozenset({'stale', 'offline', 'stopped'})
NOT_TAKING = 'gene isn’t taking commands right now, so i didn’t send that.'
HEALTH = re.compile(r"(?:(?:show|check|run|give)\s+(?:me\s+)?)?(?:(?:your|fri'?s|system)\s+)?"
                    r"health(?:\s*check)?(?:\s+(?:report|status|rn|please|pls))*")


def health_requested(text):
    text = ' '.join(text.casefold().replace('’', "'").split()).strip(' .!?')
    text = re.sub(r'^(?:(?:hey|yo)\s+)?fri\b[,:]?\s*', '', text)
    text = re.sub(r'^(?:please|pls)\s+', '', text)
    return bool(HEALTH.fullmatch(text))


def span(seconds):
    seconds = max(0, int(seconds))
    if seconds < 120:
        return f'{seconds}s'
    if seconds < 7200:
        return f'{seconds // 60} min'
    return f'{seconds / 3600:.1f} h'


def assess(health):
    """Gene's heartbeat, not `responsive`: a long command makes the poll stale while Gene is fine."""
    age, runtime = health.get('heartbeat_age_seconds'), health.get('runtime') or {}
    detail = {'age': age, 'sha': str(runtime.get('sha') or '')[:7]}
    if age is None or age >= STALE:
        return 'stale', detail
    if runtime.get('guild_connected') is not True:
        return 'offline', detail
    if runtime.get('controls_running') is not True:
        return 'stopped', detail
    return 'ok', detail


def down(kind, detail, since):
    at = f'<t:{int(since)}:t>'
    fix = 'i can’t restart it, that’s on you.'
    if kind == 'unreachable':
        return f'haven’t reached gene’s controller since {at}. my chat and memory are down with it. {fix}'
    if kind == 'rejected':
        return (f'gene’s controller has been rejecting my health checks since {at}. smells like a contract or '
                'config mismatch. i can’t fix that from here.')
    if kind == 'offline':
        return f'gene’s running but has been cut off from discord since {at}. {fix}'
    if kind == 'stopped':
        return f'gene’s command runner has been dead since {at}, so nobody’s touching the music. {fix}'
    beat = f'last heartbeat {span(detail["age"])} ago' if detail.get('age') is not None else 'no heartbeat on record'
    release = f', release {detail["sha"]}' if detail.get('sha') else ''
    return f'gene’s been dead since {at}. {beat}{release}. {fix}'


class Watch:
    def __init__(self, fri):
        self.fri = fri
        self.state = None

    @property
    def path(self):
        return self.fri.data / 'fri-watch.json'

    def load(self):
        if self.state is None:
            try:
                self.state = json.loads(self.path.read_text())
            except (OSError, ValueError):
                self.state = {}
            if not isinstance(self.state, dict):
                self.state = {}
        for key, value in {'bad': 0, 'kind': '', 'since': 0, 'checked': 0, 'alert': None}.items():
            self.state.setdefault(key, value)
        return self.state

    def alerted(self, kinds=GENE):
        alert = self.load()['alert']
        return bool(alert and alert.get('open') and alert.get('kind') in kinds)

    def refusal(self):
        return NOT_TAKING + (' owner already knows.' if self.alerted() else '')

    async def read(self):
        fri = self.fri
        scope = {'guild_id': str(fri.guild_id), 'user_id': str(fri.owner_id),
                 'channel_id': str(fri.workshop_id), 'conversation_id': f'public:{fri.workshop_id}'}
        try:
            async with asyncio.timeout(READ):
                health = await fri.api('POST', '/controls/health', scope)
        except ControllerRejected:
            return 'rejected', {}
        except (ControllerUnavailable, TimeoutError, ValueError):  # ValueError: a body that isn't JSON.
            return 'unreachable', {}
        return assess(health)

    async def check(self):
        try:
            await self.observe()
        except Exception as exc:  # A watchdog loop that stops on one odd error watches nothing.
            LOG.warning('Gene watch check failed: %s', type(exc).__name__)

    async def observe(self):
        state = self.load()
        kind, detail = await self.read()
        now = time.time()
        if now-state['checked'] > 3*INTERVAL:
            state['bad'] = 0  # Fri itself was away; reads from before don't make a streak.
        state['checked'] = now
        alert = state['alert']
        if kind == 'ok':
            state.update(bad=0, kind='')
            recovered = bool(alert and alert.get('open'))
            if recovered:
                alert.update(open=False, ended=now)
            self.save()  # Closed even if the edit below fails: the outage is over either way.
            if recovered:
                minutes = max(1, round((now-alert['since'])/60))
                await self.edit(alert, alert['text']+f'\n-# back at <t:{int(now)}:t>, down about {minutes} min.')
            return
        if not state['bad']:
            state['since'] = now
        if detail.get('age') is not None:
            state['since'] = min(state['since'], now-detail['age'])  # The heartbeat dates the real start.
        state.update(bad=state['bad']+1, kind=kind)
        text = down(kind, detail, state['since'])
        if alert and alert.get('open'):
            if alert['kind'] != kind:  # Still down, differently; keep the one DM current.
                alert.update(kind=kind, text=text)
                self.save()
                await self.edit(alert, text)
        elif state['bad'] >= CHECKS:
            again = text+f'\n-# again. it came back at <t:{int(alert.get("ended", now))}:t>.' if alert else ''
            if alert and now-alert.get('sent', 0) < RELAPSE and await self.edit(alert, again):
                alert.update(open=True, kind=kind, text=again, since=state['since'])
                self.save()
                return
            owner = self.fri.get_user(self.fri.owner_id) or await self.fri.fetch_user(self.fri.owner_id)
            sent = await owner.send(text, allowed_mentions=NO_MENTIONS)
            state['alert'] = {'channel_id': str(sent.channel.id), 'message_id': str(sent.id), 'sent': now,
                              'open': True, 'kind': kind, 'text': text, 'since': state['since']}
            LOG.warning('Owner told about a Gene outage: %s', kind)
        self.save()

    def save(self):
        private_json(self.path, self.state)

    async def edit(self, alert, content):
        try:
            channel = self.fri.get_channel(int(alert['channel_id']))
            if channel is None:
                owner = self.fri.get_user(self.fri.owner_id) or await self.fri.fetch_user(self.fri.owner_id)
                channel = await owner.create_dm()
            await channel.get_partial_message(int(alert['message_id'])).edit(content=content, allowed_mentions=NO_MENTIONS)
            return True
        except discord.HTTPException:
            LOG.info('Outage DM was not edited')
            return False

    async def report(self):
        """Inference-free, recorded nowhere, and DM-only: it names releases, loops and Lab-free ops detail."""
        fri, agent, now = self.fri, self.fri.agent, time.time()
        guild = fri.get_guild(fri.guild_id)
        connected = bool(fri.is_ready() and guild and not guild.unavailable)
        lines = [f'vitals as of <t:{int(now)}:T>:']
        sha = os.getenv('FRI_RELEASE_SHA', 'development')
        sha = sha[:7] if re.fullmatch(r'[0-9a-f]{40}', sha) else sha
        me = f"- me: release {sha}, discord {'connected' if connected else 'not connected'}"
        if agent:
            beat = 'never finished' if not agent.heartbeat_finished else f'{span(now-agent.heartbeat_finished)} ago'
            me += f', heartbeat {agent.heartbeat_status} ({beat})'
        lines.append(me)
        loops = {'heartbeat': agent.heartbeat if agent else None, 'owner dms': fri.owner_reviews,
                 'health file': fri.health, 'watch': fri.watch}
        lines.append('- loops: '+', '.join(f"{name} {'running' if loop.is_running() else 'stopped'}"
                                            + (' (failed)' if loop.failed() else '')
                                            for name, loop in loops.items() if loop))
        try:
            waiting = sum(1 for _ in (fri.data / 'usage').glob('*.json'))
        except OSError:
            waiting = '?'
        lines.append(f'- usage receipts waiting: {waiting}')
        scope = {'guild_id': str(fri.guild_id), 'user_id': str(fri.owner_id),
                 'channel_id': str(fri.workshop_id), 'conversation_id': f'public:{fri.workshop_id}'}
        status, health = await asyncio.gather(self.probe('GET', '/status'),
                                              self.probe('POST', '/controls/health', scope))
        if isinstance(status, dict):
            lines.append(f"- controller: up, release {str(status.get('active_sha') or '?')[:7]}"
                         + (', paused' if status.get('paused') else ''))
        else:
            lines.append(f'- controller: {status}')
        if isinstance(health, dict):
            runtime = health.get('runtime') or {}
            age, poll = health.get('heartbeat_age_seconds'), health.get('command_poll_age_seconds')
            gene = [f"heartbeat {'never' if age is None else span(age)+' ago'}",
                    f"command poll {'never' if poll is None else span(poll)+' ago'}",
                    'responsive' if health.get('responsive') else 'not responsive']
            if runtime.get('sha'):
                gene.append(f"release {str(runtime['sha'])[:7]}")
            lines.append('- gene: '+', '.join(gene))
        else:
            lines.append(f'- gene: {health}')
        state = self.load()
        alert = state['alert']
        if alert and alert.get('open'):
            watch = f"outage DM open since <t:{int(alert['since'])}:t> ({alert['kind']})"
        elif state['bad']:
            watch = f"{state['bad']} bad read{'s' if state['bad'] > 1 else ''} in a row ({state['kind']})"
        else:
            watch = 'all quiet'
        last = f"<t:{int(state['checked'])}:R>" if state['checked'] else 'never'
        lines.append(f'- watch: {watch}, last check {last}')
        return '\n'.join(lines)

    async def probe(self, method, path, payload=None):
        try:
            async with asyncio.timeout(PROBE):
                return await self.fri.api(method, path, payload)
        except (ControllerUnavailable, TimeoutError):
            return 'unreachable'
        except ControllerRejected:
            return 'rejected my request'
        except Exception as exc:  # The owner asked because something's off; show it, don't go silent.
            return type(exc).__name__
