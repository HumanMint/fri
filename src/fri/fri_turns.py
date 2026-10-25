"""Direct replies to a member go one at a time, in the order their messages were sent."""

import asyncio
from collections import defaultdict


class Turns:
    def __init__(self, fri):
        self.fri = fri  # Its busy set also covers panel and ambient replies, which take no turn.
        self.arrived = defaultdict(set)  # member: IDs of their messages still being handled.
        self.holders = {}  # member: the message whose reply holds their turn.
        self.wakers = {}  # member: set when their turn, busy state or earlier messages change.

    def arrive(self, user, message_id):
        self.arrived[user].add(message_id)

    def leave(self, user, message_id):
        self.arrived[user].discard(message_id)
        if not self.arrived[user]:
            del self.arrived[user]
        self.wake(user)

    def ready(self, user, message_id):
        """Nothing replying to them, and no earlier message of theirs still being handled.

        Snowflakes order a member's messages across channels, so a message held up earlier on, e.g.
        waiting for a reply to be saved, still goes before a later one.
        """
        return (user not in self.holders and user not in self.fri.busy and
                not any(other < message_id for other in self.arrived.get(user, ())))

    def take(self, user, message_id):
        self.holders[user] = message_id

    def release(self, user):
        del self.holders[user]
        self.wake(user)

    def wake(self, user):
        if user in self.wakers:
            self.wakers.pop(user).set()

    async def changed(self, user):
        waits = [asyncio.ensure_future(self.wakers.setdefault(user, asyncio.Event()).wait())]
        if user in self.fri.busy:
            waits.append(asyncio.ensure_future(self.fri.busy.finished(user)))
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for wait in waits:
                wait.cancel()
