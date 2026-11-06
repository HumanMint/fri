"""HumanMint/gene#262 and Sol round 1 of gene #264, S1: a learning pass Fri stops before its model is asked says so in
its finish, and the controller gives the batch's reservation back; one that ends otherwise says nothing, and the
reservation stays.

A pass reserves a batch of the day's scan allowance when it claims it. One stopped before its summary request (a
channel's withdrawal accepted while it read, the channel gone, a failed sign-in) asked nothing, and its slice is read
again, so a controller offering it gives the reservation back. One whose model answered incompletely did ask: its
reservation stays, as main's does, or passes in a row would send the model more than the day's allowance. An older
controller ignores the flag, and keeps every error's reservation.
"""
import asyncio

import pytest

from compat_tests.test_fri_learning import learning, value  # noqa: F401 (the fixture)
from compat_tests.test_fri_panel_and_learning_fences import batch


def gives_back():
    """Whether the controller under test reads the flag (gene #264), as its finish says."""
    from gene.fri_agent_api import Finish
    return 'before_model' in Finish.model_fields


def test_a_pass_a_channels_withdrawal_stopped_before_its_model_says_so(learning, monkeypatch):  # noqa: F811
    async def scenario():
        agent, store, _, finishes, asked, _ = batch(learning, monkeypatch)
        agent.fri.capabilities = frozenset(agent.fri.capabilities) | {'author-fence'}
        api, went = agent.fri.api, []

        async def checking(method, path, payload=None):
            if path == '/agent/authors/check' and not went:
                went.append(True)
                agent.channel_withdrawals.taken((), channels=['1'])  # Its own channel, while its authors are checked.
            return await api(method, path, payload)
        agent.fri.api = checking
        await agent.learn()
        assert asked == [] and finishes[-1]['error'] and finishes[-1]['before_model'] is True
        assert value(store, 'budget')['used'] == (0 if gives_back() else 30)
    asyncio.run(scenario())


def test_a_pass_whose_channel_is_gone_says_so(learning, monkeypatch):  # noqa: F811
    async def scenario():
        agent, store, _, finishes, asked, _ = batch(learning, monkeypatch)
        agent.fri.get_channel = lambda _: None
        await agent.learn()
        assert asked == [] and finishes[-1]['error'] and finishes[-1]['before_model'] is True
        assert value(store, 'budget')['used'] == (0 if gives_back() else 30)
    asyncio.run(scenario())


def test_a_pass_whose_sign_in_failed_says_so(learning, monkeypatch):  # noqa: F811
    from fri.subscription import SubscriptionError

    async def scenario():
        agent, store, _, finishes, asked, _ = batch(learning, monkeypatch)

        async def refused():
            raise SubscriptionError('signed out')
        agent.fri.chat.headers = refused
        await agent.learn()
        assert asked == [] and finishes[-1]['error'] and finishes[-1]['before_model'] is True
    asyncio.run(scenario())


@pytest.mark.parametrize('status', ['incomplete', 'failed'])
def test_the_s1_repro_a_pass_whose_model_answered_incompletely_says_nothing_and_keeps_its_reservation(
        learning, monkeypatch, status):  # noqa: F811
    """S1: the model was asked, and used tokens, but its answer didn't complete. Its reservation stays."""
    async def scenario():
        agent, store, _, finishes, asked, _ = batch(learning, monkeypatch)

        async def answered(chat, instruction, data, model, effort, schema, **kwargs):
            asked.append(data)
            return {}, {'input_tokens': 1200, 'output_tokens': 40}, status
        monkeypatch.setattr('fri.fri_agent.analyze', answered)
        await agent.learn()
        assert asked and finishes[-1]['error'] and 'before_model' not in finishes[-1]
        assert value(store, 'budget')['used'] == 30
    asyncio.run(scenario())


def test_a_pass_that_completes_or_erased_everything_sends_no_flag(learning, monkeypatch):  # noqa: F811
    async def scenario():
        agent, _, _, finishes, asked, _ = batch(learning, monkeypatch)
        await agent.learn()
        assert asked and not finishes[-1]['error'] and 'before_model' not in finishes[-1]
    asyncio.run(scenario())
