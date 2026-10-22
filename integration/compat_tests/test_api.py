import pytest


def headers(role):
    return {'Authorization': 'Bearer ' + role + '-' + 'x' * 32}


def actor(user='owner'):
    return {'guild_id': 'guild', 'user_id': user}


def test_health_does_not_require_or_expose_credentials(client):
    assert client.get('/health').json() == {'ok': True, 'service': 'gene-controller'}
    assert client.get('/status').status_code == 401


@pytest.mark.parametrize('role', ['worker', 'deploy'])
def test_worker_and_deployer_cannot_approve_as_owner(client, role):
    result = client.post('/requests/id/approve', headers=headers(role),
                         json={**actor(), 'sha': 'a' * 40})
    assert result.status_code == 401


@pytest.mark.parametrize('route,extra', [('/pause', {'paused': False}), ('/usage', {}),
    ('/rollback', {'sha': 'a' * 40}), ('/memory/list', {}), ('/requests/id/approve', {'sha': 'a' * 40})])
def test_member_cannot_invoke_owner_controls(client, route, extra):
    assert client.post(route, headers=headers('fri'),
                       json={**actor('member'), **extra}).status_code == 403


def test_request_is_scoped_to_guild_and_workshop(client):
    payload = {**actor('member'), 'source_id': '123', 'source_url': '',
               'body': 'Add a game command', 'channel_id': 'workshop'}
    assert client.post('/requests', headers=headers('fri'), json=payload).status_code == 200
    payload['channel_id'] = 'random-chat'
    assert client.post('/requests', headers=headers('fri'), json=payload).status_code == 403
    payload['channel_id'] = 'workshop'
    payload['guild_id'] = 'another-guild'
    assert client.post('/requests', headers=headers('fri'), json=payload).status_code == 403


@pytest.mark.parametrize('role', ['runtime', 'fri'])
def test_discord_services_cannot_claim_build_or_deployment(client, role):
    assert client.post('/worker/claim', headers=headers(role)).status_code == 401
    assert client.post('/deploy/claim', headers=headers(role)).status_code == 401


def test_owner_can_pause_without_model_or_worker(client):
    result = client.post('/pause', headers=headers('fri'), json={**actor(), 'paused': True})
    assert result.status_code == 200
    assert result.json()['paused'] is True

