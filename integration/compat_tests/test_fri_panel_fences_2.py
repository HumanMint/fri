"""#309's pre-existing findings: My memory's summary and a supplied state (fri #315), and request drafts' and
settings previews' fences (fri #316). Each shows nothing a member erased since it was read, here or through Gene."""

import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from compat_tests.test_fri_edit_reanswer import model as respond_with
from compat_tests.test_fri_member_profiles import GENERAL, world
from compat_tests.test_fri_member_summaries import ASH, SUMMARY, settled, summarized, summarizer
from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_panel_and_learning_fences import panel, shown, skipped
from compat_tests.test_fri_panel_erasure_sources import MARKER, VOICE, click, drew_on_thirty
from compat_tests.test_fri_rerun_fence import direct
from compat_tests.test_fri_turn_fences import history_source
from fri import feedback as commands
from fri.fri_chat import ChatResult
from fri.fri_member_summary import build
from fri.fri_memory import profiled
from fri.fri_memory_panel import show
from fri.panels import PrivatePanels

DM_SCOPE = {'guild_id': '123', 'user_id': '20', 'channel_id': '456', 'conversation_id': 'private:900'}


def dm_panel(fri):
    """Member 20's My memory in their DM with Fri, its panel edits recorded."""
    fri.private_panels = PrivatePanels()
    interaction = Mock(spec=discord.Interaction)
    interaction.id = discord.utils.time_snowflake(dt.datetime.now(dt.timezone.utc))
    interaction.user = SimpleNamespace(id=20, display_name='friend')
    interaction.guild_id, interaction.channel_id = None, 900
    interaction.response = SimpleNamespace(is_done=lambda: True)
    interaction.message = SimpleNamespace(id=1234, flags=SimpleNamespace(ephemeral=True))
    interaction.edit_original_response = AsyncMock(return_value=SimpleNamespace(id=1234))
    interaction.delete_original_response = AsyncMock()
    return interaction


def listed(interaction, words):
    """How many of the panel's edits showed these words."""
    return sum(words in ' '.join(embed.description or '' for embed in call.kwargs.get('embeds') or [])
               for call in interaction.edit_original_response.await_args_list)


def lock_held_after(fri, key, path, moved):
    """Another edit of the panel takes its lock once `path` is read, and lets it go after `moved()` runs."""
    lock = fri.private_panels.locks.setdefault(key, asyncio.Lock())
    api, taken = fri.api, []

    async def holder():
        while not getattr(lock, '_waiters', None):
            await asyncio.sleep(0)
        moved()
        lock.release()

    async def recording(method, path_, payload=None):
        answer = await api(method, path_, payload)
        if path_ == path and not taken:
            taken.append(path_)
            await lock.acquire()
            asyncio.get_running_loop().create_task(holder())
        return answer
    fri.api = recording
    return taken


@pytest.mark.parametrize('moved', ['local', 'gene', 'nothing'])
def test_my_memorys_summary_is_held_to_the_erasures_of_whose_words_it_drew_on(monkeypatch, tmp_path, moved):
    """fri #315 (1): member 20's summary drew on member 30's words. My memory reads it, then waits for the panel's
    lock while member 30 erases, here or through Gene (which drops the summary, but leaves member 20's revision
    where it was). The old summary showed. Its sources' erasures here and Gene's check of it are run inside the
    lock: nothing of it shows. With nothing changed it's listed as before."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            posted = w.message(GENERAL, 'ash says the raid is at nine')  # Discord holds it (fri #333).
            source = {'kind': 'discord_message', 'id': str(posted.id),
                      'user_id': '20', 'display_name': 'friend', 'channel_id': str(GENERAL), 'guild_id': '123'}
            w.store.save_chat('123', 'public:1', '20', 0, 'ash says the raid is at nine', 'ok', 'm', 'high',
                              source=source, provenance=[ASH], audience='server')
            await w.say(GENERAL, 'fri anyway')
            settled(w, 13*3600)
            summarizer(monkeypatch, w)
            assert await build(w.fri.agent) == {'kept': True} and summarized(w)['provenance'] == [ASH]
            monkeypatch.setattr('fri.fri_memory_panel.allowed', AsyncMock(return_value=True))
            interaction = dm_panel(w.fri)
            taken = lock_held_after(w.fri, (None, 20), '/chat/memory/notes', {
                'local': lambda: w.fri.agent.forget(30, erased=True),
                'gene': lambda: w.store.personal_control('30', 'disable'), 'nothing': lambda: None}[moved])
            await show(w.fri, interaction, profiled(w.fri, dict(DM_SCOPE)))
            assert taken
            if moved == 'nothing':
                assert SUMMARY in shown(interaction)
            else:
                assert listed(interaction, SUMMARY) == 0 and shown(interaction) == skipped()
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('moved', ['local', 'nothing'])
def test_a_supplied_listing_is_held_to_an_erasure_during_the_membership_lookup(monkeypatch, tmp_path, moved):
    """fri #315 (2): a removal's answer is the listing, and My memory shows it once it has checked who's asking. A
    "forget me" taken in here during that lookup became the baseline, and the earlier listing showed. The baseline
    is taken before that wait, and the listing read under it: nothing of it shows."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            for evidence, category in (('i like chess', 'games'), ('i like tea', 'food')):
                w.store.save_chat('123', 'private:900', '20', 0, evidence, 'noted', 'm', 'high',
                                  notes=[{'category': category, 'evidence': evidence}])
            scope = profiled(w.fri, dict(DM_SCOPE))
            with w.store.connection() as db:
                db.execute("DELETE FROM personal_notes WHERE evidence='i like tea'")  # As a removal leaves it.
            state = await w.fri.api('POST', '/chat/memory/notes', scope)
            assert [entry['evidence'] for entry in state['entries']] == ['i like chess']

            async def checked(*args, **kwargs):
                if moved == 'local':
                    w.fri.agent.forget(20, erased=True)  # Taken in here, before Gene has it.
                return True
            monkeypatch.setattr('fri.fri_memory_panel.allowed', checked)
            interaction = dm_panel(w.fri)
            await show(w.fri, interaction, scope, state=state, notice='Removed that preference.')
            if moved == 'nothing':
                assert 'i like chess' in shown(interaction) and 'Removed that preference.' in shown(interaction)
            else:
                assert listed(interaction, 'i like chess') == 0 and shown(interaction) == skipped()
        finally:
            await w.fri.close()
    asyncio.run(scenario())


async def draft_from_thirty(monkeypatch, tmp_path, marker_page):
    """Member 20's request draft built from member 30's words, their marker on page `marker_page`."""
    fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
    store.save_chat('123', 'public:1', '20', 0, 'fri what did ash say?', 'something about '+MARKER, 'm', 'high',
                    source=history_source(), provenance=[ASH])
    draft = ('Add a checklist. '+'ordinary detail '*50+'\n' if marker_page == 2 else '')+'Mention '+MARKER
    fri.chat_once.return_value = ChatResult(status='completed', reply='here is the draft', draft=draft)
    event = message('fri draft a feature request from that earlier discussion')
    result = await fri.talk(123, 1, 20, event.content, 'public', event=event)
    assert result.turn['provenance'] == [ASH]
    return fri, agent, store, channel, result


@pytest.mark.parametrize('moved', ['local', 'nothing'])
@pytest.mark.parametrize('step', ['next', 'preview'])
def test_a_drafts_pages_are_held_to_its_sources_erasures_here(monkeypatch, tmp_path, step, moved):
    """fri #316 (1): a request draft built from member 30's words checked only Gene before its preview and each
    page, which can't see a "forget me" Fri took in and hasn't sent on. Member 30's lands here, then the preview
    opens or Next is pressed: their marker showed. Their erasures here are checked with Gene's: nothing of it
    shows."""
    async def scenario():
        from fri.fri_requests import DraftView
        fri, agent, store, channel, result = await draft_from_thirty(monkeypatch, tmp_path, 2 if step == 'next' else 1)
        try:
            view = DraftView(fri, 20, result.draft, reviewed=step == 'next', fence=result.turn)
            if step == 'next':
                assert len(view.pages) > 1 and MARKER not in view.content
            if moved == 'local':
                agent.forget(30, erased=True)
            pressed = SimpleNamespace(user=SimpleNamespace(id=20), guild_id=None, channel_id=555, channel=channel,
                                      message=SimpleNamespace(author=fri.user, flags=SimpleNamespace(ephemeral=False)),
                                      response=SimpleNamespace(edit_message=AsyncMock()))
            view.interaction_check = AsyncMock(return_value=True)
            try:
                await (view.next(pressed) if step == 'next' else view.preview(pressed))
            except commands.UserFeedbackCheckFailure:
                pass
            edits = [str(call.kwargs.get('content')) for call in pressed.response.edit_message.await_args_list]
            if moved == 'nothing':
                assert any(MARKER in text for text in edits)
            else:
                assert not any(MARKER in text for text in edits)
        finally:
            await fri.close()
    asyncio.run(scenario())


async def owners_preview(monkeypatch, tmp_path):
    """The owner's two-page voice preview from the /fri panel, drawing on member 30's words (see #309's test)."""
    fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
    interaction = panel(fri)
    interaction.user = SimpleNamespace(id=10, display_name='owner')
    drew_on_thirty(store, user='10')

    async def respond(text, context, ready):
        await ready()
        return ChatResult(status='completed', action={'kind': 'preview_voice', 'voice_notes': VOICE})
    respond_with(fri, tmp_path, respond)
    await fri.chat_interaction(interaction, 'sharper voice please')
    view = interaction.edit_original_response.await_args.kwargs['view']
    view.interaction_check = AsyncMock(return_value=True)
    await view.next(click())  # Every page read, nothing changed yet: Apply is enabled.
    assert not view.confirm_button.disabled
    return fri, agent, store, view


@pytest.mark.parametrize('moved', ['gene', 'local', 'nothing'])
def test_apply_on_a_settings_preview_is_held_to_its_fence(monkeypatch, tmp_path, moved):
    """fri #316 (2): every page of the owner's preview read, member 30 erases, through Gene or here, and the owner
    presses Apply: the settings were saved with member 30's marker, for any later model request to read. The
    preview's whole fence is run inside Apply's lock, right before the save: nothing is saved and the preview ends.
    With nothing changed it's saved as before."""
    async def scenario():
        fri, agent, store, view = await owners_preview(monkeypatch, tmp_path)
        try:
            if moved == 'gene':
                store.personal_control('30', 'disable')
            elif moved == 'local':
                agent.forget(30, erased=True)
            api, saved = fri.api, []

            async def recording(method, path, payload=None):
                if path == '/agent/settings':
                    saved.append(payload)
                return await api(method, path, payload)
            fri.api = recording
            pressed = SimpleNamespace(user=SimpleNamespace(id=10), response=SimpleNamespace(defer=AsyncMock()),
                                      edit_original_response=AsyncMock())
            await view.apply(pressed)
            if moved == 'nothing':
                assert saved and MARKER in str(saved)
            else:
                assert not saved and view.finished
                assert MARKER not in str(pressed.edit_original_response.await_args_list)
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('moved', ['gene', 'local', 'nothing'])
def test_a_settings_preview_from_a_dm_reply_is_held_to_its_fence(monkeypatch, tmp_path, moved):
    """fri #316 (3): the owner's DM to Fri makes a two-page voice preview through an ordinary reply, drawing on
    member 30's saved words, their marker on page two. Member 30 erases, then the owner presses Next: the marker
    showed, since a preview made there had no fence. It's bound to the turn's whole fence too: nothing more of it
    shows."""
    async def scenario():
        fri, agent, store, _, _, _, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        channel, message = direct(fri, agent, rows)
        agent.member = AsyncMock(return_value=SimpleNamespace(id=10))
        store.save_chat('123', 'private:555', '10', 0, 'what did they say', 'member thirty plays the oud', 'model',
                        'high', provenance=[ASH])

        async def respond(text, context, ready):
            await ready()
            return ChatResult(status='completed', action={'kind': 'preview_voice', 'voice_notes': VOICE})
        respond_with(fri, tmp_path, respond)
        dm = message('sharper voice please')
        dm.author = SimpleNamespace(id=10, display_name='owner', bot=False)
        dm.reply.return_value = SimpleNamespace(id=dm.id+100, content='okay', edit=AsyncMock())
        try:
            await agent.handle(dm)
            view = dm.reply.await_args.kwargs['view']
            assert len(view.pages) == 2 and MARKER not in view.content and MARKER in view.pages[1]
            view.interaction_check = AsyncMock(return_value=True)
            if moved == 'gene':
                store.personal_control('30', 'disable')
            elif moved == 'local':
                agent.forget(30, erased=True)
            pressed = click()
            await view.next(pressed)
            content = pressed.response.edit_message.await_args.kwargs['content']
            if moved == 'nothing':
                assert MARKER in content
            else:
                assert MARKER not in content and view.finished
        finally:
            await fri.close()
    asyncio.run(scenario())
