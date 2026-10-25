import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from compat_tests.test_api import headers
from compat_tests.test_fri_actions import harness
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
    edits, sends = [], []
    channel = SimpleNamespace(id=777)
    async def send(content, **options):
        sends.append(content)
        return SimpleNamespace(id=555+len(sends), channel=channel)
    def message(mid):
        async def edit(**options):
            edits.append((mid, options['content']))
        return SimpleNamespace(edit=edit)
    channel.get_partial_message = message
    owner = SimpleNamespace(id=10, send=send, create_dm=AsyncMock(return_value=channel))
    fri.get_user = lambda uid: owner if uid == 10 else None
    fri.get_channel = lambda cid: channel if cid == 777 else None
    return sends, edits


def test_gene_outage_gets_one_owner_dm_edited_on_recovery_and_survives_restart(monkeypatch, tmp_path, controls_api):  # noqa: F811
    async def scenario():
        client, controls = controls_api
        fri = configured(monkeypatch)
        fri.data = tmp_path
        routed(fri, client)
        sends, edits = owner_dm(fri)
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
        sends, edits = owner_dm(fri)
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
        sends, _ = owner_dm(fri)
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
        sends, _ = owner_dm(fri)
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
        sends, _ = owner_dm(fri)
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
        sends, _ = owner_dm(fri)
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
    import discord
    async def scenario():
        fri = configured(monkeypatch)
        fri.data = tmp_path
        fri.api = AsyncMock(side_effect=ControllerUnavailable(UNREACHABLE))
        sends, _ = owner_dm(fri)
        owner = fri.get_user(10)
        working = owner.send
        owner.send = AsyncMock(side_effect=discord.HTTPException(SimpleNamespace(status=500, reason='x'), 'down'))
        try:
            for _ in range(CHECKS):
                await fri.watcher.check()
            assert owner.send.await_count == 1 and not fri.watcher.state['alert']
            owner.send = working
            await fri.watcher.check()
            assert len(sends) == 1 and fri.watcher.state['alert']['open']
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
            fri.watcher.state = {'alert': {'open': True, 'kind': 'stale'}}
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


def message(content='fri you there?', channel=456, guild=123, user=11, **changes):
    value = SimpleNamespace(id=1, guild=SimpleNamespace(id=guild) if guild else None,
        channel=SimpleNamespace(id=channel), author=SimpleNamespace(id=user, bot=False, display_name='friend'),
        content=content, mentions=[], reference=None, webhook_id=None, reply=AsyncMock())
    for key, item in changes.items():
        setattr(value, key, item)
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
        with pytest.raises(ControllerUnavailable):
            await agent.handle(again)
        again.reply.assert_awaited_once()
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
