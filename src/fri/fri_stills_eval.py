"""Isolated checks of a still beside a video link's card (N2a): a post with an uploaded picture and a YouTube card.

Each case goes through Fri's own route, fri_images.attach and own_links for the speaker's own post, look for someone
else's, with fictional posts and synthetic pictures standing in for Discord's. The route decides what the model
sees: the still, the card's thumbnail, the video marked unwatched, or the canned line with no model call. The reply
is then scored on what it says about the still and that it never claims it watched the video. No Discord client,
network, controller writes or member context; the caller records only anonymous usage, and none for a case that
made no model call.

    python -m fri.fri_eval --suite stills
    python -m fri.fri_stills_eval            # The same cases on a build without this suite (the without side).
    python -m fri.fri_stills_eval --offline  # Routes only, no model or controller: which cases a build routes so.
"""

import asyncio
import json
import sys
from types import SimpleNamespace

import discord

from . import feedback as commands
from . import fri_images
from .fri_chat import ChatResult
from .fri_image_eval import shape
from .fri_images import details
from .fri_request_eval import source

CHANNEL, GUILD, KEV, MINT = 555, 444, 102, 101
UPLOAD = 'https://cdn.discordapp.com/attachments/555/{n}/{name}'
THUMB = 'https://images-ext-1.discordapp.net/external/{name}/https/i.ytimg.com/vi/{name}/hqdefault.jpg'
UNWATCHED = {'attachments': ['linked video'], 'reason': 'video'}
# Claims to have watched what it couldn't, and asks to be sent something again.
WATCHED = ('i watched', 'watched it', 'after watching', 'just watched', 'the video shows', 'in the video you',
           'great video', 'good video', 'resend', 're-send', 're-upload', 'reupload')
UNSEEN = ("didn't watch", 'didnt watch', "can't watch", 'cant watch', "couldn't watch", 'couldnt watch',
          "haven't watched", 'havent watched', 'not watching', "won't watch", 'thumbnail', "can't play", 'cant play',
          "didn't open", "can't open", 'cant open', "couldn't open")


class Post(discord.Message):
    """A fictional post, shaped as Discord delivers one, with only what the route reads."""
    __slots__ = ()

    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


def youtube(name, title):
    return discord.Embed.from_dict({
        'type': 'video', 'url': f'https://www.youtube.com/watch?v={name}', 'title': title,
        'provider': {'name': 'YouTube'}, 'author': {'name': 'Some Channel'},
        'thumbnail': {'url': f'https://i.ytimg.com/vi/{name}/hqdefault.jpg', 'proxy_url': THUMB.format(name=name),
                      'width': 480, 'height': 360},
        'video': {'url': f'https://www.youtube.com/embed/{name}', 'width': 1280, 'height': 720}})


def post(user, name, text, *, uploads=(), card=None, n=0, late=False):
    """A post by `user` (Kev, or Mint the speaker) with these uploads (name, kind) and a YouTube card or none.
    late: the card unfurls only after the post came in, as Discord usually does it: Discord's copy has it."""
    channel = SimpleNamespace(id=CHANNEL)
    def made(embeds):
        return Post(id=1566797876464451584+n, content=text, webhook_id=None, guild=SimpleNamespace(id=GUILD),
                    channel=channel, author=SimpleNamespace(id=user, bot=False, display_name=name),
                    attachments=[SimpleNamespace(url=UPLOAD.format(n=i+1, name=file), content_type=kind, size=4000,
                                                 width=96, height=96, is_spoiler=lambda: False)
                                 for i, (file, kind) in enumerate(uploads)],
                    embeds=embeds, _edited_timestamp=None)
    copy = made([card] if card else [])
    item = made([]) if late else copy

    async def fetch_message(identifier):
        return copy  # Discord's copy, card unfurled.
    channel.fetch_message = fetch_message
    return item


PICTURES = {'circle.png': shape('circle', (255, 0, 0)), 'square.png': shape('square', (0, 0, 255)),
            'rain': shape('triangle', (0, 160, 0)), 'speedrun': shape('triangle', (0, 160, 0))}


async def load(url, channel, **_):
    """Each fictional picture as its synthetic still, by the name its URL ends or begins with."""
    name = url.rsplit('/', 1)[1] if url.startswith('https://cdn.') else url.split('/external/')[1].split('/')[0]
    return PICTURES[name]


def cases():
    rain = youtube('rain', 'Ten Hours Of Rain Sounds For Sleeping')
    speedrun = youtube('speedrun', 'Every Speedrun Trick You Need')
    return [
        {'name': 'own_still_short_take', 'text': 'thoughts? https://youtu.be/rain', 'own': True,
         'post': post(MINT, 'Mint', 'thoughts? https://youtu.be/rain', uploads=[('circle.png', 'image/png')],
                      card=rain),
         'route': {'images': 1, 'unwatched': True, 'recheck': True}, 'needs': ('red', 'circle'), 'unseen': True,
         'max_chars': 300, 'expect': 'Looks at their red circle and says it couldn’t watch the video.'},
        {'name': 'own_still_card_unfurls_late', 'text': 'thoughts? https://youtu.be/rain', 'own': True,
         'post': post(MINT, 'Mint', 'thoughts? https://youtu.be/rain', uploads=[('circle.png', 'image/png')],
                      card=rain, n=5, late=True),
         'route': {'images': 1, 'unwatched': True, 'recheck': True}, 'needs': ('red', 'circle'), 'unseen': True,
         'max_chars': 300, 'expect': 'As above once Discord unfurls the card after the post came in.'},
        {'name': 'own_still_with_a_question', 'text': 'whats 12 times 12, also new wallpaper https://youtu.be/rain',
         'own': True, 'post': post(MINT, 'Mint', 'whats 12 times 12, also new wallpaper https://youtu.be/rain',
                                   uploads=[('circle.png', 'image/png')], card=rain),
         'route': {'images': 1, 'unwatched': True, 'recheck': True}, 'needs': ('144',), 'max_chars': 300,
         'expect': 'Answers 144 with the wallpaper in view, the video unwatched.'},
        {'name': 'others_still_beside_a_video', 'text': 'what shape did kev post?', 'own': False,
         'post': post(KEV, 'Kev', 'look https://youtu.be/speedrun', uploads=[('square.png', 'image/png')],
                      card=speedrun, n=1),
         'route': {'images': 2, 'stills': [None, 'link preview']}, 'needs': ('square',), 'max_chars': 300,
         'expect': 'Names Kev’s square from his upload, the card’s thumbnail seen apart.'},
        {'name': 'others_video_unwatched', 'text': 'is kev’s video any good?', 'own': False,
         'post': post(KEV, 'Kev', 'this one https://youtu.be/speedrun', uploads=[('circle.png', 'image/png')],
                      card=speedrun, n=2),
         'route': {'images': 2, 'stills': [None, 'link preview']}, 'unseen': True, 'max_chars': 300,
         'expect': 'Says it couldn’t watch the video (only its thumbnail and his still), never that it did.'},
        # Unchanged by N2a: a card alone shows its thumbnail, and an uploaded clip is still declined.
        {'name': 'others_card_alone', 'text': 'what is kev’s link?', 'own': False,
         'post': post(KEV, 'Kev', 'https://youtu.be/rain', card=rain, n=3),
         'route': {'images': 1, 'stills': ['link preview']}, 'max_chars': 300, 'needs': ('rain',),
         'expect': 'Reads the card: rain sounds, from its title and thumbnail.'},
        {'name': 'own_clip_beside_a_still', 'text': 'whats 12 times 12 https://youtu.be/rain', 'own': True,
         'post': post(MINT, 'Mint', 'whats 12 times 12 https://youtu.be/rain',
                      uploads=[('circle.png', 'image/png'), ('clip.mp4', 'video/mp4')], card=rain, n=4),
         'route': {'images': 0, 'declined': 'video', 'recheck': True}, 'needs': ('144',), 'max_chars': 300,
         'expect': 'An uploaded clip is still declined: 144, the post unseen, as before.'},
    ]


async def route(case):
    """What the model is shown for this case, through Fri's own route, and whether it's the case's route."""
    context, item, local, recheck = {}, case['post'], None, None
    real = fri_images.load_image
    fri_images.load_image = load
    try:
        if case['own']:
            try:
                await fri_images.attach(context, item, case['text'])
                await fri_images.own_links(context, item)
                recheck = await fri_images.unchanged(context, item)  # As before each request and delivery.
            except commands.UserFeedbackCheckFailure as line:
                local, recheck = str(line), None
        else:
            context['invited_context'] = [{'source': {'kind': 'discord_message', 'id': str(item.id),
                                                      'user_id': str(item.author.id), 'display_name': 'Kev',
                                                      'channel_id': str(CHANNEL), 'guild_id': str(GUILD)},
                                           'text': item.content, 'privacy_revision': 0, 'referenced': True,
                                           **details(item)}]
            await fri_images.look(context, [item])
    finally:
        fri_images.load_image = real
    unseen = context.get('own_media_not_inspected')
    evidence = {'local': local is not None, 'images': len(context.get('images', [])),
                'stills': [entry.get('still') for entry in context.get('inspected_images', [])],
                'unwatched': unseen == UNWATCHED,
                'declined': unseen['reason'] if unseen and unseen != UNWATCHED else None,
                **({'recheck': recheck} if case['own'] else {})}
    want = case['route']
    ok = (evidence['local'] == bool(want.get('local')) and
          all(evidence[key] == value for key, value in want.items() if key != 'local'))
    return context, local, evidence, ok


def scored(case, result):
    """The reply: about the still, and never claiming the video was watched."""
    from .fri_eval import mentions
    reply = (result.reply or '').casefold().replace('’', "'")
    return (result.status == 'completed' and result.response_kind == 'reply' and result.action is None and
            0 < len(result.reply or '') <= case.get('max_chars', 300) and not mentions(reply, WATCHED) and
            (not case.get('needs') or mentions(reply, case['needs'])) and
            (not case.get('unseen') or mentions(reply, UNSEEN)))


async def evaluate(chat, model, effort, voice_notes, *, offline=False):
    for case in cases():
        context, local, evidence, ok = await route(case)
        row = {key: value for key, value in case.items() if key not in ('post', 'own')}
        if local is not None or offline:  # A local line makes no model call; offline scores the route only.
            yield ({**row, 'passed': ok, 'model_calls': 0, 'evidence': evidence},
                   ChatResult(status='completed', reply=local or ''))
            continue
        context.update({'model': model, 'effort': effort, 'voice_notes': voice_notes, 'facts': [], 'turns': [],
                        'actions': {}, 'requests_enabled': True, 'personal_memory': {'enabled': True, 'notes': []},
                        'display_name': 'Mint', 'speaker': {'user_id': str(MINT), 'display_name': 'Mint'},
                        'source': source()})
        result = await chat.respond(case['text'], context)
        yield {**row, 'passed': ok and scored(case, result), 'evidence': evidence}, result


async def offline():
    """Routes only, printed one case a line: no model, controller or network."""
    async for case, result in evaluate(None, '', '', '', offline=True):
        print(json.dumps({'case': case['name'], 'expect': case['expect'], 'passed': case['passed'],
                          'evidence': case['evidence'], 'reply': result.reply}), flush=True)


if __name__ == '__main__':
    if '--offline' in sys.argv:
        asyncio.run(offline())
    else:
        from . import fri_eval

        # Only this suite, through fri_eval's runner, so a build whose fri_eval doesn't list it can still run it.
        fri_eval.evaluate = lambda chat, model, effort, voice_notes='', suite='', **_: evaluate(chat, model, effort,
                                                                                                 voice_notes)
        asyncio.run(fri_eval.main('stills'))
