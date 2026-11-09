"""The outage notice and the DM switch (fri #396).

The controller down at a DM's final check sends its outage notice through the outage handling, which reads the message
from Discord again first. DMs switched off during that read still let the notice go. The switch is read again right
before it, as before any other DM send, a memory control's answer excepted: a control is carried out, and told, with
DMs off too.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_preview_first_page import said
from compat_tests.test_fri_rerun_fence import direct
from compat_tests.test_fri_send_fence_followups import asking_after_saved
from fri.fri_errors import UNREACHABLE, ControllerUnavailable


def switched_off_while_read(agent, message, at=1):
    """DMs go off while the outage handling reads the message again from Discord: its `at`th read of it (the last
    is the notice's own, that it's still addressed to Fri)."""
    fetch, reads = message.channel.fetch_message.side_effect, []

    async def fetching(mid):
        reads.append(mid)
        if len(reads) == at:
            agent.config['support_dms'] = False
        return fetch(mid)
    message.channel.fetch_message = AsyncMock(side_effect=fetching)
    return reads


@pytest.mark.parametrize('off', ['during its first read', 'during its last read', 'nothing'])
def test_the_outage_notice_waits_for_the_dm_switch(monkeypatch, tmp_path, off):
    """The issue's repro: the final check of the turn finds the controller down; DMs go off while the outage handling
    reads the DM again, the last read before the notice included. No notice goes out in a DM that's off."""
    async def scenario():
        fri, agent, asked, calls, answered = asking_after_saved(monkeypatch, tmp_path, 'dm')
        fri.capabilities = frozenset((await fri.api('GET', '/fri/contract'))['capabilities'])
        api, reads = fri.api, []

        async def down(method, path, payload=None):
            checks = [p for p, _ in calls[answered[0]:] if p == '/chat/turn/check'] if answered else []
            if path == '/chat/turn/check' and len(checks) == 1:  # The final fence's.
                calls.append((path, payload))
                if off != 'nothing':
                    reads.append(switched_off_while_read(agent, asked, 2 if off == 'during its last read' else 1))
                raise ControllerUnavailable('down')
            return await api(method, path, payload)
        fri.api = down
        try:
            await agent.handle(asked)
            assert 'you said chess' not in said(asked)
            assert (UNREACHABLE in said(asked)) is (off == 'nothing')
            if off == 'during its last read':
                assert len(reads[0]) == 2  # The outage handling's read, then the notice's own.
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_channels_outage_notice_goes_with_dms_switched_off(monkeypatch, tmp_path):
    """The switch is the DMs': a chat channel's message is told the controller is down whatever it says."""
    async def scenario():
        fri, agent, store, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        fri.capabilities = frozenset((await fri.api('GET', '/fri/contract'))['capabilities'])
        agent.config['support_dms'] = False
        asked = message('fri what did i say')
        asked.reply.return_value = SimpleNamespace(id=asked.id+100, content='okay', edit=AsyncMock())
        fri.chat_once = AsyncMock(side_effect=ControllerUnavailable('down'))
        try:
            await agent.handle(asked)
            assert UNREACHABLE in said(asked)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_memory_controls_outage_notice_goes_with_dms_switched_off(monkeypatch, tmp_path):
    """A memory control is told how it went with DMs off too: its outage notice still goes."""
    async def scenario():
        fri, agent, store, _, _, _, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        fri.capabilities = frozenset((await fri.api('GET', '/fri/contract'))['capabilities'])
        _, message = direct(fri, agent, rows)
        dm = message('forget me')
        dm.reply.return_value = SimpleNamespace(id=dm.id+100, content='okay', edit=AsyncMock())
        api = fri.api

        async def down(method, path, payload=None):
            if path == '/chat/memory/control':
                switched_off_while_read(agent, dm)
                raise ControllerUnavailable('down')
            return await api(method, path, payload)
        fri.api = down
        try:
            await agent.handle(dm)
            assert UNREACHABLE in said(dm)
        finally:
            await fri.close()
    asyncio.run(scenario())
