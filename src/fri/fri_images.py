"""Still images a turn may look at: the speaker's own, or ones a read turned up. Never ambient collection or tool authority."""

import asyncio

import discord
import httpx

from . import feedback as commands
from .fri_meme_media import gif_previews, load_image, sources

CHANGED = ('The image request changed while I was checking it. Any earlier tool actions keep '
           'their recorded outcomes; check their status before retrying. Ask again with the current attachment.')
MAX_IMAGES = 4  # In one model call, the speaker's own included.
MAX_LOADS = 2 * MAX_IMAGES  # Downloads one look may try, so a few dead links don't use up the budget.
MAX_CHARS = 12_000_000  # Their data URLs together, about 9 MB of image.
KEYS = ('image_sources', 'image_event', 'image_invitation', 'image_reads')


def media(message):
    """What a post carries besides text, as kinds the model can name: never URLs or file names."""
    kinds, attachments, embeds = [], getattr(message, 'attachments', None), getattr(message, 'embeds', None)
    for a in attachments if isinstance(attachments, (list, tuple)) else ():
        mime = str(getattr(a, 'content_type', None) or '').split(';')[0]
        kind = ('gif' if mime == 'image/gif' else 'image' if mime.startswith('image/') else
                'video' if mime.startswith('video/') else 'audio' if mime.startswith('audio/') else 'file')
        spoiler = getattr(a, 'is_spoiler', None)
        kinds.append('spoiler '+kind if callable(spoiler) and spoiler() else kind)
    for embed in embeds if isinstance(embeds, (list, tuple)) else ():
        kind = getattr(embed, 'type', None)
        if kind == 'gifv':
            kinds.append('linked gif')
        elif kind == 'video' or getattr(embed, 'video', None):
            kinds.append('linked video')
        elif kind == 'image' or getattr(embed, 'image', None):
            kinds.append('linked image')
    stickers = getattr(message, 'stickers', None)
    kinds += ['sticker' for _ in stickers] if isinstance(stickers, (list, tuple)) else []
    return kinds[:10]


def details(message):
    """What a reader sees around a post's text: what it carries, its reactions, the post it answers,
    whether it's pinned.

    Reaction counts only, never who reacted; Fri's own reaction is marked so it can own it."""
    extra = {}
    if media(message):
        extra['attachments'] = media(message)
    reactions = getattr(message, 'reactions', None)
    if isinstance(reactions, (list, tuple)) and reactions:
        extra['reactions'] = [{'emoji': label(r.emoji), 'count': r.count, **({'fri': True} if r.me is True else {})}
                              for r in reactions[:6] if isinstance(getattr(r, 'count', None), int)]
    reference = getattr(message, 'reference', None)
    channel = getattr(message, 'channel', None)
    if (isinstance(getattr(reference, 'message_id', None), int) and channel is not None and
            getattr(reference, 'channel_id', None) == channel.id):
        guild = getattr(getattr(message, 'guild', None), 'id', None)
        extra['reply_to'] = f'https://discord.com/channels/{guild or "@me"}/{channel.id}/{reference.message_id}'
    if getattr(message, 'pinned', None) is True:
        extra['pinned'] = True
    return extra


def label(emoji):
    """A unicode emoji as itself, a server emoji by its name."""
    from .privacy import clean
    return clean(emoji, 16) if isinstance(emoji, str) else ':'+clean(str(getattr(emoji, 'name', None) or 'emoji'), 32)+':'


def image_context(context):
    """What later checks need to notice an inspected image changing, from a turn's context."""
    return {key: context[key] for key in KEYS if key in context}


async def look(context, posts, *, invitation=None):
    """Look at still images that a read turned up, newest first, within this call's image budget.

    Other people's posts count only through their own attachments, or the still preview Discord's
    proxy made of a GIF link they sent, never other embeds or pasted links, which could point anywhere. A post that won't load is just not inspected and the next older one
    gets its slot; the model is told which posts it saw and how many it didn't."""
    from .fri_context import discord_source
    images = list(context.get('images', []))
    reads = list(context.get('image_reads', []))
    inspected = list(context.get('inspected_images', []))
    seen = {read['event'].id for read in reads}
    if context.get('image_event') is not None:
        seen.add(context['image_event'].id)
    queue, skipped, budget = [], 0, MAX_IMAGES-len(images)
    for post in sorted(posts, key=lambda m: m.id, reverse=True):
        if post.id in seen:
            continue
        seen.add(post.id)
        urls = sources(post, MAX_IMAGES, gifs=True)
        if not urls or urls != [str(a.url) for a in post.attachments]+gif_previews(post) or len(urls) > budget:
            skipped += 1
            continue
        queue.append((post, urls))
    size, looked, tried = sum(map(len, images)), 0, 0
    while queue and budget > 0 and tried < MAX_LOADS:
        # Newest first, as many as fit; a post that fails to load leaves its slot to the next round.
        picked, room = [], min(budget, MAX_LOADS-tried)
        for post, urls in list(queue):
            if len(urls) <= room:
                picked.append((post, urls))
                queue.remove((post, urls))
                room -= len(urls)
        if not picked:
            break
        tried += sum(len(urls) for _, urls in picked)
        loads = await asyncio.gather(*(load_image(url, post.channel.id, previews=True) for post, urls in picked
                                       for url in urls),
                                     return_exceptions=True)
        for value in loads:
            if isinstance(value, BaseException) and not isinstance(value, (httpx.HTTPError, TimeoutError, ValueError)):
                raise value
        for post, urls in picked:
            loaded, loads = loads[:len(urls)], loads[len(urls):]
            if any(isinstance(value, BaseException) for value in loaded) or size+sum(map(len, loaded)) > MAX_CHARS:
                skipped += 1
                continue
            size += sum(map(len, loaded))
            inspected.append({'images': list(range(len(images)+1, len(images)+len(loaded)+1)),
                              'source': discord_source(post).evidence(),
                              **({'still': 'gif preview'} if gif_previews(post) else {})})
            images += loaded
            reads.append({'event': post, 'sources': urls, 'invitation': invitation})
            budget -= len(urls)
            looked += 1
    skipped += len(queue)
    if looked:
        context.update(images=images, image_reads=reads, inspected_images=inspected)
    return {'images_inspected': looked, 'images_not_inspected': skipped}


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
    if not await looked_unchanged(context):
        return False
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


async def looked_unchanged(context):
    """Every post look() inspected still exists, by the same author, unedited, with the same images.

    An invitation already refetches its own nearby posts this way, and their authors' privacy."""
    invitations = []
    try:
        for read in context.get('image_reads', []):
            invitation, post = read['invitation'], read['event']
            if invitation is not None:
                if invitation not in invitations:
                    if not await invitation.current():
                        return False
                    invitations.append(invitation)
                continue
            current = await post.channel.fetch_message(post.id)
            # The same author as when read: the reads that call look() admit no bot but Fri itself.
            if (current.id != post.id or current.author.id != post.author.id or
                    current.webhook_id or current.channel.id != post.channel.id or current.content != post.content or
                    current.edited_at != post.edited_at or sources(current, MAX_IMAGES, gifs=True) != read['sources']):
                return False
        return True
    except discord.HTTPException:
        return False


async def require_current(context, event):
    if not await unchanged(context, event):
        raise commands.UserFeedbackCheckFailure(CHANGED)
