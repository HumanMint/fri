"""Readable, paged approval text that does not depend on Discord embeds or files."""

import asyncio

import discord

NO_MENTIONS = discord.AllowedMentions.none()


def pages(text):
    # At most 1400 UTF-16 units after Markdown escaping, leaving room for the frame.
    result = []
    while text:
        end = min(700, len(text))
        if end < len(text):
            boundary = max(text.rfind('\n', 350, end), text.rfind(' ', 350, end))
            if boundary > 0:
                end = boundary + 1
        result.append(discord.utils.escape_markdown(discord.utils.escape_mentions(text[:end])))
        text = text[end:]
    return result or ['(empty)']


class PagedPreview(discord.ui.View):
    def __init__(self, title, body, note, label, prefix, confirm):
        super().__init__(timeout=600)
        self.title, self.pages, self.note = title, pages(body), note
        self.page, self.seen, self.finished = 0, {0}, False
        self.lock = asyncio.Lock()
        self.back_button = discord.ui.Button(label='Back', custom_id=prefix+'back', row=0)
        self.next_button = discord.ui.Button(label='Next', custom_id=prefix+'next', row=0)
        self.confirm_button = discord.ui.Button(label=label, style=discord.ButtonStyle.primary,
                                               custom_id=prefix+'submit', row=1)
        self.cancel_button = discord.ui.Button(label='Cancel', custom_id=prefix+'cancel', row=1)
        self.back_button.callback = self.back
        self.next_button.callback = self.next
        self.confirm_button.callback = confirm
        self.cancel_button.callback = self.cancel
        if len(self.pages) > 1:
            self.add_item(self.back_button)
            self.add_item(self.next_button)
        self.add_item(self.confirm_button)
        self.add_item(self.cancel_button)
        self.update_buttons()

    def update_buttons(self):
        self.back_button.disabled = self.page == 0
        self.next_button.disabled = self.page == len(self.pages)-1
        self.confirm_button.disabled = len(self.seen) != len(self.pages)

    @property
    def content(self):
        progress = f'Page {self.page+1} of {len(self.pages)}. ' if len(self.pages) > 1 else ''
        if self.confirm_button.disabled:
            progress += 'Read every page to enable approval. '
        return f'**{self.title}**\n\n{self.pages[self.page]}\n\n{progress}{self.note}'

    async def move(self, interaction, offset):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.finished:
                return
            old_page, old_seen = self.page, self.seen.copy()
            self.page = max(0, min(len(self.pages)-1, self.page+offset))
            self.seen.add(self.page)
            self.update_buttons()
            try:
                await interaction.response.edit_message(content=self.content, embed=None, attachments=[], view=self,
                                                         allowed_mentions=NO_MENTIONS)
            except Exception:
                self.page, self.seen = old_page, old_seen
                self.update_buttons()
                raise

    async def back(self, interaction):
        await self.move(interaction, -1)

    async def next(self, interaction):
        await self.move(interaction, 1)

    async def cancel(self, interaction):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.finished:
                return
            await interaction.response.edit_message(content='Cancelled. Nothing changed.', embed=None,
                                                     attachments=[], view=None, allowed_mentions=NO_MENTIONS)
            self.finished = True
            self.stop()
