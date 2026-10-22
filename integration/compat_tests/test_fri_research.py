import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest
from gene.fri_research import ResearchReceipt
from gene.store import Conflict, Store

from compat_tests.test_fri_actions import harness
from compat_tests.test_gene_controls import control_body
from fri.fri_actions import Actions
from fri.fri_web import cite_reply

RECEIPT = {'query': 'Python release', 'answer': 'Public facts only.',
           'sources': ['https://www.python.org/downloads/'], 'checked_at': '2026-10-19T20:00:00+00:00'}


def save(store, receipt=RECEIPT, revision=0):
    return store.save_chat('123', 'public:1', '20', revision, 'fri lookup '+receipt['query'],
        'Earlier check: web_search', 'model', 'high', source=control_body()['source'], web_receipt=receipt)


def test_research_is_scoped_limited_dated_and_survives_restart(store):
    for i in range(5):
        save(store, {**RECEIPT, 'query': f'Python release {i}'})
    restarted = Store(store.path)
    saved = restarted.chat_context('123', 'public:1', '20')['web_research']
    assert [r['query'] for r in saved] == ['Python release 4', 'Python release 3', 'Python release 2']
    for guild, channel, user in [('777', 'public:1', '20'), ('123', 'public:2', '20'),
                                  ('123', 'public:1', '30'), ('123', 'private:1', '20')]:
        assert restarted.chat_context(guild, channel, user)['web_research'] == []
    assert saved[0]['checked_at'] == RECEIPT['checked_at']
    with store.connection() as db:
        db.execute('UPDATE web_research SET created=?', (time.time()-86401,))
    store.status()  # Expiration also runs when nobody chats.
    assert restarted.chat_context('123', 'public:1', '20')['web_research'] == []


@pytest.mark.parametrize('privacy', ['clear', 'disable', 'erase'])
def test_privacy_removes_saved_research_and_cannot_be_revived_in_flight(store, privacy):
    save(store)
    if privacy == 'clear':
        store.clear_chats('20')
    elif privacy == 'disable':
        store.personal_control('20', 'disable')
    else:
        store.erase_user('20')
    assert not store.chat_context('123', 'public:1', '20')['web_research']
    with pytest.raises(Conflict):
        save(store)
    if privacy != 'clear':
        save(store, revision=store.chat_context('123', 'public:1', '20')['revision'])
        assert not store.chat_context('123', 'public:1', '20')['web_research']


def test_research_cannot_store_reasoning_private_urls_or_export_old_context(store):
    for receipt in ({**RECEIPT, 'reasoning': 'private thinking'},
                    {**RECEIPT, 'sources': ['http://localhost/secrets']},
                    {**RECEIPT, 'checked_at': '2026-10-19'},):
        with pytest.raises(ValueError):
            ResearchReceipt.model_validate(receipt)
    with pytest.raises(ValueError):
        store.save_chat('123', 'public:1', '20', 0, 'hey fri', '', 'model', 'high', web_receipt=RECEIPT)


def test_normal_answers_hide_sources_without_losing_readable_labels():
    receipt = {'sources': ['https://www.python.org/downloads/', 'https://docs.python.org/3/']}
    reply = ('check the [release](https://www.python.org/downloads/) and [docs](https://docs.python.org/3/). '
             'this [invented link](https://example.com/fake) is invalid.\n\n'
             'Sources:\n[source 1](https://www.python.org/downloads/) · [source 2](https://docs.python.org/3/)')
    result = cite_reply(reply, receipt)
    assert 'check the release and docs' in result and 'https://' not in result
    assert 'source 1' not in result and 'Sources:' not in result and 'example.com' not in result
    assert result.endswith('invalid.')
    assert 'http' not in cite_reply('https://unverified.example/fake', receipt)
    assert cite_reply('plain answer', receipt) == 'plain answer'


def test_explicit_sources_are_bounded_and_do_not_need_model_to_reproduce_urls():
    sources = ['https://example.com/'+str(i)+'x'*300 for i in range(8)]
    reply = cite_reply('x'*1800, {'sources': sources}, include_sources=True)
    assert len(reply) <= 1800 and sources[0] in reply and 'saved links' in reply
    assert 'source 1' not in reply


def test_requested_links_are_clear_and_only_from_verified_receipt():
    url = 'https://safebooru.donmai.us/posts/123'
    receipt = {'sources': [url]}
    reply = cite_reply('here it is', receipt, include_sources=True)
    assert f'[view original artwork](<{url}>)' in reply
    inline = cite_reply(f'[the artwork]({url}) and [invented](https://evil.example/)', receipt, include_sources=True)
    assert inline.count(url) == 1 and 'evil.example' not in inline
    assert 'view original artwork' not in inline  # No redundant sources footer.


def test_source_followup_uses_saved_receipt_and_never_runs_another_search(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        actions = Actions(agent, 20, 1, True)
        actions.web_research = [RECEIPT]
        with patch('fri.fri_actions.lookup', new=AsyncMock()) as search:
            assert 'web_sources' in await actions.available()
            result = await actions.execute({'kind': 'web_sources', 'query': ''})
            assert result['checked_at'] == RECEIPT['checked_at'] and result['sources'] == RECEIPT['sources']
            assert 'blocked' in await actions.execute({'kind': 'web_sources', 'query': 'other conversation'})
            search.assert_not_awaited()
        await fri.close()
    asyncio.run(scenario())


def test_database_research_validation_has_no_model_or_auth_dependencies(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    import gene
    root = Path(gene.__file__).resolve().parents[2]
    code = '''
import sys
from pathlib import Path
for module in ('jwt', 'gene.subscription', 'gene.fri_chat', 'gene.fri_web'):
    sys.modules[module] = None
from gene.store import Store
store = Store(Path(sys.argv[1]))
receipt = {'query':'Python release', 'answer':'Public facts', 'sources':['https://www.python.org/'],
           'checked_at':'2026-10-19T20:00:00+00:00'}
store.save_chat('123','public:1','20',0,'Python release','checked','model','high',web_receipt=receipt)
assert store.chat_context('123','public:1','20')['web_research'] == [receipt]
'''
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path/'offline.sqlite3')], cwd=root,
        env={**os.environ, 'PYTHONPATH': str(root/'src')}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
