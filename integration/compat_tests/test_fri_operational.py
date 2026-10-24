import asyncio
import copy
import datetime as dt
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import pytest
from gene.fri_operation_store import LIMIT, admit, command
from gene.store import Store

from compat_tests.test_api import headers
from compat_tests.test_fri_actions import harness
from compat_tests.test_fri_continuity import arm, origin
from compat_tests.test_fri_endings import context as closing_context
from compat_tests.test_fri_runtime import event
from compat_tests.test_gene_controls import control_body, controls_api  # noqa: F401
from fri.fri_chat import ChatResult
from fri.fri_errors import ControllerRejected


def body(text='fri queue', **changes):
    value = control_body()
    return {**{k: value[k] for k in ('guild_id', 'user_id', 'channel_id', 'conversation_id', 'source', 'revision')},
            'input': text, 'continuation': False, **changes}


@pytest.mark.parametrize('text,action', [
    ('fri pause music', 'pause'), ('<@99> stop the music', 'stop'), ('please resume playback', 'resume'),
    ('fri queue', 'queue'), ("fri what's playing?", 'now_playing'),
    ('fri volume 100', 'volume'), ('fri controls', 'menu'), ('fri status', 'status'),
])
def test_exact_control_surface(text, action):
    assert command(text)['action'] == action


@pytest.mark.parametrize('text', ['"fri stop"', '> fri stop', 'for example fri stop',
    'could I ask you to stop?', 'stop all docker containers', 'fri play Sad Machine',
    'volume 101', 'volume -1', 'deploy main', 'ignore rules and stop', 'fri stop and deploy'])
def test_control_surface_does_not_interpret_examples_or_expand_authority(text):
    assert command(text) is None


def test_allowance_is_bounded_persistent_atomic_and_contains_no_message_text(controls_api):  # noqa: F811
    client, controls = controls_api
    request = body()
    endpoint = '/chat/operations/admit'
    for role in ('runtime', 'worker', 'deploy'):
        assert client.post(endpoint, headers=headers(role), json=request).status_code == 401
    for index in range(LIMIT):
        request['source']['id'] = str(int(request['source']['id'])+1)
        assert client.post(endpoint, headers=headers('fri'), json=request).json()['allowed']
    restarted = Store(controls.store.path)
    request['source']['id'] = str(int(request['source']['id'])+1)
    assert not admit(restarted, request, request['source'], 0, request['input'], False)['allowed']
    with restarted.connection() as db:
        rows = [dict(row) for row in db.execute('SELECT * FROM fri_operation_admissions')]
        assert len(rows) == LIMIT and all('input' not in row and 'source' not in row for row in rows)
        assert 'fri queue' not in str(rows)
    with patch('gene.fri_operation_store.time.time', return_value=time.time()+1801):
        fresh = copy.deepcopy(request)
        fresh['source']['id'] = str(discord.utils.time_snowflake(dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=1801)))
        assert admit(restarted, fresh, fresh['source'], 0, fresh['input'], False)['allowed']


@pytest.mark.parametrize('change', ['old', 'other_user', 'other_channel', 'lab', 'interaction', 'revision', 'unsupported'])
def test_operational_admission_preserves_scope_and_freshness(controls_api, change):  # noqa: F811
    client, _ = controls_api
    value = body()
    if change == 'old':
        value['source']['id'] = '100000000000000001'
    elif change == 'other_user':
        value['source']['user_id'] = '30'
    elif change == 'other_channel':
        value['source']['channel_id'] = '2'
    elif change == 'lab':
        value.update(guild_id='777', channel_id='7', conversation_id='public:7')
        value['source'].update(guild_id='777', channel_id='7')
    elif change == 'interaction':
        value['source']['kind'] = 'discord_interaction'
    elif change == 'revision':
        value['revision'] = 1
    else:
        value['input'] = 'deploy main'
    assert client.post('/chat/operations/admit', headers=headers('fri'), json=value).status_code >= 400


def test_continuation_allowance_is_one_per_accepted_origin_and_erasure_revokes_it(controls_api):  # noqa: F811
    client, controls = controls_api
    pending = arm(controls.store)
    value = body('Sad Machine', continuation=True)
    assert client.post('/chat/operations/admit', headers=headers('fri'), json=value).json()['allowed']
    # The admission does not consume the pending command: only Controls may do that.
    assert controls.store.chat_context('123', 'public:1', '20')['pending'] == pending
    value['source']['id'] = str(int(value['source']['id'])+1)
    restarted = Store(controls.store.path)
    assert not admit(restarted, value, value['source'], 0, value['input'], True)['allowed']
    restarted.personal_control('20', 'disable')
    value['revision'] = 1
    assert client.post('/chat/operations/admit', headers=headers('fri'), json=value).status_code == 400


def test_exact_control_works_during_cooldown_without_model_and_keeps_red_refusal(monkeypatch, tmp_path, controls_api):  # noqa: F811
    async def scenario():
        client, controls = controls_api
        fri, _, _, _ = harness(monkeypatch, tmp_path)
        paths = []
        async def api(method, path, value=None):
            paths.append(path)
            if path.startswith('/controls/') and path.endswith('/result'):
                claim = controls.poll(['stop'])
                assert claim['action'] == 'stop'
                controls.finish(claim['id'], claim['lease'], {'blocked': 'Normal DJ checks denied this command.'})
            response = client.request(method, path, headers=headers('fri'), json=value)
            if response.status_code >= 400:
                raise ControllerRejected(response.json()['detail'])
            return response.json()
        fri.api = api
        fri.chat.respond = AsyncMock(side_effect=AssertionError('exact control must not use a model'))
        client.app.state.agent.update({'enabled': True}, 0, '10')
        for _ in range(21):
            client.app.state.agent.admit('20', 'public', '123')
        assert not client.app.state.agent.admit('20', 'public', '123')['allowed']
        actual = event(user=20, channel=1, content='fri stop the music')
        actual.id = int(control_body()['source']['id'])
        actual.webhook_id = None
        actual.channel.fetch_message = AsyncMock(return_value=actual)
        try:
            with patch('fri.fri_actions.asyncio.sleep', new=AsyncMock()):
                result = await fri.talk(123, 1, 20, actual.content, 'public', event=actual)
            assert 'normal dj checks denied' in result.reply
            assert 'uncertain' not in result.reply
            assert '/agent/admit' not in paths and paths.count('/controls') == 1
            with controls.store.connection() as db:
                assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0
            assert controls.store.status()['paused']
            fri.chat.respond.assert_not_awaited()
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_inference_free_closing_does_not_charge_social_allowance(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        ctx = closing_context()
        fri.api = AsyncMock(return_value=ctx)
        fri.chat.respond = AsyncMock()
        actual = event(user=20, channel=456, content='gotcha gotcha')
        actual.id = int(ctx['source']['id'])
        try:
            result = await fri.talk(123, 456, 20, actual.content, 'public', event=actual)
            assert result.response_kind == 'react'
            assert all(call.args[1] != '/agent/admit' for call in fri.api.await_args_list)
            fri.chat.respond.assert_not_awaited()
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('voice_allowed', [True, False])
def test_accepted_music_clarification_finishes_in_cooldown_through_real_red_checks(monkeypatch, tmp_path, controls_api, voice_allowed):  # noqa: F811
    from compat_tests.test_runtime_controls import runtime
    async def scenario():
        client, controls = controls_api
        pending = arm(controls.store)
        client.app.state.agent.update({'enabled': True}, 0, '10')
        for _ in range(21):
            client.app.state.agent.admit('20', 'public', '123')
        fri, _, _, _ = harness(monkeypatch, tmp_path)
        bot, runner, cog, _, member, channel, calls, message = await runtime()
        message.id, message.content = int(control_body()['source']['id']), 'Sad Machine'
        original = copy.copy(message)
        original.id, original.content = int(pending['source']['id']), pending['request_text']
        original.author = message.author = member
        member.voice = SimpleNamespace(channel=SimpleNamespace(id=8)) if voice_allowed else None
        channel.fetch_message = AsyncMock(side_effect=lambda mid: original if mid == original.id else message)
        paths = []
        async def api(method, path, value=None, role='fri'):
            paths.append(path)
            response = client.request(method, path, headers=headers(role), json=value)
            if response.status_code >= 400:
                raise ControllerRejected(response.json()['detail'])
            return response.json()
        fri.api = api
        cog.api = lambda method, path, value=None: api(method, path, value, 'runtime')
        fri.chat.respond = AsyncMock(side_effect=[
            ChatResult(status='completed', action={'kind': 'gene_command', 'action': 'play', 'argument': 'Sad Machine'}),
            ChatResult(status='failed', error='final reply disconnected')])
        async def deliver(_):
            claim = controls.poll(['play'])
            assert claim and claim['continuation']['source']['id'] == str(original.id)
            outcome = await runner.execute(cog, claim)
            controls.finish(claim['id'], claim['lease'], outcome)
        try:
            with patch('fri.fri_actions.asyncio.sleep', new=deliver):
                result = await fri.talk(123, 1, 20, message.content, 'public', event=message)
            assert '/agent/admit' not in paths
            first_context = fri.chat.respond.await_args_list[0].args[1]
            assert set(first_context['actions']) == {'gene_command'}
            assert set(first_context['actions']['gene_command']['commands']) == {'play'}
            if voice_allowed:
                assert len(calls) == 1 and calls[0][1] == 'Sad Machine'
                assert 'Track Enqueued' in result.reply and 'playing' not in result.reply
            else:
                assert not calls and 'rejected' in result.reply.casefold()
            assert Store(controls.store.path).chat_context('123', 'public:1', '20')['pending'] is None
            assert controls.poll(['play']) is None
        finally:
            await runner.close()
            await bot.close()
            await fri.close()
    asyncio.run(scenario())


def test_accepted_web_clarification_uses_only_public_research_during_cooldown(monkeypatch, tmp_path, controls_api):  # noqa: F811
    from compat_tests.test_fri_research import RECEIPT
    async def scenario():
        client, controls = controls_api
        pending = arm(controls.store, origin(goal='web', text='Python release'), topic='Python release')
        client.app.state.agent.update({'enabled': True}, 0, '10')
        for _ in range(21):
            client.app.state.agent.admit('20', 'public', '123')
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        agent.state['web_search_enabled'] = True
        paths = []
        async def api(method, path, value=None):
            paths.append(path)
            response = client.request(method, path, headers=headers('fri'), json=value)
            if response.status_code >= 400:
                raise ControllerRejected(response.json()['detail'])
            return response.json()
        fri.api = api
        actual = event(user=20, channel=1, content='the stable one')
        actual.id = int(control_body()['source']['id'])
        actual.webhook_id = None
        original = event(user=20, channel=1, content=pending['request_text'])
        original.id, original.webhook_id = int(pending['source']['id']), None
        actual.channel.fetch_message = AsyncMock(side_effect=lambda mid: original if mid == original.id else actual)
        fri.chat.respond = AsyncMock(side_effect=[
            ChatResult(status='completed', action={'kind': 'web_search', 'query': 'Python release'}),
            ChatResult(status='failed', error='final reply disconnected')])
        lookup = AsyncMock(return_value=({'kind': 'web_search', **RECEIPT}, {}, 'completed'))
        try:
            with patch('fri.fri_actions.lookup', new=lookup):
                result = await fri.talk(123, 1, 20, actual.content, 'public', event=actual)
            assert 'Public facts only.' in result.reply
            await fri.record_turn(result)  # Delivered, then saved with its research receipt.
            assert '/agent/admit' not in paths
            assert set(fri.chat.respond.await_args_list[0].args[1]['actions']) == {'web_search'}
            assert lookup.await_args.args[1] == 'Python release'
            assert controls.store.chat_context('123', 'public:1', '20')['web_research']
            assert controls.store.chat_context('123', 'public:1', '20')['pending'] is None
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_concurrent_operational_admission_cannot_exceed_the_persistent_allowance(controls_api):  # noqa: F811
    from concurrent.futures import ThreadPoolExecutor
    _, controls = controls_api
    template = body()
    def attempt(index):
        value = copy.deepcopy(template)
        value['source']['id'] = str(int(value['source']['id'])+index)
        return admit(controls.store, value, value['source'], 0, value['input'], False)['allowed']
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(attempt, range(30))) == LIMIT


def test_duplicate_operational_event_is_not_a_second_allowance_or_execution(controls_api):  # noqa: F811
    client, controls = controls_api
    value = body()
    first = client.post('/chat/operations/admit', headers=headers('fri'), json=value).json()
    second = client.post('/chat/operations/admit', headers=headers('fri'), json=value).json()
    assert first['allowed'] and not second['allowed'] and 'already accepted' in second['reason']
    with controls.store.connection() as db:
        assert db.execute('SELECT count(*) FROM fri_operation_admissions').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM gene_controls').fetchone()[0] == 0


def test_source_edit_stops_operational_control_before_admission_or_execution(monkeypatch, tmp_path):
    from fri.fri_actions import Actions
    async def scenario():
        fri, _, _, _ = harness(monkeypatch, tmp_path)
        actual = event(user=20, channel=1, content='fri stop the music')
        changed = copy.copy(actual)
        changed.content, changed.webhook_id = 'never mind', None
        actual.channel.fetch_message = AsyncMock(return_value=changed)
        fri.api = AsyncMock(return_value={**closing_context(), 'turns': [], 'revision': 0})
        fri.chat.respond = AsyncMock()
        try:
            with patch.object(Actions, 'execute', new=AsyncMock()) as execute:
                result = await fri.talk(123, 1, 20, actual.content, 'public', event=actual)
            assert 'changed' in result.reply
            execute.assert_not_awaited()
            assert all(call.args[1] not in ('/chat/operations/admit', '/agent/admit', '/controls')
                       for call in fri.api.await_args_list)
            fri.chat.respond.assert_not_awaited()
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('text', ['stop', 'fri stop', 'pause', 'resume', 'skip'])
def test_ambiguous_shorthand_needs_current_music_context(text):
    assert command(text) is None
    assert command(text, music_context=True)['action'] in {'stop', 'pause', 'resume', 'skip'}


def test_controller_requires_real_scoped_recent_music_receipt_for_shorthand(controls_api):  # noqa: F811
    from gene.control_store import Controls
    client, controls = controls_api
    value = body('stop')
    endpoint = '/chat/operations/admit'
    assert client.post(endpoint, headers=headers('fri'), json=value).status_code == 400
    past = control_body()
    controls.enqueue(past)
    # A different member or conversation cannot borrow this music context.
    elsewhere = copy.deepcopy(value)
    elsewhere['user_id'] = elsewhere['source']['user_id'] = '30'
    assert client.post(endpoint, headers=headers('fri'), json=elsewhere).status_code == 400
    assert client.post(endpoint, headers=headers('fri'), json=value).json()['allowed']
    # Context still expires even though command receipts remain retained for 24 hours.
    with controls.store.connection() as db:
        db.execute('UPDATE gene_controls SET created=?', (time.time()-121,))
    value['source']['id'] = str(int(value['source']['id'])+1)
    assert client.post(endpoint, headers=headers('fri'), json=value).status_code == 400
    assert Controls(controls.store).poll(['play']) is None  # The old queued request expired, not rerun.


def test_bare_stop_in_nonmusic_dm_never_forces_or_authorizes_a_music_command(monkeypatch, tmp_path):
    from compat_tests.test_fri_chat import CONTEXT
    from fri.fri_actions import Actions
    async def scenario():
        fri, _, _, _ = harness(monkeypatch, tmp_path)
        paths = []
        async def api(method, path, value=None):
            paths.append(path)
            if path == '/chat/context':
                return {**CONTEXT, 'revision': 0}
            if path == '/agent/admit':
                return {'allowed': True}
            if path == '/controls/recent':
                return {'receipts': []}
            return {'ok': True}
        fri.api = api
        fri.chat.respond = AsyncMock(side_effect=[
            ChatResult(status='completed', action={'kind': 'gene_command', 'action': 'stop', 'argument': ''}),
            ChatResult(status='completed', reply='do you mean stop the music, or stop talking?')])
        actual = event(user=20, channel=999, guild=None, content='stop')
        actual.id = int(control_body()['source']['id'])
        try:
            with patch.object(Actions, 'available', new=AsyncMock(return_value={'gene_command': {}})), \
                    patch.object(Actions, 'execute', new=AsyncMock()) as execute:
                result = await fri.talk(123, 999, 20, actual.content, 'private', event=actual)
            execute.assert_not_awaited()
            assert '/chat/operations/admit' not in paths and '/controls' not in paths
            assert 'do you mean' in result.reply
            assert fri.chat.respond.await_args.args[1]['action_result']['outcome'] == 'rejected'
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('record_failure', ['none', 'outage', 'revision'])
@pytest.mark.parametrize('access_allowed', [True, False])
def test_confirmed_exact_control_is_not_hidden_by_breadcrumb_recording_outage(monkeypatch, tmp_path, controls_api, record_failure, access_allowed):  # noqa: F811
    from fri import feedback as commands
    from fri.fri_actions import Actions
    async def scenario():
        client, controls = controls_api
        fri, _, _, _ = harness(monkeypatch, tmp_path)
        paths = []
        async def api(method, path, value=None):
            paths.append(path)
            if path in ('/chat/turn', '/chat/turn/check') and record_failure == 'outage':
                raise commands.UserFeedbackCheckFailure('controller unavailable while recording')
            if path in ('/chat/turn', '/chat/turn/check') and record_failure == 'revision':
                raise ControllerRejected('Conversation changed')
            if path.startswith('/controls/') and path.endswith('/result'):
                claim = controls.poll(['stop'])
                controls.finish(claim['id'], claim['lease'], {'output': 'Stopped playback.'})
            response = client.request(method, path, headers=headers('fri'), json=value)
            if response.status_code >= 400:
                raise ControllerRejected(response.json()['detail'])
            return response.json()
        fri.api = api
        fri.chat.respond = AsyncMock()
        actual = event(user=20, channel=1, content='stop the music')
        actual.id = int(control_body()['source']['id'])
        actual.webhook_id = None
        actual.channel.fetch_message = AsyncMock(return_value=actual)
        try:
            with patch('fri.fri_actions.asyncio.sleep', new=AsyncMock()), \
                    patch.object(Actions, 'valid', new=AsyncMock(return_value=access_allowed)):
                result = await fri.talk(123, 1, 20, actual.content, 'public', event=actual)
            assert ('confirmed command result' if record_failure != 'none' or not access_allowed else 'Stopped playback.') in result.reply
            assert 'try again' not in result.reply.casefold()
            # One pre-delivery privacy check; the record itself waits for Discord delivery.
            assert paths.count('/controls') == paths.count('/chat/turn/check') == 1 and '/chat/turn' not in paths
            fri.chat.respond.assert_not_awaited()
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('text,verb', [
    ('bro stop the music', 'stop'), ('pls pause the music', 'pause'),
    ('please ask gene to skip the current track', 'skip'), ('can u resume playback', 'resume'),
    ('plz stop music', 'stop'), ('okay fri, pause playback', 'pause'),
    ('could you please skip this song', 'skip'), ('will u unpause the audio', 'resume'),
    ('i would like you to stop the music', 'stop'), ('tell gene to pause music', 'pause'),
    ('can you get gene to skip the track', 'skip'), ('hey fri, can u resume playback', 'resume'),
])
def test_explicit_music_controls_reuse_the_established_request_grammar(controls_api, text, verb):  # noqa: F811
    from fri.fri_actions import Actions
    from fri.gene_controls import requested
    assert requested(text, verb)
    assert Actions.command_requested(SimpleNamespace(message=text, event=None), {'action': verb, 'argument': ''})
    assert command(text) == {'action': verb, 'argument': ''}
    client, _ = controls_api
    assert client.post('/chat/operations/admit', headers=headers('fri'), json=body(text)).json()['allowed']
