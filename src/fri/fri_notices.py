"""Fri's canned notice voice and short-lived, bot-owned waiting messages."""

import asyncio
import re
import time
from collections import defaultdict

import discord

from . import feedback as commands

NO_MENTIONS = discord.AllowedMentions.none()
LITERALS = re.compile(r'(```[\s\S]*?```|`[^`]*`|https?://[^\s<>]+|<[^>]+>)')


def notice_text(text):
    """Lowercase canned prose without corrupting links, code or Discord tokens."""
    return ''.join(part if index % 2 else part.lower()
                   for index, part in enumerate(LITERALS.split(text)))


class ReplyInProgress(commands.UserFeedbackCheckFailure):
    def __init__(self):
        super().__init__('hold on, still typing your last one.')


class Busy(set):
    """Members with a reply running. A message that arrives meanwhile can wait for it to finish."""

    def __init__(self):
        super().__init__()
        self.idle = {}  # member: set once their running reply finishes.

    def discard(self, user):
        super().discard(user)
        if user in self.idle:
            self.idle.pop(user).set()

    async def finished(self, user):
        while user in self:
            await self.idle.setdefault(user, asyncio.Event()).wait()


class WaitingNotices:
    def __init__(self, busy):
        self.busy = busy
        # Per member: a send's last checks can wait on the controller without holding up anyone else.
        self.locks = defaultdict(asyncio.Lock)
        self.messages = {}

    async def remove(self, message):
        try:
            await message.delete()
        except discord.HTTPException:
            pass  # Also scheduled for deletion when originally sent.

    async def send(self, event, eligible=None):
        async with self.locks[event.author.id]:
            if event.author.id not in self.busy:
                return  # The reply may have finished while this notice waited.
            if eligible and (not await eligible() or event.author.id not in self.busy):
                return  # Checked here, as late as possible: another notice may have held the lock.
            # Don't retain expired handles indefinitely if a conversation fails.
            now = time.monotonic()
            self.messages = {user: [(at, msg) for at, msg in rows if now-at < 300]
                             for user, rows in self.messages.items() if any(now-at < 300 for at, _ in rows)}
            message = await event.reply(str(ReplyInProgress()), mention_author=False,
                                        allowed_mentions=NO_MENTIONS, delete_after=300)
            if event.author.id not in self.busy:
                await self.remove(message)
                return
            rows = self.messages.setdefault(event.author.id, [])
            rows.append((now, message))
            if len(rows) > 3:
                await self.remove(rows.pop(0)[1])

    async def clear(self, user):
        # Serialize with sends so a slow Discord response cannot leave a new
        # waiting notice behind after the real reply has already been delivered.
        async with self.locks[user]:
            if user in self.busy:
                return  # A privacy response or another turn must not clear active work.
            rows = self.messages.pop(user, [])
        for _, message in rows:
            await self.remove(message)
