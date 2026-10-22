"""Small audited command catalog. Model output cannot select arbitrary Red commands."""

import re
from urllib.parse import urlsplit

COMMANDS = {
    'play': ('play', 'Audio', 'Queue a song name or a YouTube URL.'),
    'pause': ('pause', 'Audio', 'Pause playback if it is playing.'),
    'resume': ('pause', 'Audio', 'Resume playback if it is paused.'),
    'skip': ('skip', 'Audio', 'Skip the current track; normal DJ/vote checks apply.'),
    'stop': ('stop', 'Audio', 'Stop music; normal DJ checks apply.'),
    'volume': ('volume', 'Audio', 'Set volume to an explicit number from 0 to 100.'),
    'queue': ('queue', 'Audio', 'Read the current track and first ten queued tracks.'),
    'now_playing': ('now', 'Audio', 'Read the current track.'),
    'status': ('gene status', 'Gene', 'Read Gene status.'),
    'version': ('gene version', 'Gene', 'Read the active release and worker model.'),
}


def arguments(action, value):
    if action not in COMMANDS or not isinstance(value, str):
        raise ValueError('That Gene command is not available through Fri')
    if action == 'play':
        if not 1 <= len(value) <= 240 or re.search(r'[\n\r<>@\\]', value):
            raise ValueError('Use a short song name or a YouTube URL')
        if '://' in value:
            url = urlsplit(value)
            if (url.scheme != 'https' or url.hostname not in {'youtube.com', 'www.youtube.com', 'music.youtube.com', 'youtu.be'}
                    or url.username or url.password or url.port not in (None, 443)):
                raise ValueError('Only song names and public YouTube URLs are supported')
        elif re.search(r'[:/]|^\.', value):
            raise ValueError('Local files and source prefixes cannot be selected through Fri')
    elif action == 'volume':
        if not re.fullmatch(r'\d{1,3}', value) or not 0 <= int(value) <= 100:
            raise ValueError('Choose a volume from 0 to 100')
    elif value:
        raise ValueError('This Gene action does not accept arguments')
    return value


VERBS = {'play': r'play|queue|put on|add', 'pause': r'pause', 'resume': r'resume|unpause',
         'skip': r'skip', 'stop': r'stop', 'volume': r'volume|turn (?:it|the music) (?:up|down)',
         'queue': r'queue|queued', 'now_playing': r'playing|song is this|track is this',
         'status': r'status|gene.*(?:doing|working|online)', 'version': r'version|release'}


def request_text(text):
    text = re.sub(r'```[\s\S]*?```|["“][^"”]*["”]|(?m:^>[^\n]*)', '', text).lower()
    return re.sub(r'<@!?\d+>', 'fri', text)


def mutation_request(text, action):
    """The established request grammar, also used to locate an explicit object."""
    prefix = r'(?:(?:hey|yo|bro|ok|okay|fri|please|pls|plz|gene)[\s,:.!]+)*'
    request = r'(?:(?:can|could|would|will) (?:you|u) |(?:i want|i would like|i\'d like) (?:you|u) to )?'
    delegate = r'(?:(?:please|pls) )?(?:(?:ask|get|tell) gene to )?(?:(?:set|change) (?:the )?)?'
    return re.search(r'(?:^|[.!?\n]\s*)'+prefix+request+delegate+(r'(?:join (?:the |my |our )?(?:vc|voice(?: channel)?) and )?' if action == 'play' else '')+r'(?:'+VERBS[action]+r')\b', text)


def requested(text, action):
    """Require a current request, not a quote, example, capability question, or old chat."""
    text = request_text(text)
    if re.search(r"\b(for example|hypothetically|can i|could i|are you able|if i|used to)\b", text):
        return False
    if re.search(r"\b(?:dont|don't|do not) (?:check|show|read|inspect|run)\b", text):
        return False
    if action in ('queue', 'now_playing', 'status') and re.search(
            r"\b(?:(?:dont|don't|do not|cant|can't|cannot) hear|no (?:sound|audio|music)|music (?:died|stopped|is broken))\b", text):
        return True  # Read-only troubleshooting is appropriate when the member reports silence.
    if re.search(r"\b(dont|don't|do not)\b", text):
        return False
    if action not in VERBS:
        return False
    if action in ('queue', 'now_playing', 'status', 'version'):
        return bool(re.search(r'\b(?:'+VERBS[action]+r')\b', text))
    # The model decides intent, but a passing mention of a verb cannot authorize a mutation.
    return bool(mutation_request(text.strip(), action))


def control_text(text):
    text = ' '.join(text.casefold().replace('’', "'").split()).strip(' .!?')
    text = re.sub(r'^(?:(?:hey|yo)\s+)?(?:fri|<@!?\d+>)[,:]?\s+', '', text)
    return re.sub(r'^(?:(?:can|could|would) you\s+)?(?:please\s+)?', '', text)


MUSIC_OBJECTS = {'pause': ('music', 'playback', 'audio'),
                 'resume': ('music', 'playback', 'audio'),
                 'skip': ('song', 'track'), 'stop': ('music', 'playback', 'audio')}


def music_object(action):
    return r'(?:the )?(?:this |current )?(?:'+'|'.join(MUSIC_OBJECTS[action])+r')'


def quick_control(text, *, music_context=False):
    """Exact current requests only; no interpretation of quoted or embedded verbs."""
    prepared = request_text(text).strip()
    for action in (*MUSIC_OBJECTS, 'volume'):
        if not requested(text, action):
            continue
        match = mutation_request(prepared, action)
        remainder = ' '.join(prepared[match.end():].split()).strip(' .!?')
        if action == 'volume':
            volume = re.fullmatch(r'(?:to )?(\d{1,3})', remainder)
            if volume:
                try:
                    arguments('volume', volume[1])
                except ValueError:
                    continue
                return {'action': 'volume', 'argument': volume[1]}
        elif re.fullmatch(music_object(action)+r'(?: now| please)?', remainder) or (not remainder and music_context):
            return {'action': action, 'argument': ''}
    text = control_text(text)
    aliases = {'pause': 'pause', 'resume': 'resume', 'unpause': 'resume', 'skip': 'skip',
               'stop': 'stop', 'queue': 'queue', 'show queue': 'queue', "what's queued": 'queue',
               'what is queued': 'queue', 'now playing': 'now_playing', "what's playing": 'now_playing',
               'what is playing': 'now_playing', 'status': 'status', 'gene status': 'status',
               'version': 'version', 'gene version': 'version'}
    if text in ('controls', 'music controls', 'gene controls'):
        return {'action': 'menu', 'argument': ''}
    if text in aliases and requested(text, aliases[text]):
        action = aliases[text]
        if action in MUSIC_OBJECTS and not music_context:
            return None
        return {'action': action, 'argument': ''}
    return None



def current_music_context(receipts):
    """Use only controller-supplied same-conversation receipts, never assistant claims."""
    import time
    return any(-5 <= time.time()-row.get('at', 0) <= 120 and
               row.get('action') in {'play', 'pause', 'resume', 'skip', 'stop', 'volume', 'queue', 'now_playing'} and
               row.get('state') in {'queued', 'claimed', 'running', 'finished', 'expired'}
               for row in receipts)


def music_control_intent(text, action, *, music_context=False):
    """An ambiguous conversation cue is not an instruction to change shared audio."""
    if action not in {'pause', 'resume', 'skip', 'stop'} or music_context:
        return True
    if not requested(text, action):
        return False
    prepared = request_text(text).strip()
    match = mutation_request(prepared, action)
    return bool(re.match(r'\s+'+music_object(action)+r'\b', prepared[match.end():]))
