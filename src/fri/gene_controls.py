"""Small audited command catalog. Model output cannot select arbitrary Red commands."""

import re
import unicodedata
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


def own_words(argument, text):
    """Every word of a command argument comes from the member's own message."""
    if argument.casefold() in text.casefold():
        return True
    return words(argument) <= words(text)


def grounded(receipts):
    """A legacy companion's argument may quote anyone, and Red's output can echo it: show only that it ran."""
    return [r if r.get('grounded') or not r.get('argument') else
            {**r, 'argument': '[not kept]', 'result': {'done': r.get('result', {}).get('done'), 'output': '[not kept]'}}
            for r in receipts]


def words(text):
    # Case, accents and punctuation may differ ("Halo Beyonce" from "Halo by Beyoncé"); words may not.
    folded = unicodedata.normalize('NFKD', text.casefold())
    return set(re.findall(r'\w+', ''.join(c for c in folded if not unicodedata.combining(c))))


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



CORRECTION = re.compile(r"\b(?:not (?:that|this|the) (?:one|song|track)|wrong (?:one|song|track)|i meant(?! to\b)|"
                        r"that'?s not (?:it|the one|the song|the track))\b")
# Words of the correction itself, never the song on their own ("not the one i wanted" names none).
CORRECTION_WORDS = {'the', 'one', 'song', 'track', 'that', 'this', 'it', 'its', "it's", 'a', 'i', 'meant',
                    'not', 'wrong', 'was', 'is', 'lol'}
CORRECTION_SECONDS = 600  # How long after Gene played a song for someone they can put it right.


def correction(text, argument):
    """A member putting right a song Gene just played for them ("not that one, X", "i meant X"), with the
    right one after it in their own lines: not a blockquote or code, though a title they put in quotes counts.
    Gene's control store holds the same rule and checks there was such a play."""
    visible = request_text(text).replace('\u2019', "'")
    if not argument or not CORRECTION.search(visible):
        return False
    if re.search(r"\b(?:for example|hypothetically|if i|used to|dont|don't|do not|said|says|saying|told|asked|"
                 r"(?:can|could|would|should) (?:i|you|we))\b", visible):
        return False  # An example, someone else's words, or a question about it: not a request.
    own = re.sub(r'```[\s\S]*?```|`[^`]*`|(?m:^>[^\n]*)', '', text).casefold().replace('\u2019', "'")
    marker = CORRECTION.search(own)
    after = own[marker.end():] if marker else ''
    named = set(re.findall(r"[\w']+", argument.casefold()))
    return bool(named - CORRECTION_WORDS) and (argument.casefold() in after or named <= set(re.findall(r"[\w']+", after)))


def just_played(receipts):
    """A play Gene finished for this member in this conversation within the correction window, by the
    controller's own receipts (never Fri's claims)."""
    import time
    return any(row.get('action') == 'play' and row.get('state') == 'finished' and
               -5 <= time.time()-row.get('at', 0) <= CORRECTION_SECONDS for row in receipts)


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
