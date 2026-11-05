"""#320's pre-existing findings (fri #322): the settings helper's own preview keeps the owner's fence for paging and
Apply, a preview from a DM reply takes its baseline before the turn waits, and My memory's summary waits for a
withdrawal Fri took in."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from compat_tests.test_fri_actions import harness
from compat_tests.test_fri_edit_reanswer import model as respond_with
from compat_tests.test_fri_member_profiles import GENERAL, world
from compat_tests.test_fri_member_summaries import ASH, SUMMARY, settled, summarized, summarizer
from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_panel_and_learning_fences import shown, skipped
from compat_tests.test_fri_panel_erasure_sources import MARKER, VOICE, click
from compat_tests.test_fri_panel_fences_2 import DM_SCOPE, dm_panel, listed, lock_held_after
from compat_tests.test_fri_rerun_fence import direct
from compat_tests.test_fri_settings_preview import gate, owner_message
from fri.fri_agent import DEFAULTS
from fri.fri_chat import ChatResult
from fri.fri_member_summary import build
from fri.fri_memory import profiled
from fri.fri_memory_panel import show


@pytest.mark.parametrize('moved', ['local', 'gene', 'nothing'])
@pytest.mark.parametrize('step', ['next', 'apply'])
def test_the_settings_helpers_own_preview_keeps_the_owners_fence(monkeypatch, tmp_path, step, moved):
    """fri #322 (1): the owner's settings request makes a two-page voice preview through the settings helper, whose
    erasure check guarded only its creation. The owner erases, here or through Gene, then presses Next, or Apply
    after reading every page: the erased words showed, or were saved. The check stays the preview's fence: nothing
    more of it shows or is saved, and the preview ends. With nothing changed it pages and saves as before."""
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        saved, revision = [], [0]

        async def api(method, path, payload=None):
            if path == '/agent/settings':
                saved.append(payload)
                return {**agent.state, 'version': 4}
            return {**agent.state, 'config': {**DEFAULTS, 'enabled': True, 'support_dms': True}, 'version': 3}
        fri.api = api
        agent.profile = AsyncMock(side_effect=lambda scope, user: {'enabled': True, 'revision': revision[0],
                                                                   'summary': ''})
        gate(monkeypatch)
        agent.record = AsyncMock()
        agent.member = AsyncMock(return_value=SimpleNamespace(id=10))
        agent.run = AsyncMock(return_value=({'explanation': '', 'changes': [
            {'key': 'voice_notes', 'json_value': json.dumps(VOICE)}]}, {}, 'completed'))
        message = owner_message()
        try:
            assert await agent.owner_control(message, 'make your voice drier') is True
            view = message.reply.await_args.kwargs['view']
            assert len(view.pages) == 2 and MARKER not in view.content
            view.interaction_check = AsyncMock(return_value=True)
            if step == 'apply':
                await view.next(click())  # Every page read before anything changed: Apply is enabled.
                assert not view.confirm_button.disabled
            if moved == 'local':
                agent.forget(10, erased=True)
            elif moved == 'gene':
                revision[0] = 1
            if step == 'next':
                pressed = click()
                await view.next(pressed)
                content = pressed.response.edit_message.await_args.kwargs['content']
                assert (MARKER in content) is (moved == 'nothing')
            else:
                pressed = SimpleNamespace(user=SimpleNamespace(id=10), response=SimpleNamespace(defer=AsyncMock()),
                                          edit_original_response=AsyncMock())
                await view.apply(pressed)
                assert bool(saved) is (moved == 'nothing')
            assert view.finished is (moved != 'nothing' or step == 'apply')
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('moved', ['local', 'nothing'])
def test_a_dm_replys_preview_takes_its_baseline_before_the_turn_waits(monkeypatch, tmp_path, moved):
    """fri #322 (2): the owner's DM makes a voice preview carrying member 30's words through an ordinary reply.
    During the DM's membership check after the model answered, member 30's "forget me" lands here, before Gene has
    it. The preview's fence took its baseline after that, so Next showed their marker. Its baseline is every
    member's erasures here as the turn began: nothing more of it shows. Since fri #324 item 1, nothing of it goes out
    at all: its first page is held to that fence too."""
    async def scenario():
        fri, agent, store, _, _, _, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        channel, message = direct(fri, agent, rows)
        answered, erased = [], []

        async def member(*args, **kwargs):
            if answered and not erased and moved == 'local':
                erased.append(agent.forget(30, erased=True))  # The membership check after the model answered.
            return SimpleNamespace(id=10)
        agent.member = AsyncMock(side_effect=member)
        store.save_chat('123', 'private:555', '10', 0, 'what did they say', 'member thirty plays the oud', 'model',
                        'high', provenance=[ASH])

        async def respond(text, context, ready):
            await ready()
            answered.append(True)
            return ChatResult(status='completed', action={'kind': 'preview_voice', 'voice_notes': VOICE})
        respond_with(fri, tmp_path, respond)
        dm = message('sharper voice please')
        dm.author = SimpleNamespace(id=10, display_name='owner', bot=False)
        dm.reply.return_value = SimpleNamespace(id=dm.id+100, content='okay', edit=AsyncMock())
        try:
            await agent.handle(dm)
            assert bool(erased) is (moved == 'local')
            if moved == 'local':  # fri #324 item 1: its first page is held to the turn's whole fence too.
                assert 'view' not in dm.reply.await_args.kwargs and MARKER not in str(dm.reply.await_args_list)
                return
            view = dm.reply.await_args.kwargs['view']
            assert len(view.pages) == 2 and MARKER not in view.content
            view.interaction_check = AsyncMock(return_value=True)
            pressed = click()
            await view.next(pressed)
            content = pressed.response.edit_message.await_args.kwargs['content']
            assert (MARKER in content) is (moved == 'nothing')
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('moved', ['deleted', 'nothing'])
def test_my_memorys_summary_waits_for_a_withdrawal_fri_took_in(monkeypatch, tmp_path, moved):
    """fri #322 (3): My memory reads member 20's summary, screens it with nothing held back, and waits for the
    panel's lock. A message under that summary is deleted, and its withdrawal stays queued here, before Gene has
    it: Gene still accepts the summary's generation, and the summary showed. The withdrawals Fri holds are part of
    the fence, before and after Gene's check: nothing of it shows until the withdrawal is taken."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            posted = w.message(GENERAL, 'ash says the raid is at nine')  # Discord holds it (fri #333).
            source = {'kind': 'discord_message', 'id': str(posted.id),
                      'user_id': '20', 'display_name': 'friend', 'channel_id': str(GENERAL), 'guild_id': '123'}
            w.store.save_chat('123', 'public:1', '20', 0, 'ash says the raid is at nine', 'ok', 'm', 'high',
                              source=source, provenance=[ASH], audience='server')
            event = w.message(GENERAL, 'fri anyway')
            await w.say(GENERAL, 'fri anyway', event=event)
            under = event.id  # A message of the exchange the summary is built from.
            settled(w, 13*3600)
            summarizer(monkeypatch, w)
            assert await build(w.fri.agent) == {'kept': True} and summarized(w)
            monkeypatch.setattr('fri.fri_memory_panel.allowed', AsyncMock(return_value=True))
            turns = w.fri.agent.saved_turns
            turns.kick = lambda: None  # Its withdrawal stays queued here: Gene hasn't taken it yet.
            interaction = dm_panel(w.fri)
            deleting = []

            def delete():
                if moved == 'deleted':
                    task = asyncio.get_running_loop().create_task(w.fri.on_raw_message_delete(
                        SimpleNamespace(channel_id=GENERAL, guild_id=123, message_id=under)))
                    deleting.append(task)
            taken = lock_held_after(w.fri, (None, 20), '/chat/memory/notes', delete)
            await show(w.fri, interaction, profiled(w.fri, dict(DM_SCOPE)))
            await asyncio.gather(*deleting)
            assert taken and turns.withholding() is (moved == 'deleted')
            if moved == 'nothing':
                assert SUMMARY in shown(interaction)
            else:
                assert listed(interaction, SUMMARY) == 0 and shown(interaction) == skipped()
        finally:
            await w.fri.close()
    asyncio.run(scenario())
