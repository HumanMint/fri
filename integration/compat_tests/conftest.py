import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi.testclient import TestClient
from gene.api import create_app
from gene.config import Settings
from gene.store import Store

from fri.reviews import request_reference

gene_test_config = TemporaryDirectory(prefix='gene-test-config-')
os.environ['XDG_CONFIG_HOME'] = gene_test_config.name


@pytest.fixture(autouse=True)
def bare_greeting_debounce(monkeypatch):
    # A bare "fri" waits up to 8 s for the rest (test_fri_summons.py). Elsewhere it keeps the usual
    # 1.5 s debounce, so tests that send one don't each wait out the longer window.
    monkeypatch.setattr('fri.fri_agent.REST_SECONDS', 1.5)


@pytest.fixture(autouse=True)
def inside_maintenance_window(monkeypatch):
    """A controller from gene #58 on starts deployments only in its production window ([04:00, 05:00)
    America/Los_Angeles). Tests not about the window run inside it; an older controller has none.

    Tomorrow's window, not a fixed date: gene #65 allows one deployment per local day, counted from rows the
    store dates with the real clock. Real rows always fall on an earlier day than the window, so a test's own
    deployments never use up that day (a fixed date broke every test with two deployments once it arrived)."""
    try:
        import gene.maintenance
    except ImportError:
        return
    import datetime as dt
    from zoneinfo import ZoneInfo
    tomorrow = dt.datetime.now(ZoneInfo('America/Los_Angeles')).date()+dt.timedelta(days=1)
    window = dt.datetime.combine(tomorrow, dt.time(4, 5), ZoneInfo('America/Los_Angeles')).timestamp()
    monkeypatch.setattr(gene.maintenance, 'clock', lambda: window)


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / 'gene.sqlite3')
    result.bootstrap('a' * 40)
    result.update_models([{'id': 'gpt-6-astra', 'name': 'GPT-6 Astra',
                           'efforts': ['medium', 'high'], 'default_effort': 'medium'}])
    result.select_model('gpt-6-astra', 'medium', 'owner')
    result.set_paused(False, 'owner')
    return result


@pytest.fixture
def client(tmp_path):
    settings = Settings(Path(tmp_path / 'api.sqlite3'), 'guild', 'owner',
                        'runtime-' + 'x' * 32, 'worker-' + 'x' * 32, 'deploy-' + 'x' * 32,
                        workshop_channel_id='workshop', fri_token='fri-' + 'x' * 32)
    return TestClient(create_app(settings))


def request(store, user='member', source='message-1'):
    job = store.request('guild', user, source, 'https://discord.com/channels/1/2/3',
                        'Add a useful feature for our server')
    if job['state'] == 'awaiting_owner':
        store.review_decision(job['id'], 'intake', request_reference(job), 'approve', 'owner')
    return store.get(job['id'])


def proposed(store, sha='b' * 40, source='message-1'):
    request(store, source=source)
    job = store.claim()
    store.finish_build(job['id'], job['lease'], {
        'sha': sha, 'base_sha': job['base_sha'], 'branch': 'codex/test',
        'tests_passed': True, 'summary': 'A tested change'})
    return store.get(job['id'])
