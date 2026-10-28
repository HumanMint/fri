"""Read small still images from Discord CDN metadata, never URLs supplied by a model."""

import asyncio
import base64
import io
import re
import warnings
from urllib.parse import urlsplit

import httpx
from PIL import Image

MAX_BYTES = 4 * 1024 * 1024
MAX_PIXELS = 20_000_000  # An attachment's limit, and a link card picture's.
MIMES = {'image/png', 'image/jpeg', 'image/webp'}
LINK_CARDS = ('rich', 'article', 'link', 'video', 'image')  # Discord's unfurls of a link, besides a GIF link's.


def gif_previews(message):
    """The still preview Discord's own proxy made of each GIF link (Tenor, Giphy) the poster sent."""
    embeds = getattr(message, 'embeds', None)
    return [str(e.thumbnail.proxy_url) for e in embeds if e.type == 'gifv' and getattr(e.thumbnail, 'proxy_url', None)] \
        if isinstance(embeds, (list, tuple)) else []


def link_previews(message):
    """The still Discord's own proxy made of each link card's picture, from the first two cards: a video's
    thumbnail, an X or Reddit card's image, a pasted image link. It's what every member sees on the card, never
    the page or the clip. None when one can't be shown whole: no proxy copy of it (never fetched from the site
    itself), or no size Discord measured within an attachment's limit. A GIF link's preview isn't one (see
    gif_previews), and a spoilered post shows none."""
    embeds = getattr(message, 'embeds', None)
    if not isinstance(embeds, (list, tuple)) or '||' in str(getattr(message, 'content', '') or ''):
        return []
    result = []
    for embed in [e for e in embeds if getattr(e, 'type', None) in LINK_CARDS][:2]:
        picture = next((part for part in (getattr(embed, 'image', None), getattr(embed, 'thumbnail', None))
                        if getattr(part, 'proxy_url', None) or getattr(part, 'url', None)), None)
        if picture is None:
            continue  # A card without a picture: its text is all there is.
        url, width, height = (getattr(picture, key, None) for key in ('proxy_url', 'width', 'height'))
        # Its size as Discord measured it, known and within an attachment's limit (see load_image for the bytes').
        if (not url or not urlsplit(str(url)).path.startswith('/external/') or
                not all(type(side) is int and side > 0 for side in (width, height)) or width * height > MAX_PIXELS):
            return None
        result.append(str(url))
    return result


def sources(message, most=2, *, gifs=False):
    """None means incomplete/unsupported media; don't judge a video from its thumbnail.

    most: how many images one post may carry; a look around takes a whole gallery post.
    gifs: a GIF link's still preview counts too. An uploaded GIF never does: nothing here decodes animations."""
    attachments = message.attachments
    if len(attachments) > most or any(a.is_spoiler() for a in attachments) or '||' in message.content:
        return None
    result = []
    for a in attachments:
        if (a.content_type not in MIMES or not 0 < a.size <= MAX_BYTES or
                not a.width or not a.height or a.width * a.height > 20_000_000):
            return None
        result.append(str(a.url))
    for embed in message.embeds:
        if gifs and embed.type == 'gifv':
            preview = getattr(embed.thumbnail, 'proxy_url', None)
            if not preview:
                return None
            result.append(str(preview))
            continue
        if embed.type in ('video', 'gifv') or embed.video:
            return None
        if embed.image:
            proxy = getattr(embed.image, 'proxy_url', None)
            if not proxy:
                return None
            result.append(str(proxy))
    if len(result) > most or (not result and re.search(r'https?://', message.content)):
        return None
    return result


def unsigned(urls):
    """sources() as the uploads they name, to tell whether a post still shows what was read: Discord renews a
    CDN link's signature (its ex, is and hm query) on refetch, while an attachment's path keeps its own ID, so
    a replaced one still differs."""
    return None if urls is None else [str(url).split('?')[0] for url in urls]


def allowed_url(url, channel, *, previews=False):
    """previews: also Discord's images-ext embed proxy, which serves a GIF link's still preview. Only
    the look path asks for it, so the meme reactions keep their old reach."""
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == 'https' and not parsed.username and not parsed.password and
                not parsed.fragment and parsed.port in (None, 443) and
                ((parsed.hostname in ('cdn.discordapp.com', 'media.discordapp.net') and
                  parsed.path.startswith(f'/attachments/{channel}/')) or
                 ((parsed.hostname == 'media.discordapp.net' or
                   (previews and re.fullmatch(r'images-ext-\d\.discordapp\.net', parsed.hostname or ''))) and
                  parsed.path.startswith('/external/'))))
    except ValueError:
        return False


async def load_image(url, channel, *, transport=None, previews=False):
    if not allowed_url(url, channel, previews=previews):
        raise ValueError('Image source is outside the Discord CDN scope')
    async with asyncio.timeout(25):
        # No Discord or subscription Authorization headers are sent to the CDN.
        async with httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=False,
                                     transport=transport) as client:
            async with client.stream('GET', url) as response:
                response.raise_for_status()
                mime = response.headers.get('content-type', '').split(';')[0].strip()
                if mime not in MIMES or int(response.headers.get('content-length', '0')) > MAX_BYTES:
                    raise ValueError('Unsupported or oversized image')
                data = bytearray()
                async for chunk in response.aiter_bytes(16384):
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise ValueError('Image exceeds the byte limit')
    body = bytes(data)
    valid = ((mime == 'image/png' and body.startswith(b'\x89PNG\r\n\x1a\n') and b'acTL' not in body) or
             (mime == 'image/jpeg' and body.startswith(b'\xff\xd8\xff')) or
             (mime == 'image/webp' and body.startswith(b'RIFF') and body[8:12] == b'WEBP' and b'ANIM' not in body))
    if not valid:
        raise ValueError('Not a supported still image')
    if previews and urlsplit(url).path.startswith('/external/') and not 0 < pixels(body) <= MAX_PIXELS:
        # A site can serve Discord's proxy something other than what Discord sized for the card.
        raise ValueError('Preview size unknown or over the pixel limit')
    return 'data:'+mime+';base64,'+base64.b64encode(body).decode()


def pixels(body):
    """A still image's width times height from its own header, read without decoding it; 0 if unreadable."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(body)) as image:
                width, height = image.size
    except Exception:  # Pillow's header parsers raise many kinds; any of them means the size is unknown.
        return 0
    return width * height if width > 0 and height > 0 else 0
