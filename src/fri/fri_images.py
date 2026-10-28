"""Still images a turn may look at: the speaker's own, or ones a read turned up. Never ambient collection or tool authority."""

import asyncio
import re

import discord
import httpx

from . import feedback as commands
from .fri_meme_media import MAX_BYTES, MIMES, gif_previews, load_image, sources, unsigned

CHANGED = ('The image request changed while I was checking it. Any earlier tool actions keep '
           'their recorded outcomes; check their status before retrying. Ask again with the current attachment.')
MAX_IMAGES = 4  # In one model call, the speaker's own included.
MAX_LOADS = 2 * MAX_IMAGES  # Downloads one look may try, so a few dead links don't use up the budget.
MAX_CHARS = 12_000_000  # Their data URLs together, about 9 MB of image.
KEYS = ('image_sources', 'image_event', 'image_invitation', 'image_reads')
MAX_CARDS = 2  # Link cards on one row.
CARD_TYPES = ('rich', 'article', 'link', 'video', 'image', 'gifv')  # Discord's unfurls, not its system embeds.


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


def forwarded(message):
    """A Discord forward: the post it copies sits in a snapshot, with no text or files of the forward's own."""
    snapshots = getattr(message, 'message_snapshots', None)
    reference = getattr(message, 'reference', None)
    return bool((isinstance(snapshots, (list, tuple)) and snapshots) or
                getattr(reference, 'type', None) == discord.MessageReferenceType.forward)


def details(message):
    """What a reader sees around a post's text: what it carries, its link cards, its reactions, the post it
    answers, whether it's pinned.

    Reaction counts only, never who reacted; Fri's own reaction is marked so it can own it."""
    extra = {}
    if media(message):
        extra['attachments'] = media(message)
    if cards := link_cards(message):
        extra['link_cards'] = cards
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


def link_cards(message):
    """The preview cards Discord unfurled from links in a member's post, the first two that say anything: the
    site, who made it, its title and a line of its text, cut short. Text only, never a URL. Members can't send
    their own embeds, so each one is Discord's preview of a link they sent; a bot's or webhook's never count,
    and a spoilered post shows none. Untrusted page text: the reply model is told it's never instructions."""
    from .privacy import clean
    embeds, author = getattr(message, 'embeds', None), getattr(message, 'author', None)
    if (not isinstance(embeds, (list, tuple)) or getattr(author, 'bot', True) is not False or
            getattr(message, 'webhook_id', None) or '||' in str(getattr(message, 'content', '') or '')):
        return []
    cards = []
    for embed in embeds:
        if getattr(embed, 'type', None) not in CARD_TYPES:
            continue
        fields = (('site', getattr(getattr(embed, 'provider', None), 'name', None), 40),
                  ('by', getattr(getattr(embed, 'author', None), 'name', None), 60),
                  ('title', getattr(embed, 'title', None), 120), ('text', getattr(embed, 'description', None), 200))
        card = {key: clean(' '.join(value.split()), most) for key, value, most in fields if isinstance(value, str)}
        card = {key: value for key, value in card.items() if value}
        if card.get('title') or card.get('text'):
            cards.append(card)
            if len(cards) == MAX_CARDS:
                break
    return cards


def label(emoji):
    """A unicode emoji as itself, a server emoji by its name."""
    from .privacy import clean
    return clean(emoji, 16) if isinstance(emoji, str) else ':'+clean(str(getattr(emoji, 'name', None) or 'emoji'), 32)+':'


def image_context(context):
    """What later checks need to notice an inspected image changing, or a declined one, from a turn's context."""
    return {key: context[key] for key in (*KEYS, 'image_declined') if key in context}


async def look(context, posts, *, invitation=None):
    """Look at still images that a read turned up, newest first, within this call's image budget.

    Other people's posts count only through their own attachments, the still preview Discord's proxy made
    of a GIF link they sent, or on a member's post with neither, the picture on a link's preview card, as
    Discord's proxy serves it to everyone (see stills). Never a page a link points at. A post that won't load
    is just not inspected and the next older one gets its slot; the model is told which posts it saw and how
    many it didn't."""
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
        urls = stills(post)[0]
        if not urls or len(urls) > budget:
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
            still = stills(post)[1]
            inspected.append({'images': list(range(len(images)+1, len(images)+len(loaded)+1)),
                              'source': discord_source(post).evidence(), **({'still': still} if still else {})})
            images += loaded
            if invitation is not None:  # Its own check covers what it looked at (see Invitation.current).
                invitation.inspected(post, urls)
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
    """An already addressed chat turn can use its attachments with any caption.

    Its own media that sources() declines, or that won't load, is never shown (see decline)."""
    source = source or event
    if not text.strip() or not has_attachments(source):
        return
    if invitation:
        await invitation.require_current()
        if source is not event and source is not invitation.image:
            raise commands.UserFeedbackCheckFailure(CHANGED)
    if context.get('image_declined') and context['image_event'].id == source.id:
        return  # Declined earlier this turn (a read tool attaches again): what the model was told stands, no retry.
    attachments = source.attachments
    # In normal chat accept only the requester's own attachments, not arbitrary
    # external embeds, referenced posts, or images from recent channel context.
    if not isinstance(getattr(source, 'embeds', None), (list, tuple)):
        return
    urls = sources(source)
    attachment_urls = [str(a.url) for a in attachments]
    if urls is None or urls != attachment_urls:
        return decline(context, event, source, declined(source), text, invitation)
    try:
        images = [await load_image(url, event.channel.id) for url in urls]
    except (httpx.HTTPError, TimeoutError):
        return decline(context, event, source, 'did not load', text, invitation)
    except ValueError:
        return decline(context, event, source, 'unsupported', text, invitation)
    context['images'] = images
    context['image_sources'] = urls
    context['image_event'] = source
    if invitation:
        from .fri_context import discord_source
        context['image_provenance'] = discord_source(source).evidence()
        context['image_invitation'] = invitation


# Why the speaker's own media went unseen, as the member hears it when their ask is about the media itself.
# Short and true, never a list of formats to resend in. Only "did not load" may work another time.
LIMITS = {'spoiler': 'i don’t open spoilers.', 'video': 'can’t watch videos, only still pics.',
          'gif': 'can’t watch gifs, only still pics.', 'more than two images': 'i only look at two pics per post.',
          'image too large': 'that pic’s too big for me to open.',
          'unsupported': 'can’t open that one, only plain still pics.', 'did not load': 'couldn’t load that just now.'}
# What an ask about the media itself is made of: reactions and pointers at it ("thoughts?", "lmao", "is it
# good"), or a pointer and one word of its own ("what song is this"). Any other question or request, however
# short ("what is recursion?", "tell me a joke"), is the text's own and goes to the model (see about_media).
REACTIONS = frozenset('''lol lmao lmfao haha hahaha xd omg wow damn bruh bro dude fr ngl tbh thoughts thought
opinion opinions take rate rating think look see watch check good bad funny real fake true mid cursed cute nice
cool crazy wild like guess'''.split())
POINTERS = frozenset('''this that these those above attached clip clips video videos vid gif gifs pic pics
picture pictures photo photos image images meme memes screenshot file post'''.split())
VAGUE = frozenset('it its them here there one ones thing'.split())  # Pointers only with no word of the ask's own.
FILLER = frozenset('''fri yo hey hi ok okay oh ah uh um hm hmm so just really actually lowkey kinda now too also
pls plz please rn what whats who whos which how hows why is are was does do did can could would will you u ur
your yours i im me my mine of about on at in out the a an and or to for any tell say describe explain'''.split())


def declined(source):
    """Why sources() turned the speaker's own post down, the first reason that applies."""
    kinds, attachments = media(source), source.attachments
    if any(kind.startswith('spoiler') for kind in kinds) or '||' in source.content:
        return 'spoiler'
    for reason in ('video', 'gif'):
        if any(reason in kind for kind in kinds):
            return reason
    if len(attachments) > 2:  # sources()' own limit for one post.
        return 'more than two images'
    if any(a.content_type in MIMES and (not 0 < a.size <= MAX_BYTES or (a.width or 0)*(a.height or 0) > 20_000_000)
           for a in attachments):
        return 'image too large'
    return 'unsupported'


def about_media(text):
    """An ask that needs the media itself to answer: only reactions and pointers at it, or a pointer at it and
    one word of its own. Emoji alone count; greetings and questions with nothing pointing at it don't."""
    words = re.findall(r"[^\W_]+", re.sub(r"<[^>]*>|['’]", '', text.casefold()))
    own = [word for word in words if word not in REACTIONS | POINTERS | VAGUE | FILLER]
    if any(word in POINTERS for word in words):
        return len(own) <= 1
    return not own and (not words or any(word in REACTIONS or word in VAGUE for word in words))


def decline(context, event, source, reason, text, invitation):
    """Own media Fri didn't see. An ask about the media itself gets a short local line and no model call; a
    question its text carries alone goes on, the model told what it didn't see (own_media_not_inspected)
    and never what a caption or file name might suggest. Someone else's post that won't load keeps its notice."""
    if context.get('image_event') is source and context.get('image_sources'):
        return  # An earlier call this turn inspected it, and its checks stand (a read tool attaches again).
    if about_media(text) or source.author.id != event.author.id:
        raise commands.UserFeedbackCheckFailure(LIMITS[reason])
    context['own_media_not_inspected'] = {'attachments': media(source), 'reason': reason}
    # Rechecked as an inspected post is (see unchanged): an answer never outlives the post it skipped.
    context['image_event'] = source
    context['image_declined'] = unsigned([str(a.url) for a in source.attachments])
    if invitation:
        context['image_invitation'] = invitation


async def unchanged(context, event):
    if not await looked_unchanged(context):
        return False
    if not context.get('image_sources') and not context.get('image_declined'):
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
                image.edited_at == source.edited_at and
                (unsigned(sources(image)) == unsigned(context['image_sources']) if context.get('image_sources') else
                 unsigned([str(a.url) for a in image.attachments]) == context['image_declined']))
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
                    current.edited_at != post.edited_at or
                    unsigned(stills(current)[0]) != unsigned(read['sources'])):
                return False
        return True
    except discord.HTTPException:
        return False


async def still_there(message):
    """An image post chosen before a wait, as Discord has it now: still there, unedited, the same images."""
    try:
        current = await message.channel.fetch_message(message.id)
    except discord.HTTPException:
        return False
    return (current.edited_at == message.edited_at and current.content == message.content and
            unsigned(sources(current)) == unsigned(sources(message)))


async def require_current(context, event):
    if not await unchanged(context, event):
        raise commands.UserFeedbackCheckFailure(CHANGED)


def stills(post):
    """What look() can see of a post, and as what: its attachments with any GIF link's still preview, or, on a
    member's post with neither, the pictures Discord's proxy made of its link cards. (None, None) when there's
    nothing it can see whole; a post with both kinds keeps today's rule and isn't looked at."""
    from .fri_meme_media import link_previews
    urls = sources(post, MAX_IMAGES, gifs=True)
    if urls and urls == [str(a.url) for a in post.attachments]+gif_previews(post):
        return urls, 'gif preview' if gif_previews(post) else None
    author = getattr(post, 'author', None)
    if (not post.attachments and not gif_previews(post) and getattr(author, 'bot', True) is False and
            not getattr(post, 'webhook_id', None) and (urls := link_previews(post))):
        return urls, 'link preview'
    return None, None


def lookable(post):
    """A post worth handing to look(): attachments, a GIF link's preview, or a link card's picture."""
    attachments = getattr(post, 'attachments', None)
    if not isinstance(attachments, (list, tuple)) or not isinstance(getattr(post, 'embeds', None), (list, tuple)):
        return False
    return bool(attachments or gif_previews(post) or stills(post)[0])


async def own_links(context, event, *, invitation=None):
    """The speaker's own post as Discord has it now, for the link cards it unfurled after it came in: the
    event that arrived usually has none yet. Their text goes beside the message (message_link_cards), and a
    card's picture or a GIF link's preview is looked at like a nearby post's, checked again as Discord has it
    before each request and before delivery: by a ping's `invitation`, else on its own refetch. Never their
    attachments: attach() has those. Only a turn that may look."""
    from .fri_conversation import LINK
    content = LINK.sub('', str(getattr(event, 'content', '') or ''))  # Discord unfurls no message link.
    if (not isinstance(event, discord.Message) or has_attachments(event) or
            not ('https://' in content or 'http://' in content) or
            not callable(getattr(getattr(event, 'channel', None), 'fetch_message', None))):
        return
    try:
        current = await event.channel.fetch_message(event.id)
    except discord.HTTPException:
        return
    if (current.id != event.id or current.author.id != event.author.id or current.author.bot or
            current.webhook_id or current.content != event.content or current.edited_at != event.edited_at):
        return  # Changed since it came in, which the turn's own checks refuse.
    if cards := link_cards(current):
        context['message_link_cards'] = cards
    if lookable(current):
        await look(context, [current], invitation=invitation)
