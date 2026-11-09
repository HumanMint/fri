"""Fri's behavior notes can name members ("learn more about bob's music recs"), so the owner sees them, and previews
of their new text, only in their DM. Public settings metadata and numeric previews still answer in a channel."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fri.fri_agent import Agent
from fri.fri_agent_settings import DEFAULTS

OWNER, GUILD, CHANNEL = 10, 123, 456
NOTES = {'attention_notes': 'ignore bob when he rants about his ex', 'learning_notes': 'learn about alice’s job hunt'}
PRIVATE = ('bob', 'alice')


def owner_agent():
    state = {'config': {**DEFAULTS, 'enabled': True, **NOTES}, 'version': 7,
             'channels': [str(CHANNEL)], 'history_channels': []}
    fri = SimpleNamespace(owner_id=OWNER, guild_id=GUILD, workshop_id=CHANNEL, user=SimpleNamespace(id=99),
                          api=AsyncMock(return_value=state))
    agent = Agent(fri)
    agent.state = state
    agent.profile = AsyncMock(return_value={'enabled': True, 'revision': 0})
    agent.settings_requested = AsyncMock(return_value=True)
    agent.dm_closed = AsyncMock(return_value=None)
    return agent


def owner_message(text, public):
    sent = []

    async def reply(content=None, **kwargs):
        attached = kwargs.get('file')
        if attached is not None:
            attached.fp.seek(0)
            kwargs['file'] = attached.fp.read().decode()
        sent.append((content, kwargs))

    @asynccontextmanager
    async def typing():
        yield
    channel = SimpleNamespace(id=CHANNEL, typing=typing)
    return SimpleNamespace(id=1, guild=SimpleNamespace(id=GUILD) if public else None, channel=channel,
                           author=SimpleNamespace(id=OWNER), content=text, reply=reply), sent


def shown(sent):
    return ' '.join(f'{content} {kwargs.get("file", "")}' for content, kwargs in sent)


def test_settings_listing_keeps_behavior_notes_in_the_owners_dm():
    async def run():
        agent = owner_agent()
        message, sent = owner_message('show current settings', public=True)
        assert await agent.owner_control(message, message.content) is True
        assert len(sent) == 1 and 'file' not in sent[0][1]
        assert not any(name in shown(sent) for name in PRIVATE)
        assert '"heartbeat_seconds"' in sent[0][0]  # The public metadata still answers there.
        message, sent = owner_message('show current settings', public=False)
        assert await agent.owner_control(message, message.content) is True
        assert json.loads(sent[0][1]['file']) == {'voice_notes': '', **NOTES}
    asyncio.run(run())


def test_a_notes_preview_is_shown_only_in_the_owners_dm():
    async def run():
        replaced = 'ignore bob when he rants, and skip carol’s spoilers'
        notes_change = {'changes': [{'key': 'attention_notes', 'json_value': json.dumps(replaced)}]}
        numeric = {'changes': [{'key': 'heartbeat_seconds', 'json_value': '600'}]}
        for change, public, previewed in ((notes_change, True, False), (numeric, True, True),
                                          (notes_change, False, True)):
            agent = owner_agent()
            agent.run = AsyncMock(return_value=(change, {}, 'completed'))
            message, sent = owner_message('change your attention settings', public)
            assert await agent.owner_control(message, message.content) is True
            assert len(sent) == 1
            assert ('view' in sent[0][1]) is previewed
            assert ('carol' in shown(sent)) is (previewed and change is notes_change)
    asyncio.run(run())
