"""Discord post links in Fri's replies: one goes out only if the model was shown that exact link."""

import re
from urllib.parse import unquote, unquote_to_bytes

import idna

# A post's link as Discord writes it, in any form it opens: any case, http, a sign-in or port, the www, ptb or canary
# host. What the model is shown is in this form (see shown).
URL = (r'https?://(?:[^\s/@<>]*@)?(?:(?:www|ptb|canary)\.)?discord(?:app)?\.com\.?(?::[0-9]*)?'
       r'/+channels/+(?:[0-9]{1,25}|@me)/+[0-9]{1,25}/+[0-9]{1,25}')
POST = re.compile(URL, re.I)
HOST = re.compile(r'(?:(?:www|ptb|canary)\.)?discord(?:app)?\.com\.?')
# A browser reads one more loosely (fri #200): past the scheme it skips any slashes or backslashes, takes the host
# after the last "@", percent-decodes and maps it as a domain (%64iscord.com, ｄｉｓｃｏｒｄ.com), splits the path at
# "/" or "\" and resolves "." and ".." ("%2e" too). The path's escapes count as what they encode ("%31" as 1), as an
# app's router may read them.
AUTHORITY = re.compile(r'[/\\]*([^/\\?#]*)')
SEGMENT = re.compile(r'[/\\]([^/\\?#]*)')
DIGITS = re.compile(r'(?:[0-9]|%3[0-9]){1,25}')
# A browser drops a tab or newline anywhere in a link, its scheme too ("htt\tps:"), and a control character before
# it (fri #208).
SCHEME = r'h[\t\n\r]*t[\t\n\r]*t[\t\n\r]*p[\t\n\r]*(?:s[\t\n\r]*)?:'
LINKED = re.compile(SCHEME, re.I)
DROPPED = re.compile(r'[\t\n\r]')
# Where a link can be: masked, in <…> (which can hold the tabs and newlines a browser drops), or bare up to a space
# or "<", whatever its host and path (see post).
LINK = re.compile(r'\[(?P<label>[^\]\n]{1,100})\]\(\s*<?(?P<masked>[\x00-\x1f]*'+SCHEME+r'[^\s<>()]*)>?\s*\)|'
                  r'<(?P<angled>[\x00-\x1f]*'+SCHEME+r'(?:[^\s<>]|[\t\n\r])*)>|<?(?P<bare>'+SCHEME+r'[^\s<]*)', re.I)
# The rest of a bare one: a trailing slash, query or fragment is part of it. Sentence punctuation or markdown at its
# end isn't, nor an emoji or other non-URL text right after it, nor a word after closing markdown ("**…**this"):
# inside it, "_" and "~" are its own (?utm_source=…), but it never ends on them, "*" or "|". Markdown opened before
# it ("__…__", "~~…~~") closes inside it, so neither that nor the word after is its own (fri #203).
# Inside <…> or a masked link's (…), it runs to the bracket.
MARKS = ('__', '~~')
TAIL = r'(?:(?=[/?#])(?:[A-Za-z0-9\-.:/?#@!$&+,;=%]|{})*[A-Za-z0-9\-/#@$&+=%])?'
TAILS = {opened: re.compile(TAIL.format('|'.join(f'{mark[0]}(?!{mark[1]})' if mark in opened else mark[0]
                                                 for mark in MARKS)))
         for opened in map(frozenset, ((), ('__',), ('~~',), MARKS))}
# Where the model reads a post's own link: a read's or search's rows, a pin list, nearby posts, the images it
# looked at, the current message. Never someone's words, saved turns or summaries.
SHOWN = ('source', 'reply_target', 'message_burst', 'invited_context', 'inspected_images', 'action_result',
         'action_results')
EMPTY = 'can’t link that one.'


def shown(context):
    """Every post link the model was shown as a post's source or url this call."""
    found = set()

    def walk(value, key=None):
        if isinstance(value, dict):
            for name, item in value.items():
                walk(item, name)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item, key)
        elif key in ('source', 'url') and isinstance(value, str) and POST.fullmatch(value):
            found.add(value)
    walk({key: context.get(key) for key in SHOWN})
    return found


def discord(host):
    """Whether a browser takes `host` for Discord's: percent-decoded, then mapped as the URL standard maps a domain."""
    try:
        return bool(HOST.fullmatch(idna.uts46_remap(unquote_to_bytes(host).decode(), std3_rules=False,
                                                    transitional=False)))
    except UnicodeError:  # Not a host a browser opens (idna's errors are UnicodeErrors too).
        return False


def post(url, whole=False):
    """The length of the post link at the start of `url`, through its message's ID, as written or as a browser reads
    it; None if `url` doesn't start with one. A tab or newline in it counts as nothing, and so does a control
    character or space before it. `whole`: `url` is all of the link (its <…> ends it), so as a browser reads it, its
    path's "." and ".." resolve to the end before it names a post or not (fri #209). Otherwise its end is Discord's
    to find (a bare link never ends on ".", see TAIL), so any start of it that names a post is one."""
    kept = None
    if url[0] <= ' ' or DROPPED.search(url):
        kept = [at for at, char in enumerate(url) if char not in '\t\n\r']
        start = next((index for index, at in enumerate(kept) if url[at] > ' '), len(kept))
        kept = kept[start:]
        url = ''.join(url[at] for at in kept)
    if (length := read(url, whole)) is None:
        return None
    return kept[length-1]+1 if kept else length  # Where it ends in `url` as written.


def read(url, whole):
    """post, for a `url` with nothing in it a browser drops."""
    if written := POST.match(url):
        return written.end()
    authority = AUTHORITY.match(url, url.index(':')+1)
    if not discord(authority[1].rpartition('@')[2].partition(':')[0]):
        return None
    # Its segments, and the ones that count, each with where its message's ID would end: as written, "//" doesn't.
    path, names, at = [], [], authority.end()
    while part := SEGMENT.match(url, at):
        segment, at = part[1], part.end()
        name = unquote(segment)
        if name == '..':
            if path and path.pop():
                names.pop()
        elif name != '.':
            path.append(name)
            if name:
                names.append((name, part.start(1)+message.end() if (message := DIGITS.match(segment)) else None))
                if len(names) == 4 and not whole and (ends := message_end(names)):
                    return ends
    return message_end(names) if whole else None


def message_end(names):
    """Where the post's message ID ends, if a path's first four segments name a post; None if they don't."""
    if (len(names) >= 4 and names[0][0].lower() == 'channels' and re.fullmatch(r'[0-9]{1,25}|@me', names[1][0], re.I)
            and re.fullmatch(r'[0-9]{1,25}', names[2][0])):
        return names[3][1]
    return None


def opened(marks, text):
    """The paired markdown ("__", "~~") still open after `text`, given what was open before it."""
    for mark in re.findall(r'__|~~', text):
        marks ^= {mark}
    return marks


def targeted(text, at):
    """Whether what starts at `at` may be a masked link's target: only spaces, as JavaScript reads them, after a "("."""
    while at and (text[at-1].isspace() or text[at-1] == '\ufeff'):
        at -= 1
    return text[at-1:at] == '('


def once(text, allowed):
    """`text` with each post link in it kept, as <link>, or dropped (see link_posts), once."""
    parts, done, at, marks, channels = [], 0, 0, frozenset(), set()
    while match := LINK.search(text, at):
        name = next(group for group in ('masked', 'angled', 'bare') if match[group])
        url, scheme = match[name], LINKED.search(text, match.start(name)).start()
        # Only <…> ends a link for certain, so only then is its whole path its own (see post). Not a masked link's
        # target out of <…>: Discord can refuse the masked link and read it bare. Nor <…> right after a "(": it may be
        # the target of a masked link LINK doesn't take ("[](…)"), and end at a ")" in it. Nor one with a "\\" in it,
        # which may escape its ">".
        whole = '\\' not in url and (
            name == 'masked' and text[match.start(name)-1] == '<' and text.startswith('>', match.end(name)) or
            name == 'angled' and not targeted(text, match.start()))
        if scheme in channels or (length := post(url, whole)) is None:
            if whole:
                channels.add(scheme)  # Not a post link, so not one read bare from the same start either.
            at = match.start()+1  # Not a post link: one may still start inside it (…?next=https://discord.com/…).
            continue
        marks, end = opened(marks, text[done:match.start()]), match.end()
        if match['masked']:
            label = once(match['label'], allowed)  # A label's own post links follow the same rule.
            kept = (f'[{label}](<{url}>)' if label.strip() else f'<{url}>') if url in allowed else label
        else:
            if match['bare']:
                start = match.start('bare')
                stop = TAILS[marks].match(text, start+length).end()
                url, end = text[start:stop], stop+text.startswith('>', stop)
            kept = f'<{url}>' if url in allowed else ''
        parts += [text[done:match.start()], kept]
        done = at = end
    return ''.join(parts)+text[done:]


def link_posts(reply, context, empty=EMPTY):
    """A post link the model was shown stays, as <link> so it shows no preview. Any other one (made up, mangled,
    another form of a shown one, or copied from someone's words) goes, and a masked one keeps its label. Other
    links are left as they are. `empty`: what to send when only a dropped link was there."""
    if not LINKED.search(reply):
        return reply
    allowed = shown(context)
    result = reply
    for _ in range(8):  # Dropping one link can join the text around it into another, so go until nothing changes.
        filtered = once(result, allowed)
        if filtered == result:
            return result if result.strip() else empty  # Only a dropped link: nothing left to send.
        result = filtered
    return empty  # Still changing: send nothing it could have made.
