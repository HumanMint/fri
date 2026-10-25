"""Read small still images from Discord CDN metadata, never URLs supplied by a model."""

import asyncio
import base64
import io
import re
from urllib.parse import urlsplit

import httpx

MAX_BYTES = 4 * 1024 * 1024
GIF_BYTES = 8 * 1024 * 1024  # An uploaded GIF; only its first frame is kept.
MAX_PIXELS = 20_000_000
STILL_SIDE = 1024  # A GIF's first frame is scaled to fit this square before the model sees it.
MIMES = {'image/png', 'image/jpeg', 'image/webp'}


def gif_previews(message):
    """The still preview Discord made for each GIF link the poster sent (Tenor, Giphy), on its own proxy."""
    embeds = getattr(message, 'embeds', None)
    return [str(e.thumbnail.proxy_url) for e in embeds if e.type == 'gifv' and getattr(e.thumbnail, 'proxy_url', None)] \
        if isinstance(embeds, (list, tuple)) else []


class Still(str):
    """A data URL holding an animation's first frame, so the model can be told it saw one frame."""
    first_frame = True


def sources(message, most=2, *, gifs=False):
    """None means incomplete/unsupported media; don't judge a video from its thumbnail.

    most: how many images one post may carry; a look around takes a whole gallery post.
    gifs: an uploaded GIF or a GIF link's preview counts too, as one still frame."""
    attachments = message.attachments
    if len(attachments) > most or any(a.is_spoiler() for a in attachments) or '||' in message.content:
        return None
    result = []
    for a in attachments:
        gif = gifs and a.content_type == 'image/gif'
        if ((a.content_type not in MIMES and not gif) or not 0 < a.size <= (GIF_BYTES if gif else MAX_BYTES) or
                not a.width or not a.height or a.width * a.height > MAX_PIXELS):
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


def allowed_url(url, channel):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == 'https' and not parsed.username and not parsed.password and
                not parsed.fragment and parsed.port in (None, 443) and
                ((parsed.hostname in ('cdn.discordapp.com', 'media.discordapp.net') and
                  parsed.path.startswith(f'/attachments/{channel}/')) or
                 ((parsed.hostname == 'media.discordapp.net' or
                   re.fullmatch(r'images-ext-\d\.discordapp\.net', parsed.hostname or '')) and
                  parsed.path.startswith('/external/'))))
    except ValueError:
        return False


async def load_image(url, channel, *, transport=None, gifs=False):
    """gifs: a GIF, animated PNG or animated WebP comes back as its first frame, a still PNG."""
    if not allowed_url(url, channel):
        raise ValueError('Image source is outside the Discord CDN scope')
    limit = GIF_BYTES if gifs else MAX_BYTES
    async with asyncio.timeout(25):
        # No Discord or subscription Authorization headers are sent to the CDN.
        async with httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=False,
                                     transport=transport) as client:
            async with client.stream('GET', url) as response:
                response.raise_for_status()
                mime = response.headers.get('content-type', '').split(';')[0].strip()
                if ((mime not in MIMES and not (gifs and mime == 'image/gif')) or
                        int(response.headers.get('content-length', '0')) > limit):
                    raise ValueError('Unsupported or oversized image')
                data = bytearray()
                async for chunk in response.aiter_bytes(16384):
                    data.extend(chunk)
                    if len(data) > limit:
                        raise ValueError('Image exceeds the byte limit')
    body = bytes(data)
    animated = ((mime == 'image/png' and b'acTL' in body) or (mime == 'image/webp' and b'ANIM' in body) or
                mime == 'image/gif')
    if gifs and animated and (body.startswith((b'GIF87a', b'GIF89a')) or mime != 'image/gif'):
        return Still('data:image/png;base64,'+base64.b64encode(first_frame(body)).decode())
    if len(body) > MAX_BYTES:
        raise ValueError('Image exceeds the byte limit')
    valid = ((mime == 'image/png' and body.startswith(b'\x89PNG\r\n\x1a\n') and b'acTL' not in body) or
             (mime == 'image/jpeg' and body.startswith(b'\xff\xd8\xff')) or
             (mime == 'image/webp' and body.startswith(b'RIFF') and body[8:12] == b'WEBP' and b'ANIM' not in body))
    if not valid:
        raise ValueError('Not a supported still image')
    return 'data:'+mime+';base64,'+base64.b64encode(body).decode()


def first_frame(body):
    """An animated image's first frame as a PNG, no wider or taller than STILL_SIDE."""
    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(io.BytesIO(body)) as image:
            if image.format not in ('GIF', 'PNG', 'WEBP') or image.width * image.height > MAX_PIXELS:
                raise ValueError('Not a supported animated image')
            image.seek(0)
            frame = image.convert('RGBA')
        frame.thumbnail((STILL_SIDE, STILL_SIDE))
        out = io.BytesIO()
        frame.save(out, 'PNG')
    except ValueError:
        raise
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError, EOFError, SyntaxError) as exc:
        raise ValueError('Not a readable animated image') from exc
    except Exception as exc:  # A malformed file can trip anything inside Pillow's decoders (IndexError, ...).
        raise ValueError('Not a readable animated image') from exc
    return out.getvalue()
