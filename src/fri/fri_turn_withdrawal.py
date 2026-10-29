"""A member's message withdrawn after Fri saved their exchange with it: the controller drops that exchange (fri #129).

Fri's live view and turns already refuse a message edited or deleted while handled or carried (see Agent.withdraw,
Agent.revise and Agent.unsend), but the member's saved exchange holding it would come back as history on any later
turn. A controller that offers turn-withdrawal drops it. Fri asks about messages whose exchanges it saved itself,
for as long as the controller keeps them, so it knows whose they are and where they were said. It keeps that in
memory only, so any other message edited or deleted where Fri saves exchanges, young enough to be in one still (one
saved before a restart, say), is asked about by ID alone from a controller that also offers saved-turn-withdrawal:
it drops only an exchange that holds it (fri #174).
"""

import asyncio
import logging
import time

CAPABILITY = 'turn-withdrawal'
SAVED_CAPABILITY = 'saved-turn-withdrawal'
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

    def withdraw(self, ids, server=None):
        """Messages taken back: each exchange Fri saved with one goes, asked for once. server: the one Fri saves
        exchanges said where they were taken back under (see Agent.saved_in); any other one young enough to be in an
        exchange still is then asked about by ID, ten at a time, from a controller offering it. Others ask nothing."""
        unplaced = []
        for i in ids:
            entry = self.saved.pop(int(i), None)
            if entry is None:
                if server is not None and saved_lately(int(i)):
                    unplaced.append(str(i))
            elif self.offered():
                self.start(self.send(int(i), entry[1]), i)
        if unplaced and self.offered() and SAVED_CAPABILITY in self.agent.fri.capabilities:
            unplaced.sort()
            for start in range(0, len(unplaced), 10):  # As many as the controller takes at once.
                self.start(self.send_unplaced(str(server), unplaced[start:start+10]), ', '.join(unplaced[start:start+10]))

    def start(self, send, ids):
        try:
            task = asyncio.get_running_loop().create_task(send)
        except RuntimeError:  # No loop: nothing here can be sent.
            send.close()
            LOG.warning('Withdrawn message %s was not dropped from saved turns: no event loop', ids)
            return
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def send(self, message_id, scope):
        try:
            await self.agent.fri.api('POST', '/chat/turn/withdraw', {**scope, 'message_id': str(message_id)})
        except Exception as exc:  # Logged without the words; whatever withdrew it goes on.
            LOG.warning('Withdrawn message %s was not dropped from saved turns: %s', message_id, type(exc).__name__)

    async def send_unplaced(self, server, message_ids):
        try:
            await self.agent.fri.api('POST', '/chat/turn/withdraw/saved', {'guild_id': server, 'message_ids': message_ids})
        except Exception as exc:  # As above.
            LOG.warning('Withdrawn messages %s were not dropped from saved turns: %s', ', '.join(message_ids),
                        type(exc).__name__)


def saved_lately(message_id):
    """Sent recently enough that the controller may keep an exchange holding it, from the time in its Discord ID."""
    return ((message_id >> 22)+1420070400000)/1000 > time.time()-KEPT_SECONDS
