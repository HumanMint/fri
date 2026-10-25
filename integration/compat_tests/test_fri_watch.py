import asyncio
import datetime
import itertools
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from compat_tests.test_api import headers
from compat_tests.test_fri_actions import harness
from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_runtime import configured, event
from compat_tests.test_gene_controls import control_body, controls_api  # noqa: F401
from fri.fri_agent import Agent
from fri.fri_errors import UNREACHABLE, ControllerRejected, ControllerUnavailable
from fri.fri_watch import CHECKS, NOT_OFFERED, NOT_TAKING, Watch, health_requested


def beat(controls, age=0, **changes):
    """Gene's runtime heartbeat as the controller last stored it, `age` seconds ago."""
    body = {'guild_id': '123', 'sha': 'c0ffee1'+'0'*33, 'started': time.time()-3600,
            'guild_connected': True, 'controls_running': True, **changes}
    with controls.store.connection() as db:
        db.execute('INSERT OR REPLACE INTO gene_runtime_health VALUES (1,?,?)', (time.time()-age, json.dumps(body)))


def stale_catalog(controls):
    with controls.store.connection() as db:
        db.execute('UPDATE gene_control_catalog SET at=? WHERE id=1', (time.time()-60,))


def routed(fri, client, paths=None):
    async def api(method, path, value=None):
        if paths is not None:
            paths.append(path)
        response = client.request(method, path, headers=headers('fri'), json=value)
        if response.status_code >= 400:
            raise ControllerRejected(response.json()['detail'])
        return response.json()
    fri.api = api


def owner_dm(fri):
    """The owner's DM channel. Like Discord's enforce_nonce, a repeated nonce returns the first message."""
    dm = SimpleNamespace(id=777, messages={}, nonces={}, sends=[], edits=[], hang=None, fail_send=None,
                         landed=False, fail_edit=None)
    async def send(content, *, nonce=None, **options):
        if dm.hang:
            await dm.hang.wait()
        if nonce is not None and nonce in dm.nonces:
            return dm.nonces[nonce]
        if dm.fail_send and not dm.landed:
            raise dm.fail_send
        sent = SimpleNamespace(id=556+len(dm.messages), channel=dm, content=content, author=SimpleNamespace(id=99),
                               created_at=datetime.datetime.now(datetime.timezone.utc))
        dm.messages[sent.id] = sent
        dm.sends.append(content)
        if nonce is not None:
            dm.nonces[nonce] = sent
        if dm.fail_send:  # Discord took it; Fri never heard back.
            raise dm.fail_send
        return sent
    async def history(limit=100, after=None, oldest_first=None):
        found = sorted(dm.messages.values(), key=lambda sent: sent.created_at,
                       reverse=not (after is not None if oldest_first is None else oldest_first))
        for sent in [sent for sent in found if after is None or sent.created_at > after][:limit]:
            yield sent
    def message(mid):
        async def edit(*, content, **options):
            if dm.fail_edit:
                raise dm.fail_edit
            dm.messages[mid].content = content
            dm.edits.append((mid, content))
        return SimpleNamespace(edit=edit)
    dm.send, dm.history, dm.get_partial_message = send, history, message
    owner = SimpleNamespace(id=10, send=send, dm_channel=None, create_dm=AsyncMock(return_value=dm))
    fri.get_user = lambda uid: owner if uid == 10 else None
    fri.get_channel = lambda cid: dm if cid == 777 else None
    fri._connection.user = SimpleNamespace(id=99)
    return dm


def http(status, code=0):
    import discord
    kind = {404: discord.NotFound, 403: discord.Forbidden}.get(status, discord.HTTPException)
    return kind(SimpleNamespace(status=status, reason='x'), {'code': code, 'message': 'x'})


def test_gene_outage_gets_one_owner_dm_edited_on_recovery_and_survives_restart(monkeypatch, tmp_path, controls_api):  # noqa: F811
    async def scenario():
        client, controls = controls_api
        fri = configured(monkeypatch)
        fri.data = tmp_path
        routed(fri, client)
        dm = owner_dm(fri)
        sends, edits = dm.sends, dm.edits
        try:
            beat(controls)
            await fri.watcher.check()
            assert not sends and fri.watcher.state['bad'] == 0
            beat(controls, age=420)  # Seven minutes without a heartbeat.
            for _ in range(CHECKS-1):
                await fri.watcher.check()
            assert not sends
            await fri.watcher.check()
            assert len(sends) == 1
            assert 'gene’s been dead since <t:' in sends[0] and 'last heartbeat 7 min ago' in sends[0]
            assert 'release c0ffee1' in sends[0] and 'i can’t restart it' in sends[0]
            await fri.watcher.check()
            assert len(sends) == 1 and not edits  # One DM per outage.
            # A restart mid-outage reads the private state file, so it neither repeats nor forgets the DM.
            fri.watcher = Watch(fri)
            await fri.watcher.check()
            assert len(sends) == 1
            beat(controls)
            await fri.watcher.check()
            assert len(sends) == 1 and len(edits) == 1
            assert edits[0][0] == 556 and edits[0][1].startswith(sends[0]) and '-# back at <t:' in edits[0][1]
            assert 'down about 7 min' in edits[0][1] or 'down about 8 min' in edits[0][1]
            await fri.watcher.check()
            assert len(edits) == 1 and not fri.watcher.state['alert']['open']
            assert json.loads((tmp_path/'fri-watch.json').read_text())['alert']['message_id'] == '556'
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_relapse_inside_half_an_hour_edits_the_last_dm_and_later_ones_send_a_new_one(monkeypatch, tmp_path, controls_api):  # noqa: F811
    async def scenario():
        client, controls = controls_api
        fri = configured(monkeypatch)
        fri.data = tmp_path
        routed(fri, client)
        dm = owner_dm(fri)
        sends, edits = dm.sends, dm.edits
        clock = [time.time()]
        monkeypatch.setattr('fri.fri_watch.time', SimpleNamespace(time=lambda: clock[0]))
        async def checks(count):
            for _ in range(count):
                clock[0] += 120
                await fri.watcher.check()
        try:
            beat(controls, age=300)
            await checks(CHECKS)
            beat(controls)
            await checks(1)
            assert len(sends) == 1 and len(edits) == 1
            beat(controls, age=300)
            await checks(CHECKS)
            assert len(sends) == 1 and len(edits) == 2 and '-# again.' in edits[1][1]
            assert fri.watcher.state['alert']['open']
            beat(controls)
            await checks(1)
            clock[0] += 1800
            beat(controls, age=300)
            await checks(CHECKS)
            assert len(sends) == 2 and fri.watcher.state['alert']['message_id'] == '557'
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('changes,expected', [
    ({'guild_connected': False}, 'cut off from discord'),
    ({'controls_running': False}, 'command runner has been dead'),
])
def test_a_live_but_broken_gene_is_an_outage_too(monkeypatch, tmp_path, controls_api, changes, expected):  # noqa: F811
    async def scenario():
        client, controls = controls_api
        fri = configured(monkeypatch)
        fri.data = tmp_path
        routed(fri, client)
        sends = owner_dm(fri).sends
        try:
            beat(controls, **changes)
            for _ in range(CHECKS):
                await fri.watcher.check()
            assert len(sends) == 1 and expected in sends[0]
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_busy_command_poll_is_not_an_outage(monkeypatch, tmp_path, controls_api):  # noqa: F811
    async def scenario():
        client, controls = controls_api
        fri = configured(monkeypatch)
        fri.data = tmp_path
        routed(fri, client)
        sends = owner_dm(fri).sends
        try:
            beat(controls)
            stale_catalog(controls)  # Gene is inside a long Red command: no poll, heartbeat still fresh.
            for _ in range(CHECKS+1):
                await fri.watcher.check()
            assert not sends and fri.watcher.state['bad'] == 0
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure,expected', [
    (ControllerUnavailable(UNREACHABLE), 'haven’t reached gene’s controller'),
    (TimeoutError(), 'haven’t reached gene’s controller'),
    (ControllerRejected('Unsupported Fri contract'), 'rejecting my health checks'),
])
def test_controller_trouble_is_its_own_outage(monkeypatch, tmp_path, failure, expected):
    async def scenario():
        fri = configured(monkeypatch)
        fri.data = tmp_path
        fri.api = AsyncMock(side_effect=failure)
        sends = owner_dm(fri).sends
        try:
            for _ in range(CHECKS):
                await fri.watcher.check()
            assert len(sends) == 1 and expected in sends[0]
            assert fri.api.await_args.args[:2] == ('POST', '/controls/health')
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_hung_controller_read_counts_as_down_and_odd_errors_never_stop_the_loop(monkeypatch, tmp_path):
    async def scenario():
        fri = configured(monkeypatch)
        fri.data = tmp_path
        async def hang(*args):
            await asyncio.sleep(60)
        fri.api = hang
        sends = owner_dm(fri).sends
        try:
            with patch('fri.fri_watch.READ', 0.01):
                for _ in range(CHECKS):
                    await fri.watcher.check()
            assert len(sends) == 1 and 'haven’t reached' in sends[0]
            fri.api = AsyncMock(return_value={'heartbeat_age_seconds': 'garbage'})
            await fri.watcher.check()  # Logged, not raised: tasks.loop would stop on it.
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_failed_owner_dm_is_retried_next_check(monkeypatch, tmp_path):
    async def scenario():
        fri = configured(monkeypatch)
        fri.data = tmp_path
        fri.api = AsyncMock(side_effect=ControllerUnavailable(UNREACHABLE))
        dm = owner_dm(fri)
        dm.fail_send = http(500)
        try:
            for _ in range(CHECKS):
                await fri.watcher.check()
            assert not dm.sends and not fri.watcher.alerted({'unreachable'})
            dm.fail_send = None
            await fri.watcher.check()
            assert len(dm.sends) == 1 and fri.watcher.alerted({'unreachable'})
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_stuck_discord_call_never_holds_up_the_next_read(monkeypatch, tmp_path):
    async def scenario():
        fri = configured(monkeypatch)
        fri.data = tmp_path
        fri.api = AsyncMock(side_effect=ControllerUnavailable(UNREACHABLE))
        dm = owner_dm(fri)
        dm.hang = asyncio.Event()  # Discord never answers the send.
        try:
            with patch('fri.fri_watch.DELIVERY', 0.05, create=True):
                for _ in range(CHECKS+1):
                    await asyncio.wait_for(fri.watcher.check(), 1)
            assert fri.api.await_count == CHECKS+1 and not dm.sends
            dm.hang = None
            await fri.watcher.check()
            assert len(dm.sends) == 1
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_failed_edits_are_retried_until_the_dm_matches_even_across_a_restart(monkeypatch, tmp_path, controls_api):  # noqa: F811
    async def scenario():
        client, controls = controls_api
        fri = configured(monkeypatch)
        fri.data = tmp_path
        routed(fri, client)
        dm = owner_dm(fri)
        try:
            beat(controls, age=300)
            for _ in range(CHECKS):
                await fri.watcher.check()
            assert len(dm.sends) == 1 and 'gene’s been dead' in dm.sends[0]
            dm.fail_edit = http(500)
            beat(controls, guild_connected=False)  # Still down, differently.
            await fri.watcher.check()
            assert not dm.edits
            dm.fail_edit = None
            await fri.watcher.check()
            assert len(dm.edits) == 1 and 'cut off from discord' in dm.edits[0][1]
            dm.fail_edit = http(500)
            beat(controls)
            await fri.watcher.check()  # Recovered, but the note didn't land.
            assert len(dm.edits) == 1
            fri.watcher = Watch(fri)
            dm.fail_edit = None
            await fri.watcher.check()
            assert len(dm.edits) == 2 and '-# back at <t:' in dm.edits[1][1]
            assert dm.messages[556].content == dm.edits[1][1]
            await fri.watcher.check()
            assert len(dm.edits) == 2 and len(dm.sends) == 1
        finally:
            await fri.close()
    asyncio.run(scenario())


class Crash(BaseException):
    """Fri's process dying mid-call: no handler of Fri's runs."""


@pytest.mark.parametrize('window', [True, False])
def test_a_dm_that_landed_before_a_crash_is_found_again_not_resent(monkeypatch, tmp_path, window):
    async def scenario():
        fri = configured(monkeypatch)
        fri.data = tmp_path
        fri.api = AsyncMock(side_effect=ControllerUnavailable(UNREACHABLE))
        dm = owner_dm(fri)
        try:
            for _ in range(CHECKS-1):
                await fri.watcher.check()
            dm.fail_send, dm.landed = Crash(), True
            with pytest.raises(Crash):
                await fri.watcher.check()
            assert len(dm.sends) == 1
            dm.fail_send = None
            if not window:  # Past Discord's nonce window, only Fri's own record can tell.
                dm.nonces.clear()
            fri.watcher = Watch(fri)
            await fri.watcher.check()
            assert len(dm.sends) == 1 and fri.watcher.alerted({'unreachable'})
            assert fri.watcher.state['alert']['message_id'] == '556'
            fri.api = AsyncMock(return_value={'heartbeat_age_seconds': 1, 'runtime': {
                'guild_connected': True, 'controls_running': True}})
            await fri.watcher.check()
            assert dm.edits and dm.edits[-1][0] == 556 and '-# back at' in dm.edits[-1][1]
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_landed_dm_is_found_however_many_dms_follow_it(monkeypatch, tmp_path):
    async def scenario():
        fri = configured(monkeypatch)
        fri.data = tmp_path
        fri.api = AsyncMock(side_effect=ControllerUnavailable(UNREACHABLE))
        dm = owner_dm(fri)
        try:
            for _ in range(CHECKS-1):
                await fri.watcher.check()
            dm.fail_send, dm.landed = Crash(), True
            with pytest.raises(Crash):
                await fri.watcher.check()
            dm.fail_send = None
            dm.nonces.clear()  # Restarted after Discord's nonce window.
            later = datetime.datetime.now(datetime.timezone.utc)
            for mid in range(1000, 1060):  # The owner kept talking while Fri was down.
                dm.messages[mid] = SimpleNamespace(id=mid, author=SimpleNamespace(id=10), content='hello?',
                                                   created_at=later+datetime.timedelta(seconds=mid-999))
            fri.watcher = Watch(fri)
            await fri.watcher.check()
            assert len(dm.sends) == 1 and fri.watcher.state['alert']['message_id'] == '556'
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_fri_waits_out_a_controller_down_at_startup_and_still_tells_the_owner(monkeypatch, tmp_path):
    async def scenario():
        fri = configured(monkeypatch)
        fri.data = tmp_path
        dm = owner_dm(fri)
        clock, reads = [time.time()], []
        monkeypatch.setattr('fri.fri_watch.time', SimpleNamespace(time=lambda: clock[0]))
        async def api(method, path, value=None):
            reads.append(path)
            if path == '/fri/contract':
                clock[0] += 10  # Each startup retry, CONTRACT_RETRY apart.
                if reads.count(path) > 40:
                    return {}  # Back, but incompatible: setup stops right after the wait.
            raise ControllerUnavailable(UNREACHABLE)
        fri.api = api
        try:
            with patch('fri.fri.CONTRACT_RETRY', 0), pytest.raises(RuntimeError, match='unsupported'):
                await fri.setup_hook()
            assert reads.count('/controls/health') == CHECKS+1  # One read per watch interval, not per retry.
            assert len(dm.sends) == 1 and 'haven’t reached gene’s controller' in dm.sends[0]
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_reads_stay_an_interval_apart_from_startup_into_the_gateway_loop(monkeypatch, tmp_path):
    from fri.fri_protocol import contract
    async def scenario():
        fri = configured(monkeypatch)
        fri.data = tmp_path
        dm = owner_dm(fri)
        start = time.time()
        clock, reads = [start], []
        monkeypatch.setattr('fri.fri_watch.time', SimpleNamespace(time=lambda: clock[0]))
        async def api(method, path, value=None):
            if path == '/fri/contract':
                clock[0] += 10
                if clock[0] < start+140:
                    raise ControllerUnavailable(UNREACHABLE)
                return contract()  # Back ten seconds after startup's second read.
            if path == '/agent/state':
                return {'config': {}, 'channels': []}
            reads.append(round(clock[0]-start))
            if clock[0] < start+150:
                raise ControllerUnavailable(UNREACHABLE)
            return GOOD  # Gene came back between the reads.
        fri.api = api
        fri.tree.sync = AsyncMock()
        for loop in (fri.health, fri.owner_reviews, fri.watch):
            monkeypatch.setattr(loop, 'start', lambda *args: None)
        try:
            with patch('fri.fri.CONTRACT_RETRY', 0), patch('fri.fri_agent.Agent.start', AsyncMock()):
                await fri.setup_hook()
                await fri.watch()  # The loop's first pass, right after startup's last read.
                assert reads == [10, 130] and fri.watcher.state['bad'] == 2
                clock[0] += 120
                await fri.watch()
            assert reads == [10, 130, 260] and fri.watcher.state['bad'] == 0 and not dm.sends
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_rejected_contract_still_stops_startup_at_once(monkeypatch, tmp_path):
    async def scenario():
        fri = configured(monkeypatch)
        fri.data = tmp_path
        fri.api = AsyncMock(side_effect=ControllerRejected('Unsupported Fri contract'))
        try:
            with pytest.raises(ControllerRejected):
                await fri.setup_hook()
            fri.api.assert_awaited_once()
        finally:
            await fri.close()
    asyncio.run(scenario())


GOOD = {'heartbeat_age_seconds': 1, 'runtime': {'guild_connected': True, 'controls_running': True}}


def test_a_landed_dm_is_found_even_behind_a_burst_just_before_it(monkeypatch, tmp_path):
    async def scenario():
        fri = configured(monkeypatch)
        fri.data = tmp_path
        fri.api = AsyncMock(side_effect=ControllerUnavailable(UNREACHABLE))
        dm = owner_dm(fri)
        try:
            for _ in range(CHECKS-1):
                await fri.watcher.check()
            earlier = datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(seconds=10)
            for mid in range(1000, 1151):  # More than a page, inside the minute before the attempt.
                dm.messages[mid] = SimpleNamespace(id=mid, author=SimpleNamespace(id=10), content='hello?',
                                                   created_at=earlier)
            dm.fail_send, dm.landed = Crash(), True
            with pytest.raises(Crash):
                await fri.watcher.check()
            dm.fail_send = None
            dm.nonces.clear()
            fri.watcher = Watch(fri)
            await fri.watcher.check()
            landed = next(sent.id for sent in dm.messages.values() if sent.author.id == 99)
            assert len(dm.sends) == 1 and fri.watcher.state['alert']['message_id'] == str(landed)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_relapse_keeps_an_unresolved_dm_instead_of_sending_another(monkeypatch, tmp_path):
    async def scenario():
        fri = configured(monkeypatch)
        fri.data = tmp_path
        fri.api = AsyncMock(side_effect=ControllerUnavailable(UNREACHABLE))
        dm = owner_dm(fri)
        try:
            for _ in range(CHECKS-1):
                await fri.watcher.check()
            dm.fail_send, dm.landed = Crash(), True
            with pytest.raises(Crash):
                await fri.watcher.check()
            dm.fail_send = None
            fri.watcher = Watch(fri)
            history = dm.history
            async def broken(**options):
                raise http(500)
                yield
            dm.history = broken  # Fri can't tell whether its DM landed.
            fri.api = AsyncMock(return_value=GOOD)
            await fri.watcher.check()
            assert not fri.watcher.state['alert']['open']
            fri.api = AsyncMock(side_effect=ControllerRejected('Unsupported Fri contract'))
            for _ in range(CHECKS-1):
                await fri.watcher.check()
            dm.history = history
            await fri.watcher.check()
            assert len(dm.sends) == 1 and 'rejecting my health checks' in dm.messages[556].content
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_the_relapse_window_starts_when_the_dm_actually_went_out(monkeypatch, tmp_path):
    async def scenario():
        fri = configured(monkeypatch)
        fri.data = tmp_path
        fri.api = AsyncMock(side_effect=ControllerUnavailable(UNREACHABLE))
        dm = owner_dm(fri)
        clock = [time.time()]
        monkeypatch.setattr('fri.fri_watch.time', SimpleNamespace(time=lambda: clock[0]))
        async def checks(count):
            for _ in range(count):
                clock[0] += 120
                await fri.watcher.check()
        dm.fail_send = http(500)
        try:
            await checks(CHECKS+16)  # Over half an hour of failed sends.
            dm.fail_send = None
            await checks(1)
            assert len(dm.sends) == 1
            fri.api = AsyncMock(return_value=GOOD)
            await checks(1)
            fri.api = AsyncMock(side_effect=ControllerUnavailable(UNREACHABLE))
            await checks(CHECKS)
            assert len(dm.sends) == 1 and '-# again.' in dm.messages[556].content
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_an_outage_over_before_its_dm_landed_sends_nothing_late(monkeypatch, tmp_path):
    async def scenario():
        fri = configured(monkeypatch)
        fri.data = tmp_path
        fri.api = AsyncMock(side_effect=ControllerUnavailable(UNREACHABLE))
        dm = owner_dm(fri)
        dm.fail_send = http(500)
        try:
            for _ in range(CHECKS):
                await fri.watcher.check()
            dm.fail_send = None
            fri.api = AsyncMock(return_value={'heartbeat_age_seconds': 1, 'runtime': {
                'guild_connected': True, 'controls_running': True}})
            await fri.watcher.check()
            await fri.watcher.check()
            assert not dm.sends and not dm.edits
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_deleted_dm_is_resent_while_the_outage_lasts_and_dropped_after(monkeypatch, tmp_path):
    async def scenario():
        fri = configured(monkeypatch)
        fri.data = tmp_path
        fri.api = AsyncMock(side_effect=ControllerUnavailable(UNREACHABLE))
        dm = owner_dm(fri)
        try:
            for _ in range(CHECKS):
                await fri.watcher.check()
            dm.fail_edit = http(404, 10008)
            fri.api = AsyncMock(side_effect=ControllerRejected('Unsupported Fri contract'))
            await fri.watcher.check()  # Kind changed, but the DM is gone: the owner still has to hear.
            await fri.watcher.check()
            assert len(dm.sends) == 2 and 'rejecting my health checks' in dm.sends[1]
            fri.api = AsyncMock(return_value={'heartbeat_age_seconds': 1, 'runtime': {
                'guild_connected': True, 'controls_running': True}})
            await fri.watcher.check()
            await fri.watcher.check()
            assert len(dm.sends) == 2 and not dm.edits
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_quick_control_with_gene_down_answers_at_once_without_charging_or_queueing(monkeypatch, tmp_path, controls_api):  # noqa: F811
    async def scenario():
        client, controls = controls_api
        fri, _, _, _ = harness(monkeypatch, tmp_path)
        paths = []
        routed(fri, client, paths)
        fri.chat.respond = AsyncMock(side_effect=AssertionError('quick controls never use a model'))
        stale_catalog(controls)
        actual = event(user=20, channel=1, content='fri stop the music')
        actual.id = int(control_body()['source']['id'])
        actual.webhook_id = None
        actual.channel.fetch_message = AsyncMock(return_value=actual)
        try:
            result = await fri.talk(123, 1, 20, actual.content, 'public', event=actual)
            assert result.reply == NOT_TAKING
            assert '/controls/catalog' in paths
            assert '/chat/operations/admit' not in paths and '/controls' not in paths
            fri.watcher.state = {'alert': {'open': True, 'kind': 'stale', 'text': 'x', 'message_id': '556'}}
            result = await fri.talk(123, 1, 20, actual.content, 'public', event=actual)
            assert result.reply == NOT_TAKING+' owner already knows.'
            client.post('/runtime/controls/poll', headers=headers('runtime'), json={'actions': ['queue']})
            result = await fri.talk(123, 1, 20, actual.content, 'public', event=actual)
            assert result.reply == NOT_OFFERED  # Gene is up; Red just isn't offering this one.
            with controls.store.connection() as db:
                assert db.execute('SELECT count(*) FROM gene_controls').fetchone()[0] == 0
            assert '/chat/operations/admit' not in paths and '/controls' not in paths
            fri.chat.respond.assert_not_awaited()
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_quick_control_still_runs_when_gene_offers_it(monkeypatch, tmp_path, controls_api):  # noqa: F811
    async def scenario():
        client, controls = controls_api
        fri, _, _, _ = harness(monkeypatch, tmp_path)
        paths = []
        async def api(method, path, value=None):
            paths.append(path)
            if path.startswith('/controls/') and path.endswith('/result'):
                claim = controls.poll(['stop'])
                controls.finish(claim['id'], claim['lease'], {'output': 'Stopped.'})
            response = client.request(method, path, headers=headers('fri'), json=value)
            if response.status_code >= 400:
                raise ControllerRejected(response.json()['detail'])
            return response.json()
        fri.api = api
        actual = event(user=20, channel=1, content='fri stop the music')
        actual.id = int(control_body()['source']['id'])
        actual.webhook_id = None
        actual.channel.fetch_message = AsyncMock(return_value=actual)
        try:
            with patch('fri.fri_actions.asyncio.sleep', new=AsyncMock()):
                result = await fri.talk(123, 1, 20, actual.content, 'public', event=actual)
            assert 'Stopped.' in result.reply
            assert paths.index('/controls/catalog') < paths.index('/chat/operations/admit') < paths.index('/controls')
        finally:
            await fri.close()
    asyncio.run(scenario())


IDS = itertools.count(1)


def message(content='fri you there?', channel=456, guild=123, user=11, **changes):
    """A fresh Discord message each time: the agent ignores a second delivery of the same ID."""
    value = SimpleNamespace(id=next(IDS), guild=SimpleNamespace(id=guild) if guild else None,
        channel=SimpleNamespace(id=channel), author=SimpleNamespace(id=user, bot=False, display_name='friend'),
        content=content, mentions=[], reference=None, webhook_id=None, reply=AsyncMock())
    for key, item in changes.items():
        setattr(value, key, item)
    value.channel.fetch_message = AsyncMock(return_value=value)  # Unchanged since Fri read it.
    return value


def down_agent():
    fri = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, user=SimpleNamespace(id=99),
                          api=AsyncMock(side_effect=ControllerUnavailable(UNREACHABLE)))
    agent = Agent(fri)
    agent.state['config']['enabled'] = True
    agent.profile = AsyncMock(side_effect=ControllerUnavailable(UNREACHABLE))
    agent.respond = AsyncMock()
    return agent


def test_controller_outage_gets_one_fixed_notice_per_channel_instead_of_silence():
    async def scenario():
        agent = down_agent()
        first = message()
        with pytest.raises(ControllerUnavailable):  # on_message still logs the failure.
            await agent.handle(first)
        first.reply.assert_awaited_once()
        assert first.reply.await_args.args == (UNREACHABLE,)
        assert first.reply.await_args.kwargs['mention_author'] is False
        again = message()
        with pytest.raises(ControllerUnavailable):
            await agent.handle(again)
        again.reply.assert_not_awaited()  # Once per channel per five minutes.
        agent.unreachable_notices[456] -= 301
        later = message()
        with pytest.raises(ControllerUnavailable):
            await agent.handle(later)
        later.reply.assert_awaited_once()
        agent.respond.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize('changes', [
    {'content': 'anyone up for ranked'},  # Not addressed to Fri.
    {'channel': 999},  # Not a configured channel.
])
def test_controller_outage_notice_needs_a_message_fri_would_answer(changes):
    async def scenario():
        agent = down_agent()
        quiet = message(**changes)
        if 'channel' in changes:  # Only an outside invitation's own reads could fail there.
            agent.receive = AsyncMock(side_effect=ControllerUnavailable(UNREACHABLE))
        with pytest.raises(ControllerUnavailable):
            await agent.handle(quiet)
        quiet.reply.assert_not_awaited()
        assert not agent.unreachable_notices
    asyncio.run(scenario())


def test_no_outage_notice_after_fri_already_answered():
    async def scenario():
        agent = down_agent()
        posted = message()
        async def preview_then_bind(event):  # The card lands; binding it on the controller fails.
            agent.answered([event])
            raise ControllerUnavailable(UNREACHABLE)
        agent.receive = preview_then_bind
        with pytest.raises(ControllerUnavailable):
            await agent.handle(posted)
        posted.reply.assert_not_awaited()
        assert not agent.unreachable_notices
    asyncio.run(scenario())


def test_reply_path_controller_failures_share_the_one_notice_and_never_show_its_detail():
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def typing():
        yield
    async def scenario():
        agent = down_agent()
        agent.fri.talk = AsyncMock(side_effect=ControllerUnavailable('sqlite3.OperationalError: database is locked'))
        agent.fri.waiting_notices = SimpleNamespace(clear=AsyncMock(), send=AsyncMock())
        agent.ambient_ready = lambda *args: True
        first, second, ambient = message(), message(), message(channel=457)
        for each in (first, second, ambient):
            each.channel.typing = typing
        await agent.reply_once(first, 'you there?')
        await agent.reply_once(second, 'hello??')
        await agent.reply_once(ambient, 'lol', ambient=True)  # Nobody asked; no notice either.
        assert first.reply.await_args.args == (UNREACHABLE,)
        second.reply.assert_not_awaited()
        ambient.reply.assert_not_awaited()
    asyncio.run(scenario())


def test_controller_5xx_detail_stays_in_the_log(monkeypatch):
    class Response:
        status = 500
        async def json(self):
            return {'detail': 'sqlite3.OperationalError: database is locked'}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            return False
    async def scenario():
        fri = configured(monkeypatch)
        fri.session = SimpleNamespace(request=lambda *a, **k: Response())
        with pytest.raises(ControllerUnavailable) as caught:
            await fri.api('GET', '/fri/contract')
        assert str(caught.value) == UNREACHABLE
        fri.session = None
        await fri.close()
    asyncio.run(scenario())


def test_an_undelivered_outage_notice_leaves_the_channel_free():
    async def scenario():
        agent = down_agent()
        first = message()
        first.reply.side_effect = http(500)
        with pytest.raises(ControllerUnavailable):
            await agent.handle(first)
        assert not agent.unreachable_notices
        second = message()
        with pytest.raises(ControllerUnavailable):
            await agent.handle(second)
        second.reply.assert_awaited_once()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', [TimeoutError(), ConnectionResetError(54, 'connection reset by peer')])
def test_a_notice_lost_in_transport_leaves_the_channel_free(failure):
    async def scenario():
        agent = down_agent()
        first = message()
        first.reply.side_effect = failure
        with pytest.raises(type(failure)):
            await agent.handle(first)
        second = message()
        with pytest.raises(ControllerUnavailable):
            await agent.handle(second)
        second.reply.assert_awaited_once()
    asyncio.run(scenario())


@pytest.mark.parametrize('changed', ['edited', 'nsfw', 'disabled'])
def test_an_early_notice_rechecks_the_mention_as_it_is_now(monkeypatch, tmp_path, changed):
    import copy
    async def scenario():
        fri, agent, _, _, channel, make_message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        event = make_message()
        async def fail_profile(*args):
            if changed == 'edited':
                edited = copy.copy(event)
                edited.content, edited.mentions = 'talking to somebody else now', []
                rows[event.id] = edited
            elif changed == 'nsfw':
                channel.nsfw = True
            else:
                agent.config['enabled'] = False
            raise ControllerUnavailable(UNREACHABLE)
        agent.profile = fail_profile
        try:
            with pytest.raises(ControllerUnavailable):
                await agent.handle(event)
            event.reply.assert_not_awaited()
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_an_early_notice_skips_a_name_mention_edited_away():
    async def scenario():
        agent = down_agent()
        edited = message()
        edited.channel.fetch_message = AsyncMock(return_value=SimpleNamespace(**{**vars(edited), 'content': 'nvm'}))
        with pytest.raises(ControllerUnavailable):
            await agent.handle(edited)
        edited.reply.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize('user,recording', [(10, False), (11, True)])
def test_an_early_dm_notice_skips_a_request_withdrawn_since(user, recording):
    async def scenario():
        agent = down_agent()
        agent.member = AsyncMock(return_value=True)
        agent.fri.api = AsyncMock(side_effect=ControllerUnavailable(UNREACHABLE))
        event = message('fri show current settings' if user == 10 else 'one more thing', guild=None, user=user)
        event.channel.fetch_message = AsyncMock(return_value=SimpleNamespace(**{**vars(event), 'content': 'never mind'}))
        if recording:  # The member's previous reply is still being saved.
            agent.recording[(event.channel.id, event.author.id)] = asyncio.Event()
        with pytest.raises(ControllerUnavailable):
            await agent.handle(event)
        event.channel.fetch_message.assert_awaited()
        event.reply.assert_not_awaited()
        assert not agent.unreachable_notices
    asyncio.run(scenario())


def test_an_early_notice_counts_as_the_mention_answered(monkeypatch, tmp_path, caplog):
    import logging
    async def scenario():
        fri, agent, _, _, _, make_message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        event = make_message()
        agent.profile = AsyncMock(side_effect=ControllerUnavailable(UNREACHABLE))
        try:
            with pytest.raises(ControllerUnavailable):
                await agent.handle(event)
            assert event.reply.await_count == 1 and event.id in agent.responded
        finally:
            await fri.close()
    with caplog.at_level(logging.INFO, logger='fri.agent'):
        asyncio.run(scenario())
    assert not [r for r in caplog.records if r.getMessage().startswith('Mention ')]


def test_an_admitted_mention_gets_the_notice_if_the_controller_dies_after_inference(monkeypatch, tmp_path):
    from fri.fri_chat import ChatResult
    async def scenario():
        fri, agent, _, _, _, make_message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        api, down = fri.api, []
        async def cut(method, path, value=None):
            if down:
                raise ControllerUnavailable(UNREACHABLE)
            return await api(method, path, value)
        async def inference(*args, **kwargs):
            down.append(True)
            return ChatResult(status='completed', reply='a safe synthetic response')
        fri.api, fri.chat_once = cut, inference
        event = make_message()
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            assert any(path == '/chat/mentions/admit' for path, _ in calls)
            assert event.reply.await_count == 1 and event.reply.await_args.args == (UNREACHABLE,)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_controller_dying_after_a_configured_mention_is_admitted_still_gets_the_notice(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, _, make_message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        api, failed = fri.api, []
        async def cut_after_admission(method, path, value=None):
            if failed or path == '/controls/recent':
                failed.append(path)
                raise ControllerUnavailable(UNREACHABLE)
            return await api(method, path, value)
        fri.api = cut_after_admission
        event = make_message()
        try:
            await agent.handle(event)
            assert failed and any(path == '/chat/mentions/admit' for path, _ in calls)
            assert event.reply.await_count == 1 and event.reply.await_args.args == (UNREACHABLE,)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_controller_trouble_while_answering_an_outside_mention_stays_quiet(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, _, make_message, _, _ = environment(monkeypatch, tmp_path, channel_id=8)
        api, failed = fri.api, []
        async def one_failure(method, path, value=None):
            if not failed and path == '/chat/context':
                failed.append(path)
                raise ControllerUnavailable(UNREACHABLE)
            return await api(method, path, value)
        fri.api = one_failure
        event = make_message()
        try:
            await agent.handle(event)
            assert failed
            event.reply.assert_not_awaited()
            assert not agent.unreachable_notices
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('text,ok', [
    ('health', True), ('fri health', True), ('fri, health check', True), ('show me your health', True),
    ('healthcheck', True), ('health report pls', True), ('Fri health?', True),
    ('how is your health going', False), ('health potions are op', False), ('fri status', False),
    ('mental health day', False),
])
def test_health_request_is_an_exact_phrase(text, ok):
    assert health_requested(text) is ok


def test_owner_health_report_works_in_dm_with_the_controller_down_and_records_nothing(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        fri.api = AsyncMock(side_effect=ControllerUnavailable(UNREACHABLE))
        fri.get_guild = lambda _: None
        (tmp_path/'usage').mkdir()
        (tmp_path/'usage'/'a.json').write_text('{}')
        agent.heartbeat_status, agent.heartbeat_finished = 'deferred', time.time()-95
        fri.watcher.state = {'bad': 2, 'kind': 'unreachable', 'checked': time.time()-30}
        agent.member = AsyncMock(return_value=SimpleNamespace(id=10))
        fri._connection.user = SimpleNamespace(id=99)
        dm = message('fri health', guild=None, user=10)
        try:
            await agent.handle(dm)
            report = dm.reply.await_args.args[0]
            assert report.startswith('vitals as of <t:')
            assert 'discord not connected' in report and 'heartbeat deferred (95s ago)' in report
            assert 'heartbeat stopped' in report and 'watch stopped' in report
            assert 'usage receipts waiting: 1' in report
            assert '- controller: unreachable' in report and '- gene: unreachable' in report
            assert '2 bad reads in a row (unreachable)' in report
            assert dm.reply.await_args.kwargs['suppress_embeds'] is True
            called = [call.args[1] for call in fri.api.await_args_list]
            assert sorted(called) == ['/controls/health', '/status']  # No turn, context or usage write.
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_health_report_stays_out_of_channels_and_other_members(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        fri._connection.user = SimpleNamespace(id=99)
        fri.api = AsyncMock(side_effect=AssertionError('no controller reads'))
        public = message('fri health', channel=1, user=10)
        assert await agent.owner_control(public, 'fri health')
        assert public.reply.await_args.args == ('dm me for that one.',)
        member = message('fri health', guild=None, user=11)
        assert not await agent.owner_control(member, 'fri health')
        lab = message('fri health', guild=777, user=10)
        assert not await agent.owner_control(lab, 'fri health')
        fri.api.assert_not_awaited()
        await fri.close()
    asyncio.run(scenario())


def test_controller_transport_errors_are_typed_as_unavailable(monkeypatch):
    import aiohttp
    async def scenario():
        fri = configured(monkeypatch)
        fri.session = SimpleNamespace(request=lambda *a, **k: (_ for _ in ()).throw(aiohttp.ClientConnectionError()))
        with pytest.raises(ControllerUnavailable, match='can’t reach my memory'):
            await fri.api('GET', '/fri/contract')
        fri.session = None
        await fri.close()
    asyncio.run(scenario())
