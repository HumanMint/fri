"""Closing the loop when Gene gets fixed, against the real controller: Fri's own line, the owner's Tell reporter."""

import asyncio
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import httpx
import pytest
from fastapi.testclient import TestClient
from gene.api import create_app
from gene.config import Settings
from gene.store import Store

from compat_tests.test_api import headers
from fri import fri_fixes
from fri.fri_agent import Agent
from fri.fri_agent_settings import DEFAULTS
from fri.fri_errors import ControllerRejected
from fri.fri_fixes import TELL_ID, TellReporter, check, follow, offers_tell, tell, tell_component
from fri.fri_reviews import review_message
from fri.reviews import request_reference

GENE, FRI, OWNER, MEMBER = 88, 99, 10, 20


def snowflake(age=0):
    return discord.utils.time_snowflake(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=age))


def message(author, id=None, bot=True):
    return SimpleNamespace(id=id or snowflake(5), author=SimpleNamespace(id=author, bot=bot), webhook_id=None,
                           add_reaction=AsyncMock(), jump_url='https://discord.com/channels/123/456/1')


def gone():
    return discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'Unknown Message')


def channel(found, last=None):
    def fetch(id):
        if id not in found:
            raise gone()
        return found[id]
    guild = SimpleNamespace(id=123, me=SimpleNamespace(id=FRI))
    return SimpleNamespace(id=456, parent_id=None, guild=guild, last_message_id=last or snowflake(600),
                           permissions_for=lambda _: discord.Permissions.all(), fetch_message=AsyncMock(side_effect=fetch))


@pytest.fixture
def world(tmp_path, monkeypatch):
    settings = Settings(Path(tmp_path/'api.sqlite3'), '123', str(OWNER), 'runtime-'+'x'*32, 'worker-'+'x'*32,
                        'deploy-'+'x'*32, workshop_channel_id='456', agent_channels='456', fri_token='fri-'+'x'*32)
    client = TestClient(create_app(settings))
    store = Store(settings.db)
    store.bootstrap('a'*40)
    store.set_paused(False, 'owner')
    store.update_models([{'id': 'gpt-6-astra', 'name': 'GPT-6 Astra', 'efforts': ['high'], 'default_effort': 'high'}])
    store.select_model('gpt-6-astra', 'high', 'owner')

    async def api(method, path, body=None):
        response = client.request(method, path, json=body, headers=headers('fri'))
        if 400 <= response.status_code < 500:
            raise ControllerRejected(response.json().get('detail', ''), response.status_code)
        assert response.status_code < 500, response.text
        return response.json()

    found = {}
    room = channel(found)
    fri = SimpleNamespace(owner_id=OWNER, guild_id=123, workshop_id=456, user=SimpleNamespace(id=FRI),
                          data=tmp_path/'data', capabilities=frozenset({'requester-notices'}),
                          api=AsyncMock(side_effect=api), get_channel=lambda id: room if id == 456 else None,
                          fetch_channel=AsyncMock(side_effect=lambda id: room))
    agent = Agent(fri)
    agent.state = {'config': {**DEFAULTS, 'enabled': True}, 'channels': ['456'], 'history_channels': []}
    agent.gene_id = GENE
    agent.member = AsyncMock(return_value=SimpleNamespace(id=MEMBER))
    fri.agent = agent
    sent = []

    async def send_once(_, post, *, reply_to=None, **__):
        sent.append({**post, 'reply_to': reply_to})
        return {'id': str(snowflake())}

    monkeypatch.setattr('fri.fri_workspace.send_once', send_once)
    return SimpleNamespace(store=store, client=client, fri=fri, agent=agent, room=room, found=found, sent=sent)


def ship(store, job_id, sha='b'*40):
    job = store.get(job_id)
    if job['state'] == 'awaiting_owner':
        store.review_decision(job_id, 'intake', request_reference(job), 'approve', 'owner')
    built = store.claim()
    assert built['id'] == job_id
    store.finish_build(job_id, built['lease'], {'sha': sha, 'base_sha': built['base_sha'], 'branch': 'codex/fix',
                                                'tests_passed': True, 'summary': 'Fix it'})
    if store.get(job_id)['state'] == 'review':
        store.approve(job_id, sha, 'owner')
    deploying = store.claim_deploy()
    store.finish_deploy(deploying['id'], deploying['lease'], True, 'Release checks passed.')
    assert store.get(job_id)['state'] == 'deployed'


def filed(world):
    error = message(GENE)
    world.found[error.id] = error
    world.agent.run = AsyncMock(return_value=({'summary': 'Evidence uncertain', 'proposed_request': 'Fix the stuck track'},
                                              {}, 'completed'))
    asyncio.run(world.agent.investigate(456, {'id': str(error.id), 'text': 'Track Stuck error'}))
    [job_id] = json.loads((world.fri.data/'gene-fixes.json').read_text())
    return error, job_id


def test_a_filed_gene_error_gets_eyes_and_one_line_once_its_fix_ships(world):
    error, job_id = filed(world)
    error.add_reaction.assert_awaited_once_with('👀')
    assert world.store.get(job_id)['source_id'] == f'gene-error:{error.id}'
    asyncio.run(check(world.agent))
    assert not world.sent and job_id in json.loads((world.fri.data/'gene-fixes.json').read_text())
    ship(world.store, job_id)
    asyncio.run(check(world.agent))
    [line] = world.sent
    assert line['reply_to'] == error.id and line['target_id'] == '456'
    assert line['body'] == 'that one should be fixed now, gene’s on `bbbbbbb`. you’re welcome'
    asyncio.run(check(world.agent))
    assert len(world.sent) == 1 and json.loads((world.fri.data/'gene-fixes.json').read_text())[job_id]['done']


def test_a_busy_room_waits_for_a_lull(world):
    error, job_id = filed(world)
    ship(world.store, job_id)
    world.room.last_message_id = snowflake(10)
    asyncio.run(check(world.agent))
    assert not world.sent
    world.room.last_message_id = snowflake(120)
    asyncio.run(check(world.agent))
    assert len(world.sent) == 1


@pytest.mark.parametrize('ending', ['rejected', 'rolled_back', 'expired', 'deleted', 'proactive_off'])
def test_nothing_but_a_live_fix_is_announced(world, ending):
    error, job_id = filed(world)
    if ending == 'rejected':
        world.store.reject(job_id, 'owner')
    else:
        ship(world.store, job_id)
    if ending == 'rolled_back':
        rollback = world.store.rollback('123', 'owner')
        deploying = world.store.claim_deploy()
        assert deploying['id'] == rollback['id']
        world.store.finish_deploy(deploying['id'], deploying['lease'], True, 'Rolled back.')
    if ending == 'expired':  # Gene's error is over three days old by now.
        path = world.fri.data/'gene-fixes.json'
        entries = json.loads(path.read_text())
        entries[job_id]['message_id'] = str(snowflake(3*86400+1))
        path.write_text(json.dumps(entries))
    if ending == 'deleted':
        del world.found[error.id]
    if ending == 'proactive_off':
        world.agent.state['config']['proactive'] = False
        world.fri.api.reset_mock()
    asyncio.run(check(world.agent))
    assert not world.sent
    remaining = json.loads((world.fri.data/'gene-fixes.json').read_text())
    if ending == 'proactive_off':
        world.fri.api.assert_not_awaited()  # Nothing is read while it couldn't post anyway.
        assert job_id in remaining
    else:
        assert remaining == {}


def test_a_line_discord_did_not_confirm_is_never_repeated(world, monkeypatch):
    error, job_id = filed(world)
    ship(world.store, job_id)
    attempts = []

    async def lost(*args, **kwargs):
        attempts.append(args)
        raise httpx.ReadTimeout('lost')

    monkeypatch.setattr('fri.fri_workspace.send_once', lost)
    asyncio.run(check(world.agent))
    asyncio.run(check(world.agent))
    assert len(attempts) == 1


def test_a_refiled_error_whose_job_ended_or_an_old_error_starts_no_watch(world, tmp_path):
    agent = world.agent
    asyncio.run(follow(agent, 456, str(snowflake(5)), {'id': 'a'*12, 'state': 'deployed'}))
    asyncio.run(follow(agent, 456, str(snowflake(5)), {'id': 'nope'}))
    asyncio.run(follow(agent, 456, str(snowflake(3*86400+1)), {'id': 'b'*12, 'state': 'queued'}))
    asyncio.run(follow(agent, 456, 'nope', {'id': 'b'*12, 'state': 'queued'}))
    assert not (world.fri.data/'gene-fixes.json').exists()


def test_refiling_an_error_through_the_other_path_gets_no_second_line(world):
    error, job_id = filed(world)
    # The same error filed again as a repair, before and after the first job's line: a second job each time.
    asyncio.run(follow(world.agent, 456, str(error.id), {'id': 'e'*12, 'state': 'queued'}))
    ship(world.store, job_id)
    asyncio.run(check(world.agent))
    asyncio.run(follow(world.agent, 456, str(error.id), {'id': 'f'*12, 'state': 'queued'}))
    asyncio.run(check(world.agent))
    assert len(world.sent) == 1 and error.add_reaction.await_count == 1
    assert list(json.loads((world.fri.data/'gene-fixes.json').read_text())) == [job_id]


def test_a_filing_during_a_pass_is_kept(world):
    error, job_id = filed(world)
    other = message(GENE)
    world.found[other.id] = other
    fetch = world.room.fetch_message.side_effect

    async def meanwhile(id):
        await follow(world.agent, 456, str(other.id), {'id': 'e'*12, 'state': 'queued'})
        return fetch(id)

    ship(world.store, job_id)
    world.room.fetch_message = AsyncMock(side_effect=meanwhile)
    asyncio.run(check(world.agent))
    assert len(world.sent) == 1 and set(json.loads((world.fri.data/'gene-fixes.json').read_text())) == {job_id, 'e'*12}


def test_a_stalled_discord_read_holds_up_one_heartbeat_stage_and_is_never_cancelled(world, monkeypatch):
    error, job_id = filed(world)
    ship(world.store, job_id)
    monkeypatch.setattr(fri_fixes, 'PASS', 0.05)

    async def scenario():
        release, reads = asyncio.Event(), []

        async def rate_limited(id):  # discord.py's global rate-limit wait, which must never be cancelled.
            reads.append(id)
            await release.wait()
            return error

        world.room.fetch_message = AsyncMock(side_effect=rate_limited)
        with pytest.raises(TimeoutError):
            await check(world.agent)
        task = fri_fixes.RUNNING[world.agent]
        with pytest.raises(TimeoutError):
            await check(world.agent)  # The next heartbeat waits on the same pass instead of starting another.
        assert fri_fixes.RUNNING[world.agent] is task and not task.done() and len(reads) == 1 and not world.sent
        release.set()
        await check(world.agent)
        assert task.done() and not task.cancelled() and len(reads) == 1 and len(world.sent) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['rolled_back', 'proactive_off', 'not_allowed', 'archived'])
def test_a_change_while_discord_answers_still_stops_the_line(world, change):
    error, job_id = filed(world)
    ship(world.store, job_id)

    def meanwhile(id):
        if change == 'rolled_back':
            world.store.rollback('123', 'owner')
            deploying = world.store.claim_deploy()
            world.store.finish_deploy(deploying['id'], deploying['lease'], True, 'Rolled back.')
        if change == 'proactive_off':
            world.agent.state['config']['proactive'] = False
        if change == 'not_allowed':
            world.agent.state['channels'] = []
            world.fri.workshop_id = 1
        if change == 'archived':
            world.room.archived = True
        return error

    world.room.fetch_message = AsyncMock(side_effect=meanwhile)
    asyncio.run(check(world.agent))
    assert not world.sent
    assert (job_id in json.loads((world.fri.data/'gene-fixes.json').read_text())) == (change != 'rolled_back')


def test_only_genes_own_error_gets_the_eyes(world):
    impostor = message(MEMBER, bot=False)
    world.found[impostor.id] = impostor
    asyncio.run(follow(world.agent, 456, str(impostor.id), {'id': 'c'*12, 'state': 'queued'}))
    impostor.add_reaction.assert_not_awaited()


def test_malformed_watch_entries_are_dropped_instead_of_failing_every_heartbeat(world):
    path = world.fri.data/'gene-fixes.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'d'*12: {'channel_id': '456'}, 'not-a-job': {}}))
    asyncio.run(check(world.agent))
    assert json.loads(path.read_text()) == {}
    path.write_text('{')
    asyncio.run(check(world.agent))  # Unreadable: starts over instead of raising.


def reported(world):
    """A member's repair report, handed to the owner by its checks, approved and shipped."""
    report = message(MEMBER, bot=False)
    world.found[report.id] = report
    with world.store.connection() as db:
        db.execute('UPDATE settings SET value="existing-features-v1" WHERE key="repair_policy"')
    response = world.client.post('/repairs', headers=headers('fri'), json={
        'guild_id': '123', 'user_id': str(MEMBER), 'channel_id': '456', 'conversation_id': 'public:456', 'revision': 0,
        'source': {'kind': 'discord_message', 'id': str(report.id), 'user_id': str(MEMBER), 'display_name': 'Member',
                   'channel_id': '456', 'guild_id': '123'}, 'body': 'The vote counter counts my self-vote.'})
    job_id = response.json()['id']
    ship(world.store, job_id)  # No verification evidence, so it went to the owner, who approved its commit.
    while True:  # The owner's DM queue, as Fri reads it.
        notice = world.client.post('/reviews/claim', headers=headers('fri')).json()
        if notice['job']['id'] == job_id:
            assert notice['phase'] == 'status' and notice['job']['state'] == 'deployed'
            return report, job_id, notice
        world.client.post(f"/reviews/{notice['job']['id']}/delivered", headers=headers('fri'),
                          json={'lease': notice['lease'], 'channel_id': '1', 'message_id': '2'})


def test_the_owner_tells_the_reporter_once(world):
    report, job_id, notice = reported(world)
    assert offers_tell(world.fri, notice)
    embed, attachment, view = review_message(notice, tell=True)
    attachment.close()
    [button] = view.children
    assert button.item.custom_id == f'fri:tell:{job_id}' and button.item.label == 'Tell reporter'
    text, sent = asyncio.run(tell(world.fri, job_id, OWNER))
    assert sent and text.startswith('told them: ')
    [reply] = world.sent
    assert reply['reply_to'] == report.id and reply['target_id'] == '456'
    assert reply['body'] == 'that gene thing you reported should be fixed now (`bbbbbbb`). try not to break it again'
    text, sent = asyncio.run(tell(world.fri, job_id, OWNER))
    assert not sent and 'already told' in text and len(world.sent) == 1
    with world.store.connection() as db:
        assert db.execute('SELECT state FROM requester_notices WHERE job_id=?', (job_id,)).fetchone()[0] == 'sent'


@pytest.mark.parametrize('status,again', [(403, True), (429, True), (500, False)])
def test_a_refused_reply_can_be_tapped_again_but_an_unconfirmed_one_cannot(world, monkeypatch, status, again):
    report, job_id, _ = reported(world)

    async def refused(*args, **kwargs):
        request = httpx.Request('POST', 'https://discord.com/api/v10/channels/456/messages')
        raise httpx.HTTPStatusError('refused', request=request, response=httpx.Response(status, request=request))

    monkeypatch.setattr('fri.fri_workspace.send_once', refused)
    text, sent = asyncio.run(tell(world.fri, job_id, OWNER))
    assert not sent
    assert ('tap again' in text) == again and ('not sending it twice' in text) != again
    with world.store.connection() as db:
        state = db.execute('SELECT state FROM requester_notices WHERE job_id=?', (job_id,)).fetchone()[0]
    assert state == ('failed' if again else 'uncertain')
    text, sent = asyncio.run(tell(world.fri, job_id, OWNER))
    assert ('may already' in text) != again


@pytest.mark.parametrize('change', ['deleted', 'edited_author', 'not_allowed', 'archived', 'bot_reporter'])
def test_nothing_posts_when_the_report_is_not_reachable_now(world, change):
    report, job_id, notice = reported(world)
    if change == 'deleted':
        del world.found[report.id]
    if change == 'edited_author':
        report.author = SimpleNamespace(id=MEMBER+1, bot=False)
    if change == 'not_allowed':
        world.agent.state['channels'] = []
        world.fri.workshop_id = 1
    if change == 'archived':  # A reply would reopen their thread.
        world.room.archived = True
    if change == 'bot_reporter':
        world.agent.gene_id = MEMBER
        assert not offers_tell(world.fri, notice)
    text, sent = asyncio.run(tell(world.fri, job_id, OWNER))
    assert not sent and text.startswith('didn’t send it') and not world.sent
    with world.store.connection() as db:  # Read before the claim, so nothing is held: a later tap can still try.
        assert db.execute('SELECT state FROM requester_notices WHERE job_id=?', (job_id,)).fetchone() is None


def test_no_button_without_the_capability_or_for_a_bot_or_unshipped_job(world):
    _, job_id, notice = reported(world)
    assert offers_tell(world.fri, notice)
    assert not offers_tell(SimpleNamespace(**{**vars(world.fri), 'capabilities': frozenset()}), notice)
    for changes in ({'state': 'deploying'}, {'source_url': ''}, {'source_id': 'gene-error:1'},
                    {'user_id': str(GENE)}, {'user_id': str(FRI)}):
        assert not offers_tell(world.fri, {**notice, 'job': {**notice['job'], **changes}})
    assert not offers_tell(world.fri, {**notice, 'phase': 'release'})
    embed, attachment, view = review_message(notice)
    attachment.close()
    assert not view.children


def test_tell_buttons_are_the_owners_on_fris_own_dms_only(monkeypatch):
    async def scenario():
        fri = SimpleNamespace(owner_id=OWNER, user=SimpleNamespace(id=FRI), api=AsyncMock())
        event = SimpleNamespace(guild_id=None, type=discord.InteractionType.component, user=SimpleNamespace(id=OWNER),
                                client=fri, message=SimpleNamespace(author=SimpleNamespace(id=FRI)),
                                data={'custom_id': 'fri:tell:0123456789ab'},
                                response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock(),
                                followup=SimpleNamespace(send=AsyncMock()))
        assert tell_component(event, FRI, OWNER)
        for attribute, value in [('user', SimpleNamespace(id=MEMBER)), ('guild_id', 123),
                                 ('message', SimpleNamespace(author=SimpleNamespace(id=MEMBER))),
                                 ('data', {'custom_id': 'fri:tell:nope'})]:
            original = getattr(event, attribute)
            setattr(event, attribute, value)
            assert not tell_component(event, FRI, OWNER)
            setattr(event, attribute, original)
        button = await TellReporter.from_custom_id(event, None, TELL_ID.fullmatch(event.data['custom_id']))
        told = AsyncMock(return_value=('told them: link', True))
        monkeypatch.setattr(fri_fixes, 'tell', told)
        await button.callback(event)
        told.assert_awaited_once_with(fri, '0123456789ab', OWNER)
        assert event.edit_original_response.await_args.kwargs == {'view': None}
        assert event.followup.send.await_args.kwargs['ephemeral'] is True
        event.user = SimpleNamespace(id=MEMBER)
        told.reset_mock()
        await button.callback(event)
        told.assert_not_awaited()
    asyncio.run(scenario())


def released(store, sha='c'*40):
    """An ordinary request shipped after the repair, so the repair isn't the live release anymore."""
    job = store.request('123', '30', 'message-later', 'https://discord.com/channels/123/456/1', 'Add a vote command')
    ship(store, job['id'], sha)


def rolled_back(store):
    rollback = store.rollback('123', 'owner')
    deploying = store.claim_deploy()
    assert deploying['id'] == rollback['id']
    store.finish_deploy(deploying['id'], deploying['lease'], True, 'Rolled back.')


@pytest.mark.parametrize('change', ['rolled_back', 'erased', 'released', 'archived', 'not_allowed', 'proactive_off'])
def test_a_change_while_discord_reads_the_report_still_stops_the_reply(world, change):
    report, job_id, _ = reported(world)

    def meanwhile(id):
        if change == 'rolled_back':
            rolled_back(world.store)
        if change == 'erased':
            world.store.erase_user(str(MEMBER))
        if change == 'released':
            released(world.store)
        if change == 'archived':
            world.room.archived = True
        if change == 'not_allowed':
            world.agent.state['channels'] = []
            world.fri.workshop_id = 1
        if change == 'proactive_off':
            world.agent.state['config']['proactive'] = False
        return report

    world.room.fetch_message = AsyncMock(side_effect=meanwhile)
    text, sent = asyncio.run(tell(world.fri, job_id, OWNER))
    assert not sent and not world.sent
    with world.store.connection() as db:
        assert db.execute('SELECT state FROM requester_notices WHERE job_id=?', (job_id,)).fetchone() is None


def test_a_newer_release_before_the_tap_stops_the_reply(world):
    report, job_id, _ = reported(world)
    released(world.store)
    text, sent = asyncio.run(tell(world.fri, job_id, OWNER))
    assert not sent and not world.sent and 'newer release' in text


@pytest.mark.parametrize('change', ['proactive_off', 'not_allowed', 'archived'])
def test_a_change_while_the_claim_is_out_stops_the_reply_and_frees_it(world, change):
    report, job_id, _ = reported(world)
    api = world.fri.api.side_effect

    async def meanwhile(method, path, body=None):
        result = await api(method, path, body)
        if path.endswith('/requester-notice'):
            if change == 'proactive_off':
                world.agent.state['config']['proactive'] = False
            if change == 'not_allowed':
                world.agent.state['channels'] = []
                world.fri.workshop_id = 1
            if change == 'archived':
                world.room.archived = True
        return result

    world.fri.api.side_effect = meanwhile
    text, sent = asyncio.run(tell(world.fri, job_id, OWNER))
    assert not sent and not world.sent and text.startswith('didn’t send it')
    with world.store.connection() as db:  # Recorded as refused, so a later tap may try again.
        assert db.execute('SELECT state FROM requester_notices WHERE job_id=?', (job_id,)).fetchone()[0] == 'failed'


def test_the_outcome_is_recorded_as_the_owner_on_this_server(world):
    _, job_id, _ = reported(world)
    asyncio.run(tell(world.fri, job_id, OWNER))
    [finish] = [c.args for c in world.fri.api.await_args_list if c.args[1].endswith('/finish')]
    assert finish[2]['guild_id'] == '123' and finish[2]['user_id'] == str(OWNER) and finish[2]['state'] == 'sent'


@pytest.mark.parametrize('change', ['left', 'disabled', 'proactive_off'])
def test_a_public_report_needs_the_member_still_here_and_fri_posting(world, change):
    report, job_id, _ = reported(world)
    if change == 'left':
        world.agent.member = AsyncMock(return_value=None)
    if change == 'disabled':
        world.agent.state['config']['enabled'] = False
    if change == 'proactive_off':
        world.agent.state['config']['proactive'] = False
    text, sent = asyncio.run(tell(world.fri, job_id, OWNER))
    assert not sent and not world.sent and text.startswith('didn’t send it')
    if change == 'left':
        world.agent.member.assert_awaited_once_with(MEMBER, fresh=True)


def test_a_dm_report_waits_for_support_dms(world):
    report, job_id, _ = reported(world)
    dm = Mock(spec=discord.DMChannel, id=777, recipient=SimpleNamespace(id=MEMBER), fetch_message=AsyncMock(return_value=report))
    with world.store.connection() as db:
        db.execute('UPDATE jobs SET source_url=? WHERE id=?', (f'https://discord.com/channels/@me/777/{report.id}', job_id))
    world.fri.fetch_channel = AsyncMock(return_value=dm)
    world.agent.state['config']['support_dms'] = False
    text, sent = asyncio.run(tell(world.fri, job_id, OWNER))
    assert not sent and not world.sent and 'support dms are off' in text
    world.agent.state['config']['support_dms'] = True
    text, sent = asyncio.run(tell(world.fri, job_id, OWNER))
    assert sent and world.sent[0]['target_id'] == '777'


def owner_tap(fri, job_id):
    return SimpleNamespace(guild_id=None, type=discord.InteractionType.component, user=SimpleNamespace(id=OWNER),
                           client=fri, message=SimpleNamespace(author=SimpleNamespace(id=FRI)),
                           data={'custom_id': 'fri:tell:'+job_id}, response=SimpleNamespace(defer=AsyncMock()),
                           edit_original_response=AsyncMock(), followup=SimpleNamespace(send=AsyncMock()))


def report_thread(world, report, job_id):
    """A real discord.py guild with the report in a thread under the configured channel."""
    client = discord.Client(intents=discord.Intents.none())
    state = client._connection
    state.user = SimpleNamespace(id=FRI)
    guild = discord.Guild(state=state, data={
        'id': '123', 'name': 'synthetic', 'owner_id': str(OWNER),
        'roles': [{'id': '123', 'name': '@everyone', 'permissions': str(discord.Permissions.all().value),
                   'position': 0, 'color': 0, 'hoist': False, 'managed': False, 'mentionable': False}],
        'members': [{'user': {'id': str(FRI), 'username': 'fri', 'discriminator': '0', 'avatar': None, 'bot': True},
                     'roles': [], 'flags': 0, 'joined_at': '2026-10-01T00:00:00+00:00'}],
        'channels': [{'id': '456', 'type': 0, 'name': 'room', 'position': 0, 'permission_overwrites': []}]})
    state._guilds[guild.id] = guild
    payload = {'id': '789', 'guild_id': '123', 'parent_id': '456', 'owner_id': str(MEMBER), 'name': 'report',
               'type': 11, 'message_count': 1, 'member_count': 1,
               'thread_metadata': {'archived': False, 'auto_archive_duration': 60,
                                   'archive_timestamp': '2026-10-25T00:00:00+00:00', 'locked': False}}
    world.fri.get_channel = guild.get_channel_or_thread
    with world.store.connection() as db:
        db.execute('UPDATE jobs SET source_url=? WHERE id=?', (f'https://discord.com/channels/123/789/{report.id}', job_id))
    return client, state, guild, payload


@pytest.mark.parametrize('when', ['read', 'claim'])
def test_a_thread_whose_channel_was_deleted_gets_an_answer_and_frees_the_claim(world, monkeypatch, when):
    report, job_id, _ = reported(world)
    client, state, guild, payload = report_thread(world, report, job_id)
    guild._add_thread(discord.Thread(guild=guild, state=state, data=payload))
    deleted = lambda: state.parse_channel_delete({'guild_id': '123', 'id': '456'})  # noqa: E731

    async def fetch(*args):
        if when == 'read':
            deleted()
        return report

    monkeypatch.setattr(discord.Thread, 'fetch_message', fetch)
    api = world.fri.api.side_effect

    async def meanwhile(method, path, body=None):
        result = await api(method, path, body)
        if when == 'claim' and path.endswith('/requester-notice'):
            deleted()
        return result

    world.fri.api.side_effect = meanwhile
    event = owner_tap(world.fri, job_id)
    asyncio.run(TellReporter(job_id).callback(event))
    assert not world.sent and event.followup.send.await_args.args[0].startswith('didn’t send it')
    with world.store.connection() as db:
        row = db.execute('SELECT state FROM requester_notices WHERE job_id=?', (job_id,)).fetchone()
    assert (row[0] if row else None) == ('failed' if when == 'claim' else None)  # Either way a later tap can try.


def test_an_uncached_thread_archived_while_discord_reads_gets_no_reply(world, monkeypatch):
    report, job_id, _ = reported(world)
    client, state, guild, payload = report_thread(world, report, job_id)
    archived = {**payload, 'thread_metadata': {**payload['thread_metadata'], 'archived': True}}
    state.http.get_channel = AsyncMock(side_effect=[payload, archived])  # Discord's answer before and after.
    world.fri.fetch_channel = client.fetch_channel

    async def fetch(*args):
        state.parse_thread_update(archived)  # Never reaches the fetched copy: it isn't in the cache.
        return report

    monkeypatch.setattr(discord.Thread, 'fetch_message', fetch)
    text, sent = asyncio.run(tell(world.fri, job_id, OWNER))
    assert not sent and not world.sent and 'archived' in text


@pytest.mark.parametrize('bad', [None, [], 'job', {'source_url': 123, 'state': 'deployed', 'approved_sha': 'b'*40,
                                                   'user_id': str(MEMBER)}])
def test_a_malformed_job_from_the_controller_gets_an_answer(world, bad):
    _, job_id, _ = reported(world)
    api = world.fri.api.side_effect

    async def malformed(method, path, body=None):
        return bad if (method, path) == ('GET', '/requests/'+job_id) else await api(method, path, body)

    world.fri.api.side_effect = malformed
    event = owner_tap(world.fri, job_id)
    asyncio.run(TellReporter(job_id).callback(event))
    assert not world.sent and event.followup.send.await_args.args[0].startswith('didn’t send it')


def test_a_malformed_claim_is_still_finished(world):
    _, job_id, _ = reported(world)
    api = world.fri.api.side_effect

    async def malformed(method, path, body=None):
        result = await api(method, path, body)
        return {**result, 'job': None} if path.endswith('/requester-notice') else result

    world.fri.api.side_effect = malformed
    text, sent = asyncio.run(tell(world.fri, job_id, OWNER))
    assert not sent and not world.sent
    with world.store.connection() as db:
        assert db.execute('SELECT state FROM requester_notices WHERE job_id=?', (job_id,)).fetchone()[0] == 'failed'


def test_fri_checks_the_live_release_itself_before_claiming(world):
    """An older controller (gene #40) claims for a repair a newer release replaced; Fri doesn't ask it to."""
    _, job_id, _ = reported(world)
    released(world.store)
    text, sent = asyncio.run(tell(world.fri, job_id, OWNER))
    assert not sent and not world.sent and 'newer release' in text
    assert not [c for c in world.fri.api.await_args_list if c.args[1].endswith('/requester-notice')]


def test_a_claim_without_a_lease_sends_nothing(world):
    _, job_id, _ = reported(world)
    api = world.fri.api.side_effect

    async def leaseless(method, path, body=None):
        result = await api(method, path, body)
        return {'job': result['job']} if path.endswith('/requester-notice') else result

    world.fri.api.side_effect = leaseless
    text, sent = asyncio.run(tell(world.fri, job_id, OWNER))
    assert not sent and not world.sent and text == 'didn’t send it. tap again later.'


def test_a_finish_answer_fri_cant_read_still_gets_the_owner_an_answer(world):
    _, job_id, _ = reported(world)
    api = world.fri.api.side_effect

    async def garbled(method, path, body=None):
        result = await api(method, path, body)
        if path.endswith('/finish'):
            raise json.JSONDecodeError('Expecting value', '{"ok":', 6)  # What aiohttp's json() raises on a cut-off body.
        return result

    world.fri.api.side_effect = garbled
    event = owner_tap(world.fri, job_id)
    asyncio.run(TellReporter(job_id).callback(event))
    assert len(world.sent) == 1 and event.followup.send.await_args.args[0].startswith('told them: ')


@pytest.mark.parametrize('dm', [False, True])
def test_a_member_who_leaves_while_discord_reads_gets_no_reply(world, dm):
    report, job_id, _ = reported(world)
    gone = []
    if dm:
        dm_channel = Mock(spec=discord.DMChannel, id=777, recipient=SimpleNamespace(id=MEMBER))
        with world.store.connection() as db:
            db.execute('UPDATE jobs SET source_url=? WHERE id=?', (f'https://discord.com/channels/@me/777/{report.id}', job_id))
        world.fri.fetch_channel = AsyncMock(return_value=dm_channel)
        room = dm_channel
    else:
        room = world.room

    async def fetch(id):
        gone.append(True)  # They leave while Discord answers.
        return report

    room.fetch_message = AsyncMock(side_effect=fetch)
    world.agent.member = AsyncMock(side_effect=lambda *a, **k: None if gone else SimpleNamespace(id=MEMBER))
    text, sent = asyncio.run(tell(world.fri, job_id, OWNER))
    assert not sent and not world.sent and 'not in the server' in text


def test_fri_waits_out_another_release_going_out_even_if_the_controller_would_claim(world):
    """Gene #40 claims during a deploy; Fri reads /status and doesn't ask it to."""
    _, job_id, _ = reported(world)
    job = world.store.request('123', '30', 'message-later', 'https://discord.com/channels/123/456/1', 'Add a vote command')
    world.store.review_decision(job['id'], 'intake', request_reference(world.store.get(job['id'])), 'approve', 'owner')
    built = world.store.claim()
    world.store.finish_build(built['id'], built['lease'], {'sha': 'c'*40, 'base_sha': built['base_sha'],
                             'branch': 'codex/vote', 'tests_passed': True, 'summary': 'Vote'})
    if world.store.get(built['id'])['state'] == 'review':
        world.store.approve(built['id'], 'c'*40, 'owner')
    assert world.store.claim_deploy()['id'] == built['id']  # Going out, not recorded yet.
    text, sent = asyncio.run(tell(world.fri, job_id, OWNER))
    assert not sent and not world.sent and 'going out' in text
    assert not [c for c in world.fri.api.await_args_list if c.args[1].endswith('/requester-notice')]


def test_a_hung_mark_never_holds_up_the_heartbeat(world, monkeypatch):
    """Discord holding the 👀's fetch (a rate limit, say) doesn't stall attention or the stages after it,
    and the mark still lands once Discord answers: it is never cancelled."""
    async def scenario():
        agent = world.agent
        error_id = str(snowflake(5))
        blocked, release = asyncio.Event(), asyncio.Event()
        gene_error = SimpleNamespace(author=SimpleNamespace(id=agent.gene_id), webhook_id=None, add_reaction=AsyncMock())
        async def fetch(mid):
            blocked.set()
            await release.wait()
            return gene_error
        world.room.fetch_message.side_effect = fetch
        agent.recent[456].append({'id': error_id, 'text': 'Track Stuck error', 'gene_error': True,
                                  'user_id': str(agent.gene_id), 'at': __import__('time').time()})
        agent.run = AsyncMock(side_effect=[
            ({'action': 'investigate', 'source_id': error_id}, {}, 'completed'),
            ({'summary': 'uncertain', 'proposed_request': 'Fix the stuck track'}, {}, 'completed'),
        ])
        original = world.fri.api
        async def api(method, path, body=None):
            return agent.state if path == '/agent/state' else await original(method, path, body)
        world.fri.api = api
        monkeypatch.setattr('fri.fri_workspace.deliver_one', AsyncMock())
        monkeypatch.setattr('fri.fri_social.deliver_one', AsyncMock())
        monkeypatch.setattr(fri_fixes, 'PASS', 0.01)
        monkeypatch.setattr(fri_fixes, 'MARK', 0.01)
        agent.check_presence, agent.send_reports, agent.learn = AsyncMock(), AsyncMock(), AsyncMock()
        later = agent.check_memes = AsyncMock()
        agent.config['meme_enabled'] = True
        await asyncio.wait_for(agent.heartbeat.coro(agent), 2)
        assert blocked.is_set() and (world.fri.data / fri_fixes.FILE).exists()
        later.assert_awaited_once()
        gene_error.add_reaction.assert_not_awaited()
        release.set()
        await asyncio.wait_for(asyncio.gather(*fri_fixes.MARKING), 2)
        gene_error.add_reaction.assert_awaited_once_with('👀')
    asyncio.run(scenario())


def test_a_pass_still_running_posts_nothing_once_fri_is_paused(world, monkeypatch):
    error, job_id = filed(world)
    ship(world.store, job_id)
    async def scenario():
        blocked, release = asyncio.Event(), asyncio.Event()
        async def fetch(mid):
            blocked.set()
            await release.wait()
            return error
        world.room.fetch_message.side_effect = fetch
        monkeypatch.setattr(fri_fixes, 'PASS', 0.01)
        with pytest.raises(TimeoutError):
            await check(world.agent)
        await asyncio.wait_for(blocked.wait(), 2)
        world.agent.config['enabled'] = False  # The owner pauses Fri while Discord answers.
        release.set()
        await asyncio.wait_for(fri_fixes.RUNNING[world.agent], 2)
        assert world.sent == []
    asyncio.run(scenario())
