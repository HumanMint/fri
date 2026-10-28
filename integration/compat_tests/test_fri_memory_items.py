import asyncio
import copy
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import pytest
from gene.fri_memory_store import edit, entries, save
from gene.store import Conflict, Store

from compat_tests.test_api import headers
from compat_tests.test_fri_actions import harness
from compat_tests.test_gene_controls import control_body, controls_api  # noqa: F401
from fri.fri_memory_panel import EditMemory, MemoryView, allowed, memory_component


def remember(store, text='I like Minecraft', category='games', user='20', scope='public:1', revision=0):
    return store.save_chat('123', scope, user, revision, text, 'noted', 'model', 'high',
                          notes=[{'category': category, 'evidence': text}])


def source():
    return {**control_body()['source'], 'kind': 'discord_interaction'}


def test_preferences_keep_several_per_category_dedupe_bound_and_survive_restart(store):
    for index in range(5):
        remember(store, 'I like game '+str(index))
    for category in ('music', 'hobbies', 'conversation', 'nickname'):
        for index in range(3):
            remember(store, 'I like '+category+' '+str(index), category)
    profile = entries(Store(store.path), '123', 'public:1', '20')
    assert len(profile['entries']) == 10
    assert all(sum(row['category'] == category for row in profile['entries']) <= 3
               for category in ('games', 'music', 'hobbies', 'conversation', 'nickname'))
    latest = profile['entries'][0]
    remember(store, latest['evidence'], latest['category'])
    assert len(entries(store, '123', 'public:1', '20')['entries']) == 10
    assert latest['expires_at']-latest['at'] == 90*86400
    assert not entries(store, '123', 'public:2', '20')['entries']
    assert len(entries(store, '123', 'private:1', '20')['entries']) == 10  # Only the member sees a DM.
    assert not entries(store, '123', 'public:1', '30')['entries']


def test_legacy_note_migration_preserves_identity_evidence_and_time(tmp_path):
    path = tmp_path/'legacy.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE personal_notes (id INTEGER PRIMARY KEY,guild_id TEXT,channel_id TEXT,user_id TEXT,'
            'category TEXT,evidence TEXT,at REAL,UNIQUE(guild_id,channel_id,user_id,category))')
        db.execute('INSERT INTO personal_notes VALUES (7,?,?,?,?,?,?)',
                   ('123', 'public:1', '20', 'games', 'I like chess', time.time()))
    store = Store(path)
    assert entries(store, '123', 'public:1', '20')['entries'][0]['id'] == 7
    remember(store)
    assert len(entries(Store(path), '123', 'public:1', '20')['entries']) == 2


def test_remove_preserves_other_items_blocks_old_learning_and_fences_inflight(store):
    remember(store)
    remember(store, 'I like Terraria')
    state = entries(store, '123', 'public:1', '20')
    minecraft = next(row for row in state['entries'] if row['evidence'] == 'I like Minecraft')
    old_time = minecraft['at']
    result = edit(store, '123', 'public:1', '20', minecraft['id'], 0, source(), 'remove')
    assert [row['evidence'] for row in result['entries']] == ['I like Terraria']
    assert result['revision'] == 1
    assert not store.chat_context('123', 'public:1', '20')['turns']
    with store.connection() as db:
        assert not save(db, '123', 'public:1', '20', ('games', 'I like Minecraft'), old_time)
        assert not save(db, '123', 'public:1', '20', ('games', 'I like Minecraft modpacks'), old_time)
    with pytest.raises(Conflict):
        remember(store, 'I like stale responses')
    with pytest.raises(ValueError, match='changed'):
        edit(store, '123', 'public:1', '20', minecraft['id'], 0, source(), 'remove')
    # A new explicit disclosure may renew the preference after a deliberate removal.
    remember(store, revision=1)
    assert len(entries(store, '123', 'public:1', '20')['entries']) == 2
    store.personal_control('20', 'disable')
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM personal_note_removals').fetchone()[0] == 0
    assert not entries(store, '123', 'public:1', '20')['entries']


def test_replace_is_explicit_and_does_not_remove_other_scopes(store):
    remember(store)
    remember(store, 'I like private chess', scope='private:1')
    note = entries(store, '123', 'public:1', '20')['entries'][0]
    result = edit(store, '123', 'public:1', '20', note['id'], 0, source(), 'replace', 'I prefer Terraria')
    assert result['entries'][0]['evidence'] == 'I prefer Terraria'
    assert entries(store, '123', 'private:1', '20')['entries'][0]['evidence'] == 'I like private chess'
    with store.connection() as db:
        assert not save(db, '123', 'public:1', '20', ('games', 'I like Minecraft'), note['at'])


@pytest.mark.parametrize('change', ['user', 'conversation', 'source_user', 'source_channel', 'source_guild', 'stale', 'expired', 'sensitive', 'quoted'])
def test_item_edits_fail_closed_at_scope_source_age_and_content_boundaries(store, change):
    remember(store)
    note = entries(store, '123', 'public:1', '20')['entries'][0]
    user, scope, event, text = '20', 'public:1', source(), 'I like chess'
    if change == 'user':
        user = event['user_id'] = '30'
    elif change == 'conversation':
        scope, event['channel_id'] = 'public:2', '2'
    elif change.startswith('source_'):
        event[change[7:]+'_id'] = '999'
    elif change == 'stale':
        event['id'] = str(int(event['id'])-(301000 << 22))
    elif change == 'expired':
        with store.connection() as db:
            db.execute('UPDATE personal_notes SET at=?', (time.time()-91*86400,))
    elif change == 'sensitive':
        text = 'I like sharing my password'
    elif change == 'quoted':
        text = '"I like chess"'
    with pytest.raises(ValueError):
        edit(store, '123', scope, user, note['id'], 0, event, 'replace', text)


def test_memory_api_requires_fri_scope_fresh_actor_and_revision(controls_api):  # noqa: F811
    client, controls = controls_api
    remember(controls.store)
    scope = {key: control_body()[key] for key in ('guild_id', 'channel_id', 'user_id', 'conversation_id')}
    for role in ('runtime', 'worker', 'deploy'):
        assert client.post('/chat/memory/notes', headers=headers(role), json=scope).status_code == 401
    state = client.post('/chat/memory/notes', headers=headers('fri'), json=scope).json()
    identifier = state['entries'][0]['id']
    body = {**scope, 'revision': 0, 'source': source(), 'action': 'remove'}
    path = '/chat/memory/notes/'+str(identifier)
    assert client.post(path, headers=headers('runtime'), json=body).status_code == 401
    assert client.post(path, headers=headers('fri'), json={**body, 'source': {**source(), 'user_id': '30'}}).status_code == 400
    assert client.post(path, headers=headers('fri'), json=body).status_code == 200
    assert client.post(path, headers=headers('fri'), json=body).status_code == 400


def test_memory_controls_bind_member_audience_and_modal_and_send_exact_evidence(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channels, _ = harness(monkeypatch, tmp_path)
        fri._connection.user = SimpleNamespace(id=99)
        state = {'enabled': True, 'revision': 2, 'entries': [
            {'id': 7, 'category': 'games', 'evidence': 'I like chess', 'expires_at': time.time()+90*86400}]}
        scope = {'guild_id': '123', 'user_id': '20', 'channel_id': '1', 'conversation_id': 'public:1'}
        interaction = Mock(spec=discord.Interaction, id=int(source()['id']), guild_id=123, channel_id=1,
            channel=channels[1], user=SimpleNamespace(id=20, display_name='friend'),
            message=SimpleNamespace(author=fri.user), response=SimpleNamespace(send_modal=AsyncMock()))
        view = MemoryView(fri, scope, state, selected=7)
        try:
            assert await view.interaction_check(interaction)  # The reply's own button: test_fri_live_buttons.py.
            wrong = copy.copy(interaction)
            wrong.user = SimpleNamespace(id=30)
            assert not await view.interaction_check(wrong)
            wrong = copy.copy(interaction)
            wrong.channel_id = 2
            assert not await view.interaction_check(wrong)
            await view.replace(interaction)
            modal = interaction.response.send_modal.await_args.args[0]
            assert isinstance(modal, EditMemory)
            modal.preference._value = 'I prefer Terraria'
            fri.api = AsyncMock(return_value={**state, 'revision': 3})
            agent.forget = Mock()
            with patch('fri.fri_memory_panel.show', new=AsyncMock()) as show:
                await modal.on_submit(interaction)
            body = fri.api.await_args.args[2]
            assert body['evidence'] == 'I prefer Terraria' and body['revision'] == 2
            assert body['source']['user_id'] == '20' and body['conversation_id'] == 'public:1'
            agent.forget.assert_called_once_with(20)
            show.assert_awaited_once()
            agent.member.return_value = None
            assert not await allowed(fri, interaction, scope)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_dm_memory_dispatch_only_admits_our_components_and_bound_modal():
    interaction = SimpleNamespace(guild_id=None, type=SimpleNamespace(value=3),
        message=SimpleNamespace(author=SimpleNamespace(id=99)),
        data={'custom_id': 'fri:memory:open:'+'a'*32})
    assert memory_component(interaction, 99)
    interaction.message.author.id = 30
    assert not memory_component(interaction, 99)
    interaction.type.value = 5
    interaction.data['custom_id'] = 'fri:memory:edit:'+'a'*32
    assert memory_component(interaction, 99)  # The registered modal independently binds its member and scope.
    interaction.data['custom_id'] = 'other:memory:edit:'+'a'*32
    assert not memory_component(interaction, 99)
