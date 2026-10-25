"""Fri looks at the images a conversation points at: several nearby memes, a read, its live view."""

import asyncio
import copy
import datetime as dt
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import httpx
import pytest

from compat_tests.test_fri_actions import harness
from compat_tests.test_fri_chat import CONTEXT, response_event
from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_sass_eval import Scripted, grader  # noqa: F401
from fri import feedback as commands
from fri.fri_actions import Actions
from fri.fri_chat import Chat, ChatResult
from fri.fri_eval import evaluate, sass_cases
from fri.fri_images import MAX_IMAGES, look, media, require_current

ASK = '<@99> whats ur favorite meme out of the memes above'


def picture(channel, name, kind='image/png'):
    return SimpleNamespace(url=f'https://cdn.discordapp.com/attachments/{channel}/1/{name}',
        content_type=kind, size=100, width=100, height=100, is_spoiler=lambda: False)


def loads(monkeypatch, fail=()):
    """Each image loads as a data URL naming its file, so tests can see which ones Fri looked at."""
    async def load(url, channel, **_):
        if url.rsplit('/', 1)[1] in fail:
            raise httpx.ConnectError('cdn down')
        return 'data:image/png;base64,'+url.rsplit('/', 1)[1]
    loader = AsyncMock(side_effect=load)
    monkeypatch.setattr('fri.fri_images.load_image', loader)
    return loader


def asked_then(channel, contexts, final='kev’s circle. low effort, high payoff'):
    """The model reads the channel first, as the instructions ask for a meme question, then answers."""
    async def respond(_, context):
        contexts.append(context)
        if len(contexts) == 1:
            return ChatResult(status='completed', action={'kind': 'read_recent', 'channel_id': str(channel.id),
                                                          'window': 'recent'})
        return ChatResult(status='completed', reply=final)
    return respond


@pytest.mark.parametrize('configured_channel', [True, False])
def test_a_ping_about_the_memes_above_looks_at_each_of_them(monkeypatch, tmp_path, configured_channel):
    async def scenario():
        fri, agent, store, _, channel, message, calls, _ = environment(
            monkeypatch, tmp_path, channel_id=1 if configured_channel else 8)
        kev = message('', user=30, age=3*3600, attachments=[picture(channel.id, 'circle.png')])
        jo = message('this one', user=40, age=2*3600, attachments=[picture(channel.id, 'square.png')])
        dance = message('', user=60, age=90*60, attachments=[picture(channel.id, 'dance.gif', 'image/gif')])
        message('', user=70, age=60*60, attachments=[picture(channel.id, 'private.png')])
        sam = message('', user=50, age=30*60, attachments=[picture(channel.id, 'triangle.png')])
        store.personal_control('70', 'disable')
        event = message(ASK)
        loader = loads(monkeypatch)
        contexts = []
        if configured_channel:
            fri.chat_once.side_effect = asked_then(channel, contexts)
        else:
            fri.chat_once.side_effect = lambda _, context: contexts.append(context) or ChatResult(
                status='completed', reply='kev’s circle. low effort, high payoff')
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            context = contexts[-1]
            # Newest first, each image tied to its own post and poster, the gif as its first frame; the
            # opted-out post isn't.
            assert context['images'] == ['data:image/png;base64,triangle.png', 'data:image/png;base64,dance.gif',
                                         'data:image/png;base64,square.png', 'data:image/png;base64,circle.png']
            assert [(row['images'], row['source']['id'], row['source']['user_id'], row.get('still'))
                    for row in context['inspected_images']] == [
                ([1], str(sam.id), '50', None), ([2], str(dance.id), '60', 'gif first frame'),
                ([3], str(jo.id), '40', None), ([4], str(kev.id), '30', None)]
            assert sorted(call.args[0].rsplit('/', 1)[1] for call in loader.await_args_list) == [
                'circle.png', 'dance.gif', 'square.png', 'triangle.png']
            assert all(call.kwargs == {'gifs': True} for call in loader.await_args_list)
            rows = {row['source']['user_id']: row for row in context['invited_context']}
            assert '70' not in rows and rows['60']['attachments'] == ['gif'] and rows['30']['attachments'] == ['image']
            assert rows['30']['text'] == '' and rows['40']['text'] == 'this one'
            if configured_channel:
                receipt = context['action_result']
                assert receipt['images_inspected'] == 4 and receipt['images_not_inspected'] == 0
            event.reply.assert_awaited_once()
            assert event.reply.await_args.args[0] == 'kev’s circle. low effort, high payoff'
            saved = store.chat_context('123', f'public:{channel.id}', '20')
            assert not saved['personal_memory']['notes'] and 'this one' not in json.dumps(saved)
            assert sum(path == '/chat/mentions/admit' for path, _ in calls) == 1
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_look_keeps_to_its_image_budget_newest_first(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, channel, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        for age, name in [(600, 'a'), (500, 'b'), (400, 'c'), (300, 'd')]:
            message('', user=30, age=age, attachments=[picture(channel.id, name+'.png')])
        message('', user=40, age=200, attachments=[picture(channel.id, 'e1.png'), picture(channel.id, 'e2.png')])
        event = message(ASK)
        loads(monkeypatch)
        contexts = []
        fri.chat_once.side_effect = asked_then(channel, contexts)
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            context = contexts[-1]
            assert len(context['images']) == MAX_IMAGES == 4
            assert [name.rsplit(',', 1)[1] for name in context['images']] == ['e1.png', 'e2.png', 'd.png', 'c.png']
            assert [row['images'] for row in context['inspected_images']] == [[1, 2], [3], [4]]
            assert context['action_result']['images_not_inspected'] == 2
            event.reply.assert_awaited_once()
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_an_image_that_wont_load_is_skipped_and_the_rest_still_answer(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, channel, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        message('', user=30, age=300, attachments=[picture(channel.id, 'ok.png')])
        message('', user=40, age=200, attachments=[picture(channel.id, 'broken.png')])
        event = message(ASK)
        loads(monkeypatch, fail={'broken.png'})
        contexts = []
        fri.chat_once.side_effect = asked_then(channel, contexts)
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            context = contexts[-1]
            assert context['images'] == ['data:image/png;base64,ok.png']
            assert [row['source']['user_id'] for row in context['inspected_images']] == ['30']
            assert context['action_result']['images_not_inspected'] == 1
            event.reply.assert_awaited_once()
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['edited', 'privacy', 'none'])
def test_a_change_to_any_meme_fri_looked_at_withholds_the_reply(monkeypatch, tmp_path, change):
    async def scenario():
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        message('', user=30, age=300, attachments=[picture(channel.id, 'first.png')])
        second = message('', user=40, age=200, attachments=[picture(channel.id, 'second.png')])
        event = message(ASK)
        loads(monkeypatch)
        contexts = []
        async def respond(_, context):
            contexts.append(context)
            if len(contexts) == 1:
                return ChatResult(status='completed', action={'kind': 'read_recent', 'channel_id': '1', 'window': 'recent'})
            assert len(context['images']) == 2
            if change == 'edited':
                edited = copy.copy(second)
                edited.edited_at = dt.datetime.now(dt.timezone.utc)
                rows[second.id] = edited
            elif change == 'privacy':
                store.personal_control('40', 'disable')
            return ChatResult(status='completed', reply='the square, obviously')
        fri.chat_once.side_effect = respond
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            if change == 'none':
                event.reply.assert_awaited_once()
            else:
                event.reply.assert_not_awaited()
                assert not any(path == '/chat/turn' for path, _ in calls)
        finally:
            await fri.close()
    asyncio.run(scenario())


def read_harness(monkeypatch, tmp_path):
    """A DM read of a configured channel, with real-shaped messages that can be refetched."""
    fri, agent, _, _, channel, _, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
    agent.member = AsyncMock(return_value=SimpleNamespace(id=20))
    def post(text, *, user=30, age=60, attachments=(), bot=False):
        mid = discord.utils.time_snowflake(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=age))
        while mid in rows:
            mid += 1
        item = Mock(spec=discord.Message, id=mid, content=text, channel=channel, guild=channel.guild,
            author=SimpleNamespace(id=user, display_name=f'member {user}', bot=bot), webhook_id=None,
            attachments=list(attachments), embeds=[], edited_at=None, jump_url=f'https://discord.com/channels/123/1/{mid}',
            created_at=discord.utils.snowflake_time(mid))
        rows[mid] = item
        return item
    async def history(**kwargs):
        for item in sorted(rows.values(), key=lambda m: m.id, reverse=True)[:kwargs['limit']]:
            if item.created_at > kwargs['after']:
                yield item
    channel.history = history
    return fri, agent, channel, rows, post


def test_a_read_shows_what_posts_carry_and_looks_at_the_newest_images(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channel, rows, post = read_harness(monkeypatch, tmp_path)
        meme = post('', attachments=[picture(1, 'meme.png')], age=120)
        post('look at this', user=40, attachments=[picture(1, 'clip.mp4', 'video/mp4')], age=90)
        post('nice', user=50, age=30)
        loads(monkeypatch)
        actions = Actions(agent, 20, 999, False)
        actions.turn_context, actions.media_allowed = {}, True
        actions.event = SimpleNamespace(id=1, channel=SimpleNamespace(id=999))
        try:
            receipt = await actions.execute({'kind': 'read_recent', 'channel_id': '1', 'window': 'recent'})
            assert [(row['text'], row.get('attachments')) for row in receipt['messages']] == [
                ('', ['image']), ('look at this', ['video']), ('nice', None)]
            assert receipt['images_inspected'] == 1 and receipt['images_not_inspected'] == 1
            assert actions.turn_context['images'] == ['data:image/png;base64,meme.png']
            assert actions.turn_context['inspected_images'][0]['source']['id'] == str(meme.id)
            assert await actions.valid()
            await require_current(actions.image_context, actions.event)
            edited = copy.copy(meme)
            edited.content = 'actually never mind'
            rows[meme.id] = edited
            assert not await actions.valid()
            with pytest.raises(commands.UserFeedbackCheckFailure):
                await require_current(actions.image_context, actions.event)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_without_media_a_read_still_says_what_posts_carry(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, post = read_harness(monkeypatch, tmp_path)
        post('', attachments=[picture(1, 'meme.png')])
        loader = loads(monkeypatch)
        actions = Actions(agent, 20, 999, False)
        actions.turn_context = {}  # Ambient attention never inspects images.
        try:
            receipt = await actions.execute({'kind': 'read_recent', 'channel_id': '1', 'window': 'recent'})
            assert receipt['messages'][0]['attachments'] == ['image'] and receipt['images_inspected'] == 0
            assert not actions.turn_context and not loader.await_count
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_catch_up_names_attachments_without_looking(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, post = read_harness(monkeypatch, tmp_path)
        post('', attachments=[picture(1, 'meme.png')], age=60)
        loader = loads(monkeypatch)
        actions = Actions(agent, 20, 999, False)
        actions.turn_context, actions.media_allowed = {}, True
        try:
            receipt = await actions.execute({'kind': 'read_recent', 'channel_id': '1', 'window': 'since_my_last_message'})
            assert receipt['messages'][0]['attachments'] == ['image'] and not loader.await_count
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('opted_out', [False, True])
def test_an_uncaptioned_meme_shows_in_the_live_view_but_never_draws_an_ambient_reply(monkeypatch, tmp_path, opted_out):
    async def scenario():
        fri, agent, store, _, channel, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        if opted_out:
            store.personal_control('30', 'disable')
        meme = message('', user=30, attachments=[picture(channel.id, 'meme.png')])
        try:
            await agent.handle(meme)
            rows = agent.messages(1)
            if opted_out:
                assert not rows
                return
            assert [(row['id'], row['text'], row['attachments']) for row in rows] == [(str(meme.id), '', ['image'])]
            # Even when attention picks the bare image next to a real question, it isn't answered.
            question = message('anyone know why gene is broken?', user=40)
            await agent.handle(question)
            agent.direct.pop(str(question.id), None)
            agent.config.update(proactive=True, heartbeat_seconds=0)
            agent.run = AsyncMock(return_value=({'action': 'reply', 'source_id': str(meme.id)}, {}, 'completed'))
            agent.respond = AsyncMock()
            await agent.attend()
            assert [row['id'] for row in agent.run.await_args.args[2]['recent']] == [str(meme.id), str(question.id)]
            assert agent.run.await_args.args[2]['fresh_ids'] == [str(question.id)]
            agent.respond.assert_not_awaited()
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_media_names_kinds_never_urls_or_file_names():
    message = SimpleNamespace(attachments=[
        picture(1, 'secret-name.png'), picture(1, 'a.gif', 'image/gif'), picture(1, 'b.mp4', 'video/mp4'),
        picture(1, 'c.ogg', 'audio/ogg'), picture(1, 'd.zip', None),
        SimpleNamespace(url='x', content_type='image/png', is_spoiler=lambda: True)],
        embeds=[SimpleNamespace(type='gifv', video=None, image=None), SimpleNamespace(type='rich', video=None, image=None)])
    assert media(message) == ['image', 'gif', 'video', 'audio', 'file', 'spoiler image', 'linked gif']
    assert media(SimpleNamespace()) == [] and media(Mock(spec=discord.Message)) == []


def test_look_never_follows_embeds_or_another_posts_link(monkeypatch):
    async def scenario():
        loader = loads(monkeypatch)
        channel = SimpleNamespace(id=1)
        embedded = SimpleNamespace(id=2, channel=channel, content='', attachments=[picture(1, 'a.png')],
            embeds=[SimpleNamespace(type='image', video=None, image=SimpleNamespace(proxy_url='https://media.discordapp.net/external/x'))],
            author=SimpleNamespace(id=30, display_name='kev'), guild=None)
        context = {}
        assert await look(context, [embedded]) == {'images_inspected': 0, 'images_not_inspected': 1}
        assert not context and not loader.await_count
    asyncio.run(scenario())


def test_inspected_images_reach_the_model_after_the_text(monkeypatch):
    async def scenario():
        seen = []
        def handler(request):
            seen.append(json.loads(request.content))
            reply = {'response_kind': 'reply', 'reaction': None, 'reply': 'the square', 'request_draft': None,
                     'action': None, 'notes': [], 'allow_closing': False}
            return httpx.Response(200, text='data: '+json.dumps(response_event(content=json.dumps(reply)))+'\n\n')
        inspected = [{'images': [1], 'source': {'id': '2', 'user_id': '30'}}, {'images': [2], 'source': {'id': '1', 'user_id': '40'}}]
        context = {**CONTEXT, 'images': ['data:image/png;base64,AA==', 'data:image/png;base64,BB=='],
                   'inspected_images': inspected}
        result = await Chat(SimpleNamespace(access_token=lambda: 'test'), httpx.MockTransport(handler)).respond('which one', context)
        assert result.reply == 'the square'
        content = seen[0]['input'][-1]['content']
        assert [part['type'] for part in content] == ['input_text', 'input_image', 'input_image']
        assert json.loads(content[0]['text'])['inspected_images'] == inspected
        assert 'Never ask anyone to resend' in seen[0]['instructions']
        assert 'ambiguous_context_images' not in seen[0]['instructions']
    asyncio.run(scenario())


def test_the_memes_above_sass_case_shows_fri_the_images_and_grades_a_pick(grader):  # noqa: F811
    seen, _ = grader
    shown = []

    class Recording(Scripted):
        async def respond(self, text, context):
            shown.append(context)
            return await super().respond(text, context)

    case = next(case for case in sass_cases() if case['name'] == 'pick_from_many_memes')
    async def run():
        return {result['name']: result['passed'] async for result, _ in evaluate(Recording({
            case['text']: 'kev’s red circle. minimalism wins'}), 'gpt-6.1-sol', 'high', suite='sass')}
    results = asyncio.run(run())
    assert results['pick_from_many_memes'] and results['pick_from_many_memes:grade']
    context = next(context for context in shown if context['images'])
    assert len(context['images']) == 3 and [row['images'] for row in context['inspected_images']] == [[1], [2], [3]]
    graded = next(data for data in seen if data['situation'].get('image_notes'))
    assert 'red circle posted by Kev' in graded['situation']['image_notes']
    for reply in ('send them again and i’ll pick', 'which one?', "i can't see the images above"):
        async def again(reply=reply):
            return {result['name']: result['passed'] async for result, _ in evaluate(Scripted({
                case['text']: reply}), 'gpt-6.1-sol', 'high', suite='sass')}
        assert not asyncio.run(again())['pick_from_many_memes']


def test_time_is_measured_from_the_ping(monkeypatch, tmp_path):
    """Six hours back from the ping is near; a minute more is not."""
    async def scenario():
        fri, agent, _, _, channel, message, _, _ = environment(monkeypatch, tmp_path, channel_id=8)
        message('', user=30, age=6*3600-60, attachments=[picture(channel.id, 'near.png')])
        message('', user=40, age=6*3600+60, attachments=[picture(channel.id, 'far.png')])
        event = message(ASK)
        loads(monkeypatch)
        try:
            await agent.handle(event)
            context = fri.chat_once.await_args.args[1]
            assert context['images'] == ['data:image/png;base64,near.png']
            assert time.time()-discord.utils.snowflake_time(event.id).timestamp() < 60
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_the_controls_suite_expects_a_read_before_judging_memes():
    async def run(result):
        return {case['name']: case.get('passed') async for case, _ in evaluate(Scripted({
            'whats ur favorite meme out of the memes above': result}), 'gpt-6.1-sol', 'high', suite='controls')}
    def read(channel='555', window='recent', reply=''):
        return ChatResult(status='completed', reply=reply, action={'kind': 'read_recent', 'channel_id': channel, 'window': window})
    assert asyncio.run(run(read()))['memes_above_reads_first'] is True
    assert asyncio.run(run(read(channel=None, window=None)))['memes_above_reads_first'] is True
    # A catch-up never looks at images, another channel isn't where the memes are, and a read is no
    # excuse to ask for a resend.
    for wrong in (read(window='since_my_last_message'), read(channel='999'), read(reply='resend them and i’ll pick')):
        assert asyncio.run(run(wrong))['memes_above_reads_first'] is False
    assert asyncio.run(run('resend them and i’ll pick'))['memes_above_reads_first'] is False


@pytest.mark.parametrize('lost', ['access', 'membership', 'poster_opts_out_during_the_access_check'])
def test_a_channel_they_lose_mid_read_never_reaches_the_next_model_request(monkeypatch, tmp_path, lost):
    """Access is rechecked before each model request, not only before delivery: by then the images
    and text a read turned up would already have gone to the provider."""
    from compat_tests.test_fri_member_profiles import DM, GENERAL, model, world

    async def scenario():
        w = world(monkeypatch, tmp_path)
        general = w.channels[GENERAL]
        mid = discord.utils.time_snowflake(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=60))
        meme = Mock(spec=discord.Message, id=mid, content='', channel=general, guild=general.guild,
            author=SimpleNamespace(id=30, display_name='Kev', bot=False), webhook_id=None,
            attachments=[picture(GENERAL, 'meme.png')], embeds=[], edited_at=None,
            jump_url=f'https://discord.com/channels/123/{GENERAL}/{mid}', created_at=discord.utils.snowflake_time(mid))
        async def history(**kwargs):
            yield meme
        general.history, general.fetch_message = history, AsyncMock(return_value=meme)
        async def load(url, channel, **_):  # They lose the channel while its images download.
            if lost == 'access':
                general.permissions_for = lambda who: (discord.Permissions.none() if getattr(who, 'id', None) == 20
                                                       else discord.Permissions.all())
            elif lost == 'membership':
                general.guild.fetch_member.side_effect = discord.NotFound(SimpleNamespace(status=404, reason=''), 'left')
            else:  # The access check passes, but the poster opts out while it waits on Discord.
                async def fetched(user):
                    w.store.personal_control('30', 'disable')
                    return SimpleNamespace(id=user, bot=False)
                general.guild.fetch_member.side_effect = fetched
            return 'data:image/png;base64,meme.png'
        monkeypatch.setattr('fri.fri_images.load_image', AsyncMock(side_effect=load))
        sent = []
        async def respond(text, context):
            sent.append(list(context.get('images', [])))  # What this request carries, as it's sent.
            if len(sent) == 1:
                assert 'read_recent' in context['actions']
                return ChatResult(status='completed', action={'kind': 'read_recent', 'channel_id': str(GENERAL),
                                                              'window': 'recent'})
            return ChatResult(status='completed', reply='kev wins, the font alone')
        model(w, respond)
        text = 'fri whats the best meme in general rn'
        try:
            try:
                result = await w.fri.talk(123, DM, 20, text, 'private', 'member 20', event=w.message(DM, text))
                assert 'kev' not in result.reply.casefold()
            except commands.UserFeedbackCheckFailure:
                pass
            assert sent == [[]]
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_each_model_request_checks_access_first_and_privacy_last(monkeypatch, tmp_path):
    """Nothing waits between the privacy check and the request. Gene receipts count as sources too."""
    from compat_tests.test_fri_member_profiles import model, world

    async def scenario():
        w = world(monkeypatch, tmp_path)
        order = []
        async def respond(text, context):
            order.append('request')
            return ChatResult(status='completed', reply='ok')
        model(w, respond)
        async def channels(fresh=False, around=False):
            order.append('access')
            return readable
        async def api(method, path, payload=None):
            order.append(path)
            return {}
        w.fri.api = api
        w.fri.flush_usage = AsyncMock()
        # A Gene receipt from #1 on an ordinary turn: no read, so no private evidence.
        actions = SimpleNamespace(sources={'1'}, private_evidence=False, channels=channels, image_context={}, event=None,
                                  conversation={}, reads_unchanged=AsyncMock(return_value=True))
        context = {'model': 'm', 'effort': 'high', 'fence': {'turn': {'user_id': '20'}, 'actions': actions,
                                                              'shown': set()}}
        monkeypatch.setattr('fri.fri.turn_provenance', lambda *_: [])
        try:
            readable = {'1': object()}
            await w.fri.chat_once('hi', dict(context))
            assert order == ['access', '/chat/turn/check', 'request']
            order.clear()
            readable = {}
            with pytest.raises(commands.UserFeedbackCheckFailure):
                await w.fri.chat_once('hi', dict(context))
            assert order == ['access']
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_mention_read_and_retained_reports_record_where_they_came_from(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channel, _, post = read_harness(monkeypatch, tmp_path)
        post('nearby', age=60)
        event = Mock(spec=discord.Message, id=discord.utils.time_snowflake(dt.datetime.now(dt.timezone.utc)),
                     channel=channel, attachments=[], embeds=[], content='<@99> thoughts?')
        actions = Actions(agent, 20, channel.id, True)
        actions.event, actions.turn_context, actions.message = event, {}, event.content
        actions.invitation = SimpleNamespace(outside=False, require_current=AsyncMock(), collect=AsyncMock(),
            context=dict, look=AsyncMock(return_value={}), image=None, rows=[])
        try:
            await actions.execute({'kind': 'read_recent', 'channel_id': str(channel.id), 'window': 'recent'})
            assert actions.sources == {str(channel.id)}
        finally:
            await fri.close()
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        # A report kept from #2, which is no longer a learning channel.
        fri.api = AsyncMock(return_value={'history_channels': [], 'summaries': [],
                                          'reports': [{'id': 'r', 'channel_id': '2', 'body': 'memes'}]})
        owner = Actions(agent, 10, 999, False)
        try:
            await owner.execute({'kind': 'learning_overview', 'channel_id': ''})
            assert owner.sources == {'2'}
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_gallery_post_is_looked_at_whole_within_the_budget(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channel, rows, post = read_harness(monkeypatch, tmp_path)
        post('', attachments=[picture(1, f'big{n}.png') for n in range(5)], age=60)  # Over the budget alone.
        gallery = post('three for the price of one', attachments=[picture(1, f'g{n}.png') for n in range(3)], age=120)
        loads(monkeypatch)
        actions = Actions(agent, 20, 999, False)
        actions.turn_context, actions.media_allowed = {}, True
        actions.event = SimpleNamespace(id=1, channel=SimpleNamespace(id=999))
        try:
            receipt = await actions.execute({'kind': 'read_recent', 'channel_id': '1', 'window': 'recent'})
            assert receipt['images_inspected'] == 1 and receipt['images_not_inspected'] == 1
            assert actions.turn_context['images'] == [f'data:image/png;base64,g{n}.png' for n in range(3)]
            assert actions.turn_context['inspected_images'] == [
                {'images': [1, 2, 3], 'source': actions.turn_context['inspected_images'][0]['source']}]
            assert await actions.valid()
            trimmed = copy.copy(gallery)
            trimmed.attachments = gallery.attachments[:2]
            rows[gallery.id] = trimmed
            assert not await actions.valid()
        finally:
            await fri.close()
    asyncio.run(scenario())
