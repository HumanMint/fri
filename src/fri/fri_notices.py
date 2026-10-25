"""Fri's canned notice voice and short-lived, bot-owned waiting messages."""

import asyncio
import re
import time

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


class WaitingNotices:
    def __init__(self, busy):
        self.busy = busy
        self.lock = asyncio.Lock()
        self.messages = {}

    async def remove(self, message):
        try:
            await message.delete()
        except discord.HTTPException:
            pass  # Also scheduled for deletion when originally sent.

    async def send(self, event):
        async with self.lock:
            if event.author.id not in self.busy:
                return  # The reply may have finished while this notice waited.
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
        async with self.lock:
            if user in self.busy:
                return  # A privacy response or another turn must not clear active work.
            rows = self.messages.pop(user, [])
        for _, message in rows:
            await self.remove(message)
