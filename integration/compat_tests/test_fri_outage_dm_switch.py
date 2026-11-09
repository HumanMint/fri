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


def switched_off_while_read(agent, dm):
    """DMs go off while the outage handling reads the DM again from Discord."""
    fetch = dm.channel.fetch_message.side_effect

    async def fetching(mid):
        agent.config['support_dms'] = False
        return fetch(mid)
    dm.channel.fetch_message = AsyncMock(side_effect=fetching)


@pytest.mark.parametrize('off', [True, False], ids=['dms switched off', 'nothing'])
def test_the_outage_notice_waits_for_the_dm_switch(monkeypatch, tmp_path, off):
    """The issue's repro: the final check of the turn finds the controller down; DMs go off while the outage handling
    reads the DM again. No notice goes out in a DM that's off."""
    async def scenario():
        fri, agent, asked, calls, answered = asking_after_saved(monkeypatch, tmp_path, 'dm')
        fri.capabilities = frozenset((await fri.api('GET', '/fri/contract'))['capabilities'])
        api = fri.api

        async def down(method, path, payload=None):
            checks = [p for p, _ in calls[answered[0]:] if p == '/chat/turn/check'] if answered else []
            if path == '/chat/turn/check' and len(checks) == 1:  # The final fence's.
                calls.append((path, payload))
                if off:
                    switched_off_while_read(agent, asked)
                raise ControllerUnavailable('down')
            return await api(method, path, payload)
        fri.api = down
        try:
            await agent.handle(asked)
            assert 'you said chess' not in said(asked)
            assert (UNREACHABLE in said(asked)) is not off
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
