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



FILLER = r"(?:(?:no|nah|nope|oops|sorry|bro|yo|fri|lol|wait|actually)[\s,.!]*)*"
MARKER = r"(?:not (?:that|this|the) (?:one|song|track)|wrong (?:one|song|track)|that'?s not (?:it|the one|the song|the track))"
LINK = r"(?:i meant|it'?s|its|it was)"
# The whole message is one correction: a marker, then a separator or "i meant"/"it's", then the song (or just
# "i meant <song>"). Nothing before it but a filler word, so a quote, an example, someone else's words or a
# question never fits. The link is consumed once (possessively), never given back to become the song.
OPENING = re.compile(
    rf"{FILLER}(?:{MARKER}(?:\s+(?:lol|lmao|bro))?\s*(?:[,:;.!—–-]+\s*(?:{LINK}\s+)?+|\s+{LINK}\s+)|i meant\s+)")
# The song may follow their own request words ("play X", "i said X"); its first word starts the replacement.
LEAD = re.compile(r"(?:(?:can|could|would) (?:you|u) )?(?:please |pls )?(?:play|queue|put on|add|i said|i asked for|i wanted)\s+")
# A title they quoted, or a link, keeps every word, whatever it says. A single quote, straight or curly, opens one only
# before a word: an apostrophe inside a word ("don't") never opens or closes one, nor does a leading one ("'til").
OPENER = r"(?<![\w'‘])['‘](?!\d|(?:til|cause|cuz|cos|coz|em|bout|round|n|tis|twas|nuff|fore|kay|sup)\b)(?=[^\s'])"
TITLE_QUOTE = re.compile(rf'(?P<double>["“])|(?P<link>https://\S+)|{OPENER}')
# The first mark after a word closes it, though not a dropped letter with more words after it ("Rockin' Robin");
# another opening, or a loose mark, before the close leaves it ambiguous.
CLOSER = re.compile(rf"(?P<loose>{OPENER}|(?<!\S)['‘](?!\w))|(?P<dropped>(?:(?<=in)|(?<=\bol)|(?<=\blil)|(?<=\bn)|(?<=\bo)|(?<=\ban))'"
                    r"(?=\s+\w))|(?<=\S)'(?!\w)")
WORD = re.compile(r"[\w']+")
CLAUSE = r"(?:^|[,;:.!?—–\-\"'‘“”]\s*|\b(?:and|but|or|so|then|also|plus|like|actually|wait|lol|nah|no|oh|please|pls|just)\s+)"
# Outside a quoted title, any of these means the message is not asking for that song now.
REFUSED = re.compile(
    rf"{CLAUSE}(?:don'?t|do not)\b"  # A clause that opens with "don't": "... do not queue anything yet".
    r"|\b(?:don'?t|do not|not|never)\s+(?:(?:even|actually|really|just|yet|to|ever|again)\s+){0,2}(?:play|queue|add|put|start)\b"
    r"|\b(?:don'?t|do not)[\s.!?'\"”]*$"
    r"|\b(?:cancel\w*|never ?mind|nvm|forget (?:it|that|about (?:it|that))|scratch that|hold off|not yet|no need|jk|just kidding)\b"
    # An example, or someone else's pick ("for example X", "according to alex"; see ATTRIBUTED for "alex said X").
    rf"|{CLAUSE}(?:according to|for example|for instance|e\.g\b|eg\b|such as|something like|stuff like|hypothetically)"
    # "i meant to say thanks" is chat, not a title; "To The Moon" can be one. After "i meant to", only a word that
    # can't start an infinitive may follow ("i meant To Love Somebody" fails closed; quoted, or "wrong song, To ...",
    # still counts).
    r"|\bi meant to\b(?!\s+(?:the|a|an|my|your|his|her|our|their|this|that|these|those|me|you|him|us|them|all)\b)")
# Someone else's words introducing more ("alex said X"; a title like "She Said" ends there). Their own "i said ..." or
# "we said ..." aren't someone else's, nor is an artist's name ("'All I Got' by Said The Sky"), so it's read with the
# first word of that masked, as WITHDRAWN reads it, which keeps the count of words before a verb.
ATTRIBUTED = re.compile(rf"{CLAUSE}(?!(?:i|we)\b)(?:[\w']+\s+){{1,3}}?(?:said|says|asked for|suggested|suggests|recommended"
                        r"|recommends|requested|requests|mentioned)\b(?=[\s,:]*[\w\"'‘“])")
# After the song, a change of heart withdraws it: "wrong song, Love Story, actually no", "... not that one either",
# a bare "nope" or "no thanks", or a closing "nah" or "no" after a pause, whatever punctuation or filler follows
# ("nah, lol"; "Hell No" stays a title), a closing "not now" or "no more" after a pause or a "no" ("..., no, not right
# now"), or a closing "i said no (to that)", "i told you not to", "we said no more" or "i said not now", which may end
# "this time" or "ever again". Checked after the correction only, since "actually no, wrong song, X" opens one.
CLOSING = (r"(?:[\s,;:.!?…—–\-'\"”]|\b(?:lol|lmao|lmfao|haha|bro|bruh|dude|man|fri|thanks|thank you|thx|ty|tho|though"
           r"|sorry|please|pls|plz|again|ever again|this time)\b)*+$")
LATER = (r"(?:not\s+(?:(?:right|just)\s+)?(?:now|rn)|not\s+(?:for\s+now|any\s*more|today|tonight)"
         r"|no\s+more(?:\s+(?:of\s+)?(?:(?:that|this|these|those|it|them)(?:\s+(?:ones?|songs?|tracks?))?|music|songs?))?)\b")
WITHDRAWN = re.compile(r"\b(?:actually,?\s+(?:no(?:pe|t that)?|nah)|no,?\s+wait|wait,?\s+no|(?:i\s+)?change[ds]?\s+my\s+mind"
                       r"|on second thought|never\s+mind|leave it|nope|n(?:o|ah)[\s,]+(?:thanks|thank you|thx|ty)"
                       r"|not\s+(?:(?:that|this|the|it)\s+)?(?:(?:one|song|track)\s+)?either)\b"
                       rf"|\bnah\b{CLOSING}|[,;:.!?…—–\-'\"]\s*no\b{CLOSING}"
                       rf"|(?:[,;:.!?…—–\-'\"]|\bn(?:o|ah)\b[,.!]?)\s*{LATER}{CLOSING}"
                       r"|\b(?:i|we)(?:'ve|'d)?\s+(?:(?:just|already|literally|clearly|even|actually|originally)\s+){0,2}"
                       rf"(?:said|told\s+(?:you|u)|asked(?:\s+(?:you|u))?|meant)\s+(?:{LATER}|(?:not|never)(?:\s+to(?:\s+do\s+"
                       rf"(?:that|this|it))?)?|(?:no(?:pe)?|nah)(?:\s+to\s+(?:that|this|it)(?:\s+(?:one|song|track))?)?)\b{CLOSING}")
# After the song, a clause handing it to someone else, quoting someone or calling it an example takes it back too:
# "'Love Story' was an example", "... is what i heard alex say", "..., alex said", "... was a quote from alex". Their
# own "i said" or "i asked for it" doesn't, nor does an inclusive "we want".
TRAILING = re.compile(r"\b(?:examples?|for instance|e\.?g|hypothetical(?:ly)?|such as|according to|quot\w*)\b"
                      r"|\bhear[sd]?\b(?!\s+me\b)"
                      r"|(?P<own>\b(?:i|we)(?:'ve|'d)?\s+(?:(?:just|already|literally|clearly|even|actually|originally)\s+){0,2})?"
                      r"\b(?P<verb>said|says?|told|tells|asked|asks|suggested|suggests|recommended|recommends|requested"
                      r"|requests|mentioned|mentions|wants?|wanted|picked|meant)\b")
# Unless they quoted it, a title ends with the song they named or sooner, at a clause break: punctuation, "is" or
# "was" (or "isn't"), a joining word, or an example ("Love Story, alex said" is no title, whatever song is proposed).
# A verb that opens a clause inside the named song is still the title's ("Say You, Say Me").
BREAK = re.compile(r"[,;:!?…—–()\[\]\"“”]|\.(?!\w)|\s-+(?=\s|$)|'s\b|\b(?:is|was|were|are|(?:isn|wasn|aren|weren|ain)'?t"
                   r"|like|as|and|but|or|so|then|also"
                   r"|plus|actually|wait|lol|lmao|nah|no|oh|please|pls|examples?|for instance|e\.?g|hypothetical(?:ly)?"
                   r"|such as|according to|quot\w*)\b")
PAUSE = re.compile(r"[,;:.!?…—–()\[\]\"“”]\s?$")
ADDRESSED = re.compile(r"(?<!\blike )(?<!\bas )\b(?:you|u|we)\s$")  # Not "like you say", which is someone else's.
# Right after the song (and its artist), in the same clause, their own words denying it: "... is (absolutely) not what i meant",
# "... isn't the one i asked for", "... is what i didn't ask for", "... is not what we want", "... isn't what alex and i
# asked for". ("..., that's not what i asked for" is about the song Gene played.)
DENIAL = (r"(?:said|say(?:ing)?|told|tell(?:ing)?|ask(?:ed|ing)?|want(?:ed|ing)?|meant|mean(?:ing)?|pick(?:ed|ing)?"
          r"|request(?:ed|ing)?|cho(?:se|ose|osing)|had\s+in\s+mind)\b")
ALONG = r"(?:(?:[\w']+,?\s+){1,3}?(?:and|&)\s+)?"  # Someone with them: "alex and i", "you, sam and i".
HEDGE = r"(?:really|quite|exactly|even|at\s+all|absolutely|definitely|certainly|totally|clearly|honestly|literally|actually|just|so|simply)"
DISOWNED = re.compile(rf"[^,;:.!?…—–()\[\]\"“”]*?\b(?:(?:(?:is|was|are|were)\s+(?:{HEDGE}\s+){{0,2}}not|(?:isn|wasn|aren|weren|ain)'?t)\s+"
                      rf"(?:{HEDGE}\s+){{0,2}}(?:what|who|the\s+(?:one|song|track))\s+(?:that\s+)?{ALONG}"
                      r"(?:i(?:'ve|'d|'m)?|we(?:'ve|'d|'re)?)\s+(?:(?:just|already|literally|clearly|even|actually|originally|been)\s+){0,2}"
                      r"|(?:is|was|are|were)\s+(?:(?:really|exactly|just)\s+)?(?:what|who|the\s+(?:one|song|track))\s+"
                      rf"(?:that\s+)?{ALONG}(?:i|we)\s+(?:didn'?t|did\s+not|don'?t|do\s+not|never)\s+(?:(?:even|really|ever)\s+)?)"+DENIAL)
# Between a title and its artist: "Love Story by Taylor Swift" may be named "Love Story Taylor Swift", though
# "Stand By Me" can't be "Stand Me". In the title or right after it, the word after one names the artist, never a
# withdrawal ("by Nope"), and with a leading "the" the artist's name is no one's words ("by The Wanted"), though
# never an example ("from the example").
CONNECTORS = {'by', 'from', 'ft', 'feat', 'featuring'}
NOT_ARTISTS = {'me', 'you', 'u', 'him', 'her', 'us', 'them', 'it', 'my', 'your', 'his', 'its', 'our', 'their', 'this',
               'that'}
ARTIST = re.compile(r"\b((?:by|from|featuring|ft\.?|feat\.?)\s+)(?P<first>[\w']+)"
                    r"(?:(?<=\bthe)\s+(?!(?:examples?|quot\w*|hypothetical\w*|hear[sd]?)\b)[\w']+)?")
CORRECTION_SECONDS = 600  # How long after Gene played a song for someone they can put it right.


def correction_text(text):
    """A message as its author wrote it: no code, no quoted lines (">>>" quotes the rest), no mentions."""
    text = re.sub(r'(?s)```.*?```|``.*?``|`[^`\n]*`', ' ', text)
    text = re.sub(r'(?ms)^>>>.*', ' ', text)
    text = re.sub(r'(?m)^>[^\n]*', ' ', text)
    text = re.sub(r'<@!?\d+>', ' ', text)
    return ' '.join(text.casefold().replace('’', "'").split())


def song_words(text):
    """A title's words in order, without stray apostrophes ("believin'" and "believin" match)."""
    return [word.strip("'") for word in WORD.findall(text) if word.strip("'")]


def quoted_title(text, start):
    """Where a title quoted (or linked) right at `start` ends, just past its closing mark: None if it isn't quoted
    or might not close (so nothing in it is protected), or -1 if it's ambiguous."""
    opening = TITLE_QUOTE.match(text, start)
    if not opening:
        return None
    if opening['link']:
        return opening.end()
    if opening['double']:
        close = re.compile(r'["”]').search(text, start+1)
        return close.end() if close else None
    for mark in CLOSER.finditer(text, start+1):
        if mark['loose']:
            return -1
        if not mark['dropped']:
            return mark.end()
    return None


def grounded_song(said, named, quoted=0):
    """How many of the words they said the named song covers from the start, word for word, or 0: never words
    picked from a later clause ("To The Moon, not To Be With You"). A title they quoted (its first `quoted` words)
    keeps every word. Otherwise only a leading article may be left out, and only connectors between a title and its
    artist skipped: never the title's first word ("From Me to You" isn't "Me to You") or one before a pronoun."""
    article = 0
    if not quoted and said[:1] in (['the'], ['a'], ['an']) and named[:1] != said[:1]:
        said, article = said[1:], 1  # "the X" may be named just "X".
    at = 0
    for word in named:
        while (max(quoted, 1) <= at < len(said)-1 and said[at] != word and said[at] in CONNECTORS
               and said[at+1] not in NOT_ARTISTS):
            at += 1
        if at >= len(said) or said[at] != word:
            return 0
        at += 1
    return article+at if at >= quoted else 0


def masked(text, spans):
    """The text with each of these (start, end) spans, in order, blanked to one word."""
    parts, at = [], 0
    for begin, finish in spans:
        parts += [text[at:begin], '_'*(finish-begin)]
        at = finish
    return ''.join(parts)+text[at:]


def handed_off(text, clause, song_end):
    """From `clause`, the title's end, the words hand the song to someone else or call it an example. Their own "i
    said" or "we want" doesn't, unless the "i" or "we" was part of the song they named; nor does a verb opening a
    clause in that song, or a bare "you say" or "we want" in it ("I Like the Way You Say My Name")."""
    for m in TRAILING.finditer(text, clause):
        verb = m.start('verb') if m['verb'] else -1
        if m['own'] and not m.start() < song_end <= verb:
            continue
        if 0 <= verb < song_end and (PAUSE.search(text, max(0, verb-2), verb)
                                     or m['verb'] in ('say', 'want') and ADDRESSED.search(text, max(0, verb-4), verb)):
            continue
        return True
    return False


def replacement(text, song, start, named):
    """The named song is the replacement at `start`, and nothing outside a title they quoted refuses it, takes it
    back after the song starts at `song`, or hands it to someone else or denies it after the title itself."""
    end = quoted_title(text, start)
    if end == -1:
        return False
    said = [(m[0].strip("'"), m.start(), m.end()) for m in WORD.finditer(text, start) if m[0].strip("'")]
    quoted = sum(at < end for _, at, _ in said) if end else 0
    covered = grounded_song([word for word, _, _ in said], named, quoted)
    if not covered:
        return False
    title = said[0][2]  # An artist follows the title's first word, never starts it.
    if quoted:  # Only this title is protected, never another quote in the message, and it ends where they closed it.
        text = text[:start]+'"'+' '*(end-start-2)+'"'+text[end:]
        song_end = clause = end
    else:
        song_end = said[covered-1][2]
        brk = BREAK.search(text, title, song_end)
        clause = brk.start() if brk else song_end
    # Only an artist in the song they named or right after it, never one in a later clause ("... was a quote from
    # Nope"). A clause break can be part of a title ("Say You, Say Me by Nope").
    limit = next((at for _, at, _ in said if at >= song_end), len(text))
    artists = [m for m in ARTIST.finditer(text, title) if m.start() <= limit]
    plain = masked(text, [(m.end(1), m.end('first')) for m in artists])
    nameless = masked(text, [(m.end(1), m.end()) for m in artists])
    return not (REFUSED.search(text) or ATTRIBUTED.search(plain) or WITHDRAWN.search(plain[song:])
                or DISOWNED.match(nameless, clause) or DISOWNED.match(nameless, song_end)
                or handed_off(nameless, clause, song_end))


def correction(text, argument):
    """A member putting right a song Gene just played for them ("not that one, X", "i meant X"): the song is
    the argument, in their own words, starting the replacement right after the correction. Whether there was
    such a play is checked by Gene's control store (Controls.enqueue)."""
    text = correction_text(text)
    opening = OPENING.match(text)
    named = song_words(argument.casefold()) if isinstance(argument, str) else []
    if not opening or not named:
        return False
    lead = LEAD.match(text, opening.end())
    return any(replacement(text, opening.end(), start, named)
               for start in (opening.end(), *([lead.end()] if lead else [])))


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
