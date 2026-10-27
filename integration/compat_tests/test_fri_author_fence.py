"""fri #84: attention's last privacy recheck asks the real controller about every author at once, under its lock,
so an author who turns memory off through Gene while another author's read is still out isn't sent."""
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from gene.api import create_app
from gene.config import Settings

from compat_tests.test_api import headers
from compat_tests.test_fri_withdrawn_rows import model
from compat_tests.test_split_contract import Session
from fri.fri import Fri
from fri.fri_agent import Agent
from fri.fri_errors import UNREACHABLE, ControllerRejected, ControllerUnavailable

ALICE, BOB, CAROL = 11, 12, 13
MARKER = 'saffron-kayak-private'
QUESTION = 'anyone know why the build is broken?'
FENCE = '/agent/authors/check'


class World:
    """Attention in channel 456 (or a thread in it) against the real controller. Once attention has signed in,
    the last recheck's request that reads Bob fails as `outage` or `refusal` say, or is held during_recheck."""

    def __init__(self, tmp_path, capable=True, thread=False):
        settings = Settings(Path(tmp_path/'fence.db'), '123', '10', 'runtime-'+'x'*32, 'worker-'+'x'*32,
                            'deploy-'+'x'*32, fri_token='fri-'+'x'*32, workshop_channel_id='456',
                            agent_channels='456')
        self.client = TestClient(create_app(settings))
        offered = set(self.client.get('/fri/contract', headers=headers('fri')).json()['capabilities'])
        runtime = SimpleNamespace(url='http://controller', token='fri-'+'x'*32, session=Session(self.client))
        self.where = SimpleNamespace(id=70, parent_id=456) if thread else SimpleNamespace(id=456)
        self.fri = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, get_channel=lambda _: self.where,
                                   capabilities=frozenset(offered if capable else offered-{'author-fence'}))
        self.requests, self.calls, self.rechecks = [], [], []
        model(self.fri, self.requests, attention=True)
        signin = self.fri.chat.headers
        self.signed_in, self.hold, self.holding, self.release = False, False, asyncio.Event(), asyncio.Event()
        self.outage, self.refusal = False, False  # refusal: the status of a 4xx, None when it isn't Gene's.

        async def headers_():
            self.signed_in = True  # Every controller read from here on is the last recheck's.
            return await signin()

        async def api(method, path, payload=None):
            self.calls.append((path, payload))
            if self.signed_in:
                self.rechecks.append((path, payload))
                if path == FENCE or (path == '/agent/context' and payload['user_id'] == str(BOB)):
                    if self.outage:
                        raise ControllerUnavailable(UNREACHABLE)
                    if self.refusal is not False and path == FENCE:
                        raise ControllerRejected('Someone in that conversation changed their privacy settings.',
                                                 self.refusal)
                    if self.hold:
                        self.hold = False
                        self.holding.set()
                        await asyncio.wait_for(self.release.wait(), 5)
            return await Fri.api(runtime, method, path, payload)

        self.fri.api, self.fri.chat.headers = api, headers_
        self.agent = Agent(self.fri)
        self.agent.record = AsyncMock()
        self.say(100, ALICE, MARKER+' help?')
        self.say(101, BOB, QUESTION)

    def say(self, mid, user, text, **extra):
        self.agent.recent[self.where.id].append({'id': str(mid), 'user_id': str(user), 'name': 'member', 'text': text,
                                                 'at': time.time(), 'revision': 0, 'gene_error': False, **extra})

    def control(self, user, action):
        """Memory off (or on) through Gene, not Fri: Fri's live view still holds the member's rows."""
        response = self.client.post('/chat/memory/control', headers=headers('fri'), json={
            'guild_id': '123', 'user_id': str(user), 'channel_id': '456', 'conversation_id': 'public:456',
            'action': action})
        assert response.status_code == 200

    def sent(self):
        return [json.dumps(request) for request in self.requests]

    async def during_recheck(self, change):
        """Attention up to its held recheck read, `change`, then the rest of attention."""
        self.hold = True
        self.holding.clear()
        self.release.clear()
        task = asyncio.create_task(self.agent.attend())
        await asyncio.wait_for(self.holding.wait(), 2)
        change()
        self.release.set()
        await asyncio.wait_for(task, 2)


def test_an_author_who_turns_memory_off_while_another_authors_read_is_held_is_not_sent(tmp_path):
    """The repro from fri #84: Alice turns memory off through Gene while the recheck reads Bob."""
    async def scenario():
        w = World(tmp_path)
        await w.during_recheck(lambda: w.control(ALICE, 'disable'))
        assert not any(MARKER in request for request in w.sent())
        assert not w.requests  # The whole batch waits: the refusal names nobody.
        w.agent.last_attention[456] = 0  # The next heartbeat window offers it again, without her words.
        await w.agent.attend()
        assert len(w.requests) == 1 and MARKER not in w.sent()[0] and QUESTION in w.sent()[0]
    asyncio.run(scenario())


@pytest.mark.parametrize('thread', [False, True])
def test_the_recheck_is_one_call_for_every_author_at_the_revision_read(tmp_path, thread):
    async def scenario():
        w = World(tmp_path, thread=thread)
        w.say(102, 99, 'fri said a thing', fri=True, provenance=[{'user_id': str(CAROL), 'revision': 0},
                                                                    {'user_id': str(ALICE), 'revision': 0}])
        await w.agent.attend()
        # Asked in the parent channel, about each author once, in order.
        assert w.rechecks == [(FENCE, {'guild_id': '123', 'channel_id': '456', 'authors': [
            {'user_id': str(ALICE), 'revision': 0}, {'user_id': str(BOB), 'revision': 0},
            {'user_id': str(CAROL), 'revision': 0}]})]
        assert len(w.requests) == 1 and MARKER in w.sent()[0] and QUESTION in w.sent()[0]
    asyncio.run(scenario())


def test_an_older_controller_keeps_the_per_author_reads(tmp_path):
    async def scenario():
        w = World(tmp_path, capable=False)
        await w.agent.attend()
        assert sorted(payload['user_id'] for path, payload in w.rechecks if path == '/agent/context') == [
            str(ALICE), str(BOB)]
        assert all(path == '/agent/context' for path, _ in w.rechecks) and FENCE not in [p for p, _ in w.calls]
        assert len(w.requests) == 1 and MARKER in w.sent()[0]
        # Memory off before the recheck still keeps her words out, as it always has.
        w.agent.recent[456].clear()
        w.say(103, ALICE, MARKER+' still broken?')
        w.say(104, BOB, QUESTION)
        w.agent.last_attention[456] = 0

        async def off():
            w.control(ALICE, 'disable')
            return {'Authorization': 'Bearer synthetic-review'}
        w.fri.chat.headers = AsyncMock(side_effect=off)
        await w.agent.attend()
        assert len(w.requests) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('status', [409, 403, 422, None])
def test_any_refusal_of_the_recheck_sends_nothing(tmp_path, status):
    """Stale (409), outside Fri's scope (403), malformed (422), or a refusal that isn't the controller's."""
    async def scenario():
        w = World(tmp_path)
        w.refusal = status
        await w.agent.attend()  # Quiet, not an error: the batch is offered again on the next window.
        assert not w.requests and 456 not in w.agent.seen
        assert [path for path, _ in w.rechecks] == [FENCE]
    asyncio.run(scenario())


@pytest.mark.parametrize('capable', [True, False])
def test_an_outage_during_the_recheck_sends_nothing_either_way(tmp_path, capable):
    async def scenario():
        w = World(tmp_path, capable=capable)
        w.outage = True
        with pytest.raises(ControllerUnavailable):
            await w.agent.attend()
        assert not w.requests and 456 not in w.agent.seen
    asyncio.run(scenario())


def test_a_row_taken_back_while_the_fence_is_out_is_not_sent(tmp_path):
    async def scenario():
        w = World(tmp_path)
        await w.during_recheck(lambda: w.agent.unsend(456, [100]))
        assert [path for path, _ in w.rechecks] == [FENCE] and not w.requests
    asyncio.run(scenario())


@pytest.mark.parametrize('count', [200, 201])
def test_more_authors_than_the_fence_takes_stay_quiet_without_reading_them_one_by_one(tmp_path, count):
    async def scenario():
        w = World(tmp_path)
        authors = [{'user_id': str(1000+i), 'revision': 0} for i in range(count-1)]
        w.say(102, 99, 'fri said a thing', fri=True, provenance=authors)
        records = [row for row in w.agent.recent[456] if row['id'] != '100']  # Bob and Fri's reply.
        w.signed_in = True
        assert await w.agent.unwithdrawn(456, records) == (count <= 200)
        assert [path for path, _ in w.calls] == ([FENCE] if count <= 200 else [])
    asyncio.run(scenario())


def test_only_gene_errors_need_no_fence(tmp_path):
    async def scenario():
        w = World(tmp_path)
        w.outage = True
        w.say(105, 7, 'traceback', gene_error=True)
        records = [row for row in w.agent.recent[456] if row['gene_error']]
        w.signed_in = True
        assert await w.agent.unwithdrawn(456, records) and not w.calls
    asyncio.run(scenario())
