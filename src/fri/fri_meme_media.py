"""Read small still images from Discord CDN metadata, never URLs supplied by a model."""

import asyncio
import base64
import re
from urllib.parse import urlsplit

import httpx

MAX_BYTES = 4 * 1024 * 1024
MIMES = {'image/png', 'image/jpeg', 'image/webp'}


def gif_previews(message):
    """The still preview Discord's own proxy made of each GIF link (Tenor, Giphy) the poster sent."""
    embeds = getattr(message, 'embeds', None)
    return [str(e.thumbnail.proxy_url) for e in embeds if e.type == 'gifv' and getattr(e.thumbnail, 'proxy_url', None)] \
        if isinstance(embeds, (list, tuple)) else []


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
    return 'data:'+mime+';base64,'+base64.b64encode(body).decode()
