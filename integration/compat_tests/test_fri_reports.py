"""Learning receipt persistence, owner-only navigation and actual Discord call signatures."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec, patch

import discord
import pytest
from fastapi.testclient import TestClient
from gene.api import create_app
from gene.fri_agent_store import AgentStore
from gene.store import Store

from compat_tests.test_api import headers
from compat_tests.test_fri_exploration import archive, assigned, finish, note, row
from compat_tests.test_fri_learning import learning as learning
from compat_tests.test_fri_runtime import configured
from fri import feedback as commands
from fri.fri_agent import Agent
from fri.fri_reports import REPORT_ID, ReportButton, report_component, report_message, summary


def complete(learning, outcome='note'):
    clock, store, settings, scheduler = archive(learning)
    claim = assigned(scheduler, scheduler.claim())
    scheduler.exploration.prepare(claim['id'], '11', '', 'Read an unread slice')
    item = row(clock[0]-86400)
    if outcome == 'duplicate':
        with store.connection() as db:
            db.execute('INSERT INTO fri_explore_seen VALUES (?,?,?,?)', (item['id'], '1', '11', clock[0]))
        scheduler.finish(claim['id'], [], {}, 1, item['id'], True, {}, scanned_ids=[item['id']])
    else:
        finish(scheduler, claim, [] if outcome == 'empty' else [item],
               observations=[note(item)], error='Model unavailable' if outcome == 'error' else '')
    return clock, store, settings, scheduler, scheduler.report(claim['id'])


@pytest.mark.parametrize('outcome,expected', [
    ('note', '1 new note'), ('duplicate', 'all already checked'),
    ('empty', 'No matching messages'), ('error', 'couldn’t finish')])
def test_reports_describe_real_results_without_inventing_learning(learning, outcome, expected):
    _, _, settings, _, report = complete(learning, outcome)
    text = summary(report)
    assert expected in text
    assert len(text) < 650
    assert 'Round 1 · member 1/1' in text
    assert 'Tokens:' not in text and 'Model:' not in text
    if outcome != 'note':
        assert not report['card']['historical_notes'] and 'Saved 1' not in text
    else:
        assert 'Minecraft' in text and 'historical note' in text
    restored = AgentStore(Store(settings.db), settings)
    assert restored.report(report['id']) == report
    assert restored.reports() == [report]


@pytest.mark.parametrize('removal', ['optout', 'expiry'])
def test_card_retention_tracks_report_and_privacy_erasure(learning, removal):
    clock, store, _, scheduler, report = complete(learning)
    scheduler.delivered(report['id'])
    assert not scheduler.reports() and scheduler.report(report['id'])
    if removal == 'optout':
        store.personal_control('11', 'disable')
    else:
        clock[0] += 7*86400+1
        assert scheduler.report(report['id']) is None
        scheduler.state()  # Scheduled retention also removes the companion row.
    assert scheduler.report(report['id']) is None
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM agent_report_cards').fetchone()[0] == 0
        # Additive migration preserves the old INSERT layout for release rollback.
        db.execute('INSERT INTO agent_reports VALUES (?,?,?,0)', ('b'*32, clock[0], 'Legacy report'))
    assert scheduler.report('b'*32)['card'] == {}


def test_report_endpoint_requires_fri_service_and_configured_owner(learning):
    clock, _, settings, scheduler, report = complete(learning)
    client = TestClient(create_app(settings))
    endpoint = f"/agent/reports/{report['id']}/view"
    identity = {'guild_id': '123', 'user_id': '10'}
    for role in ('runtime', 'worker', 'deployer'):
        assert client.post(endpoint, headers=headers(role), json=identity).status_code in (401, 403)
    for identity_bad in ({'guild_id': '999', 'user_id': '10'}, {'guild_id': '123', 'user_id': '11'}):
        assert client.post(endpoint, headers=headers('fri'), json=identity_bad).status_code == 403
    result = client.post(endpoint, headers=headers('fri'), json=identity)
    assert result.status_code == 200 and result.json() == report
    assert scheduler.reports() == [report]  # Reading never acknowledges or approves anything.
    clock[0] += 7*86400+1
    assert client.post(endpoint, headers=headers('fri'), json=identity).status_code == 404


def event(report):
    # Autospec checks real discord.py method parameters, not just mock call counts.
    interaction = create_autospec(discord.Interaction, instance=True)
    fri = SimpleNamespace(owner_id=10, guild_id=123, user=SimpleNamespace(id=99),
        api=AsyncMock(return_value=report), agent=SimpleNamespace(member=AsyncMock(return_value=object())))
    interaction.client, interaction.guild_id = fri, None
    interaction.type = discord.InteractionType.component
    interaction.user = SimpleNamespace(id=10)
    interaction.message = SimpleNamespace(author=SimpleNamespace(id=99))
    interaction.data = {'custom_id': f"fri:learning:{report['id']}:0"}
    interaction.response = SimpleNamespace(defer=AsyncMock())
    interaction.followup = SimpleNamespace(send=AsyncMock())
    return fri, interaction


def test_persistent_navigation_edits_original_dm_and_returns_to_summary(learning):
    _, _, _, _, report = complete(learning)

    async def scenario():
        fri, interaction = event(report)
        content, view = report_message(report)
        assert view.is_persistent() and view.children[0].item.label == 'Details'
        assert len(content) < 650
        # Reconstruct the dynamic button from its ID as Discord does after restart.
        button = await ReportButton.from_custom_id(interaction, None, REPORT_ID.fullmatch(interaction.data['custom_id']))
        await button.callback(interaction)
        fri.agent.member.assert_awaited_once_with(10, fresh=True)
        fri.api.assert_awaited_once_with('POST', f"/agent/reports/{report['id']}/view",
                                       {'guild_id': '123', 'user_id': '10'})
        first = interaction.edit_original_response.await_args.kwargs
        assert 'Learning details' in first['content'] and first['attachments'] == []
        assert first['allowed_mentions'].everyone is False
        interaction.followup.send.assert_not_awaited()
        assert all(child.item.label not in ('Apply', 'Approve', 'Allow coding') for child in first['view'].children)
        for page in ('1', '99', 'summary'):
            interaction.data['custom_id'] = f"fri:learning:{report['id']}:{page}"
            button = await ReportButton.from_custom_id(
                interaction, None, REPORT_ID.fullmatch(interaction.data['custom_id']))
            await button.callback(interaction)
        assert interaction.edit_original_response.await_count == 4
        assert interaction.edit_original_response.await_args.kwargs['content'] == content
        interaction.followup.send.assert_not_awaited()
    asyncio.run(scenario())


def test_forged_components_and_departed_owner_cannot_read_reports(learning):
    _, _, _, _, report = complete(learning)

    async def scenario():
        for field, value in [('guild_id', 123), ('user', SimpleNamespace(id=11)),
            ('message', SimpleNamespace(author=SimpleNamespace(id=1))),
            ('type', discord.InteractionType.application_command),
            ('data', {'custom_id': f"fri:learning:{report['id']}:approve"})]:
            fri, interaction = event(report)
            setattr(interaction, field, value)
            assert not report_component(interaction, 99, 10)
            await ReportButton(report['id'], '0').callback(interaction)
            fri.api.assert_not_awaited()
            interaction.edit_original_response.assert_not_awaited()
        fri, interaction = event(report)
        fri.agent.member.return_value = None
        await ReportButton(report['id'], '0').callback(interaction)
        fri.api.assert_not_awaited()
        interaction.edit_original_response.assert_not_awaited()
        assert interaction.followup.send.await_args.kwargs['ephemeral']
    asyncio.run(scenario())


def test_expired_or_temporarily_unavailable_report_keeps_original_message_retryable(learning):
    _, _, _, _, report = complete(learning)

    async def scenario():
        fri, interaction = event(report)
        fri.api.side_effect = commands.UserFeedbackCheckFailure('Gone or unavailable')
        await ReportButton(report['id'], '0').callback(interaction)
        interaction.edit_original_response.assert_not_awaited()
        assert interaction.followup.send.await_args.kwargs['ephemeral']
    asyncio.run(scenario())


def test_real_dispatch_accepts_owner_report_dm_but_not_other_members(monkeypatch, learning):
    _, _, _, _, report = complete(learning)

    async def scenario():
        fri = configured(monkeypatch)
        fri._connection.user = SimpleNamespace(id=99)
        _, interaction = event(report)
        interaction.channel_id = 999
        with patch.object(discord.Client, 'dispatch') as dispatch:
            fri.dispatch('interaction', interaction)
            dispatch.assert_called_once()
            interaction.user.id = 11
            fri.dispatch('interaction', interaction)
            assert dispatch.call_count == 1
        await fri.close()
    asyncio.run(scenario())


def test_mobile_content_bounds_and_legacy_reports(learning):
    _, _, _, _, report = complete(learning)
    report['card'].update(member='@everyone **nickname**'*10, term='@here _term_'*10,
                          summary='@everyone **summary** 🦆'*100)
    report['body'] = ('@everyone *details* 🦆\n'*200)

    async def scenario():
        for page in ('summary', '0', '1', '99'):
            content, view = report_message(report, page)
            assert len(content.encode('utf-16-le'))//2 < 2000
            assert '@everyone' not in content and '@here' not in content
            assert view.is_persistent()
        for body in ('Legacy report', 'Learning pass interrupted.'):
            legacy = {'id': 'a'*32, 'body': body}
            content, view = report_message(legacy)
            assert 'Details' in content and view.children
            details, _ = report_message(legacy, '0')
            assert body in details
    asyncio.run(scenario())


def test_sends_compact_dm_then_acknowledges_with_no_attachment_or_model_call(learning):
    _, _, settings, scheduler, report = complete(learning)
    client = TestClient(create_app(settings))

    async def scenario():
        owner = SimpleNamespace(send=AsyncMock())

        async def api(method, path, payload=None):
            if path.endswith('/delivered'):
                owner.send.assert_awaited_once()
            response = client.request(method, path, headers=headers('fri'), json=payload)
            assert response.status_code == 200, response.text
            return response.json()

        fri = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456,
                              api=api, get_user=lambda _: owner)
        agent = Agent(fri)
        agent.run = AsyncMock()
        await agent.send_reports()
        agent.run.assert_not_awaited()
        assert owner.send.await_args.args[0] == summary(report)
        kwargs = owner.send.await_args.kwargs
        assert 'file' not in kwargs and 'embed' not in kwargs
        assert kwargs['view'].is_persistent() and kwargs['suppress_embeds']
        assert kwargs['allowed_mentions'].everyone is False
        assert not scheduler.reports() and scheduler.report(report['id'])
    asyncio.run(scenario())
