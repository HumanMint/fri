"""Item edits invalidate derivations without revoking confirmed reminder delivery."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from gene.api import create_app
from gene.config import Settings
from gene.fri_memory_store import edit, entries
from gene.fri_social_store import Social
from gene.store import Conflict, Store

from compat_tests.test_fri_social import DM, api, confirmed, due, future, reminder, runtime, source  # noqa: F401
from fri.fri_social import deliver_one


def preference(store, revision=0):
    store.save_chat('123', 'public:456', '10', revision, 'I like chess', 'noted', 'model', 'high',
                    notes=[{'category': 'games', 'evidence': 'I like chess'}])
    return entries(store, '123', 'public:456', '10')['entries'][0]


def change_preference(store, note, action='remove', revision=0):
    return edit(store, '123', 'public:456', '10', note['id'], revision,
                source(40+revision, public=True, interaction=True).model_dump(), action,
                'I prefer Terraria' if action == 'replace' else '')


@pytest.mark.parametrize('action', ['remove', 'replace'])
@pytest.mark.parametrize('stage', ['scheduled', 'claimed', 'sending'])
def test_item_edit_preserves_confirmed_state_and_lease_across_restart(store, stage, action):
    social = Social(store)
    row = confirmed(social, 1)
    note = preference(store)
    due(store, row['id'])
    if stage != 'scheduled':
        claim = social.reminder_claim()
        if stage == 'sending':
            social.reminder_sending(row['id'], claim['lease'])
    with store.connection() as db:
        before = social.reminder_row(db, row['id'])
    state = change_preference(store, note, action)
    assert state['revision'] == 1
    social = Social(Store(store.path))
    with store.connection() as db:
        after = social.reminder_row(db, row['id'])
    assert after == {**before, 'revision': 1}
    if stage == 'scheduled':
        claim = social.reminder_claim()
        assert claim['id'] == row['id']
    if stage != 'sending':
        assert social.reminder_sending(row['id'], claim['lease'])['state'] == 'sending'
    assert social.reminder_claim() is None
    with pytest.raises(ValueError):
        social.reminder_sending(row['id'], claim['lease'])
    social.reminder_finish(row['id'], claim['lease'], 'sent', '987')
    assert Social(Store(store.path)).reminder_claim() is None
    assert social.reminder_list(DM)['items'][0]['state'] == 'sent'


def test_item_edit_keeps_pending_preview_and_inflight_chat_invalid(store):
    social = Social(store)
    pending = reminder(social, 1)
    social.bind('reminder', pending['id'], DM, '888')
    note = preference(store)
    change_preference(store, note)
    with pytest.raises(Conflict):
        store.save_chat('123', 'private:999', '10', 0, 'old input', 'old reply', 'model', 'high')
    for revision in (0, 1):
        with pytest.raises(ValueError):
            social.reminder_decide(pending['id'], DM, source(5+revision, interaction=True), revision, 'confirm', '888')
    with store.connection() as db:
        assert social.reminder_row(db, pending['id'])['revision'] == 0
    due(store, pending['id'])
    assert Social(Store(store.path)).reminder_claim() is None


@pytest.mark.parametrize('action', ['remove', 'replace'])
def test_lab_guild_item_edit_preserves_main_guild_reminder_without_advancing_other_member(tmp_path, action):
    settings = Settings(tmp_path/'lab.db', '123', '10', 'runtime', 'worker', 'deploy',
        workshop_channel_id='456', fri_token='fri', test_guild_id='321', test_channel_id='654')
    client = TestClient(create_app(settings))
    store = client.app.state.store
    social = Social(store)
    row = confirmed(social, 1)
    other = {**DM, 'user_id': '11', 'conversation_id': 'private:1000'}
    other_row = social.reminder_prepare(other, source(2, user='11', channel='1000'), 0, 'keep my reminder', future(), 'UTC')
    social.bind('reminder', other_row['id'], other, '889')
    social.reminder_decide(other_row['id'], other, source(3, user='11', channel='1000', interaction=True), 0, 'confirm', '889')
    store.save_chat('321', 'public:654', '10', 0, 'I like chess', 'noted', 'model', 'high',
        notes=[{'category': 'games', 'evidence': 'I like chess'}])
    note = entries(store, '321', 'public:654', '10')['entries'][0]
    event = source(4, public=True, interaction=True).model_copy(update={'guild_id': '321', 'channel_id': '654'})
    result = client.post('/chat/memory/notes/'+str(note['id']), headers={'Authorization': 'Bearer fri'},
        json={'guild_id': '321', 'channel_id': '654', 'conversation_id': 'public:654', 'user_id': '10',
              'revision': 0, 'source': event.model_dump(), 'action': action,
              'evidence': 'I prefer Terraria' if action == 'replace' else ''})
    assert result.status_code == 200 and result.json()['revision'] == 1
    with store.connection() as db:
        assert social.reminder_row(db, row['id'])['revision'] == 1
        assert social.reminder_row(db, other_row['id'])['revision'] == 0
    due(store, row['id'])
    claim = Social(Store(store.path)).reminder_claim()
    assert claim['id'] == row['id'] and claim['body'] == 'update the mods'


@pytest.mark.parametrize('stage', ['stale', 'cancelled', 'sent', 'failed', 'uncertain', 'expired_claim', 'expired_send'])
def test_item_edit_never_revives_stale_closed_or_expired_delivery(store, stage):
    social = Social(store)
    row = confirmed(social, 1)
    note = preference(store)
    due(store, row['id'])
    if stage == 'stale':
        with store.connection() as db:
            db.execute('UPDATE fri_reminders SET revision=-1 WHERE id=?', (row['id'],))
    elif stage == 'cancelled':
        social.reminder_decide(row['id'], DM, source(3), 0, 'cancel')
    else:
        claim = social.reminder_claim()
        if stage in ('sent', 'uncertain', 'expired_send'):
            social.reminder_sending(row['id'], claim['lease'])
        if stage.startswith('expired_'):
            with store.connection() as db:
                db.execute('UPDATE fri_reminders SET lease_until=0 WHERE id=?', (row['id'],))
        else:
            social.reminder_finish(row['id'], claim['lease'], stage, '987' if stage == 'sent' else '')
    change_preference(store, note)
    with store.connection() as db:
        actual = social.reminder_row(db, row['id'])
        assert actual['revision'] == (-1 if stage == 'stale' else 0)
        if stage.startswith('expired_'):
            assert actual['state'] == ('failed' if stage == 'expired_claim' else 'uncertain')
            assert actual['lease'] == claim['lease'] and actual['lease_until'] == 0
    assert Social(Store(store.path)).reminder_claim() is None
    if stage == 'stale':
        assert social.reminder_list(DM)['items'][0]['state'] == 'cancelled'
    note = preference(store, 1)
    change_preference(store, note, revision=1)
    assert social.reminder_claim() is None


@pytest.mark.parametrize('stage', ['scheduled', 'claimed', 'sending'])
def test_global_forget_after_item_edit_erases_even_preserved_confirmations(store, stage):
    social = Social(store)
    row = confirmed(social, 1)
    note = preference(store)
    due(store, row['id'])
    if stage != 'scheduled':
        claim = social.reminder_claim()
        if stage == 'sending':
            social.reminder_sending(row['id'], claim['lease'])
    change_preference(store, note)
    store.personal_control('10', 'disable')
    assert Social(Store(store.path)).reminder_claim() is None
    assert not social.reminder_list(DM)['items']
    if stage != 'scheduled':
        with pytest.raises(KeyError):
            social.reminder_sending(row['id'], claim['lease'])
        with pytest.raises(KeyError):
            social.reminder_finish(row['id'], claim['lease'], 'sent', '987')


@pytest.mark.parametrize('stage', ['scheduled', 'claimed', 'sending'])
def test_item_edit_during_real_delivery_retains_exact_confirmation_and_send_once(api, tmp_path, monkeypatch, stage):  # noqa: F811
    async def scenario():
        fri, social, row = runtime(api, tmp_path)
        note = preference(social.store)
        real_api = fri.api
        edited = False
        async def edit_during_delivery(method, path, body=None):
            nonlocal edited
            marker = '/claim' if stage == 'scheduled' else '/sending' if stage == 'claimed' else '/finish'
            if path.endswith(marker) and not edited:
                change_preference(social.store, note)
                edited = True
            return await real_api(method, path, body)
        fri.api = edit_during_delivery
        sender = AsyncMock(return_value={'id': '987'})
        monkeypatch.setattr('fri.fri_social.send_once', sender)
        await deliver_one(fri)
        assert edited and sender.await_count == 1
        assert sender.await_args.kwargs['embed']['description'] == 'update the mods'
        assert social.reminder_list(DM)['items'][0]['state'] == 'sent'
        await deliver_one(fri)
        assert Social(Store(social.store.path)).reminder_claim() is None and sender.await_count == 1
    asyncio.run(scenario())
