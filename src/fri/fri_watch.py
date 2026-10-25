"""Fri keeps an eye on Gene and its controller: one owner DM per outage, plus an owner "fri health"."""

import asyncio
import datetime as dt
import json
import logging
import os
import re
import secrets
import time

import discord

from .fri_errors import ControllerRejected, ControllerUnavailable
from .private_files import private_json

LOG = logging.getLogger('fri.watch')
STARTED = time.time()  # A send from an earlier run of Fri can't have landed after this one started.
NO_MENTIONS = discord.AllowedMentions.none()
INTERVAL = 120  # Fri.watch runs every two minutes.
CHECKS = 3  # Consecutive bad reads before the owner hears about it, so restarts and deploys stay quiet.
STALE = 60  # Gene's runtime reports every 10 s; a minute without one is several missed beats.
RELAPSE = 1800  # A new outage this soon after the last DM edits that DM instead of sending another.
READ = 10  # A watch read that hangs this long counts as an unreachable controller.
PROBE = 4  # Each live read in the owner's health report.
DELIVERY = 30  # How long a pass waits on the owner DM's delivery before moving on. It never cancels it.
GENE = frozenset({'stale', 'offline', 'stopped'})
NOT_TAKING = 'gene isn’t taking commands right now, so i didn’t send that.'
NOT_OFFERED = 'gene isn’t offering that command right now, so i didn’t send it.'
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
        self.delivering = None

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
            alert = self.state.get('alert')
            if not isinstance(alert, dict) or not {'open', 'kind', 'since', 'sent', 'text', 'nonce'} <= set(alert):
                self.state['alert'] = None
        for key, value in {'bad': 0, 'kind': '', 'since': 0, 'checked': 0, 'alert': None}.items():
            self.state.setdefault(key, value)
        return self.state

    def alerted(self, kinds=GENE):
        alert = self.load()['alert']
        return bool(alert and alert.get('open') and alert.get('kind') in kinds and alert.get('message_id'))

    def due(self, slack=0):
        """Whether a read is due, an interval after the last one. `slack` covers a read's own latency."""
        since = time.time()-self.load()['checked']
        return since >= INTERVAL-slack or since < 0  # A last read "in the future" means the clock went back.

    def refusal(self, offered=()):
        if offered:  # Gene is polling; this one command is disabled or unloaded in Red.
            return NOT_OFFERED
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

    async def check(self, observe=True):
        # A watchdog loop that stops on one odd error watches nothing, and a DM that fails waits for the next pass.
        for step in (self.observe, self.deliver) if observe else (self.deliver,):
            try:
                await step()
            except Exception as exc:
                LOG.warning('Gene watch %s failed: %s', step.__name__, type(exc).__name__)

    async def observe(self):
        """Decides what the owner's DM should say; `deliver` makes Discord show it."""
        state = self.load()
        kind, detail = await self.read()
        now = time.time()
        if not 0 <= now-state['checked'] <= 3*INTERVAL:
            state['bad'] = 0  # Fri itself was away, or the clock moved back: reads from before don't make a streak.
        state['checked'] = now
        alert = state['alert']
        if kind == 'ok':
            state.update(bad=0, kind='')
            if alert and alert['open']:
                minutes = max(1, round((now-alert['since'])/60))
                alert.update(open=False, ended=now,
                             text=alert['text']+f'\n-# back at <t:{int(now)}:t>, down about {minutes} min.')
            self.save()
            return
        if not state['bad']:
            state['since'] = now
        if detail.get('age') is not None:
            state['since'] = min(state['since'], now-detail['age'])  # The heartbeat dates the real start.
        state.update(bad=state['bad']+1, kind=kind)
        text = down(kind, detail, state['since'])
        if alert and alert['open']:
            if alert['kind'] != kind:  # Still down, differently; keep the one DM current.
                alert.update(kind=kind, text=text+alert.get('again', ''))
        elif state['bad'] >= CHECKS:
            # A DM whose last edit hasn't landed still reads as down, so it carries the relapse too.
            if alert and alert.get('message_id') and (now-(alert['sent'] or 0) < RELAPSE or
                                                      alert.get('shown') != alert['text']):
                again = f'\n-# again. it came back at <t:{int(alert["ended"])}:t>.'
                alert.update(open=True, kind=kind, since=state['since'], text=text+again, again=again)
            elif alert and alert.get('attempted') and not alert.get('message_id') and alert.get('shown') != alert['text']:
                # Its DM may have landed without Fri hearing back. Keep the nonce and attempt so delivery
                # can still find that DM and edit it to this outage, instead of sending a second one.
                alert.update(open=True, kind=kind, since=state['since'], text=text, again='')
            else:
                # The nonce is saved before the send, so a resend Discord already took returns that message.
                # `sent` is when a DM actually went out, which starts the relapse window.
                state['alert'] = {'open': True, 'kind': kind, 'since': state['since'], 'sent': None, 'text': text,
                                  'nonce': secrets.randbits(62)}
        self.save()

    async def deliver(self):
        """Has Discord show what `observe` decided, waiting up to DELIVERY before the pass moves on.

        A Discord call is never cancelled: discord.py can't recover from a cancel inside its global
        rate-limit wait, and every later Discord call would stall. A delivery still running is left to
        finish, and the next pass waits on it again instead of starting another."""
        if self.delivering is None or self.delivering.done():
            alert = self.load()['alert']
            if not alert or alert.get('shown') == alert['text']:
                return
            self.delivering = asyncio.create_task(self.push(alert))
        done, _ = await asyncio.wait({self.delivering}, timeout=DELIVERY)
        for task in done:
            task.result()  # Re-raises only what ends Fri's process; `push` logs its own failures.

    async def push(self, alert):
        try:
            channel = self.fri.get_channel(int(alert['channel_id'])) if alert.get('channel_id') else None
            if channel is None:
                owner = self.fri.get_user(self.fri.owner_id) or await self.fri.fetch_user(self.fri.owner_id)
                channel = owner.dm_channel or await owner.create_dm()
            if not alert.get('message_id') and alert.get('attempted'):
                # The last send may have landed after all, before it failed or Fri restarted. Discord can only
                # have taken it between the attempt and when that send settled, or this run of Fri started if
                # it never did. Every DM in that window is checked; a full pass with no match means it never
                # landed. Clocks get a minute either side.
                at = alert['attempted_at']
                end = max(at, alert.get('settled') or STARTED)
                after, until = (dt.datetime.fromtimestamp(t, dt.timezone.utc) for t in (at-60, end+60))
                async for sent in channel.history(limit=None, after=after, oldest_first=True):
                    if sent.created_at > until:
                        break
                    if sent.author.id == self.fri.user.id and sent.content == alert['attempted']:
                        alert.update(message_id=str(sent.id), channel_id=str(channel.id), shown=sent.content,
                                     sent=sent.created_at.timestamp())
                        self.save()
                        break
            text = alert['text']
            if not alert.get('message_id'):
                if not alert['open']:  # Over before the owner heard; a late DM about it is noise.
                    alert['shown'] = text
                    self.save()
                    return
                alert.update(attempted=text, attempted_at=time.time())
                alert.pop('settled', None)
                self.save()
                try:
                    sent = await channel.send(text, nonce=alert['nonce'], allowed_mentions=NO_MENTIONS)
                except (Exception, asyncio.CancelledError):
                    alert['settled'] = time.time()  # Discord can't take this send any later.
                    self.save()
                    raise
                # A nonce Discord already saw returns that first message, maybe with older text; the next pass edits it.
                alert.update(message_id=str(sent.id), channel_id=str(channel.id), shown=getattr(sent, 'content', text),
                             sent=time.time())
                self.save()
                LOG.warning('Owner told about a Gene outage: %s', alert['kind'])
                return
            try:
                await channel.get_partial_message(int(alert['message_id'])).edit(
                    content=text, allowed_mentions=NO_MENTIONS)
            except discord.NotFound:
                # Only Fri or Discord can delete Fri's message in a DM. If it's gone mid-outage, send it again.
                if alert['open']:
                    for key in ('message_id', 'channel_id', 'shown', 'attempted', 'attempted_at', 'settled'):
                        alert.pop(key, None)
                    alert.update(nonce=secrets.randbits(62), sent=None)
                else:
                    alert['shown'] = text
                    LOG.info('Outage DM is gone; its recovery note was dropped')
                self.save()
                return
            alert['shown'] = text
            self.save()
        except Exception as exc:  # It runs on its own, so nobody else would hear; the next pass tries again.
            LOG.warning('Gene watch deliver failed: %s', type(exc).__name__)

    def save(self):
        private_json(self.path, self.state)

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
        if isinstance(status, dict):  # `active_sha` is Gene's deployed release; the controller has none of its own here.
            lines.append(f"- controller: up, gene deployed {str(status.get('active_sha') or '?')[:7]}"
                         + (', deploys paused' if status.get('paused') else ''))
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
            watch += '' if alert.get('message_id') else ', not delivered yet'
        elif alert and alert.get('message_id') and alert.get('shown') != alert['text']:
            watch = 'all quiet, recovery note not delivered yet'
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
