"""fri #260: Fri's requester notices are never left up where main (a0ef9e7) takes them back.

Head's settling passes and tap run beside a verbatim, test-only copy of main's fri_fixes.py
(reference_fri_fixes_a0ef9e7.py), from the same notes and against the same scripted world:
- what the controller answers each finish, including answers no controller gives;
- what a read of the request says, or that it fails;
- a storage write or read failing at each point, or all of them, then storage coming back;
- Discord's copy of the notice arriving before, during each wait, or after;
- what Discord answers the send and each delete.

Whatever main deletes, head deletes too. Head deletes more only where the controller said the report is no
longer on record, or where it kept that claim's copy in memory after storage failed, and never another message
of Fri's: the copies of other claims, other channels and other authors that arrive with it."""

import asyncio
import importlib.util
import itertools
import json
import random
from pathlib import Path
from types import SimpleNamespace

import discord
import httpx

from fri import fri_fixes as head
from fri.fri_errors import ControllerRejected, ControllerUnavailable


def reference():
    path = Path(__file__).with_name('reference_fri_fixes_a0ef9e7.py')
    spec = importlib.util.spec_from_file_location('fri.reference_fri_fixes_a0ef9e7', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MAIN = reference()
GUILD, GENE, FRI, OWNER, MEMBER, CHANNEL, ELSEWHERE = 123, 88, 99, 10, 20, 456, 789
JOB, LEASE, SHA = 'abcdef012345', '0123456789abcdef0123456789abcdef', 'b'*40
NONCE = str(int((JOB+LEASE[:8])[:20], 16))
COPY, OTHER_CLAIM, OTHER_CHANNEL, OTHER_AUTHOR, REPORT = 1111, 2222, 3333, 4444, 5555
STRANGERS = {OTHER_CLAIM, OTHER_CHANNEL, OTHER_AUTHOR}
SOURCE = f'https://discord.com/channels/{GUILD}/{CHANNEL}/{REPORT}'


def note(age=600, **extra):
    at = head.time.time()-age
    return {'lease': LEASE, 'actor': str(OWNER), 'channel_id': str(CHANNEL), 'since': at, 'at': at, **extra}


SHAPES = {  # The job's note as a pass finds it.
    'cleared': lambda: note(cleared=True),
    'cleared, copy noted': lambda: note(cleared=True, message_id=str(COPY)),
    'not cleared': lambda: note(),
    'just sent': lambda: note(age=5, cleared=True),
    'take-back owed': lambda: note(cleared=True, message_id=str(COPY), take_back=True),
    'take-back owed, owner told': lambda: note(cleared=True, message_id=str(COPY), take_back=True, told=True),
    'tombstone': lambda: {'nonce': NONCE, 'lease': LEASE[:8], 'channel_id': str(CHANNEL), 'at': head.time.time()-600},
    'tombstone, copy noted': lambda: {'nonce': NONCE, 'lease': LEASE[:8], 'channel_id': str(CHANNEL),
                                      'at': head.time.time()-600, 'message_id': str(COPY), 'take_back': True},
    'lease without a nonce': lambda: {**note(cleared=True), 'lease': 'legacy-lease'},
}
FINISHES = ('sent', 'uncertain', 'invalidated', 'no state', 'failed', 'nothing', 'garbage', 'refused', 'lost')
READS = ('on record', 'gone', 'lost', 'refused', 'garbage', 'nothing')
FAULTS = (None, ('write', 1), ('write', 2), ('write', 3), ('write', 'all'), ('read', 1), ('read', 2), ('read', 'all'))
ECHOES = (None, 'before', 'send', 'finish', 'read', 'delete', 'after')
DELETES = ('ok', 'forbidden', 'unconfirmed', 'already gone')
LATER = ('sent', 'refused', 'uncertain')
CAPABILITIES = {'older': frozenset({'requester-notices'}),
                'authorize': frozenset({'requester-notices', 'requester-notice-authorize'}),
                'authorize, recheck': frozenset({'requester-notices', 'requester-notice-authorize',
                                                 'requester-notice-recheck'}),
                'recheck': frozenset({'requester-notices', 'requester-notice-recheck'})}
SENDS = ('ok', 'lost', '5xx', '4xx')


def answer(token, kind):
    if token == 'refused':
        raise ControllerRejected('This reply has already been recorded', 409)
    if token == 'lost':
        raise ControllerUnavailable('Gene did not answer')
    if kind == 'read':
        return {'on record': {'id': JOB, 'source_url': SOURCE, 'state': 'deployed'},
                'gone': {'id': JOB, 'source_url': '', 'state': 'deployed'},
                'garbage': {'id': JOB, 'source_url': None}, 'nothing': None}[token]
    return {'sent': {'state': 'sent', 'reason': None}, 'uncertain': {'state': 'uncertain', 'reason': None},
            'invalidated': {'state': 'invalidated', 'reason': 'The report is no longer on record'},
            'no state': {'ok': True}, 'failed': {'state': 'failed', 'reason': None}, 'nothing': None,
            'garbage': 'garbage'}[token]


def refusal(status):
    return SimpleNamespace(status=status, reason='Discord said no')


class Storage:
    """requester-notices.json in memory, each read and write counted so one can fail at any of them."""

    def __init__(self, entries, fault):
        self.text, self.fault, self.reads, self.writes = json.dumps(entries) if entries else None, fault, 0, 0

    def __truediv__(self, name):
        assert name == 'requester-notices.json'
        return self

    def read_text(self):
        self.reads += 1
        if self.fault in (('read', self.reads), ('read', 'all')):
            raise OSError('disk')
        if self.text is None:
            raise FileNotFoundError('requester-notices.json')
        return self.text

    def write(self, data):
        self.writes += 1
        if self.fault in (('write', self.writes), ('write', 'all')):
            raise OSError('disk')
        self.text = json.dumps(data)


class World:
    """One run of one module through a scripted case: the tap (if it has one) or a pass, then two more passes with
    storage back, as the heartbeat would run them."""

    def __init__(self, module, case):
        self.module, self.case = module, case
        self.deleted, self.heard_gone, self.heard_on_record, self.remembered = [], False, False, False
        self.echoed = case.get('echo') is None
        self.finishes, self.tapping, self.deletes = [case['finish']], 'shape' not in case, [case.get('delete', 'ok')]
        self.storage = Storage({JOB: SHAPES[case['shape']]()} if 'shape' in case else {}, case.get('fault'))
        room = SimpleNamespace(id=CHANNEL, guild=SimpleNamespace(id=GUILD, me=SimpleNamespace(id=FRI)),
                               permissions_for=lambda _: discord.Permissions.all(), parent_id=None,
                               get_partial_message=lambda message_id: SimpleNamespace(
                                   delete=lambda: self.delete(message_id)),
                               fetch_message=self.report)
        self.room, self.told = room, []
        owner = SimpleNamespace(dm_channel=SimpleNamespace(send=self.dm))
        agent = SimpleNamespace(config={'enabled': True, 'proactive': True, 'support_dms': True},
                                allowed=lambda channel: True, gene_id=GENE, member=self.member)
        self.fri = SimpleNamespace(guild_id=GUILD, owner_id=OWNER, user=SimpleNamespace(id=FRI), data=self.storage,
                                   capabilities=CAPABILITIES[case.get('capabilities', 'authorize, recheck')],
                                   api=self.api, agent=agent, get_user=lambda _: owner,
                                   get_channel=lambda id: room if id == CHANNEL else None,
                                   fetch_channel=self.fetch_channel)

    async def report(self, message_id):
        assert message_id == REPORT
        return SimpleNamespace(id=REPORT, author=SimpleNamespace(id=MEMBER, bot=False), webhook_id=None,
                               jump_url=SOURCE)

    async def member(self, *_, **__):
        return SimpleNamespace(id=MEMBER)

    async def fetch_channel(self, id):
        if id != CHANNEL:
            raise discord.NotFound(refusal(404), 'Unknown Channel')
        return self.room

    async def dm(self, text, **_):
        self.told.append(text)

    def arrive(self, point):
        """Discord's copy of the notice, at this point of the case if it's this one: with three of Fri's that
        aren't it (another claim's nonce, another channel, another author)."""
        if self.echoed or point != self.case.get('echo') and point != 'late':
            return
        self.echoed = True
        for message_id, nonce, channel, author in ((OTHER_CLAIM, str(int(NONCE)+1), CHANNEL, FRI),
                                                   (OTHER_CHANNEL, NONCE, ELSEWHERE, FRI),
                                                   (OTHER_AUTHOR, NONCE, CHANNEL, MEMBER),
                                                   (COPY, NONCE, CHANNEL, FRI)):
            self.module.seen(self.fri, SimpleNamespace(id=message_id, nonce=nonce, author=SimpleNamespace(id=author),
                                                       channel=SimpleNamespace(id=channel)))
        self.remembered = self.remembered or bool(getattr(self.module, 'COPIES', None))

    async def api(self, method, path, body=None):
        if path == '/requests/'+JOB and self.tapping:
            return {'id': JOB, 'state': 'deployed', 'approved_sha': SHA, 'source_url': SOURCE, 'user_id': str(MEMBER)}
        if path == '/status':
            return {'active_sha': SHA, 'jobs': {}, 'runtime_unknown': False}
        if path == f'/requests/{JOB}/requester-notice':
            return {'lease': LEASE, 'job': {'user_id': str(MEMBER), 'source_url': SOURCE, 'approved_sha': SHA}}
        if path.endswith('/requester-notice/authorize'):
            return {'ok': True}
        if path.endswith('/requester-notice/finish'):
            self.tapping = False  # Any read of the request from here on is the new one (see fri_fixes.withdrawn).
            self.arrive('finish')
            return answer(self.finishes.pop(0) if self.finishes else self.case.get('later', 'sent'), 'finish')
        if path == '/requests/'+JOB:
            self.arrive('read')
            self.heard_gone = self.heard_gone or self.case['read'] == 'gone'
            self.heard_on_record = self.heard_on_record or self.case['read'] == 'on record'
            return answer(self.case['read'], 'read')
        raise AssertionError(path)

    async def send(self, fri, post, *, reply_to=None, **_):
        assert post['id'] == JOB+LEASE[:8] and reply_to == REPORT
        outcome = self.case.get('send', 'ok')
        if outcome != '4xx':
            self.arrive('send')
        if outcome == 'lost':
            raise TimeoutError
        if outcome in ('5xx', '4xx'):
            response = httpx.Response(502 if outcome == '5xx' else 400, request=httpx.Request('POST', 'https://x'))
            raise httpx.HTTPStatusError('discord', request=response.request, response=response)
        return {'id': str(COPY)}

    async def delete(self, message_id):
        self.deleted.append(message_id)
        self.arrive('delete')
        outcome = self.deletes.pop(0) if self.deletes else 'ok'
        if outcome == 'forbidden':
            raise discord.Forbidden(refusal(403), 'Missing Permissions')
        if outcome == 'unconfirmed':
            raise discord.HTTPException(refusal(500), 'Internal Server Error')
        if outcome == 'already gone':
            raise discord.NotFound(refusal(404), 'Unknown Message')

    async def heartbeat(self):
        try:  # As Agent.heartbeat runs it: never at the cost of the tick.
            self.module.expire(self.fri)
        except Exception:
            pass
        await self.module.settle_all(self.fri)

    async def play(self):
        for module in (MAIN, head):
            module.DELETING.clear()
            module.TOLD.clear()
            module.TELLING.clear()
        getattr(head, 'COPIES', {}).clear()
        self.arrive('before')
        if self.tapping:
            await self.module.tell(self.fri, JOB, OWNER)
            self.tapping = False
        else:
            await self.heartbeat()
        self.storage.fault = None  # Storage is back.
        self.arrive('late')  # A copy whose moment never came here (a wait main doesn't have) comes now.
        self.finishes = []
        await self.heartbeat()
        await self.heartbeat()
        return self


def run(module, case, monkeypatch):
    world = World(module, case)
    monkeypatch.setattr(module, 'private_json', lambda path, data: path.write(data))
    monkeypatch.setattr('fri.fri_workspace.send_once', world.send)
    return asyncio.run(world.play())


def check(cases, monkeypatch):
    """What main deletes, head deletes too: the one exception allowed, a reply spared once a read of the request
    said its report is on record, head never takes. Head deletes more only with a reason, and never a stranger.
    How many cases head took more back in, by the report read and by a copy kept in memory alone."""
    wrong, spared, by_read, by_memory = [], [], 0, 0
    for case in cases:
        main, mine = run(MAIN, case, monkeypatch), run(head, case, monkeypatch)
        assert not STRANGERS & set(main.deleted), case
        missing, extra = set(main.deleted)-set(mine.deleted), set(mine.deleted)-set(main.deleted)
        if STRANGERS & extra or missing and not mine.heard_on_record:
            wrong.append((case, main.deleted, mine.deleted))
        elif missing:
            spared.append((case, main.deleted, mine.deleted))
        elif extra and mine.heard_gone:
            by_read += 1
        elif extra and mine.remembered:
            by_memory += 1
        elif extra:
            wrong.append((case, main.deleted, mine.deleted))
    assert not wrong, f'{len(wrong)} of {len(cases)}, first: {wrong[:3]}'
    assert not spared, f'{len(spared)} spared of {len(cases)}, first: {spared[:3]}'
    return by_read, by_memory


def cases(**dimensions):
    keys = list(dimensions)
    return [dict(zip(keys, values)) for values in itertools.product(*dimensions.values())]


def sample(count, seed, **dimensions):
    chosen = random.Random(seed)
    return [{key: chosen.choice(values) for key, values in dimensions.items()} for _ in range(count)]


PASS = dict(shape=tuple(SHAPES), finish=FINISHES, read=READS, fault=FAULTS, echo=tuple(e for e in ECHOES if e != 'send'),
            delete=DELETES, later=LATER)
TAP = dict(capabilities=tuple(CAPABILITIES), send=SENDS, finish=FINISHES, read=READS,
           fault=FAULTS+(('write', 4), ('write', 5)), echo=ECHOES, delete=DELETES, later=LATER)


def test_a_pass_never_leaves_up_what_main_takes_back_whatever_the_controller_answers(monkeypatch):
    by_read, _ = check(cases(shape=PASS['shape'], finish=FINISHES, read=READS, echo=PASS['echo']), monkeypatch)
    assert by_read  # The report read does take some back: #257, #262.


def test_a_pass_never_leaves_up_what_main_takes_back_whatever_fails_to_save(monkeypatch):
    _, by_memory = check(cases(shape=PASS['shape'], finish=FINISHES, read=('gone', 'lost', 'on record'), fault=FAULTS,
                               echo=PASS['echo']), monkeypatch)
    assert by_memory  # A copy kept in memory is taken back too: #258.


def test_a_pass_never_leaves_up_what_main_takes_back_whatever_discord_answers(monkeypatch):
    check(cases(shape=PASS['shape'], finish=FINISHES, read=('gone', 'on record'), echo=(None, 'finish', 'delete'),
                delete=DELETES, later=LATER), monkeypatch)


def test_a_pass_never_leaves_up_what_main_takes_back_in_any_mix(monkeypatch):
    check(sample(3000, 260, **PASS), monkeypatch)


def test_a_tap_never_leaves_up_what_main_takes_back_whatever_the_controller_answers(monkeypatch):
    by_read, _ = check(cases(capabilities=TAP['capabilities'], send=SENDS, finish=FINISHES, read=READS,
                             echo=(None, 'send', 'read', 'after')), monkeypatch)
    assert by_read  # #262 on the tap.


def test_a_tap_never_leaves_up_what_main_takes_back_whatever_fails_to_save(monkeypatch):
    _, by_memory = check(cases(capabilities=('authorize', 'authorize, recheck'), send=('ok', 'lost'), finish=FINISHES,
                               read=('gone', 'lost'), fault=TAP['fault'], echo=(None, 'send', 'finish', 'after')),
                         monkeypatch)
    assert by_memory


def test_a_tap_never_leaves_up_what_main_takes_back_in_any_mix(monkeypatch):
    check(sample(2000, 262, **TAP), monkeypatch)
