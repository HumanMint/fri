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
# Another job's send, settled unconfirmed before the case starts: its tombstone waits for its one copy (#260's round 7).
NEIGHBOUR, NEIGHBOUR_LEASE, NEIGHBOUR_COPY = 'fedcba987654', 'fedcba98'+'0'*24, 6666
NEIGHBOUR_NONCE = str(int((NEIGHBOUR+NEIGHBOUR_LEASE[:8])[:20], 16))
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
EVERY_READ = tuple(('read', n) for n in range(1, 9))  # Each read of a pass in turn (#260's round 7).
EVERY_WRITE = tuple(('write', n) for n in range(1, 6))
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


class Gene:
    """What a controller answers each finish, from what it holds: 'pin' (the pinned Gene: it checks a sent reply again
    and answers a repeat with its record) or 'live' (one before gene #143: it records without checking, says nothing
    of the state, and refuses a repeat). recorded: a record it already holds. lose: the first answer is lost after it
    records."""

    def __init__(self, kind, erased, recorded, lose):
        self.kind, self.erased, self.record, self.lose = kind, erased, recorded, lose

    def finish(self, state):
        if self.record is None:
            self.record = 'invalidated' if self.kind == 'pin' and state == 'sent' and self.erased else state
            reply = {'state': self.record, 'reason': None} if self.kind == 'pin' else {'ok': True}
        elif self.kind == 'pin':
            reply = {'state': self.record, 'reason': None}
        else:
            raise ControllerRejected('This reply has already been recorded', 409)
        if self.lose:
            self.lose = False
            raise ControllerUnavailable('Gene did not answer')
        return reply


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
        self.echoed, self.again, self.phase = case.get('echo') is None, case.get('again') is None, 1
        self.finishes, self.tapping, self.deletes = [case.get('finish')], 'shape' not in case, [case.get('delete', 'ok')]
        self.gene = Gene(case['controller'], case['erased'], case['recorded'], case['lose']) if 'controller' in case \
            else None
        entries = {JOB: SHAPES[case['shape']]()} if 'shape' in case else {}
        if case.get('neighbour', True):  # Another claim's send settled unconfirmed, waiting for its one copy.
            entries[NEIGHBOUR] = {'nonce': NEIGHBOUR_NONCE, 'lease': NEIGHBOUR_LEASE[:8], 'channel_id': str(CHANNEL),
                                  'at': head.time.time()-300}
        self.storage = Storage(entries, case.get('fault'))
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

    def copy(self, message_id, nonce, channel=CHANNEL, author=FRI):
        self.module.seen(self.fri, SimpleNamespace(id=message_id, nonce=nonce, author=SimpleNamespace(id=author),
                                                   channel=SimpleNamespace(id=channel)))
        self.remembered = self.remembered or bool(getattr(self.module, 'COPIES', None))

    def arrive(self, point):
        """Discord's copy of the notice, at this point of the case if it's this one: with three of Fri's that
        aren't it (another claim's nonce, another channel, another author). `again`: the gateway delivers that same
        message a second time, at that point of the passes after storage came back."""
        if not self.again and point == self.case.get('again'):
            self.again = True
            self.copy(COPY, NONCE)
        if self.echoed or point != self.case.get('echo') and point != 'late':
            return
        self.echoed = True
        for message_id, nonce, channel, author in ((OTHER_CLAIM, str(int(NONCE)+1), CHANNEL, FRI),
                                                   (OTHER_CHANNEL, NONCE, ELSEWHERE, FRI),
                                                   (OTHER_AUTHOR, NONCE, CHANNEL, MEMBER),
                                                   (COPY, NONCE, CHANNEL, FRI)):
            self.copy(message_id, nonce, channel, author)

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
            assert path == f'/requests/{JOB}/requester-notice/finish'  # The neighbour's tombstone asks nothing.
            self.tapping = False  # Any read of the request from here on is the new one (see fri_fixes.withdrawn).
            self.arrive('finish')
            self.arrive('finish '+str(self.phase))
            if self.gene:
                return self.gene.finish(body['state'])
            return answer(self.finishes.pop(0) if self.finishes else self.case.get('later', 'sent'), 'finish')
        if path == '/requests/'+JOB:
            self.arrive('read')
            read = self.case['read']
            if read == 'model':  # What the modelled controller holds: the report, unless the reporter erased.
                read = 'gone' if self.case['erased'] else 'on record'
            self.heard_gone = self.heard_gone or read == 'gone'
            self.heard_on_record = self.heard_on_record or read == 'on record'
            return answer(read, 'read')
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
        if self.case.get('neighbour', True):
            self.copy(NEIGHBOUR_COPY, NEIGHBOUR_NONCE)  # The neighbour's one copy, which main takes back.
        self.finishes, self.phase = [], 2
        await self.heartbeat()
        self.arrive('after 2')
        self.phase = 3
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
        missing = set(main.deleted)-set(mine.deleted)
        # The neighbour's reply is held only to main's: one main's own write over unreadable notes lost may come down.
        extra = set(mine.deleted)-set(main.deleted)-{NEIGHBOUR_COPY}
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
    assert by_read
    # #262 on the tap itself: a controller that isn't asked for the send leaves no note for a pass to read again.
    by_read, _ = check(cases(capabilities=('older',), send=('ok',), finish=FINISHES, read=READS), monkeypatch)
    assert by_read


def test_a_tap_never_leaves_up_what_main_takes_back_whatever_fails_to_save(monkeypatch):
    _, by_memory = check(cases(capabilities=('authorize', 'authorize, recheck'), send=('ok', 'lost'), finish=FINISHES,
                               read=('gone', 'lost'), fault=TAP['fault'], echo=(None, 'send', 'finish', 'after')),
                         monkeypatch)
    assert by_memory


def test_a_tap_never_leaves_up_what_main_takes_back_in_any_mix(monkeypatch):
    check(sample(2000, 262, **TAP), monkeypatch)


MODEL = dict(controller=('pin', 'live'), erased=(True, False), recorded=(None, 'sent', 'uncertain'), lose=(False, True))
AGAIN = (None, 'finish 1', 'finish 2', 'after 2')


def test_a_take_back_never_writes_another_notices_tombstone_away(monkeypatch):
    """#260's round 7: taking one notice back wrote its mark through note(), whose own read could fail after the one
    before it worked, and then saved that note alone: the neighbour's tombstone went, and its one copy, coming later,
    matched nothing. Every read and every write of a pass fails in turn here, with that tombstone beside the note."""
    check(cases(shape=PASS['shape'], finish=FINISHES, read=('gone', 'on record', 'lost'),
                fault=(None, ('read', 'all'), ('write', 'all'))+EVERY_READ+EVERY_WRITE,
                echo=(None, 'before', 'finish', 'after')), monkeypatch)


def test_a_pass_records_what_main_records_whatever_copies_it_kept(monkeypatch):
    """#260's round 7: a copy kept in memory while its note couldn't be saved was applied to the next read, so the pass
    recorded the send as sent where main recorded it unconfirmed. A controller that records a sent one unchecked then
    held that, and with the request read failing the note was forgotten with the reply up, where main, hearing the
    same copy again during its finish, took it back. The controller answers here from what it holds and is asked."""
    by_read, by_memory = check(cases(
        shape=('cleared', 'cleared, copy noted', 'tombstone', 'take-back owed'), **MODEL, read=('model', 'lost'),
        fault=(None, ('write', 1), ('write', 'all'), ('read', 1)), echo=(None, 'before', 'finish'), again=AGAIN),
        monkeypatch)
    assert by_read and by_memory


def test_a_tap_records_what_main_records_against_a_controller_answering_from_what_it_holds(monkeypatch):
    check(cases(capabilities=('older', 'authorize', 'authorize, recheck'), send=('ok', 'lost'), **{
        **MODEL, 'recorded': (None,)}, read=('model', 'lost'), fault=(None, ('write', 3), ('write', 4), ('read', 2)),
        echo=(None, 'send', 'after'), again=AGAIN), monkeypatch)


def test_a_pass_or_tap_never_leaves_up_what_main_takes_back_in_any_mix_against_a_controller(monkeypatch):
    check(sample(1500, 2607, shape=PASS['shape'], **MODEL, read=('model', 'lost', 'garbage'),
                 fault=FAULTS+EVERY_READ+EVERY_WRITE, echo=PASS['echo'], again=AGAIN, delete=DELETES), monkeypatch)
    check(sample(1000, 2608, capabilities=TAP['capabilities'], send=SENDS, **{**MODEL, 'recorded': (None,)},
                 read=('model', 'lost'), fault=TAP['fault']+EVERY_READ, echo=ECHOES, again=AGAIN, delete=DELETES),
          monkeypatch)
