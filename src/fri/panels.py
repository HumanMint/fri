"""One disposable private panel per member; interaction tokens stay in memory."""

import asyncio
from dataclasses import dataclass

import discord


def is_private(interaction):
    return bool(getattr(getattr(interaction.message, 'flags', None), 'ephemeral', False))


async def defer_panel(interaction):
    if not interaction.response.is_done():
        # A component update acknowledges the click without creating another bubble.
        await interaction.response.defer(ephemeral=True, thinking=not is_private(interaction))


@dataclass
class Panel:
    interaction: object
    message_id: int
    view: object
    message: object


class PrivatePanels:
    def __init__(self):
        self.entries = {}
        self.locks = {}
        self.latest = {}

    @staticmethod
    def key(interaction):
        return interaction.guild_id, interaction.user.id

    def current_view(self, interaction):
        entry = self.entries.get(self.key(interaction))
        if entry and is_private(interaction) and interaction.message.id == entry.message_id:
            return entry.view

    async def show(self, interaction, *, content=None, embed=None, embeds=None, attachments=None, view):
        await defer_panel(interaction)
        key = self.key(interaction)
        async with self.locks.setdefault(key, asyncio.Lock()):
            if interaction.id < self.latest.get(key, 0):
                entry = self.entries.get(key)
                if entry is None or entry.view is not view:
                    view.stop()
                if not is_private(interaction):
                    try:
                        await interaction.delete_original_response()
                    except discord.HTTPException:
                        pass
                return entry.message if entry else None
            payload = {'content': content, 'view': view, 'allowed_mentions': discord.AllowedMentions.none()}
            payload['embeds'] = embeds if embeds is not None else ([embed] if embed else [])
            if attachments is not None:
                payload['attachments'] = attachments
            message = await interaction.edit_original_response(**payload)
            previous = self.entries.get(key)
            self.entries[key] = Panel(interaction, message.id, view, message)
            self.latest[key] = interaction.id
            if previous:
                if previous.view is not view:
                    previous.view.stop()
                if previous.message_id != message.id:
                    try:
                        await previous.interaction.delete_original_response()
                    except discord.HTTPException:
                        # Expired tokens or a manually dismissed bubble cannot be recovered.
                        pass
            return message

    async def close(self, key, view):
        async with self.locks.setdefault(key, asyncio.Lock()):
            entry = self.entries.get(key)
            if not entry or entry.view is not view:
                return  # An old timeout must never delete its replacement.
            self.entries.pop(key)
            self.latest[key] = entry.interaction.id
            view.stop()
            try:
                await entry.interaction.delete_original_response()
            except discord.HTTPException:
                pass

    async def close_all(self):
        for key, entry in list(self.entries.items()):
            await self.close(key, entry.view)
