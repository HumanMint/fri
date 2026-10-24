"""Synthetic full-handler replay: real Fri pipeline and Gene controller, fake Discord and model.

Nothing here touches production code, live Discord or live state. Scripted mode is offline.
Routes come from observable effects plus a pass-through spy on Actions.execute; source is unchanged.
Heartbeat work (attention, learning, memes, outbox delivery) is not driven yet; steps are messages.
"""

import asyncio
import contextlib
import datetime as dt
import hashlib
import importlib
import json
import os
import pkgutil
import re
import sys
import tempfile
import threading
import time
import types
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import discord
import httpx
from fastapi.testclient import TestClient
from gene.api import create_app
from gene.config import Settings
from gene.fri_agent_store import AgentStore
from gene.fri_mentions import BREADCRUMB_REPLY

from fri.fri_actions import Actions
from fri.fri_chat import Chat
from fri.fri_protocol import compatible

GUILD, OWNER, FRI = 123, 10, 99
WORKSHOP, CONFIGURED, OUTSIDE = 456, 1, 8
TOKEN = 'fri-' + 'x' * 32
BASE = 'http://controller.replay'
ADMISSION = ('/agent/admit', '/chat/mentions/admit', '/chat/operations/admit')
NAMES = {OWNER: 'owner', 20: 'bo', 30: 'ash', 40: 'kit'}
# Exact neutral placeholders stored instead of a reply (fri.py:237, :342-346, :393; Gene's mention
# breadcrumb). Full matches only: a changed wording fails loudly rather than passing as neutral.
PLACEHOLDERS = (
    re.compile(r'Earlier check: [a-z_]+(?: in channel \d+)?\. (?:Public research receipts are available '
               r'separately for follow-ups\.|Fetch current evidence for follow-up details; no findings are '
               r'retained here\.)'),
    re.compile(re.escape('Earlier reply-context check. Fetch current Discord evidence for follow-up details; '
                         'no quoted findings are retained here.')),
    re.compile(re.escape(BREADCRUMB_REPLY)),
)


def placeholder(reply):
    return any(pattern.fullmatch(reply or '') for pattern in PLACEHOLDERS)


class BudgetExhausted(httpx.TransportError):
    """Raised before sending; production code treats it as an interrupted connection."""


class UnscriptedCall(RuntimeError):
    """Not an AssertionError, so it can never satisfy a known-gap xfail."""


class Clock:
    """Virtual wall clock for Fri and the controller. Monotonic time stays real for asyncio."""

    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += max(0.0, seconds)


class _RealDatetime(type):
    """isinstance/issubclass against the virtual class still accept real datetimes."""

    def __instancecheck__(cls, value):
        return isinstance(value, dt.datetime)

    def __subclasscheck__(cls, value):
        return issubclass(value, dt.datetime)


def clock_patches(clock):
    """Point every loaded fri/gene module's `dt` at a datetime module whose now() is virtual."""

    class VirtualDatetime(dt.datetime, metaclass=_RealDatetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime.fromtimestamp(clock(), tz)

        @classmethod
        def utcnow(cls):
            return dt.datetime.fromtimestamp(clock(), dt.timezone.utc).replace(tzinfo=None)

        @classmethod
        def today(cls):
            return dt.datetime.fromtimestamp(clock())

    import fri
    for info in pkgutil.iter_modules(fri.__path__):
        importlib.import_module('fri.'+info.name)  # Lazily imported modules must see the proxy too.
    proxy = types.ModuleType('datetime')
    proxy.__dict__.update({key: value for key, value in vars(dt).items() if not key.startswith('__')})
    proxy.datetime = VirtualDatetime
    return [patch.object(module, 'dt', proxy) for name, module in list(sys.modules.items())
            if name.split('.')[0] in ('fri', 'gene') and getattr(module, 'dt', None) is dt]


@dataclass
class Call:
    method: str
    path: str
    status: int
    payload_keys: list
    allowed: bool | None = None
    saved: dict | None = None  # An accepted save request, plus what the store actually retained.
    rejected: bool = False  # A /chat/turn save the controller refused.
    tokens: dict | None = None
    inference: str | None = None  # Provider attempt status from Fri's own usage receipt.


@dataclass
class Delivery:
    kind: str
    channel: int
    target: int
    text: str = ''
    view: bool = False
    failed: bool = False
    message_id: int = 0


@dataclass
class Step:
    index: int
    label: str
    actor: int | None
    channel: int | None
    controller: list = field(default_factory=list)
    model: list = field(default_factory=list)
    deliveries: list = field(default_factory=list)
    actions: list = field(default_factory=list)  # Executed tools, including early-return ones.

    def conversation(self):
        return [m for m in self.model if m['kind'] == 'conversation']

    def route(self):
        """Classify the decision from observable effects only; production code is not instrumented."""
        paths = [c.path for c in self.controller]
        saved = [c.saved for c in self.controller if c.saved]
        sent = [d for d in self.deliveries if not d.failed]
        local = next((s['model'] for s in saved if s['model'].startswith('local-')), None)
        conversation = self.conversation()
        if any(c.allowed is False for c in self.controller):
            kind = 'cooldown'
        elif local:
            kind = local
        elif '/chat/memory/control' in paths:
            kind = 'local-privacy'
        elif conversation:
            kind = 'model'
        elif self.model and sent:
            kind = 'owner-control'
        elif sent and not saved and any(d.view for d in sent) and '/chat/context' in paths:
            kind = 'local-memory'
        elif sent:
            kind = 'notice'
        elif self.controller:
            kind = 'observed'
        else:
            kind = 'ignored'
        if any(d.kind == 'reply' for d in sent):
            outcome = 'reply'
        elif any(d.kind == 'reaction' for d in sent):
            outcome = 'react'
        elif any(d.failed for d in self.deliveries):
            outcome = 'delivery-failed'
        else:
            outcome = 'silent'
        texts = [d.text for d in self.deliveries if d.kind == 'reply']
        stored = []
        for item in saved:
            kept = item['retained']
            reply = (kept or {}).get('reply') or ''
            if kept is None:
                stored.append('not-retained')
            elif kept.get('response_kind', 'reply') != 'reply':
                stored.append(kept['response_kind'])
            elif placeholder(reply):
                stored.append('breadcrumb')
            elif any(reply and text.startswith(reply) for text in texts):
                stored.append('reply-text')
            else:
                stored.append('other')
        receipts = conversation[-1]['receipts'] if conversation else []
        return {'kind': kind, 'outcome': outcome, 'admission': sorted({p for p in paths if p in ADMISSION}),
                'model_calls': len(self.model), 'conversation_calls': len(conversation),
                'inference': [c.inference for c in self.controller if c.inference],
                'tools': [item['kind'] for item in self.actions], 'tool_outcomes': self.actions,
                'receipts': receipts, 'stored': stored, 'save_rejected': sum(c.rejected for c in self.controller),
                'clarification': any(item.get('clarification') for item in saved),
                'reaction': next((d.text for d in sent if d.kind == 'reaction'), None)}

    def answered(self):
        """A delivered text reply backed by completed inference, not an error notice."""
        route = self.route()
        return (route['outcome'] == 'reply' and bool(route['inference']) and
                all(status == 'completed' for status in route['inference']))

    def tokens(self):
        rows = [c.tokens for c in self.controller if c.tokens]
        return {key: sum(row.get(key) or 0 for row in rows) for key in ('input_tokens', 'output_tokens')}

    def report(self):
        return {'label': self.label, 'actor': self.actor, 'channel': self.channel, 'route': self.route(),
                'model': self.model, 'controller_paths': [c.path for c in self.controller],
                'deliveries': [d.__dict__ for d in self.deliveries], 'tokens': self.tokens()}


def size(value):
    if isinstance(value, bool) or value is None:
        return bool(value)
    if isinstance(value, (list, dict, str)):
        return len(value)
    return value


def manifest(payload, kind):
    """Context shape and versions without raw member text."""
    instructions = payload.get('instructions', '')
    result = {'kind': kind, 'model': payload.get('model'),
              'effort': (payload.get('reasoning') or {}).get('effort'),
              'prompt_version': hashlib.sha256(instructions.encode()).hexdigest()[:12],
              'input_chars': len(json.dumps(payload.get('input', [])))}
    if kind != 'conversation':
        return result
    messages = payload['input']
    content, images = messages[-1]['content'], 0
    if isinstance(content, list):
        images = sum(part.get('type') == 'input_image' for part in content)
        content = next(part['text'] for part in content if part.get('type') == 'input_text')
    data = json.loads(content)
    schema = payload['text']['format']['schema']['properties']['response_kind']
    recent = data.get('recent_channel_context') or []
    result.update(prior_turns=(len(messages)-1)//2, images=images,
                  fields={key: size(value) for key, value in data.items() if key != 'message'},
                  recent_authors=len({row.get('user_id') for row in recent if isinstance(row, dict)}),
                  offered_actions=sorted(data.get('available_actions') or {}),
                  receipts=[row.get('kind') for row in data.get('action_results_untrusted') or []],
                  quiet_allowed=data['response_options']['quiet'],
                  text_only=schema.get('enum') == ['reply'])
    return result


def classify(request, payload):
    if request.method == 'GET':
        return 'catalog'
    if any(tool.get('type') == 'web_search' for tool in payload.get('tools', [])):
        return 'web'
    name = ((payload.get('text') or {}).get('format') or {}).get('name')
    return 'conversation' if name == 'fri_reply' else 'internal'


def reply(text, *, allow_closing=False, draft=None):
    return {'response_kind': 'reply', 'allow_closing': allow_closing, 'reaction': None, 'reply': text,
            'request_draft': draft, 'notes': [], 'action': None}


def react(emoji='👍'):
    return {**reply(''), 'response_kind': 'react', 'reaction': emoji}


def quiet():
    return {**reply(''), 'response_kind': 'quiet'}


def act(kind, **fields):
    return {**reply(''), 'action': {'kind': kind, **fields}}


def web(answer, url):
    return {'answer': answer, 'url': url}


def _stream(output, payload):
    estimate = len(json.dumps(payload.get('input', [])))//4
    event = {'type': 'response.completed', 'response': {'output': output, 'usage': {
        'input_tokens': estimate, 'output_tokens': 40, 'input_tokens_details': {'cached_tokens': 0},
        'output_tokens_details': {'reasoning_tokens': 0}}}}
    return httpx.Response(200, text='data: '+json.dumps(event)+'\n\n',
                          headers={'content-type': 'text/event-stream'})


class ModelTransport(httpx.AsyncBaseTransport):
    """Records every provider request; answers from a script or forwards to a live transport."""

    def __init__(self, steps, script=None, live=None, max_calls=None):
        self.steps, self.live, self.max_calls = steps, live, max_calls
        self.script = {kind: list(rows) for kind, rows in (script or {}).items()}
        self.calls = 0

    async def handle_async_request(self, request):
        payload = json.loads(await request.aread() or b'{}')
        kind = classify(request, payload)
        if self.max_calls is not None and self.calls >= self.max_calls:
            raise BudgetExhausted('replay model budget exhausted', request=request)
        self.calls += 1
        self.steps[-1].model.append(manifest(payload, kind))
        if self.live is not None:
            return await self.live.handle_async_request(request)
        rows = self.script.get(kind) or []
        if not rows:
            raise UnscriptedCall(f'unscripted {kind} model call in step {self.steps[-1].label!r}')
        row = rows.pop(0)
        if callable(row):  # A scripted response may change the world mid-turn, e.g. an erasure.
            row = row(self.replay)
        if kind == 'web':
            return _stream([{'id': 'ws_1', 'type': 'web_search_call', 'status': 'completed'},
                            {'id': 'msg_1', 'type': 'message', 'content': [{
                                'type': 'output_text', 'text': row['answer'],
                                'annotations': [{'type': 'url_citation', 'url': row['url']}]}]}], payload)
        return _stream([{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(row)}]}],
                       payload)

    def unused(self):
        return {kind: len(rows) for kind, rows in self.script.items() if rows}


class _Response:
    def __init__(self, response):
        self.status, self._response = response.status_code, response

    async def json(self):
        try:
            return self._response.json()
        except ValueError:
            raise aiohttp.ClientError('non-JSON controller response') from None  # aiohttp's mapping


class ControllerSession:
    """aiohttp-shaped adapter: Fri.api's real headers and error mapping run unchanged."""

    def __init__(self, client, steps):
        self.client, self.steps = client, steps

    @contextlib.asynccontextmanager
    async def _exchange(self, method, url, payload, headers):
        path = url.removeprefix(BASE)
        response = self.client.request(method, path, json=payload, headers=headers)
        call = Call(method, path, response.status_code, sorted(payload or {}))
        body = response.json() if response.headers.get('content-type', '').startswith('application/json') else {}
        if path in ADMISSION and isinstance(body, dict):
            call.allowed = body.get('allowed')
        if path == '/chat/turn' and payload and response.status_code >= 400:
            call.rejected = True
        elif path == '/chat/turn' and payload:
            call.saved = {'model': payload.get('model') or '', 'clarification': payload.get('clarification'),
                          'retained': self._retained(payload)}
        if path == '/chat/usage/report' and payload:
            call.tokens = payload.get('counters') or {}
            call.inference = payload.get('status') or 'unknown'
        self.steps[-1].controller.append(call)
        yield _Response(response)

    def request(self, method, url, *, json=None, headers=None):
        return self._exchange(method, url, json, headers)

    def _retained(self, payload):
        """Read back what the controller actually kept for this source event (None if nothing)."""
        try:
            turns = self.client.app.state.store.chat_context(
                payload['guild_id'], payload['conversation_id'], payload['user_id'])['turns']
        except (KeyError, ValueError):
            return None
        source = (payload.get('source') or {}).get('id')
        turn = next((t for t in reversed(turns) if source and (t.get('source') or {}).get('id') == source), None)
        return {'reply': turn['reply'], 'response_kind': turn.get('response_kind', 'reply')} if turn else None

    async def close(self):
        pass


class World:
    """Fake Discord guild, channels, members and messages with virtual-clock snowflakes."""

    def __init__(self, fri, clock, steps):
        self.fri, self.clock, self.steps = fri, clock, steps
        self.rows, self.labels, self.channels, self.denied = {}, {}, {}, set()
        self.fail_next_delivery = False
        self.last_id = 0
        self.guild = SimpleNamespace(id=GUILD, me=fri.user, unavailable=False, filesize_limit=8 << 20)
        self.guild.fetch_channel = AsyncMock(side_effect=lambda cid: self.channels[cid])
        self.guild.fetch_member = AsyncMock(side_effect=self.member)
        self.guild.fetch_roles = AsyncMock(return_value=[])
        for cid, name in ((WORKSHOP, 'workshop'), (CONFIGURED, 'general'), (OUTSIDE, 'memes')):
            self.add_channel(cid, name)
        fri.get_channel = lambda cid: self.channels.get(cid)
        fri.get_guild = lambda gid: self.guild if gid == GUILD else None

    async def member(self, user):
        return SimpleNamespace(id=user, bot=user == FRI, roles=[], display_name=NAMES.get(user, f'member {user}'))

    def add_channel(self, cid, name):
        world = self

        @contextlib.asynccontextmanager
        async def typing():
            yield

        async def history(*, limit=100, before=None, after=None, oldest_first=None, around=None):
            items = [m for m in world.rows.values() if m.channel.id == cid]
            if before is not None:
                items = [m for m in items if m.id < getattr(before, 'id', before)]
            if isinstance(after, dt.datetime):
                items = [m for m in items if m.created_at > after]
            elif after is not None:
                items = [m for m in items if m.id > after.id]
            oldest = oldest_first if oldest_first is not None else after is not None
            for item in sorted(items, key=lambda m: m.id, reverse=not oldest)[:limit]:
                yield item

        async def fetch_message(mid):
            if mid not in world.rows or world.rows[mid].channel.id != cid:
                raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'Unknown Message')
            return world.rows[mid]

        def permissions_for(member):
            denied = (cid, getattr(member, 'id', None)) in world.denied
            return discord.Permissions.none() if denied else discord.Permissions.all()

        channel = SimpleNamespace(id=cid, guild=self.guild, parent_id=None, nsfw=False, name=name,
                                  typing=typing, history=history, permissions_for=permissions_for)
        async def send(content=None, **kwargs):
            return await world._sent(None, channel, content, kwargs)

        channel.fetch_message = fetch_message
        channel.send = AsyncMock(side_effect=send)
        self.channels[cid] = channel
        return channel

    def message(self, user, text, channel, *, reference=None, label=''):
        mid = discord.utils.time_snowflake(dt.datetime.fromtimestamp(self.clock.now, dt.timezone.utc))
        mid = max(mid, self.last_id+1)
        self.last_id = mid
        author = SimpleNamespace(id=user, display_name=NAMES.get(user, f'member {user}'), bot=user == FRI,
                                 name=NAMES.get(user, str(user)))
        if reference and reference.startswith('@'):  # '@step' targets Fri's reply delivered in that step.
            step = next(s for s in self.steps if s.label == reference[1:])
            target = self.rows.get(next(d.message_id for d in step.deliveries if d.kind == 'reply' and not d.failed))
        else:
            target = self.rows.get(self.labels.get(reference)) if reference else None
        item = Mock(spec=discord.Message, id=mid, content=text, channel=self.channels[channel],
                    guild=self.guild, author=author, webhook_id=None, attachments=[], embeds=[],
                    stickers=[], reactions=[], edited_at=None, created_at=discord.utils.snowflake_time(mid),
                    mentions=[self.fri.user] if f'<@{FRI}>' in text or f'<@!{FRI}>' in text else [],
                    jump_url=f'https://discord.com/channels/{GUILD}/{channel}/{mid}',
                    reference=SimpleNamespace(message_id=target.id, channel_id=channel, resolved=target)
                    if target else None)
        async def respond(content=None, **kwargs):
            return await self._sent(item, item.channel, content, kwargs)

        async def reaction(emoji):
            return await self._reacted(item, emoji)

        item.reply = AsyncMock(side_effect=respond)
        item.add_reaction = AsyncMock(side_effect=reaction)
        item.delete = AsyncMock()
        self.rows[mid] = item
        if label:
            self.labels[label] = mid
        return item

    async def _sent(self, target, channel, content, kwargs):
        delivery = Delivery('reply', channel.id, target.id if target else 0, content or '',
                            view=kwargs.get('view') is not None)
        self.steps[-1].deliveries.append(delivery)
        if self.fail_next_delivery:
            self.fail_next_delivery, delivery.failed = False, True
            raise discord.HTTPException(SimpleNamespace(status=503, reason='synthetic'), 'synthetic delivery failure')
        sent = self.message(FRI, content or '', channel.id, reference=None)
        delivery.message_id = sent.id
        return sent

    async def _reacted(self, target, emoji):
        self.steps[-1].deliveries.append(Delivery('reaction', target.channel.id, target.id, emoji))
        target.reactions.append(SimpleNamespace(emoji=emoji, me=True))


@dataclass
class Say:
    actor: int
    text: str
    channel: int = CONFIGURED
    label: str = ''
    reply_to: str = ''


@dataclass
class Wait:
    seconds: float


@dataclass
class FailNextDelivery:
    pass


@dataclass
class Deny:
    """Remove a member's permissions in one channel (fresh fetches observe it)."""
    channel: int
    actor: int


@dataclass
class Inspect:
    """Record fn(replay) mid-run, inside the virtual clock, for checks to read later."""
    label: str
    fn: object


@dataclass
class Check:
    name: str
    test: object
    scripted_only: bool = False
    known_gap: str = ''


@dataclass
class Scenario:
    name: str
    description: str
    steps: list
    script: dict = field(default_factory=dict)
    checks: list = field(default_factory=list)
    expected_rejections: list = field(default_factory=list)  # Controller paths allowed to return 4xx.


class Replay:
    """One scenario run. Exposes the trace plus the synthetic controller store for checks."""

    def __init__(self, scenario, root, *, live=None, subscription=None, max_calls=None, chat_model=None):
        """live: a transport instance, or a zero-argument factory (e.g. httpx.AsyncHTTPTransport).

        A factory's transport is created and closed inside this run's event loop, so pooled
        connections never outlive it. Prefer a factory when running several scenarios.
        """
        self.scenario, self.root, self.chat_model, self.live = scenario, Path(root), chat_model, live
        self.steps = [Step(0, 'setup', None, None)]
        self.transport = ModelTransport(self.steps, None if live else scenario.script, None, max_calls)
        self.transport.replay = self
        self.subscription = subscription or SimpleNamespace(access_token=lambda: 'synthetic-plan-token')
        self.mode = 'live' if live else 'scripted'
        self.inspections = {}

    def step(self, label):
        return next(step for step in self.steps if step.label == label)

    def stored(self, user, channel=CONFIGURED):
        return self.store.chat_context(str(GUILD), f'public:{channel}', str(user))

    def retained(self, label):
        """Turns the controller still holds for one step's source event, independent of wording."""
        step, source = self.step(label), str(self.world.labels[label])
        return [turn for turn in self.stored(step.actor, step.channel)['turns']
                if (turn.get('source') or {}).get('id') == source]

    def run(self):
        real_time = time.time
        clock = Clock(real_time())
        thread = threading.get_ident()
        real_sleep = asyncio.sleep
        # Subscription expiry and refresh bookkeeping persist to disk: keep them on real time.
        import fri.subscription
        subscription_time = types.ModuleType('time')
        subscription_time.__dict__.update(vars(time))
        subscription_time.time = real_time

        async def sleep(delay, result=None):
            if threading.get_ident() == thread:
                clock.advance(delay)
                return await real_sleep(0, result)
            return await real_sleep(delay, result)

        environment = {'GENE_GUILD_ID': str(GUILD), 'GENE_OWNER_ID': str(OWNER),
                       'GENE_WORKSHOP_CHANNEL_ID': str(WORKSHOP), 'GENE_FRI_TOKEN': TOKEN,
                       'GENE_CONTROLLER_URL': BASE, 'FRI_DATA': str(self.root/'data'),
                       'FRI_CHATGPT_AUTH': str(self.root/'unused-session.json'), 'FRI_PRESENCE': 'invisible',
                       'GENE_BOT_ID': '0', 'FRI_CHAT_CHANNEL_ID': '0',
                       'XDG_CONFIG_HOME': str(self.root/'config')}
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, environment))
            stack.enter_context(patch.object(time, 'time', clock))
            stack.enter_context(patch.object(asyncio, 'sleep', sleep))
            stack.enter_context(patch.object(fri.subscription, 'time', subscription_time))
            stack.enter_context(patch.object(Actions, 'execute', self._spy(Actions.execute)))
            for item in clock_patches(clock):
                stack.enter_context(item)
            asyncio.run(self._run(clock))
            self.results = self.evaluate()  # Store reads purge by TTL, so judge on the virtual clock.
        return self

    def _spy(self, execute):
        steps = self.steps

        async def spy(actions, action):
            kind = action.get('kind') if isinstance(action, dict) else None
            try:
                receipt = await execute(actions, action)
            except (Exception, asyncio.CancelledError) as exc:  # The runner's timeout cancels the tool.
                steps[-1].actions.append({'kind': kind, 'outcome': type(exc).__name__})
                raise
            outcome = next((key for key in ('blocked', 'uncertain') if key in receipt), 'returned')
            steps[-1].actions.append({'kind': kind, 'outcome': outcome})
            return receipt
        return spy

    async def _run(self, clock):
        from fri.fri import Fri
        from fri.fri_agent import Agent
        settings = Settings(self.root/'controller.sqlite3', str(GUILD), str(OWNER), 'runtime-'+'x'*32,
                            'worker-'+'x'*32, 'deploy-'+'x'*32, workshop_channel_id=str(WORKSHOP),
                            fri_token=TOKEN, agent_channels=str(CONFIGURED), web_search_enabled=True)
        self.client = TestClient(create_app(settings), raise_server_exceptions=False)
        self.store = self.client.app.state.store
        AgentStore(self.store, settings).update({'enabled': True}, 0, str(OWNER))
        if self.chat_model:
            model, effort = self.chat_model
            self.store.update_chat_models([model])
            self.store.select_chat_model(model, effort, str(OWNER))
        self.fri = fri = Fri()
        fri._connection.user = SimpleNamespace(id=FRI, display_name='Fri', bot=True, name='fri')
        fri.session = ControllerSession(self.client, self.steps)
        owned = callable(self.live) and not isinstance(self.live, httpx.AsyncBaseTransport)
        self.transport.live = self.live() if owned else self.live
        fri.chat = Chat(self.subscription, self.transport)
        self.world = World(fri, clock, self.steps)
        try:
            if not compatible(await fri.api('GET', '/fri/contract')):
                raise RuntimeError('synthetic controller contract is incompatible')
            self.agent = fri.agent = Agent(fri)
            self.agent.state = await fri.api('GET', '/agent/state')
            for index, action in enumerate(self.scenario.steps, 1):
                if isinstance(action, Wait):
                    clock.advance(action.seconds)
                elif isinstance(action, FailNextDelivery):
                    self.world.fail_next_delivery = True
                elif isinstance(action, Deny):
                    self.world.denied.add((action.channel, action.actor))
                elif isinstance(action, Inspect):
                    self.inspections[action.label] = action.fn(self)
                else:
                    self.steps.append(Step(index, action.label or f'step-{index}', action.actor, action.channel))
                    event = self.world.message(action.actor, action.text, action.channel,
                                               reference=action.reply_to, label=action.label)
                    await fri.on_message(event)
                    clock.advance(1)
        finally:
            await fri.close()
            if owned:
                await self.transport.live.aclose()

    def evaluate(self):
        results = []
        for check in self.scenario.checks:
            if check.scripted_only and self.mode == 'live':
                continue
            try:
                passed, detail = bool(check.test(self)), ''
            except Exception as exc:  # A failing check reports, never aborts the report.
                passed, detail = False, f'{type(exc).__name__}: {exc}'
            results.append({'name': check.name, 'passed': passed, 'known_gap': check.known_gap,
                            'detail': detail})
        return results

    def report(self):
        steps = [step.report() for step in self.steps]
        calls = [(s.label, c) for s in self.steps for c in s.controller]
        return {'scenario': self.scenario.name, 'mode': self.mode, 'synthetic': True,
                'model_calls': self.transport.calls, 'unused_script': self.transport.unused(),
                'controller_errors': [c.path for _, c in calls if c.status >= 500],
                'controller_rejections': [[label, c.path, c.status] for label, c in calls if 400 <= c.status < 500],
                'unexpected_rejections': [[label, c.path, c.status] for label, c in calls if 400 <= c.status < 500
                                          and c.path not in self.scenario.expected_rejections],
                'rejected_saves': [label for label, c in calls if c.rejected],
                'inference_failures': [[label, c.inference] for label, c in calls
                                       if c.inference and c.inference != 'completed'],
                'prompt_versions': sorted({m['prompt_version'] for s in self.steps for m in s.model}),
                'tokens': {key: sum(s['tokens'][key] for s in steps) for key in ('input_tokens', 'output_tokens')},
                'steps': steps, 'checks': self.results}


def failures(reports):
    """The single validity rule shared by pytest and the CLI; any entry makes a run unsuccessful."""
    skipped = [f"{report['scenario']} skipped: {report['skipped']}" for report in reports if 'skipped' in report]
    return skipped + [item for report in reports if 'checks' in report for item in
            [c['name'] for c in report['checks'] if c['detail'] or (not c['passed'] and not c['known_gap'])] +
            [f'controller error for {path}' for path in report['controller_errors']] +
            [f'controller {status} for {path} in {label}' for label, path, status in report['unexpected_rejections']] +
            [f'rejected save in {label}' for label in report['rejected_saves']] +
            [f'inference {status} in {label}' for label, status in report['inference_failures']] +
            [f'unused scripted {kind} responses: {count}' for kind, count in report['unused_script'].items()]]


def run(scenario, root=None, **kwargs):
    """Run one scenario and return its JSON-serializable report (checks evaluated in place)."""
    if root is not None:
        return Replay(scenario, root, **kwargs).run().report()
    with tempfile.TemporaryDirectory(prefix='fri-replay-') as directory:
        return Replay(scenario, directory, **kwargs).run().report()
