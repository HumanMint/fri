"""scripts/local_env.py: the local trial's env file is written privately and checked without echoing values."""

import importlib.util
from pathlib import Path

import httpx
import pytest

from fri.fri_protocol import contract

SCRIPT = Path(__file__).resolve().parents[1]/'scripts'/'local_env.py'
spec = importlib.util.spec_from_file_location('local_env', SCRIPT)
local_env = importlib.util.module_from_spec(spec)
spec.loader.exec_module(local_env)

# Invented values shaped like the real ones; none of them work anywhere.
DISCORD = 'MTAwMDAwMDAwMDAwMDAwMDA0.FAKEXX.'+'x'*38
SERVICE = 's'*40


def complete(tmp_path, **changes):
    values = {'DISCORD_TOKEN': DISCORD, 'GENE_FRI_TOKEN': SERVICE, 'GENE_GUILD_ID': '100000000000000001',
              'GENE_OWNER_ID': '100000000000000002', 'GENE_WORKSHOP_CHANNEL_ID': '100000000000000003',
              'GENE_CONTROLLER_URL': 'http://127.0.0.1:8787', 'FRI_PRESENCE': 'invisible',
              'FRI_DATA': str(tmp_path/'data'), 'FRI_CHATGPT_AUTH': str(tmp_path/'auth'/'session.json')}
    return {**values, **changes}


def levels(found):
    return {key: level for level, key, _ in found}


def test_init_writes_a_private_file_with_local_paths(tmp_path):
    env = tmp_path/'.env.local'
    assert local_env.init(env, local_env.EXAMPLE) == 0
    assert env.stat().st_mode & 0o777 == 0o600
    values = local_env.read_env(env)
    assert set(values) == set(local_env.read_env(local_env.EXAMPLE))
    assert values['GENE_CONTROLLER_URL'] == 'http://127.0.0.1:8787'
    assert values['FRI_DATA'] == '.state/fri-data' and values['FRI_CHATGPT_AUTH'] == '.secrets/fri-chatgpt.json'
    assert not values['DISCORD_TOKEN'] and not values['GENE_FRI_TOKEN']


def test_init_keeps_every_existing_value_and_adds_only_missing_settings(tmp_path):
    env = tmp_path/'.env.local'
    # As Gene's `scripts/dev.py fri-env` leaves it: the controller's share only, without a final newline.
    env.write_text(f'GENE_CONTROLLER_URL=http://127.0.0.1:9999\nGENE_FRI_TOKEN={SERVICE}\nFRI_DATA=')
    env.chmod(0o600)
    assert local_env.init(env, local_env.EXAMPLE) == 0
    values = local_env.read_env(env)
    assert set(values) == set(local_env.read_env(local_env.EXAMPLE))
    assert values['GENE_CONTROLLER_URL'] == 'http://127.0.0.1:9999' and values['GENE_FRI_TOKEN'] == SERVICE
    assert values['FRI_DATA'] == ''  # Present, even empty: kept. The local default applies when it's empty.
    assert values['FRI_CHATGPT_AUTH'] == '.secrets/fri-chatgpt.json'
    assert env.stat().st_mode & 0o777 == 0o600
    before = env.read_text()
    assert local_env.init(env, local_env.EXAMPLE) == 0
    assert env.read_text() == before


def test_local_paths_stay_inside_the_checkout_and_are_git_ignored():
    ignored = (local_env.ROOT/'.gitignore').read_text().split()
    assert '.env*' in ignored and '.state/' in ignored and '.secrets/' in ignored
    for value in ('.state/fri-data', '.secrets/fri-chatgpt.json'):
        assert value in local_env.LOCAL_DEFAULTS.values()


def test_a_fresh_file_lists_every_value_still_to_fill_in(tmp_path):
    env = tmp_path/'.env.local'
    local_env.init(env, local_env.EXAMPLE)
    found = local_env.findings(local_env.read_env(env), root=tmp_path)
    assert {key for level, key, _ in found if level == 'error'} == set(local_env.REQUIRED) - {'GENE_CONTROLLER_URL'}
    assert levels(found)['GENE_CONTROLLER_URL'] == 'ok' and levels(found)['FRI_DATA'] == 'ok'
    example = local_env.findings(local_env.read_env(local_env.EXAMPLE), root=tmp_path)
    assert levels(example)['GENE_CONTROLLER_URL'] == 'warn'  # Docker's name for it, not this machine's.


def test_a_complete_config_passes_and_never_echoes_a_value(tmp_path, capsys):
    (tmp_path/'auth').mkdir(mode=0o700)
    (tmp_path/'auth'/'session.json').write_text('{}')
    (tmp_path/'auth'/'session.json').chmod(0o600)
    found = local_env.findings(complete(tmp_path), root=tmp_path, env_mode=0o600)
    assert not [item for item in found if item[0] != 'ok'], found
    local_env.report(found)
    output = capsys.readouterr().out
    assert DISCORD not in output and SERVICE not in output


@pytest.mark.parametrize('changes, key, level', [
    ({'DISCORD_TOKEN': 'Bot '+DISCORD}, 'DISCORD_TOKEN', 'error'),
    ({'DISCORD_TOKEN': 'Bot'+DISCORD}, 'DISCORD_TOKEN', 'error'),
    ({'DISCORD_TOKEN': 'short.token.here'}, 'DISCORD_TOKEN', 'error'),
    ({'DISCORD_TOKEN': '"'+DISCORD+'"'}, 'DISCORD_TOKEN', 'error'),
    ({'GENE_FRI_TOKEN': 's'*31}, 'GENE_FRI_TOKEN', 'error'),
    ({'GENE_FRI_TOKEN': DISCORD}, 'GENE_FRI_TOKEN', 'error'),
    ({'GENE_GUILD_ID': '12345'}, 'GENE_GUILD_ID', 'error'),
    ({'GENE_OWNER_ID': '<@100000000000000002>'}, 'GENE_OWNER_ID', 'error'),
    ({'GENE_CONTROLLER_URL': '127.0.0.1:8787'}, 'GENE_CONTROLLER_URL', 'error'),
    ({'GENE_CONTROLLER_URL': 'http://controller:8787'}, 'GENE_CONTROLLER_URL', 'warn'),
    ({'GENE_CONTROLLER_URL': 'https://controller.example.com'}, 'GENE_CONTROLLER_URL', 'warn'),
    ({'FRI_TIMEZONE': 'Mars/Olympus'}, 'FRI_TIMEZONE', 'error'),
    ({'FRI_CHAT_CHANNEL_ID': 'general'}, 'FRI_CHAT_CHANNEL_ID', 'error'),
    ({'FRI_PRESENCE': 'idle'}, 'FRI_PRESENCE', 'warn'),
    ({'GENE_FRI_TOKN': SERVICE}, 'GENE_FRI_TOKN', 'warn'),
])
def test_mistakes_are_named_without_their_values(tmp_path, capsys, changes, key, level):
    found = local_env.findings(complete(tmp_path, **changes), root=tmp_path)
    assert levels(found)[key] == level
    local_env.report(found)
    output = capsys.readouterr().out
    assert DISCORD not in output and SERVICE not in output


@pytest.mark.parametrize('url', [
    'http://[::1', 'http://127.0.0.1:abc', 'http://127.0.0.1:0', 'http://127.0.0.1:65536',
    'http://fri:hunter2-secret@127.0.0.1:abc', 'http://fri:hunter2-secret@[::1',
])
def test_an_unusable_controller_url_is_an_error_not_a_crash(tmp_path, capsys, url):
    found = local_env.findings(complete(tmp_path, GENE_CONTROLLER_URL=url), root=tmp_path)
    assert levels(found)['GENE_CONTROLLER_URL'] == 'error'
    local_env.report(found)
    output = capsys.readouterr().out
    assert url not in output and 'hunter2' not in output


@pytest.mark.parametrize('url', ['http://127.0.0.1', 'http://127.0.0.1:1', 'http://[::1]:65535'])
def test_a_local_controller_url_with_or_without_a_port_is_fine(tmp_path, url):
    assert levels(local_env.findings(complete(tmp_path, GENE_CONTROLLER_URL=url), root=tmp_path))[
        'GENE_CONTROLLER_URL'] == 'ok'


def test_a_missing_sign_in_is_a_warning_and_its_contents_are_never_needed(tmp_path):
    found = local_env.findings(complete(tmp_path), root=tmp_path)
    assert levels(found)['FRI_CHATGPT_AUTH'] == 'warn'
    assert not [item for item in found if item[0] == 'error']
    (tmp_path/'auth').mkdir()
    (tmp_path/'auth'/'session.json').write_text('not json, never parsed')
    (tmp_path/'auth'/'session.json').chmod(0o644)
    assert levels(local_env.findings(complete(tmp_path), root=tmp_path))['FRI_CHATGPT_AUTH'] == 'warn'
    (tmp_path/'auth'/'session.json').chmod(0o600)
    assert levels(local_env.findings(complete(tmp_path), root=tmp_path))['FRI_CHATGPT_AUTH'] == 'ok'


def controller(status=200, body=None):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, json=contract() if body is None else body)
    return httpx.MockTransport(handler), seen


def test_the_controller_probe_reads_the_contract_as_fri_does():
    transport, seen = controller()
    assert local_env.probe('http://127.0.0.1:8787/', SERVICE, transport) == [
        ('ok', 'controller contract gene-fri/1, every required capability offered')]
    assert str(seen[0].url) == 'http://127.0.0.1:8787/fri/contract'
    assert seen[0].headers['authorization'] == 'Bearer '+SERVICE
    assert seen[0].headers['x-fri-contract'] == 'gene-fri/1'


def test_the_controller_probe_explains_each_refusal():
    old = {**contract(), 'capabilities': [c for c in contract()['capabilities'] if c not in ('turn-provenance', 'usage-window')]}
    [(level, message)] = local_env.probe('http://127.0.0.1:8787', SERVICE, controller(body=old)[0])
    assert level == 'error' and 'turn-provenance' in message and 'usage-window' not in message
    optional = {**contract(), 'capabilities': [c for c in contract()['capabilities'] if c != 'usage-window']}
    assert local_env.probe('http://127.0.0.1:8787', SERVICE, controller(body=optional)[0])[1] == (
        'warn', 'optional capabilities not offered (those features stay off): usage-window')
    [(level, message)] = local_env.probe('http://127.0.0.1:8787', SERVICE, controller(401, {'detail': 'x'})[0])
    assert level == 'error' and 'GENE_FRI_TOKEN' in message

    def down(request):
        raise httpx.ConnectError('refused', request=request)
    [(level, message)] = local_env.probe('http://127.0.0.1:8787', SERVICE, httpx.MockTransport(down))
    assert level == 'error' and 'no controller answered' in message and SERVICE not in message
    # A URL carrying a user name and password is valid, but the failure names only where it went.
    [(level, message)] = local_env.probe('http://fri:synthetic-password@127.0.0.1:8787/x?k=v', SERVICE,
                                         httpx.MockTransport(down))
    assert 'at http://127.0.0.1:8787 (' in message
    assert not any(secret in message for secret in ('synthetic-password', 'fri:', '?k=v', SERVICE))


def test_check_exits_non_zero_until_the_file_is_complete(tmp_path, capsys):
    env = tmp_path/'.env.local'
    assert local_env.check(env) == 1
    local_env.init(env, local_env.EXAMPLE)
    assert local_env.check(env) == 1
    values = complete(tmp_path)
    env.write_text(''.join(f'{key}={value}\n' for key, value in values.items()))
    env.chmod(0o600)
    assert local_env.check(env, controller=True, transport=controller()[0]) == 0
    assert local_env.check(env, controller=True, transport=controller(401, {})[0]) == 1
    output = capsys.readouterr().out
    assert DISCORD not in output and SERVICE not in output
