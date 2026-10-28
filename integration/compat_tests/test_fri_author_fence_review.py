"""The author fence fails closed on anything but Gene's explicit yes, and the fixed line waits out an unknown runtime (sol-loop round 1 of fri #131)."""
import asyncio
from types import SimpleNamespace

import pytest

from compat_tests.test_fri_author_fence import ALICE, FENCE, MARKER, World
from compat_tests.test_fri_fixes import OWNER, reported, unknown, world  # noqa: F401
from fri.fri import Fri
from fri.fri_errors import ControllerUnavailable
from fri.fri_fixes import tell


class Reply:
    def __init__(self, value, status=200):
        self.value, self.status = value, status

    async def json(self):
        return self.value

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


@pytest.mark.parametrize('value', [None, {}, [], False, {'ok': False}, {'ok': 'true'}, {'ok': 1}])
def test_malformed_author_success_never_sends_erased_words(tmp_path, value):
    async def scenario():
        w = World(tmp_path)
        real = w.fri.api
        signin = w.fri.chat.headers

        async def headers():
            result = await signin()
            # Initial context was permitted, then the author opts out through Gene.
            # The final read must require positive evidence from the controller.
            w.control(ALICE, 'disable')
            return result

        async def api(method, path, payload=None):
            if path == FENCE:
                runtime = SimpleNamespace(url='http://controller', token='synthetic-review',
                    session=SimpleNamespace(request=lambda *a, **k: Reply(value)))
                return await Fri.api(runtime, method, path, payload)
            return await real(method, path, payload)

        w.fri.api, w.fri.chat.headers = api, headers
        await w.agent.attend()
        assert not any(MARKER in item for item in w.sent()), 'erased words reached model without an affirmative fence'
        assert not w.requests
    asyncio.run(scenario())


@pytest.mark.parametrize('status', [500, 503])
def test_server_failure_still_aborts_author_check(tmp_path, status):
    async def scenario():
        w = World(tmp_path)
        real = w.fri.api

        async def api(method, path, payload=None):
            if path == FENCE:
                runtime = SimpleNamespace(url='http://controller', token='synthetic-review',
                    session=SimpleNamespace(request=lambda *a, **k: Reply({}, status)))
                return await Fri.api(runtime, method, path, payload)
            return await real(method, path, payload)

        w.fri.api = api
        with pytest.raises(ControllerUnavailable):
            await w.agent.attend()
        assert not w.requests
    asyncio.run(scenario())


def test_erasing_a_provenance_only_author_during_the_fence_blocks_the_whole_batch(tmp_path):
    async def scenario():
        w = World(tmp_path)
        w.agent.recent[456].clear()
        w.say(102, 99, MARKER, fri=True, provenance=[{'user_id': str(ALICE), 'revision': 0}])
        w.say(103, 12, 'why is the build broken?')
        await w.during_recheck(lambda: w.control(ALICE, 'disable'))
        assert not w.requests
    asyncio.run(scenario())


def test_runtime_unknown_during_second_live_read_does_not_tell_reporter(world):  # noqa: F811
    _, job_id, _ = reported(world)
    real = world.fri.api.side_effect
    reads = 0

    async def api(method, path, body=None):
        nonlocal reads
        if path == '/status':
            reads += 1
            if reads == 2:
                unknown(world.store)
        return await real(method, path, body)

    world.fri.api.side_effect = api
    text, sent = asyncio.run(tell(world.fri, job_id, OWNER))
    assert world.store.status()['runtime_unknown']
    assert not sent and not world.sent, (text, world.sent)
