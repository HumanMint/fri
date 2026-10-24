"""Save-after-delivery fences: what is checked before Discord, and what is recorded after."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_runtime import configured
from fri.fri import MAX_PROVENANCE, UNATTRIBUTED, UNAVAILABLE
from fri.fri_chat import ChatResult
from fri.fri_errors import ControllerRejected
from fri.panels import PrivatePanels


def staged(fri, context, result=None):
    scope = {'guild_id': '123', 'channel_id': '1', 'user_id': '20', 'conversation_id': 'public:1'}
    source = SimpleNamespace(model_dump=lambda: {'kind': 'discord_message', 'id': '1', 'user_id': '20'})
    return fri.stage_turn(scope, source, 'yo', {'revision': 0, 'model': 'm', 'effort': 'high', **context},
                          result or ChatResult(reply='an answer', status='completed'))


def test_overflowing_provenance_refuses_instead_of_dropping_sources(monkeypatch):
    fri = configured(monkeypatch)
    rows = [{'user_id': str(1000+n), 'revision': 0} for n in range(MAX_PROVENANCE+1)]
    from fri import feedback as commands
    with pytest.raises(commands.UserFeedbackCheckFailure, match='too many people'):
        staged(fri, {'recent': rows})
    assert len(staged(fri, {'recent': rows[:MAX_PROVENANCE]}).turn['provenance']) == MAX_PROVENANCE


def test_unattributed_fri_reference_answers_live_but_retains_nothing_derived(monkeypatch):
    async def scenario():
        fri = configured(monkeypatch)
        fri._connection.user = SimpleNamespace(id=99)
        fri.api = AsyncMock(return_value={'messages': {}})  # Unknown, e.g. sent before provenance existed.
        context = {'reply_target': [{'source': {'id': '777', 'user_id': '99'}, 'text': 'old fri answer',
                                     'privacy_revision': None}]}
        scope = {'guild_id': '123', 'user_id': '20'}
        await fri.resolve_references(scope, context)
        assert context['unattributed_reference'] and context['reference_provenance'] == []
        result = staged(fri, context)
        assert result.reply == 'an answer'  # The live reply is unchanged.
        assert result.turn['reply'] == UNATTRIBUTED and not result.turn['verbatim'] and result.echo is None
        fri.api = AsyncMock(return_value={'messages': {'777': {'verbatim': False, 'provenance': []}}})
        context.pop('unattributed_reference')
        await fri.resolve_references(scope, context)
        assert context['unattributed_reference']  # A breadcrumbed private read is never re-derived.
        fri.api = AsyncMock(return_value={'messages': {'777': {'verbatim': True, 'provenance': [
            {'user_id': '30', 'revision': 2}, {'user_id': '40', 'revision': 0}]}}})
        context.pop('unattributed_reference')
        await fri.resolve_references(scope, context)
        assert 'unattributed_reference' not in context
        assert staged(fri, context).turn['provenance'] == [{'user_id': '30', 'revision': 2},
                                                          {'user_id': '40', 'revision': 0}]
        await fri.close()
    asyncio.run(scenario())


def test_post_delivery_record_failure_keeps_the_delivered_reply(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, _, channel, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        controller = fri.api

        async def api(method, path, payload=None):
            if path == '/chat/turn':
                calls.append((path, payload))
                raise ControllerRejected('synthetic refusal after delivery')
            return await controller(method, path, payload)

        fri.api = api
        fri.chat_once.return_value = ChatResult(status='completed', reply='delivered anyway')
        event = message('fri yo')
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            event.reply.assert_awaited_once()
            assert event.reply.await_args.args[0] == 'delivered anyway'
            paths = [path for path, _ in calls]
            assert paths.index('/chat/turn/check') < paths.index('/chat/turn')  # Checked, delivered, then saved.
            assert not store.chat_context('123', 'public:1', '20')['turns']
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_superseded_panel_answer_is_never_recorded(monkeypatch):
    async def scenario():
        fri = configured(monkeypatch)
        fri.private_panels = PrivatePanels()
        interaction = SimpleNamespace(id=100, user=SimpleNamespace(id=20, display_name='bo'), guild_id=123,
            channel_id=456, response=SimpleNamespace(is_done=lambda: True),
            message=SimpleNamespace(id=1234, flags=SimpleNamespace(ephemeral=True)),
            edit_original_response=AsyncMock(return_value=SimpleNamespace(id=1234)),
            delete_original_response=AsyncMock())

        async def talk(*args, **kwargs):
            fri.private_panels.latest[(123, 20)] = 200  # A newer panel interaction wins meanwhile.
            return ChatResult(reply='this answer was never displayed', status='completed',
                              turn={'reply': 'this answer was never displayed'})

        fri.talk = AsyncMock(side_effect=talk)
        fri.api = AsyncMock(return_value={'ok': True})
        await fri.chat_interaction(interaction, 'a question')
        assert not fri.api.await_args_list  # Nothing displayed, so nothing recorded.
        await fri.close()
    asyncio.run(scenario())


def test_displayed_panel_answer_is_recorded(monkeypatch):
    async def scenario():
        fri = configured(monkeypatch)
        fri.private_panels = PrivatePanels()
        interaction = SimpleNamespace(id=100, user=SimpleNamespace(id=20, display_name='bo'), guild_id=123,
            channel_id=456, response=SimpleNamespace(is_done=lambda: True),
            message=SimpleNamespace(id=1234, flags=SimpleNamespace(ephemeral=True)),
            edit_original_response=AsyncMock(return_value=SimpleNamespace(id=1234)),
            delete_original_response=AsyncMock())
        fri.talk = AsyncMock(return_value=ChatResult(reply='shown', status='completed', turn={'reply': 'shown'}))
        fri.api = AsyncMock(return_value={'ok': True})
        await fri.chat_interaction(interaction, 'a question')
        assert [call.args[1] for call in fri.api.await_args_list] == ['/chat/turn']
        await fri.close()
    asyncio.run(scenario())



def test_unresolved_fri_reference_is_withheld_from_the_model(monkeypatch, tmp_path):
    from compat_tests.test_fri_endings import source
    async def scenario():
        fri, agent, store, _, channel, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        old = message('ash is moving to denver next month', user=99, age=20)
        old.author.bot = True
        store.save_chat('123', 'public:1', '20', 0, 'where is ash moving', old.content, 'm', 'high',
                        source=source(age=30, channel='1'), provenance=[{'user_id': '30', 'revision': 0}],
                        reply_message_id=str(old.id), verbatim=True)
        store.personal_control('30', 'disable')  # The Fri reply's source erases.
        event = message('fri wait where?', user=40)
        event.reference = SimpleNamespace(message_id=old.id, channel_id=channel.id)
        fri.chat_once.return_value = ChatResult(status='completed', reply='which one? i dont have that anymore')
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            context = fri.chat_once.await_args.args[1]
            assert context['reply_target'][0]['text'] == UNAVAILABLE and context['unattributed_reference']
            assert 'denver' not in str(context['reply_target'])
            event.reply.assert_awaited_once()
            saved = store.chat_context('123', 'public:1', '40')['turns']
            assert saved and saved[-1]['reply'] == UNATTRIBUTED
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_nested_diagnosis_is_kept_only_as_a_breadcrumb(monkeypatch, tmp_path):
    from gene.control_store import Controls
    async def scenario():
        fri, agent, store, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        event = message('fri play a song')
        Controls(store).poll(['play'])
        controller = fri.api

        async def api(method, path, payload=None):
            if path.startswith('/controls/') and path.endswith('/result'):
                return {'done': True, 'uncertain': 'Gene restarted before confirming the command'}
            return await controller(method, path, payload)

        fri.api = api
        fri.chat_once.side_effect = [ChatResult(status='completed', action={
            'kind': 'gene_command', 'action': 'play', 'argument': 'a song'}),
            ChatResult(status='completed', reply='diagnostic evidence marker')]
        agent.run = AsyncMock(side_effect=[
            ({'action': 'investigate', 'source_id': str(event.id)}, {}, 'completed'),
            ({'summary': 'diagnostic evidence marker', 'proposed_request': ''}, {}, 'completed')])
        try:
            with patch('fri.fri_actions.asyncio.sleep', new=AsyncMock()):
                result = await fri.talk(123, 1, 20, event.content, 'public', event=event)
            assert fri.chat_once.await_args.args[1]['action_result']['diagnostic']
            assert result.reply == 'diagnostic evidence marker'
            assert result.turn['verbatim'] is False and 'diagnostic evidence' not in result.turn['reply']
            assert result.echo is None
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_attachment_fallback_records_what_discord_showed(monkeypatch, tmp_path):
    from fri.fri_image_share import send_reply
    async def scenario():
        fri, _, store, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        event = message('fri explain this')
        fri.chat_once.return_value = ChatResult(status='completed', reply='a lovely red panda in a tree')
        shown = 'i checked this one, but can’t attach it here. credit'
        event.reply.return_value = SimpleNamespace(id=123456, content=shown)
        try:
            result = await fri.talk(123, 1, 20, event.content, 'public', event=event)
            event.guild.filesize_limit = 0
            result.media = SimpleNamespace(body=b'x', guard=SimpleNamespace(current=AsyncMock(return_value=True)),
                                           attribution=lambda: 'credit')
            sent = await send_reply(event, result)
            await fri.record_turn(result, sent)
            assert store.chat_context('123', 'public:1', '20')['turns'][-1]['reply'] == shown
            assert store.message_sources('123', ['123456'])['123456']['verbatim'] is True
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_tool_answer_survives_a_post_delivery_save_failure_without_its_receipt(monkeypatch, tmp_path):
    import datetime as dt
    async def scenario():
        fri, agent, store, _, _, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        controller = fri.api

        async def api(method, path, payload=None):
            if path == '/chat/turn':
                raise ControllerRejected('synthetic refusal after delivery')
            return await controller(method, path, payload)

        fri.api = api
        question = 'what is the current Python release?'
        receipt = {'kind': 'web_search', 'query': question, 'answer': 'public evidence',
                   'sources': ['https://www.python.org/downloads/'], 'checked_at': dt.datetime.now(dt.timezone.utc).isoformat()}
        fri.chat_once.side_effect = [ChatResult(status='completed', action={'kind': 'web_search', 'query': question}),
                                     ChatResult(status='completed', reply='3.14 is current')]
        event = message('fri '+question)
        try:
            with patch('fri.fri_actions.lookup', new=AsyncMock(return_value=(receipt, {}, 'completed'))), \
                    patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            assert event.reply.await_args.args[0].startswith('3.14 is current')
            context = store.chat_context('123', 'public:1', '20')
            assert not context['turns'] and not context['web_research']
        finally:
            await fri.close()
    asyncio.run(scenario())


MARKER = 'ash is moving to denver next month'


def test_history_derived_draft_is_refused_after_its_source_erases(monkeypatch, tmp_path):
    from fri.fri_requests import DraftView
    async def scenario():
        fri, agent, store, _, channel, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        original = message(MARKER, user=30, age=40)
        first = message('fri explain that', age=20)
        first.reference = SimpleNamespace(message_id=original.id, channel_id=channel.id)
        first.reply.return_value = SimpleNamespace(id=first.id+1, content=MARKER)
        follow = message('fri draft a feature request using that earlier discussion')
        follow.reply.return_value = SimpleNamespace(id=follow.id+1, content='here is the draft')
        fri.chat_once.side_effect = [ChatResult(status='completed', reply=MARKER),
                                     ChatResult(status='completed', reply='here is the draft',
                                                draft='Add a moving checklist because '+MARKER)]
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(first)
                agent.recent.clear()  # The follow-up relies on saved history only.
                await agent.handle(follow)
            view = follow.reply.await_args.kwargs['view']
            assert view.fence['provenance'] == [{'user_id': '30', 'revision': 0}]
            await view.fenced()  # Still valid before the erasure.
            store.erase_user('30')
            with pytest.raises(ControllerRejected):
                await view.fenced()  # Preview refuses.
            reviewed = DraftView(fri, 20, view.draft, reviewed=True, source_id=view.source_id, fence=view.fence)
            click = SimpleNamespace(user=SimpleNamespace(id=20), guild_id=123, channel_id=1, channel=channel,
                message=SimpleNamespace(author=fri.user), response=SimpleNamespace(defer=AsyncMock()),
                edit_original_response=AsyncMock())
            with pytest.raises(ControllerRejected):
                await reviewed.submit(click)
            with store.connection() as db:
                assert not db.execute('SELECT 1 FROM jobs WHERE user_id=?', ('20',)).fetchone()
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_delivered_preview_is_recorded_even_if_binding_fails(monkeypatch, tmp_path):
    import datetime as dt
    async def scenario():
        fri, agent, store, _, _, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        event = message('fri plan minecraft tomorrow at 7 or 8 UTC')
        event.reply.return_value = SimpleNamespace(id=event.id+1)
        day = dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=2)
        times = '\n'.join(day.replace(hour=h, minute=0).strftime('%Y-%m-%dT%H:%M') for h in (19, 20))
        fri.chat_once.return_value = ChatResult(status='completed', action={
            'kind': 'plan_prepare', 'title': 'minecraft', 'timezone': 'UTC', 'local_times': times})
        controller = fri.api

        async def api(method, path, payload=None):
            if path.startswith('/social/plans/') and path.endswith('/bind'):
                raise ControllerRejected('synthetic bind failure after Discord accepted the preview')
            return await controller(method, path, payload)

        fri.api = api
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()), pytest.raises(ControllerRejected):
                await agent.handle(event)
            event.reply.assert_awaited_once()
            assert store.chat_context('123', 'public:1', '20')['turns']  # The delivered card is recorded.
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_invalidated_fri_reply_is_pruned_from_the_raw_channel_cache(monkeypatch, tmp_path):
    import time
    async def scenario():
        fri, agent, store, _, _, _, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        agent.recent[1].append({'id': '555', 'user_id': '99', 'text': MARKER, 'name': 'Fri', 'at': time.time(),
                                'gene_error': False, 'fri': True, 'provenance': [{'user_id': '30', 'revision': 0}]})
        try:
            store.erase_user('30')
            assert await agent.context_messages(1) == []
            assert MARKER not in str(agent.messages(1))
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_draft_paging_rechecks_the_fence(monkeypatch, tmp_path):
    from fri.fri_requests import DraftView
    async def scenario():
        fri, _, store, _, channel, _, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        try:
            store.save_chat('123', 'public:1', '20', 0, 'fri what did ash say?', MARKER, 'm', 'high',
                            source=history_source(), provenance=[{'user_id': '30', 'revision': 0}])
            fence = {'guild_id': '123', 'conversation_id': 'public:1', 'user_id': '20', 'revision': 0,
                     'provenance': [{'user_id': '30', 'revision': 0}]}
            view = DraftView(fri, 20, 'Add a checklist. '+'ordinary detail '*50+'\n'+MARKER,
                             reviewed=True, fence=fence)
            assert len(view.pages) > 1 and MARKER not in view.content
            click = SimpleNamespace(user=SimpleNamespace(id=20), guild_id=123, channel_id=1, channel=channel,
                message=SimpleNamespace(author=fri.user), response=SimpleNamespace(edit_message=AsyncMock()))
            store.erase_user('30')
            before = len(calls)
            with pytest.raises(ControllerRejected):
                await view.next(click)
            click.response.edit_message.assert_not_awaited()
            assert view.page == 0 and [p for p, _ in calls[before:]] == ['/chat/turn/check']
        finally:
            await fri.close()
    asyncio.run(scenario())


def history_source(age=25):
    from compat_tests.test_fri_endings import source
    return source(age=age, channel='1')


def queue_play(store, argument_seen):
    from gene.control_store import Controls
    controls = Controls(store)
    controls.poll(['play', 'volume'])  # Gene is online, so commands are offered.

    async def deliver(_):
        claim = controls.poll(['play', 'volume'])
        if claim:
            argument_seen.append(claim['argument'])
            controls.finish(claim['id'], claim['lease'], {'output': 'queued it'})
    return controls, deliver


def test_history_derived_command_argument_is_refused(monkeypatch, tmp_path):
    async def scenario():
        fri, _, store, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        seen = []
        controls, deliver = queue_play(store, seen)
        store.save_chat('123', 'public:1', '20', 0, 'fri what did ash say?', MARKER, 'm', 'high',
                        source=history_source(), provenance=[{'user_id': '30', 'revision': 0}])
        event = message('fri play their recommendation')
        fri.chat_once.side_effect = [
            ChatResult(status='completed', action={'kind': 'gene_command', 'action': 'play', 'argument': MARKER}),
            ChatResult(status='completed', reply='say the song name and ill queue it')]
        try:
            with patch('fri.fri_actions.asyncio.sleep', new=deliver):
                result = await fri.talk(123, 1, 20, event.content, 'public', event=event)
            receipt = fri.chat_once.await_args.args[1]['action_results'][-1]
            assert 'own message' in str(receipt)
            assert not seen and not controls.recent('20', '123', 'public:1')
            assert result.reply == 'say the song name and ill queue it'
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize(('text', 'action', 'argument'), [
    ('fri play never gonna give you up by rick astley', 'play', 'Never Gonna Give You Up - Rick Astley'),
    ('fri play Halo by Beyoncé', 'play', 'Halo Beyonce'),
    ('fri turn it up', 'volume', '70')])
def test_own_command_words_still_run_with_peers_in_context(monkeypatch, tmp_path, text, action, argument):
    async def scenario():
        fri, _, store, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        seen = []
        controls, deliver = queue_play(store, seen)
        store.save_chat('123', 'public:1', '20', 0, 'fri what did ash say?', MARKER, 'm', 'high',
                        source=history_source(), provenance=[{'user_id': '30', 'revision': 0}])
        event = message(text)
        fri.chat_once.side_effect = [
            ChatResult(status='completed', action={'kind': 'gene_command', 'action': action, 'argument': argument}),
            ChatResult(status='completed', reply='done')]
        try:
            with patch('fri.fri_actions.asyncio.sleep', new=deliver):
                result = await fri.talk(123, 1, 20, event.content, 'public', event=event)
            assert seen == [argument] and result.turn['provenance'] == [{'user_id': '30', 'revision': 0}]
            assert [row['grounded'] for row in controls.recent('20', '123', 'public:1')] == [True]
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('erasure', ['after_submission', 'between_fence_and_write'])
def test_submitted_derived_draft_keeps_its_sources(monkeypatch, tmp_path, erasure):
    from fri.fri_requests import DraftView
    async def scenario():
        fri, _, store, _, channel, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        store.save_chat('123', 'public:1', '20', 0, 'fri what did ash say?', MARKER, 'm', 'high',
                        source=history_source(), provenance=[{'user_id': '30', 'revision': 0}])
        event = message('fri draft a feature request from that earlier discussion')
        fri.chat_once.return_value = ChatResult(status='completed', reply='here is the draft',
                                                draft='Add a moving checklist because '+MARKER)
        try:
            result = await fri.talk(123, 1, 20, event.content, 'public', event=event)
            view = DraftView(fri, 20, result.draft, reviewed=True, fence=result.turn)
            click = SimpleNamespace(user=SimpleNamespace(id=20), guild_id=123, channel_id=1, channel=channel,
                message=SimpleNamespace(author=fri.user), response=SimpleNamespace(defer=AsyncMock()),
                edit_original_response=AsyncMock())
            controller = fri.api
            async def api(method, path, payload=None):
                if path == '/requests' and erasure == 'between_fence_and_write':
                    store.erase_user('30')  # After the fence passed, before the job exists.
                return await controller(method, path, payload)
            fri.api = api
            if erasure == 'between_fence_and_write':
                with pytest.raises(ControllerRejected):
                    await view.submit(click)
            else:
                await view.submit(click)
                store.erase_user('30')
            with store.connection() as db:
                bodies = [row[0] for row in db.execute('SELECT body FROM jobs')]
            assert all(MARKER not in body for body in bodies)
            assert bodies == ([] if erasure == 'between_fence_and_write' else
                              ['[Request withdrawn: someone it drew on changed their privacy settings]'])
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_gene_error_text_reaches_only_the_repair_investigation(monkeypatch, tmp_path):
    import discord

    from fri.fri_agent import GENE_ERROR
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        agent.gene_id = 88
        error = message('', user=88, age=20)
        error.author.bot = True
        # Red Audio's no-match embed echoes the member's query, so it can't be attributed.
        error.embeds = [discord.Embed(title='Unable To Play Tracks', description='No tracks found for `failed '+MARKER+'`.')]
        try:
            await agent.handle(message('!play failed '+MARKER, user=30, age=30))
            await agent.handle(error)
            event = message('fri explain that gene error')
            fri.chat_once.return_value = ChatResult(status='completed', reply='gene hit an error')
            await fri.talk(123, 1, 20, event.content, 'public', event=event)
            shown = fri.chat_once.await_args.args[1]['recent']
            assert MARKER not in str([row for row in shown if row['gene_error']])
            assert [row['text'] for row in shown if row['gene_error']] == [GENE_ERROR]
            raw = await agent.context_messages(1, raw_errors=True)
            assert MARKER in [row for row in raw if row['gene_error']][0]['text']
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_conflicting_revisions_refuse_instead_of_relabelling():
    from fri import feedback as commands
    from fri.fri import CHANGED, turn_provenance
    old, new = {'user_id': '30', 'revision': 0}, {'user_id': '30', 'revision': 1}
    assert turn_provenance({'reference_provenance': [old], 'summary_provenance': [old]}, '20') == [old]
    assert turn_provenance({'reference_provenance': [{'user_id': '20', 'revision': 0}],
                            'summary_provenance': [{'user_id': '20', 'revision': 1}]}, '20') == []
    for context in ({'reference_provenance': [old], 'summary_provenance': [new]},
                    {'turns': [{'provenance': [old]}], 'recent': [{'user_id': '30', 'revision': 1}]}):
        with pytest.raises(commands.UserFeedbackCheckFailure, match=CHANGED):
            turn_provenance(context, '20')


def relabel_ash_mid_turn(fri, store):
    """Ash clears chats after Bo's history was read; a fresh summary then labels ash with the new revision."""
    import time
    controller, changed = fri.api, []

    async def api(method, path, payload=None):
        if path == '/agent/context' and not changed:
            changed.append(True)
            store.clear_chats('30')
            with store.connection() as db:
                db.execute('INSERT INTO agent_summaries(channel_id,body,authors,at,revisions) VALUES (?,?,?,?,?)',
                           ('1', 'ash now likes chess', '["30"]', time.time(), '{"30": 1}'))
        return await controller(method, path, payload)
    fri.api = api


@pytest.mark.parametrize('path', ['reply', 'tool'])
def test_privacy_change_between_context_reads_keeps_nothing(monkeypatch, tmp_path, path):
    import datetime as dt

    from fri import feedback as commands
    from fri.fri import CHANGED
    async def scenario():
        fri, _, store, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        store.save_chat('123', 'public:1', '20', 0, 'fri what did ash say?', MARKER, 'm', 'high',
                        source=history_source(), provenance=[{'user_id': '30', 'revision': 0}])
        relabel_ash_mid_turn(fri, store)
        question = 'what is the current Python release?'
        receipt = {'kind': 'web_search', 'query': question, 'answer': 'public evidence',
                   'sources': ['https://www.python.org/downloads/'], 'checked_at': dt.datetime.now(dt.timezone.utc).isoformat()}
        fri.chat_once.side_effect = ([ChatResult(status='completed', reply=MARKER)] if path == 'reply' else
            [ChatResult(status='completed', action={'kind': 'web_search', 'query': question}),
             ChatResult(status='completed', reply='3.14, and '+MARKER)])
        event = message('fri '+question)
        try:
            with patch('fri.fri_actions.lookup', new=AsyncMock(return_value=(receipt, {}, 'completed'))):
                if path == 'reply':
                    with pytest.raises(commands.UserFeedbackCheckFailure, match=CHANGED):
                        await fri.talk(123, 1, 20, event.content, 'public', event=event)
                else:
                    result = await fri.talk(123, 1, 20, event.content, 'public', event=event)
                    assert MARKER not in result.reply and result.turn is None
            assert MARKER not in str(store.chat_context('123', 'public:1', '20')['turns'])
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_own_erasure_after_the_draft_fence_refuses_the_request(monkeypatch, tmp_path):
    from fri.fri_requests import DraftView
    async def scenario():
        fri, _, store, _, channel, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        event = message('fri draft a feature for me')
        fri.chat_once.return_value = ChatResult(status='completed', reply='draft ready', draft='Add a personal checklist')
        try:
            result = await fri.talk(123, 1, 20, event.content, 'public', event=event)
            view = DraftView(fri, 20, result.draft, reviewed=True, fence=result.turn)
            click = SimpleNamespace(user=SimpleNamespace(id=20), guild_id=123, channel_id=1, channel=channel,
                message=SimpleNamespace(author=fri.user), response=SimpleNamespace(defer=AsyncMock()),
                edit_original_response=AsyncMock())
            controller = fri.api
            async def api(method, path, payload=None):
                if path == '/requests':
                    store.erase_user('20')
                return await controller(method, path, payload)
            fri.api = api
            with pytest.raises(ControllerRejected):
                await view.submit(click)
            with store.connection() as db:
                assert not db.execute('SELECT 1 FROM jobs').fetchone()
        finally:
            await fri.close()
    asyncio.run(scenario())



def test_history_saved_without_provenance_is_withheld(monkeypatch, tmp_path):
    from fri.fri import LEGACY
    async def scenario():
        fri, _, store, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        # Saved before this release (or by a legacy companion): it may quote ash, with no record of it.
        store.save_chat('123', 'public:1', '20', 0, 'fri where is ash moving', MARKER, 'm', 'high', source=history_source())
        store.save_chat('123', 'public:1', '20', 0, 'fri thanks', 'anytime', 'm', 'high',
                        source=history_source(24), provenance=[])
        question = {'goal': 'web', 'question': 'before or after ash moves to denver?', 'topic': 'how are the lakers doing'}
        store.save_chat('123', 'public:1', '20', 0, 'fri how are the lakers doing', question['question'], 'm', 'high',
                        source=history_source(23), clarification=question)
        event = message('fri what did you tell me earlier')
        fri.chat_once.return_value = ChatResult(status='completed', reply='something from before i tracked sources')
        try:
            result = await fri.talk(123, 1, 20, event.content, 'public', event=event)
            shown = fri.chat_once.await_args.args[1]
            assert [turn['reply'] for turn in shown['turns']] == [LEGACY, 'anytime', LEGACY]
            assert shown.get('pending') is None and MARKER not in str(shown)
            assert result.turn['provenance'] == []
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_receipts_from_a_legacy_companion_hide_their_argument_and_output():
    from fri.gene_controls import grounded
    # Red's no-match output echoes the argument, which a legacy companion may have copied from anyone.
    legacy = {'action': 'play', 'argument': MARKER, 'grounded': False,
              'result': {'done': True, 'output': 'No tracks found for `'+MARKER+'`.'}}
    capable = {**legacy, 'argument': 'sandstorm', 'grounded': True, 'result': {'done': True, 'output': 'Track Enqueued'}}
    skip = {'action': 'skip', 'argument': '', 'result': {'done': True, 'output': 'Skipped'}}
    assert grounded([legacy, capable, skip]) == [
        {**legacy, 'argument': '[not kept]', 'result': {'done': True, 'output': '[not kept]'}}, capable, skip]


def test_a_later_history_page_cannot_relabel_an_earlier_one(monkeypatch, tmp_path):
    import discord
    async def scenario():
        fri, agent, store, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        posts = [message(MARKER if i == 0 else f'played game {i}', user=30, age=60+i) for i in range(6)]
        event = message('fri search history for what we played')
        for post in posts+[event]:
            post.created_at = discord.utils.snowflake_time(post.id)
            post.jump_url = f'https://discord.com/channels/123/1/{post.id}'
        event.reply.return_value = SimpleNamespace(id=event.id+1, content='done')
        query = {'kind': 'history_search', 'channel_id': '1', 'query': '', 'author': '', 'after': '', 'before': '', 'cursor': ''}
        async def http(route, *, params):
            return {'messages': [[{'id': str(p.id), 'channel_id': '1', 'content': p.content,
                'author': {'id': '30', 'bot': False}, 'hit': True}] for p in posts
                if int(params['min_id']) <= p.id < int(params['max_id'])][:params['limit']]}
        fri.http.request = AsyncMock(side_effect=http)
        steps = []
        async def respond(text, context):
            steps.append(context.get('action_result'))
            if len(steps) == 1:
                return ChatResult(status='completed', action=query)
            if len(steps) == 2:
                store.clear_chats('30')  # Between pages: page two sees ash's new revision.
                return ChatResult(status='completed', action={**query, 'cursor': context['action_result']['next_cursor']})
            return ChatResult(reply=MARKER, status='completed')
        fri.chat_once.side_effect = respond
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            assert MARKER in str(steps[1]['messages'])  # Page one was read at ash's old revision.
            assert MARKER not in event.reply.await_args.args[0]
            assert MARKER not in str(store.chat_context('123', 'public:1', '20')['turns'])
        finally:
            await fri.close()
    asyncio.run(scenario())



def test_a_redelivered_mention_waits_for_the_first_reply_to_be_delivered_and_recorded(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, _, _, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=8)
        event = message('<@99> hello')
        in_send, release, sends = asyncio.Event(), asyncio.Event(), []

        async def reply(content, **kwargs):
            sends.append(content)
            in_send.set()
            await release.wait()
            return SimpleNamespace(id=event.id+len(sends), content=content)
        event.reply.side_effect = reply
        first = asyncio.create_task(agent.handle(event))
        try:
            await asyncio.wait_for(in_send.wait(), 5)
            await asyncio.wait_for(agent.handle(event), 5)  # Discord redelivers while the first send waits.
            release.set()
            await asyncio.wait_for(first, 5)
            assert len(sends) == 1 and fri.chat_once.await_count == 1
            assert sum(path == '/chat/turn' for path, _ in calls) == 1
            await agent.handle(event)  # After it's recorded, the consumed grant refuses a repeat.
            assert fri.chat_once.await_count == 1 and sends[0] not in sends[1:]
            assert all('cannot renew its mention invitation' in text for text in sends[1:])
        finally:
            release.set()
            await first
            await fri.close()
    asyncio.run(scenario())


def test_an_answer_sent_while_the_question_is_being_saved_is_not_lost(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        first, answer = message('fri play'), message('blind melon no rain')
        saving, release = asyncio.Event(), asyncio.Event()
        controller, talked = fri.api, []

        async def api(method, path, body=None):
            if path == '/chat/turn':
                saving.set()
                await release.wait()
            return await controller(method, path, body)
        fri.api = api
        talk = fri.talk
        async def spy(*args, **kwargs):
            talked.append(args[3])
            return await talk(*args, **kwargs)
        fri.talk = spy
        fri.chat_once.return_value = ChatResult(status='completed', action={
            'kind': 'clarify', 'goal': 'play', 'topic': '', 'question': 'what song?'})
        first.reply.return_value = SimpleNamespace(id=first.id+1, content='what song?')
        task = asyncio.create_task(agent.handle(first))
        try:
            await asyncio.wait_for(saving.wait(), 5)  # The member has the question; it isn't saved yet.
            pending = asyncio.create_task(agent.handle(answer))
            await asyncio.sleep(0.05)
            assert talked == ['fri play']  # The answer waits for the save instead of being dropped.
            release.set()
            await asyncio.wait_for(task, 5)
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await asyncio.wait_for(pending, 5)
            assert talked == ['fri play', 'blind melon no rain']
        finally:
            release.set()
            await task
            await fri.close()
    asyncio.run(scenario())


def hold_first_save(fri):
    saving, release = asyncio.Event(), asyncio.Event()
    controller = fri.api

    async def api(method, path, body=None):
        if path == '/chat/turn' and not saving.is_set():
            saving.set()
            await release.wait()
        return await controller(method, path, body)
    fri.api = api
    return saving, release


def test_a_redelivered_event_never_waits_its_way_past_the_reply_guard(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, _, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        event, sends = message('fri hello'), []
        saving, release = hold_first_save(fri)

        async def send(content, **kwargs):
            sends.append(content)
            return SimpleNamespace(id=event.id+len(sends), content=content)
        event.reply.side_effect = send
        first = asyncio.create_task(agent.handle(event))
        try:
            await asyncio.wait_for(saving.wait(), 5)
            await asyncio.wait_for(agent.handle(event), 5)  # Ignored at once, not queued behind the save.
            release.set()
            await asyncio.wait_for(first, 5)
            assert len(sends) == 1 and fri.chat_once.await_count == 1
            assert sum(path == '/chat/turn' for path, _ in calls) == 1
        finally:
            release.set()
            await first
            await fri.close()
    asyncio.run(scenario())


def test_ambient_attention_never_answers_a_direct_reply_that_is_still_saving(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        original, sends = message('fri hello'), []
        saving, release = hold_first_save(fri)

        async def send(content, **kwargs):
            sends.append(content)
            return SimpleNamespace(id=original.id+len(sends), content=content)
        original.reply.side_effect = send
        agent.run = AsyncMock(return_value=({'action': 'reply', 'source_id': str(original.id)}, {}, 'completed'))
        task = asyncio.create_task(agent.handle(original))
        try:
            await asyncio.wait_for(saving.wait(), 5)
            await asyncio.wait_for(agent.attend(), 5)
            release.set()
            await asyncio.wait_for(task, 5)
            assert len(sends) == 1 and fri.chat_once.await_count == 1
        finally:
            release.set()
            await task
            await fri.close()
    asyncio.run(scenario())


def test_an_answer_sent_before_discord_confirms_the_question_is_not_lost(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        first, answer = message('fri play'), message('blind melon no rain')
        accepted, release = asyncio.Event(), asyncio.Event()
        fri.chat_once.return_value = ChatResult(status='completed', action={
            'kind': 'clarify', 'goal': 'play', 'topic': '', 'question': 'what song?'})

        async def send(content, **kwargs):
            accepted.set()  # Discord shows the question; its HTTP response hasn't returned yet.
            await release.wait()
            return SimpleNamespace(id=first.id+1, content=content)
        first.reply.side_effect = send
        talk, talked = fri.talk, []
        async def spy(*args, **kwargs):
            talked.append(args[3])
            return await talk(*args, **kwargs)
        fri.talk = spy
        task = asyncio.create_task(agent.handle(first))
        try:
            await asyncio.wait_for(accepted.wait(), 5)
            pending = asyncio.create_task(agent.handle(answer))
            await asyncio.sleep(0.05)
            assert talked == ['fri play']
            release.set()
            await asyncio.wait_for(task, 5)
            await asyncio.wait_for(pending, 5)
            assert talked == ['fri play', 'blind melon no rain']
        finally:
            release.set()
            await task
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('text', ['fri forget me', '<@99> forget me'])
def test_privacy_requests_never_wait_for_an_earlier_delivery(monkeypatch, tmp_path, text):
    async def scenario():
        fri, agent, store, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        initial, privacy = message('fri hello'), message(text)
        sending, release = asyncio.Event(), asyncio.Event()

        async def send(content, **kwargs):
            sending.set()
            await release.wait()
            return SimpleNamespace(id=initial.id+100, content=content)
        initial.reply.side_effect = send
        real_sleep = asyncio.sleep
        async def no_debounce(*args, **kwargs):
            await real_sleep(0)
        monkeypatch.setattr('fri.fri_agent.asyncio.sleep', no_debounce)
        first = asyncio.create_task(agent.handle(initial))
        second = None
        try:
            await asyncio.wait_for(sending.wait(), 5)
            second = asyncio.create_task(agent.handle(privacy))
            await real_sleep(0.1)
            assert store.chat_context('123', 'public:1', '20')['revision'] == 1  # Erased at once.
        finally:
            release.set()
            await first
            if second:
                await second
            await fri.close()
    asyncio.run(scenario())


def test_bursts_group_by_arrival_and_only_a_later_message_in_the_same_burst_supersedes():
    from fri.fri_conversation import Bursts
    def msg(mid):
        return SimpleNamespace(id=mid, content='hi', author=SimpleNamespace(id=20), channel=SimpleNamespace(id=1))
    bursts, (a, b, c) = Bursts(), (msg(1), msg(2), msg(3))
    with patch('fri.fri_conversation.time.monotonic', return_value=100.0):
        bursts.add(a, arrived=90.0)
        bursts.add(b, arrived=96.1)  # Over five seconds later: its own burst, though processed together.
        assert bursts.latest(a) and bursts.latest(b)
        bursts.add(c, arrived=97.0)
        assert bursts.latest(a) and not bursts.latest(b) and bursts.latest(c)
        assert [row[1] for row in bursts.take(a)] == [a]
        assert [row[1] for row in bursts.take(c)] == [b, c] and not bursts.rows


def test_messages_queued_behind_a_delivery_still_get_answered(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        initial = message('fri hello')
        sending, release = asyncio.Event(), asyncio.Event()

        async def send(content, **kwargs):
            sending.set()
            await release.wait()
            return SimpleNamespace(id=initial.id+100, content=content)
        initial.reply.side_effect = send
        # Sent 6.1 seconds apart, both while Fri's earlier reply was still being delivered.
        first, second = message('fri first follow-up', age=6.2), message('fri second follow-up', age=0.1)
        add = agent.bursts.add
        def arrive(event, arrived=None):  # The second arrived over five seconds later: a separate burst.
            return add(event, arrived+6.1 if event is second else arrived)
        agent.bursts.add = arrive
        tasks = [asyncio.create_task(agent.handle(initial))]
        try:
            await asyncio.wait_for(sending.wait(), 5)
            tasks.append(asyncio.create_task(agent.handle(first)))
            await asyncio.sleep(0.05)
            tasks.append(asyncio.create_task(agent.handle(second)))
            await asyncio.sleep(0.05)
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 10)
            assert fri.chat_once.await_count >= 2  # Not dropped as one mismatched burst.
            for event in (first, second):  # Each is answered, or told Fri is still replying.
                assert event.reply.await_count >= 1
                assert all('burst evidence' not in str(call) for call in event.reply.await_args_list)
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('erase', ['spoken', 'clear_chats', 'erase_user', 'disable'])
@pytest.mark.parametrize('mention', [False, True])
def test_a_message_waiting_behind_a_delivery_is_dropped_if_its_author_erases(monkeypatch, tmp_path, caplog, erase, mention):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, store, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        initial, privacy = message('fri hello'), message('fri forget me')
        queued = message('<@99> play old marker' if mention else 'fri play old marker')
        sending, release = asyncio.Event(), asyncio.Event()

        async def send(content, **kwargs):
            sending.set()
            await release.wait()
            return SimpleNamespace(id=initial.id+100, content=content)
        initial.reply.side_effect = send
        real_sleep = asyncio.sleep
        async def no_debounce(*args, **kwargs):
            await real_sleep(0)
        monkeypatch.setattr('fri.fri_agent.asyncio.sleep', no_debounce)
        async def answer(text, context):
            if 'old marker' in text:
                return ChatResult(status='completed', action={'kind': 'clarify', 'goal': 'play', 'topic': '',
                                                             'question': 'which version of old marker?'})
            return ChatResult(status='completed', reply='hello')
        fri.chat_once.side_effect = answer
        tasks = [asyncio.create_task(agent.handle(initial))]
        try:
            await asyncio.wait_for(sending.wait(), 5)
            tasks.append(asyncio.create_task(agent.handle(queued)))  # Waits behind the delivery.
            await real_sleep(0.05)
            if erase == 'spoken':
                await agent.handle(privacy)
            elif erase == 'disable':
                store.personal_control('20', 'disable')
            else:  # The panel's "Forget my chats", or Gene/Red erasure through /privacy/erase.
                getattr(store, erase)('20')
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 5)
            assert store.chat_context('123', 'public:1', '20')['pending'] is None
            assert all('old marker' not in call.args[0] for call in fri.chat_once.await_args_list)
            if mention:
                assert f'Mention 1/{queued.id} ignored: author privacy changed' in caplog.messages
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await fri.close()
    asyncio.run(scenario())


def test_a_dm_follow_up_during_delivery_is_still_answered(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=999)
        first, follow = message('fri hello'), message('my follow-up')
        first.guild = follow.guild = None
        sending, release = asyncio.Event(), asyncio.Event()
        real_sleep = asyncio.sleep
        async def no_debounce(*args, **kwargs):
            await real_sleep(0)
        monkeypatch.setattr('fri.fri_agent.asyncio.sleep', no_debounce)

        async def send(content, **kwargs):
            sending.set()
            await release.wait()
            return SimpleNamespace(id=first.id+100, content=content)
        first.reply.side_effect = send
        task = asyncio.create_task(agent.handle(first))
        try:
            await asyncio.wait_for(sending.wait(), 5)
            pending = asyncio.create_task(agent.handle(follow))  # Its privacy check can't use the DM channel.
            await real_sleep(0.03)
            release.set()
            await asyncio.wait_for(asyncio.gather(task, pending), 5)
            assert fri.chat_once.await_count == 2
        finally:
            release.set()
            await task
            await fri.close()
    asyncio.run(scenario())


def test_an_erasure_right_after_the_first_privacy_read_still_drops_the_waiting_message(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        initial, queued = message('fri hello'), message('fri play old marker')
        sending, release = asyncio.Event(), asyncio.Event()
        real_sleep = asyncio.sleep
        async def no_debounce(*args, **kwargs):
            await real_sleep(0)
        monkeypatch.setattr('fri.fri_agent.asyncio.sleep', no_debounce)

        async def send(content, **kwargs):
            sending.set()
            await release.wait()
            return SimpleNamespace(id=initial.id+100, content=content)
        initial.reply.side_effect = send
        async def answer(text, context):
            if 'old marker' in text:
                return ChatResult(status='completed', action={'kind': 'clarify', 'goal': 'play', 'topic': '',
                                                             'question': 'which version of old marker?'})
            return ChatResult(status='completed', reply='hello')
        fri.chat_once.side_effect = answer
        read, reads = agent.profile, []
        async def profile(channel, user):
            result = await read(channel, user)
            if asyncio.current_task().get_name() == 'queued' and not reads:
                reads.append(result['revision'])
                store.clear_chats('20')  # Lands just after Fri first saw revision 0.
            return result
        agent.profile = profile
        first = asyncio.create_task(agent.handle(initial))
        second = None
        try:
            await asyncio.wait_for(sending.wait(), 5)
            second = asyncio.create_task(agent.handle(queued), name='queued')
            await real_sleep(0.05)
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 5)
            assert reads == [0]
            assert store.chat_context('123', 'public:1', '20')['pending'] is None
            assert all('old marker' not in call.args[0] for call in fri.chat_once.await_args_list)
        finally:
            release.set()
            await asyncio.gather(*([first, second] if second else [first]), return_exceptions=True)
            await fri.close()
    asyncio.run(scenario())
