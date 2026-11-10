"""Local withdrawal fences for confirmed delivery."""
import asyncio
import logging
import time

HOLD_WAIT_SECONDS = 90
HOLD_POLL_SECONDS = 1.5
LOG = logging.getLogger(__name__)


class DeliveryHoldTimeout(RuntimeError):
    pass


class DeliveryFence:
    def __init__(self, agent, before, kind):
        self.agent, self.before, self.kind = agent, before, kind
        self.deadline = None

    @staticmethod
    def mark(agent):
        return agent.taken_back_mark(), agent.channel_withdrawals.generation

    def current(self):
        return (not self.agent.reports_held() and not self.agent.channel_withdrawals.holding()
                and self.before == self.mark(self.agent))

    async def available(self):
        if not self.current():
            return None
        config = (await self.agent.fri.api('GET', '/agent/state'))['config']
        return config if self.current() else None

    def claiming(self):
        # Start before the claim request: slow controller I/O must not extend the 120-second lease.
        self.deadline = time.monotonic()+HOLD_WAIT_SECONDS

    async def run(self, check):
        """After any wait, repeat Discord, switch and controller fences; never renew a claim."""
        limit = asyncio.timeout_at(self.deadline)
        try:
            async with limit:
                while True:
                    while self.agent.reports_held() or self.agent.channel_withdrawals.holding():
                        await asyncio.sleep(HOLD_POLL_SECONDS)
                    self.before = self.mark(self.agent)
                    config = await self.available()
                    if config is None:
                        continue
                    if not await check(config):
                        return False  # The controller put a summary-held grant back unsent.
                    latest = await self.available()
                    if latest is not None and all(latest.get(key) == config.get(key)
                                                  for key in ('enabled', 'support_dms')):
                        return True
        except TimeoutError:
            if not limit.expired():
                raise
            self.agent.delivery_hold_timeouts[self.kind] += 1
            self.agent.stages[self.kind] = {'at': time.time(),
                'last_ok': self.agent.stages.get(self.kind, {}).get('last_ok'), 'error': 'DeliveryHoldTimeout'}
            LOG.warning('Fri %s delivery hold timed out; unsent grant left to expire', self.kind)
            raise DeliveryHoldTimeout from None
