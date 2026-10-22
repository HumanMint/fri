import copy
import datetime as dt
import time

import discord
import pytest
from fastapi.testclient import TestClient
from gene.api import create_app
from gene.config import Settings
from gene.control_store import Controls
from gene.store import Store

from compat_tests.test_api import headers
from fri.gene_controls import COMMANDS, arguments, requested


def control_body(**changes):
    event_id = str(discord.utils.time_snowflake(dt.datetime.now(dt.timezone.utc)))
    body = {'guild_id': '123', 'user_id': '20', 'channel_id': '1', 'conversation_id': 'public:1',
            'source': {'kind': 'discord_message', 'id': event_id, 'user_id': '20', 'display_name': 'Nova',
                       'channel_id': '1', 'guild_id': '123'},
            'request_text': 'fri play Sad Machine', 'target_channel_id': '1',
            'action': 'play', 'argument': 'Porter Robinson Sad Machine', 'revision': 0}
    return {**body, **changes}


@pytest.fixture
def controls_api(tmp_path):
    settings = Settings(tmp_path/'api.sqlite3', '123', '10',
        'runtime-'+'x'*32, 'worker-'+'x'*32, 'deploy-'+'x'*32, fri_token='fri-'+'x'*32,
        workshop_channel_id='456', command_channel_id='2', agent_channels='1,2', test_guild_id='777', test_channel_id='7')
    client = TestClient(create_app(settings))
    client.post('/runtime/controls/poll', headers=headers('runtime'), json={'actions': list(COMMANDS)})
    return client, Controls(client.app.state.store)


@pytest.mark.parametrize('action,text,ok', [
    ('play', 'fri play Sad Machine', True), ('play', '<@123> can you play this one?', True),
    ('play', 'yo fri could u please play that Porter song that goes "she depends on you"?', True),
    ('play', 'can you get gene to play Said The Sky', True),
    ('play', 'can i ask you to play music in the future?', False),
    ('play', 'for example play music', False), ('play', 'I play minecraft every day', False),
    ('play', 'fri he told me to play music', False), ('play', '> play this\nhey fri', False),
    ('play', '"fri play music" was the message', False), ('play', 'fri dont play anything', False),
    ('pause', 'fri pause please', True), ('resume', 'resume please', True),
    ('stop', 'hey fri stop the music', True), ('skip', 'can you skip this one', True),
    ('volume', 'fri set volume to 50', True), ('volume', 'volume 40', True),
    ('queue', 'whats in the queue', True), ('now_playing', 'what song is this', True),
    ('now_playing', 'thanks. gene joined but i dont hear music', True),
    ('play', 'thanks. gene joined but i dont hear music', False),
    ('now_playing', 'for example i dont hear music', False),
    ('status', 'dont check gene, i just said no music', False),
    ('status', 'is gene working?', True), ('version', 'which release is gene on', True),
])
def test_only_current_explicit_mutations_are_requests(action, text, ok):
    assert requested(text, action) is ok


@pytest.mark.parametrize('action,value', [('eval', 'anything'), ('audioset', ''), ('gene model', ''),
    ('play', 'file:///etc/passwd'), ('play', 'http://youtube.com/watch?v=x'),
    ('play', 'https://youtube.com@evil.example/'), ('play', '../secrets'), ('play', 'ytsearch:foo'),
    ('play', 'music\n!eval dangerous'), ('play', 'https://localhost/song'), ('play', '<@123>'),
    ('volume', '101'), ('volume', '-1'), ('volume', '0;eval'), ('stop', 'extra')])
def test_argument_boundary_rejects_admin_local_sources_and_injected_commands(action, value):
    with pytest.raises(ValueError):
        arguments(action, value)


def test_service_roles_cannot_cross_bridge_authority(controls_api):
    client, _ = controls_api
    for role in ('runtime', 'worker', 'deploy'):
        assert client.post('/controls', headers=headers(role), json=control_body()).status_code == 401
        assert client.get('/controls/catalog', headers=headers(role)).status_code == 401
    for role in ('fri', 'worker', 'deploy'):
        assert client.post('/runtime/controls/poll', headers=headers(role), json={'actions': ['play']}).status_code == 401
    result = client.post('/controls', headers=headers('fri'), json=control_body()).json()
    assert client.post('/runtime/controls/'+result['id']+'/start', headers=headers('fri'),
                       json={'lease': 'a'*32}).status_code == 401
    assert client.app.state.store.status()['paused']  # Everyday controls don't grant deployment authority.


def test_private_control_checks_source_and_lab_cannot_redirect_to_ff(controls_api):
    client, _ = controls_api
    body = control_body()
    for changes in ({'guild_id': '777', 'channel_id': '7'}, {'target_channel_id': '2'},
                    {'target_channel_id': '999'}, {'user_id': '30'}, {'conversation_id': 'public:2'},
                    {'source': {**body['source'], 'guild_id': None}}, {'action': 'eval'},
                    {'source': {**body['source'], 'id': '100000000000000001'}}):
        assert client.post('/controls', headers=headers('fri'), json={**body, **changes}).status_code >= 400
    private = {**body, 'channel_id': '456', 'target_channel_id': '2', 'conversation_id': 'private:999',
               'source': {**body['source'], 'guild_id': None, 'channel_id': '999'}}
    assert client.post('/controls', headers=headers('fri'), json=private).status_code == 200


def test_one_event_one_execution_even_after_restart_and_duplicate_delivery(controls_api):
    client, controls = controls_api
    body = control_body()
    identifier = controls.enqueue(body)['id']
    assert controls.enqueue(body)['id'] == identifier
    claim = controls.poll(['play'])
    assert claim['id'] == identifier and controls.poll(['play']) is None
    with pytest.raises(ValueError):
        controls.start(identifier, 'bad-lease')
    controls.start(identifier, claim['lease'])
    with pytest.raises(ValueError):
        controls.start(identifier, claim['lease'])
    restarted = Controls(Store(client.app.state.store.path))
    assert restarted.poll(['play']) is None
    assert not restarted.result(identifier, '20', '123', 'public:1')['done']
    restarted.finish(identifier, 'wrong-lease', {'output': 'wrong'})
    assert not restarted.result(identifier, '20', '123', 'public:1')['done']
    restarted.finish(identifier, claim['lease'], {'output': 'Track enqueued'})
    assert restarted.result(identifier, '20', '123', 'public:1') == {'done': True, 'output': 'Track enqueued'}
    assert restarted.enqueue(body)['id'] == identifier and restarted.poll(['play']) is None
    with pytest.raises(KeyError):
        restarted.result(identifier, '30', '123', 'public:1')
    with pytest.raises(ValueError):
        restarted.result(identifier, '20', '123', 'private:999')


def test_expiration_never_replays_uncertain_work_and_limits_catalog_age(controls_api):
    _, controls = controls_api
    body = control_body()
    identifier = controls.enqueue(body)['id']
    with controls.store.connection() as db:
        db.execute('UPDATE gene_controls SET created=?', (time.time()-35,))
        db.execute('UPDATE gene_control_catalog SET at=?', (time.time()-25,))
    assert controls.catalog() == []
    assert controls.poll(['play']) is None
    assert 'nothing was run' in controls.result(identifier, '20', '123', 'public:1')['blocked']
    second = copy.deepcopy(body)
    second['source']['id'] = str(int(body['source']['id'])+1)
    second_id = controls.enqueue(second)['id']
    claim = controls.poll(['play'])
    with controls.store.connection() as db:
        db.execute('UPDATE gene_controls SET created=? WHERE id=?', (time.time()-120, second_id))
    with pytest.raises(ValueError):
        controls.start(second_id, claim['lease'])
    assert 'uncertain' in controls.result(second_id, '20', '123', 'public:1')
    assert controls.poll(['play']) is None


@pytest.mark.parametrize('privacy', ['clear', 'disable', 'erase'])
def test_erasure_cancels_and_redacts_pending_commands(controls_api, privacy):
    _, controls = controls_api
    body = control_body()
    identifier = controls.enqueue(body)['id']
    claim = controls.poll(['play'])
    if privacy == 'clear':
        controls.store.clear_chats('20')
    elif privacy == 'erase':
        controls.store.erase_user('20')
    else:
        controls.store.personal_control('20', 'disable')
    with pytest.raises(ValueError):
        controls.start(identifier, claim['lease'])
    controls.finish(identifier, claim['lease'], {'output': 'must not revive'})
    with controls.store.connection() as db:
        row = db.execute('SELECT * FROM gene_controls').fetchone()
        assert row['state'] == 'cancelled' and row['payload'] == '{}' and row['result'] == '{}'
        assert row['user_id'] == 'deleted'
    assert controls.poll(['play']) is None


def test_recent_receipts_are_scoped_expiring_erased_and_read_only(controls_api):
    client, controls = controls_api
    body = control_body()
    identifier = controls.enqueue(body)['id']
    claim = controls.poll(['play'])
    controls.finish(identifier, claim['lease'], {'output': 'Track Enqueued: Sad Machine'})
    scope = {k: body[k] for k in ('guild_id','user_id','channel_id','conversation_id')}
    def read(**changes):
        return client.post('/controls/recent', headers=headers('fri'), json={**scope, **changes}).json()['receipts']
    rows = read()
    assert len(rows) == 1 and rows[0]['result']['output'].startswith('Track Enqueued')
    assert read(user_id='30') == [] and read(conversation_id='private:1') == []
    assert controls.poll(['play']) is None
    with controls.store.connection() as db:
        db.execute('UPDATE gene_controls SET created=?', (time.time()-86401,))
    assert read() == []
    controls.store.clear_chats('20')
    assert read() == []
    for role in ('runtime', 'worker', 'deploy'):
        assert client.post('/controls/recent', headers=headers(role), json=scope).status_code == 401


def test_dm_target_cannot_fall_back_to_workshop_or_an_arbitrary_channel(controls_api):
    client, _ = controls_api
    body = control_body(channel_id='456', conversation_id='private:999')
    body['source'].update(guild_id=None, channel_id='999')
    for channel in ('456', '1', '', '999'):
        assert client.post('/controls', headers=headers('fri'), json={**body, 'target_channel_id': channel}).status_code == 403
