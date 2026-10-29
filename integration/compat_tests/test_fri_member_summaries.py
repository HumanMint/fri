"""Member summaries end to end: Fri builds them in the background, the real controller fences and serves them."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import httpx
import pytest

from compat_tests.test_fri_chat import CONTEXT, response_event
from compat_tests.test_fri_heartbeat import STATE
from compat_tests.test_fri_member_profiles import (
    DM,
    GAMING,
    GENERAL,
    STAFF,
    dm_control,
    model,
    model_context,
    run,
    turns,
    world,
)
from fri import feedback as commands
from fri.fri import SUMMARIZED, Fri
from fri.fri_agent import Agent
from fri.fri_chat import INSTRUCTIONS, SUMMARY_RULE, Chat, ChatResult
from fri.fri_errors import ControllerRejected
from fri.fri_member_summary import SCHEMA, build
from fri.fri_memory import profiled
from fri.fri_memory_panel import EditSummary, MemoryView, show

ASH = {'user_id': '30', 'revision': 0}
SUMMARY = 'they keep planning a raid and asking about builds'


def settled(w, seconds=7200):
    with w.store.connection() as db:  # As if the chat ended a while ago.
        db.execute('UPDATE chat_turns SET at=at-?', (seconds,))
        db.execute("DELETE FROM agent_state WHERE key='next_member_summary'")


def summarizer(monkeypatch, w, reply=SUMMARY, during=None):
    seen = []

    async def analyze(chat, instruction, data, name, effort, schema, headers):
        assert headers == {'Authorization': 'Bearer test'}  # Signed in before the claim.
        seen.append({'data': data, 'model': (name, effort), 'schema': schema, 'instruction': instruction})
        if during:
            during()
        return {'summary': reply}, {'input_tokens': 7}, 'completed'
    monkeypatch.setattr('fri.fri_member_summary.analyze', analyze)
    w.fri.chat = SimpleNamespace(headers=AsyncMock(return_value={'Authorization': 'Bearer test'}))
    return seen


def summarized(w, user='20', conversation='public:1'):
    return w.store.chat_context('123', conversation, user, True, True)['member_summary']


async def summarize(monkeypatch, w, seconds=7200):
    """Two readable exchanges in #general, then one background build."""
    await w.say(GENERAL, 'fri raid tonight?')
    await w.say(GENERAL, 'fri which build for it')
    settled(w, seconds)
    summarizer(monkeypatch, w)
    assert await build(w.fri.agent) == {'kept': True}


def test_the_builder_sends_only_readable_exchanges_once_a_day(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await w.say(GENERAL, 'fri raid tonight?', 'tank. obviously.')
            await w.say(GENERAL, 'fri which build for it')
            await w.say(GAMING, 'fri gaming channel words')
            await w.say(STAFF, 'fri staff only words')
            await w.say(DM, 'fri dm only words')
            settled(w)
            w.readable[GAMING] = False  # Restricted since it was said.
            seen = summarizer(monkeypatch, w)
            assert await build(w.fri.agent) == {'kept': True}
            sent = seen[0]
            assert sent['data'] == {'previous_summary': '', 'messages': ['fri raid tonight?', 'fri which build for it']}
            assert sent['model'] == ('gpt-6-luna', 'medium') and sent['schema'] == SCHEMA
            assert 'tank' not in json.dumps(sent)  # Fri's replies stay out.
            w.fri.agent.record.assert_awaited_once_with('summary', 'gpt-6-luna', 'medium', {'input_tokens': 7},
                                                         'completed', deliver=False)
            stored = summarized(w)
            assert stored['text'] == SUMMARY and stored['origins'] == ['public:1']
            await w.say(GENERAL, 'fri more raid talk')
            await w.say(GENERAL, 'fri and more')
            settled(w)
            assert await build(w.fri.agent) is None and len(seen) == 1  # One call a member a day.
        finally:
            await w.fri.close()
    run(scenario)


@pytest.mark.parametrize('change', ['forget', 'clear', 'quoted'])
def test_a_privacy_change_during_the_call_keeps_nothing(monkeypatch, tmp_path, change):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            source = {'kind': 'discord_message', 'id': str(discord.utils.time_snowflake(discord.utils.utcnow())),
                      'user_id': '20', 'display_name': 'friend', 'channel_id': str(GENERAL), 'guild_id': '123'}
            w.store.save_chat('123', 'public:1', '20', 0, 'what did ash say', 'ash said denver', 'm', 'high',
                              source=source, provenance=[ASH], audience='server')
            await w.say(GENERAL, 'fri anyway')
            settled(w)
            summarizer(monkeypatch, w, during={'forget': lambda: w.store.personal_control('20', 'disable'),
                                               'clear': lambda: w.store.clear_chats('20'),
                                               'quoted': lambda: w.store.personal_control('30', 'disable')}[change])
            assert await build(w.fri.agent) is None  # The controller's fence refused it.
            assert summarized(w) is None
        finally:
            await w.fri.close()
    run(scenario)


def test_the_summary_reaches_only_the_members_own_turns_where_it_may(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await summarize(monkeypatch, w)
            for where in (GAMING, DM):
                await w.say(where, 'fri what were we on about')
                assert model_context(w)['member_summary']['text'] == SUMMARY
            await w.say(STAFF, 'fri what were we on about')  # A restricted channel never gets it.
            assert model_context(w)['member_summary'] is None
            await w.say(GENERAL, 'fri hi', user=30)
            assert model_context(w).get('member_summary') is None  # Never in another member's turn.
            w.readable[GENERAL] = False  # Where it came from, closed: it stays out until it opens again.
            await w.say(GAMING, 'fri and now')
            assert model_context(w)['member_summary'] is None
            assert all(body.get('summaries') for path, body in w.calls if path == '/chat/context'
                       and body and body.get('profiles'))
        finally:
            await w.fri.close()
    run(scenario)


def test_a_reply_built_on_the_summary_inherits_whose_words_it_drew_on(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            source = {'kind': 'discord_message', 'id': str(discord.utils.time_snowflake(discord.utils.utcnow())),
                      'user_id': '20', 'display_name': 'friend', 'channel_id': str(GENERAL), 'guild_id': '123'}
            w.store.save_chat('123', 'public:1', '20', 0, 'ash says the raid is at nine', 'ok', 'm', 'high',
                              source=source, provenance=[ASH], audience='server')
            await w.say(GENERAL, 'fri anyway')
            settled(w, 13*3600)  # Past what other channels see of it, so only the summary carries ash.
            summarizer(monkeypatch, w)
            assert await build(w.fri.agent) == {'kept': True}
            assert summarized(w)['provenance'] == [ASH]
            await w.say(GAMING, 'fri when is it again')
            assert ASH in turns(w)[-1]['provenance']
            w.store.personal_control('30', 'disable')  # Ash's privacy change takes the summary too.
            assert summarized(w) is None
            await w.say(GAMING, 'fri and now')
            assert model_context(w)['member_summary'] is None
        finally:
            await w.fri.close()
    run(scenario)


def test_a_channel_closed_while_fri_signs_in_drops_the_summary(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await summarize(monkeypatch, w)
            sent = []

            def handler(request):
                sent.append(json.loads(json.loads(request.content)['input'][-1]['content']))
                return httpx.Response(200, text='data: '+json.dumps(response_event(content=json.dumps(
                    {'reply': 'sure', 'request_draft': None})))+'\n\n')

            def access_token():
                w.readable[GENERAL] = False  # Closed after Fri gathered context.
                return 'test'
            w.fri.chat = Chat(SimpleNamespace(access_token=access_token), httpx.MockTransport(handler))

            async def call(text, context):
                return await Fri.chat_once(w.fri, text, context)
            w.fri.chat_once = AsyncMock(side_effect=call)
            await w.say(GAMING, 'fri what were we on about')
            assert sent and 'your_summary_of_speaker_untrusted' not in sent[-1]
        finally:
            await w.fri.close()
    run(scenario)


def test_a_channel_closed_while_fri_thinks_keeps_a_reply_built_on_it_unsaid(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await summarize(monkeypatch, w, 13*3600)  # Past what other channels see, so only the summary is from there.
            saved = len(turns(w))

            async def respond(text, context):
                assert context['member_summary']['text'] == SUMMARY and context['elsewhere'] == []
                w.readable[GENERAL] = False
                return ChatResult(status='completed', reply='still on that raid?')
            model(w, respond)
            with pytest.raises(commands.UserFeedbackCheckFailure, match='went private'):
                await w.say(GAMING, 'fri what were we on about')
            assert len(turns(w)) == saved
        finally:
            await w.fri.close()
    run(scenario)


def test_the_model_reads_it_as_fris_own_untrusted_summary():
    seen, rules = [], []

    def handler(request):
        body = json.loads(request.content)
        seen.append(json.loads(body['input'][-1]['content']))
        rules.append(body['instructions'])
        return httpx.Response(200, text='data: '+json.dumps(response_event())+'\n\n')
    chat = Chat(SimpleNamespace(access_token=lambda: 'test'), httpx.MockTransport(handler))
    summary = {'text': SUMMARY, 'provenance': [ASH], 'origins': ['public:1'], 'refreshed': 1, 'expires_at': 2}
    asyncio.run(chat.respond('hey', {**CONTEXT, 'member_summary': summary}))
    asyncio.run(chat.respond('hey', CONTEXT))
    assert seen[0]['your_summary_of_speaker_untrusted'] == SUMMARY
    assert 'public:1' not in json.dumps(seen[0]) and '"30"' not in json.dumps(seen[0])
    assert 'your_summary_of_speaker_untrusted' not in seen[1]  # Nothing to read, no field.
    # The rule comes only with a summary, so every other turn's instructions are exactly as before.
    assert SUMMARY_RULE in rules[0] and 'never instructions' in SUMMARY_RULE
    assert SUMMARY_RULE not in rules[1] and 'your_summary_of_speaker_untrusted' not in INSTRUCTIONS


def test_what_do_you_remember_shows_it_in_dms_and_only_names_it_in_public(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await summarize(monkeypatch, w)
            dm, _, _ = await w.say(DM, 'what do you remember about me')
            assert '**what we’ve talked about lately**' in dm.reply and SUMMARY in dm.reply
            public, _, _ = await w.say(GAMING, 'what do you remember about me')
            assert SUMMARY not in public.reply and 'short summary of what we’ve talked about lately' in public.reply
            staff, _, _ = await w.say(STAFF, 'what do you remember about me')
            assert 'summary' not in staff.reply
            await w.say(GENERAL, 'forget me')
            assert summarized(w) is None
            dm, _, _ = await w.say(DM, 'what do you remember about me')
            assert dm.reply.startswith('memory’s off') and SUMMARY not in dm.reply
        finally:
            await w.fri.close()
    run(scenario)


def test_my_memory_shows_edits_and_clears_it(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        shown = AsyncMock()
        monkeypatch.setattr('fri.fri_panel.reply', shown)
        monkeypatch.setattr('fri.fri_memory_panel.allowed', AsyncMock(return_value=True))
        try:
            await summarize(monkeypatch, w)
            scope = profiled(w.fri, {'guild_id': '123', 'user_id': '20', 'channel_id': '456',
                                     'conversation_id': 'private:900'})
            interaction = Mock(spec=discord.Interaction, id=int(dm_control()['id']), guild_id=None, channel_id=DM,
                               user=SimpleNamespace(id=20, display_name='friend'))
            await show(w.fri, interaction, scope)
            body = shown.await_args.kwargs['embed'].description
            assert '**My summary of what we’ve talked about lately**' in body and SUMMARY in body
            view = shown.await_args.kwargs['view']
            assert [item.label for item in view.children] == ['Edit summary', 'Clear summary']
            modal = EditSummary(view)
            modal.summary._value = 'mostly raids'
            await modal.on_submit(interaction)
            assert 'Updated my summary.' in shown.await_args.kwargs['embed'].description
            assert summarized(w)['text'] == 'mostly raids' and summarized(w)['origins'] == []
            await w.say(GAMING, 'fri what were we on about')
            assert model_context(w)['member_summary']['text'] == 'mostly raids'
            interaction.id = int(dm_control()['id'])
            await show(w.fri, interaction, scope)
            await shown.await_args.kwargs['view'].clear_summary(interaction)
            assert 'Cleared my summary.' in shown.await_args.kwargs['embed'].description
            assert summarized(w) is None and shown.await_args.kwargs['view'].children == []
        finally:
            await w.fri.close()
    run(scenario)


def test_an_older_controller_means_no_builder_and_no_field(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await summarize(monkeypatch, w)
            w.fri.capabilities = w.fri.capabilities - {'member-summaries'}
            await w.say(GAMING, 'fri what were we on about')
            assert 'member_summary' not in model_context(w)
            assert not any('summaries' in (body or {}) for path, body in w.calls[-4:] if path == '/chat/context')
            dm, _, _ = await w.say(DM, 'what do you remember about me')
            assert SUMMARY not in dm.reply
            assert MemoryView(w.fri, profiled(w.fri, {'guild_id': '123', 'user_id': '20', 'channel_id': '456',
                                                      'conversation_id': 'private:900'}),
                              {'enabled': True, 'revision': 0, 'entries': []}).children == []
        finally:
            await w.fri.close()
    run(scenario)


def test_the_builder_runs_as_its_own_stage_only_against_a_capable_controller(monkeypatch, tmp_path):
    built = AsyncMock()
    monkeypatch.setattr('fri.fri_member_summary.build', built)
    for name in ('fri.fri_workspace.deliver_one', 'fri.fri_social.deliver_one', 'fri.fri_fixes.check'):
        monkeypatch.setattr(name, AsyncMock())

    async def api(method, path, payload=None):
        return {'/agent/state': STATE, '/agent/reports': []}[path]
    fri = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, api=AsyncMock(side_effect=api),
                          get_user=lambda _: None, wait_until_ready=AsyncMock(), capabilities=frozenset(),
                          data=tmp_path)
    agent = Agent(fri)
    agent.learn, agent.attend, agent.check_memes = AsyncMock(), AsyncMock(), AsyncMock()
    asyncio.run(agent.heartbeat.coro(agent))
    built.assert_not_awaited()
    assert 'member_summaries' not in agent.stages and agent.heartbeat_status == 'ok'
    fri.capabilities = frozenset({'member-summaries'})
    asyncio.run(agent.heartbeat.coro(agent))
    built.assert_awaited_once_with(agent)
    assert agent.stages['member_summaries']['error'] is None
    monkeypatch.setitem(STATE, 'config', {**STATE['config'], 'learning_enabled': False})
    asyncio.run(agent.heartbeat.coro(agent))
    assert built.await_count == 1 and 'member_summaries' not in agent.stages


def test_a_controller_rolled_back_past_summaries_stops_the_builder(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            seen = summarizer(monkeypatch, w)
            original = w.fri.api

            async def older(method, path, payload=None):
                if path.startswith('/agent/member-summary'):
                    raise ControllerRejected('Not Found', 404)
                return await original(method, path, payload)
            w.fri.api = older
            assert await build(w.fri.agent) is None
            assert 'member-summaries' not in w.fri.capabilities and not seen
        finally:
            await w.fri.close()
    run(scenario)


CONTINUATION = 'still up for that raid?'


def recalls(context):
    """The model, answering from the summary when it has one."""
    return ChatResult(status='completed', reply=CONTINUATION if context.get('member_summary') else 'no summary')


def signed_in_chat(w, handler, access_token=lambda: 'test'):
    """Fri's real Chat over a mock transport, so ready() runs once signed in, as in production."""
    w.fri.chat = Chat(SimpleNamespace(access_token=access_token), httpx.MockTransport(handler))

    async def call(text, context):
        return await Fri.chat_once(w.fri, text, context)
    w.fri.chat_once = AsyncMock(side_effect=call)


@pytest.mark.parametrize('when', ['signin', 'inference'])
def test_a_destination_closed_mid_turn_drops_the_summary_or_keeps_its_reply_unsaid(monkeypatch, tmp_path, when):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await summarize(monkeypatch, w, 13*3600)  # From #general; #general stays readable throughout.
            saved, sent = len(turns(w)), []

            def access_token():
                if when == 'signin':
                    w.readable[GAMING] = False  # Where it would show, restricted after Fri gathered context.
                return 'test'

            def handler(request):
                sent.append(json.loads(json.loads(request.content)['input'][-1]['content']))
                if when == 'inference':
                    w.readable[GAMING] = False
                return httpx.Response(200, text='data: '+json.dumps(response_event(content=json.dumps(
                    {'reply': CONTINUATION, 'request_draft': None})))+'\n\n')
            signed_in_chat(w, handler, access_token)
            if when == 'signin':
                await w.say(GAMING, 'fri what were we on about')
                assert sent and 'your_summary_of_speaker_untrusted' not in sent[-1]
            else:
                with pytest.raises(commands.UserFeedbackCheckFailure, match='went private'):
                    await w.say(GAMING, 'fri what were we on about')
                assert SUMMARY in sent[-1]['your_summary_of_speaker_untrusted']  # It was readable when sent.
                assert len(turns(w)) == saved  # Nothing built on it is said or kept.
        finally:
            await w.fri.close()
    run(scenario)


@pytest.mark.parametrize('closed', [GENERAL, GAMING])
def test_a_reply_built_on_a_summary_from_another_channel_is_saved_as_a_breadcrumb(monkeypatch, tmp_path, closed):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await summarize(monkeypatch, w, 13*3600)  # From #general.

            async def first(text, context):
                return recalls(context)
            model(w, first)
            result, saved, _ = await w.say(GAMING, 'fri what were we on about')
            assert result.reply == CONTINUATION and saved  # The member still gets the answer.
            assert turns(w)[-1]['reply'] == SUMMARIZED
            w.readable[closed] = False  # Where it came from, or where it was said, restricted since.
            carried = []

            async def followup(text, context):
                assert context.get('member_summary') is None
                carried.extend(turn['reply'] for turn in context['turns'])
                return ChatResult(status='completed', reply='sure')
            model(w, followup)
            await w.say(GAMING, 'fri remind me again')
            assert carried and CONTINUATION not in carried
        finally:
            await w.fri.close()
    run(scenario)


def test_a_reply_built_on_a_summary_from_another_channel_never_reaches_the_live_view(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await summarize(monkeypatch, w, 13*3600)

            async def first(text, context):
                return recalls(context)
            model(w, first)
            result, saved, _ = await w.say(GAMING, 'fri what were we on about')
            assert result.reply == CONTINUATION and saved
            assert result.echo is None  # So Agent.reply_once leaves it out of every other member's view.
            w.readable[GENERAL] = False
            recent = []

            async def followup(text, context):
                recent.extend(row['text'] for row in context.get('recent', []))
                return ChatResult(status='completed', reply='sure')
            model(w, followup)
            await w.say(GAMING, 'fri what is going on', user=30)
            assert CONTINUATION not in recent
        finally:
            await w.fri.close()
    run(scenario)


@pytest.mark.parametrize('where', [GENERAL, DM])
def test_a_reply_built_on_a_summary_from_the_same_channel_or_in_private_is_kept_as_said(monkeypatch, tmp_path, where):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await summarize(monkeypatch, w, 13*3600)  # From #general.

            async def respond(text, context):
                return recalls(context)
            model(w, respond)
            result, saved, _ = await w.say(where, 'fri what were we on about')
            assert result.reply == CONTINUATION and saved and turns(w)[-1]['reply'] == CONTINUATION
            # Where it was said, its own audience; in private, only the member, as for their notes.
            assert (result.echo or {}).get('text') == (CONTINUATION if where == GENERAL else None)
        finally:
            await w.fri.close()
    run(scenario)


def test_a_reply_after_a_tool_built_on_a_summary_from_another_channel_is_a_breadcrumb(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await summarize(monkeypatch, w, 13*3600)

            async def respond(text, context):
                if 'work_list' in context['actions']:
                    return ChatResult(status='completed', action={'kind': 'work_list', 'work_id': ''})
                return recalls(context)
            model(w, respond)
            text = 'fri anything saved from what we were on about'
            event = w.message(GAMING, text)
            w.channels[GAMING].fetch_message = AsyncMock(return_value=event)  # The tool rereads it, unchanged.
            result, saved, _ = await w.say(GAMING, text, event=event)
            assert result.reply == CONTINUATION and saved
            assert turns(w)[-1]['reply'] == SUMMARIZED and result.echo is None
        finally:
            await w.fri.close()
    run(scenario)


def test_an_edited_summary_fences_delivery_on_where_it_shows(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await summarize(monkeypatch, w)
            await w.fri.api('POST', '/chat/memory/summary', {
                'guild_id': '123', 'user_id': '20', 'channel_id': '456', 'conversation_id': 'private:900',
                'source': dm_control(), 'revision': 0, 'action': 'replace', 'text': 'mostly raids'})
            assert summarized(w)['origins'] == []  # In their own words: it came from nowhere.
            saved = len(turns(w))

            async def respond(text, context):
                assert context['member_summary']['text'] == 'mostly raids'
                w.readable[GAMING] = False  # Where it shows, restricted while Fri thinks.
                return ChatResult(status='completed', reply='still mostly raids?')
            model(w, respond)
            with pytest.raises(commands.UserFeedbackCheckFailure, match='went private'):
                await w.say(GAMING, 'fri what were we on about')
            assert len(turns(w)) == saved
        finally:
            await w.fri.close()
    run(scenario)


@pytest.mark.parametrize('followup_user', [20, 30])
def test_a_clarification_drawing_on_a_summary_from_elsewhere_is_kept_neutral(monkeypatch, tmp_path, followup_user):
    """Its saved reply and pending question go neutral: closing the summary's channel later leaves nothing behind,
    for the member's own follow-up or for another member replying to the question."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await w.say(GENERAL, 'fri we are planning a violet dragon raid')
            await w.say(GENERAL, 'fri which class for the violet dragon raid')
            settled(w, 13*3600)
            summarizer(monkeypatch, w, reply='they are planning a violet dragon raid')
            assert await build(w.fri.agent) == {'kept': True}
            w.fri.agent.state['web_search_enabled'] = True
            text = 'fri look up raid builds'
            event = w.message(GAMING, text)
            w.channels[GAMING].fetch_message = AsyncMock(return_value=event)
            question = 'for that violet dragon raid you are planning, which class?'

            async def clarify(text, context):
                assert context['member_summary']['text'] == 'they are planning a violet dragon raid'
                assert 'clarify' in context['actions']
                return ChatResult(status='completed', action={'kind': 'clarify', 'goal': 'web',
                    'topic': 'raid builds', 'question': question})
            model(w, clarify)
            result, saved, sent = await w.say(GAMING, text, event=event)
            assert saved and result.clarification
            w.readable[GENERAL] = False

            async def followup(text, context):
                assert context.get('member_summary') is None
                return ChatResult(status='completed', reply='which one?')
            model(w, followup)
            followup_text = 'fri what were we talking about'
            followup_event = w.message(GAMING, followup_text, user=followup_user)
            events = {event.id: event, followup_event.id: followup_event}
            if followup_user != 20:
                target = w.message(GAMING, question, user=99)
                target.id = sent.id
                target.author.bot = True
                events[target.id] = target
                followup_event.reference = SimpleNamespace(message_id=target.id, channel_id=GAMING)
            w.channels[GAMING].fetch_message = AsyncMock(side_effect=lambda mid: events[mid])
            await w.say(GAMING, followup_text, user=followup_user, event=followup_event)
            context = model_context(w)
            assert 'violet dragon' not in json.dumps(context, default=str)
        finally:
            await w.fri.close()
    asyncio.run(scenario())
