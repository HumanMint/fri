"""Explicit current-message still images, never ambient collection or tool authority."""

import discord
import httpx

from . import feedback as commands
from .fri_meme_media import load_image, sources

CHANGED = ('The image request changed while I was checking it. Any earlier tool actions keep '
           'their recorded outcomes; check their status before retrying. Ask again with the current attachment.')


def has_attachments(event):
    """Attachment presence comes from the current Discord event, never model text."""
    attachments = getattr(event, 'attachments', ())
    return isinstance(event, discord.Message) and isinstance(attachments, (list, tuple)) and bool(attachments)


async def select_source(event):
    """On demand, pair a caption only with the immediately preceding own photo-only post."""
    if has_attachments(event):
        return event
    if (not isinstance(event, discord.Message) or
            not isinstance(getattr(event, 'attachments', None), (list, tuple)) or
            not callable(getattr(event.channel, 'history', None))):
        return None
    try:
        async for previous in event.channel.history(limit=1, before=event, oldest_first=False):
            gap = (event.id-previous.id)/(1 << 22)/1000
            if (0 < gap <= 60 and previous.author.id == event.author.id and
                    not previous.author.bot and not previous.webhook_id and
                    previous.channel.id == event.channel.id and not previous.content.strip() and
                    has_attachments(previous)):
                return previous
            break
    except discord.HTTPException:
        return None
    return None


async def attach(context, event, text, *, source=None, invitation=None):
    """An already addressed chat turn can use its attachments with any caption."""
    source = source or event
    if not text.strip() or not has_attachments(source):
        return
    if invitation:
        await invitation.require_current()
        if source is not event and source is not invitation.image:
            raise commands.UserFeedbackCheckFailure(CHANGED)
    attachments = source.attachments
    # In normal chat accept only the requester's own attachments, not arbitrary
    # external embeds, referenced posts, or images from recent channel context.
    if not isinstance(getattr(source, 'embeds', None), (list, tuple)):
        return
    urls = sources(source)
    attachment_urls = [str(a.url) for a in attachments]
    if urls is None or urls != attachment_urls:
        raise commands.UserFeedbackCheckFailure('Attach up to two non-spoiler PNG, JPEG or WebP still images, up to 4 MB each. Videos and GIFs are not supported.')
    try:
        context['images'] = [await load_image(url, event.channel.id) for url in urls]
    except (httpx.HTTPError, TimeoutError, ValueError):
        raise commands.UserFeedbackCheckFailure('I couldn’t inspect that image. Try a small PNG, JPEG or WebP attachment.') from None
    context['image_sources'] = urls
    context['image_event'] = source
    if invitation:
        from .fri_context import discord_source
        context['image_provenance'] = discord_source(source).evidence()
        context['image_invitation'] = invitation


async def unchanged(context, event):
    if not context.get('image_sources'):
        return True
    invitation = context.get('image_invitation')
    if invitation and not await invitation.current():
        return False
    try:
        current = await event.channel.fetch_message(event.id)
        if (current.author.id != event.author.id or current.webhook_id or
                current.content != event.content or current.edited_at != event.edited_at):
            return False
        source = context.get('image_event', event)
        image = current if source.id == event.id else await event.channel.fetch_message(source.id)
        return ((image.author.id == event.author.id or
                 (invitation and image.author.id == source.author.id)) and not getattr(image.author, 'bot', False) and not image.webhook_id and
                image.channel.id == event.channel.id and image.content == source.content and
                image.edited_at == source.edited_at and sources(image) == context['image_sources'])
    except discord.HTTPException:
        return False


async def still_there(message):
    """An image post chosen before a wait, as Discord has it now: still there, unedited, the same images."""
    try:
        current = await message.channel.fetch_message(message.id)
    except discord.HTTPException:
        return False
    return (current.edited_at == message.edited_at and current.content == message.content and
            sources(current) == sources(message))


async def require_current(context, event):
    if not await unchanged(context, event):
        raise commands.UserFeedbackCheckFailure(CHANGED)
