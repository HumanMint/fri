"""A member's message withdrawn after Fri saved their exchange with it: the controller drops that exchange (fri #129).

Fri's live view and turns already refuse a message edited or deleted while handled or carried (see Agent.withdraw,
Agent.revise and Agent.unsend), but the member's saved exchange holding it would come back as history on any later
turn. A controller that offers turn-withdrawal drops it. Fri asks only about messages whose exchanges it saved itself,
for as long as the controller keeps them, so it knows whose they are and where they were said. It keeps that in
memory only: a message saved before a restart isn't asked about.
"""

import asyncio
import logging
import time

CAPABILITY = 'turn-withdrawal'
KEPT_SECONDS = 7*86400  # As long as the controller keeps a saved exchange.
MOST = 20000  # Bounds memory only: the controller keeps six exchanges per member and conversation.

LOG = logging.getLogger('fri.agent')


class SavedTurns:
    def __init__(self, agent):
        self.agent = agent
        self.saved = {}  # Discord message ID -> (when saved, the scope it was saved under), oldest first.
        self.tasks = set()  # Withdrawals still being sent.

    def offered(self):
        return CAPABILITY in getattr(self.agent.fri, 'capabilities', ())

    def kept(self, payload, saved, began):
        """The controller saved this turn, or may have (`saved` None: no answer came). `began`: time.time() and
        time.monotonic() just before it was sent. One of its messages withdrawn since went while the save was on its
        way, before anything here knew of it, so its exchange goes now."""
        if not self.offered() or payload.get('mention_grant'):  # A mention keeps only a placeholder.
            return
        if saved is not None and saved.get('kept', True) is False:  # Memory off, or a removal since: nothing kept.
            return
        scope = {key: str(payload.get(key) or '') for key in ('guild_id', 'channel_id', 'conversation_id', 'user_id')}
        if not all(scope.values()):  # Not a turn this could name again; the save it follows still stands.
            return
        ids = {int(item['id']) for item in [payload.get('source') or {}, *(
            row.get('source') or {} for row in payload.get('burst') or ())]  # A burst's earlier messages too.
               if item.get('kind') == 'discord_message' and str(item.get('id', '')).isdigit()}
        now = time.time()
        for i in ids:
            self.saved.pop(i, None)
            self.saved[i] = (now, scope)
        while self.saved and (next(iter(self.saved.values()))[0] < now-KEPT_SECONDS or len(self.saved) > MOST):
            del self.saved[next(iter(self.saved))]
        wall, monotonic = began
        agent = self.agent
        self.withdraw(i for i in ids if agent.withdrawn.get(str(i), 0) >= wall or agent.revised.get(i, 0) >= wall
                      or agent.unsent.get(i, -1) >= monotonic)

    def withdraw(self, ids):
        """Messages taken back: each exchange Fri saved with one goes, asked for once. Others ask nothing."""
        for i in ids:
            entry = self.saved.pop(int(i), None)
            if entry is None or not self.offered():
                continue
            try:
                task = asyncio.get_running_loop().create_task(self.send(int(i), entry[1]))
            except RuntimeError:  # No loop: nothing here can be sent.
                LOG.warning('Withdrawn message %s was not dropped from saved turns: no event loop', i)
                continue
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def send(self, message_id, scope):
        try:
            await self.agent.fri.api('POST', '/chat/turn/withdraw', {**scope, 'message_id': str(message_id)})
        except Exception as exc:  # Logged without the words; whatever withdrew it goes on.
            LOG.warning('Withdrawn message %s was not dropped from saved turns: %s', message_id, type(exc).__name__)
