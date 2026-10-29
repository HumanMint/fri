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
# "/" or "\" and resolves "." and ".." ("%2e" too). Discord's app reads the path's escapes too ("%31" is 1).
AUTHORITY = re.compile(r'[/\\]*([^/\\?#]*)')
SEGMENT = re.compile(r'[/\\]([^/\\?#]*)')
DIGITS = re.compile(r'(?:[0-9]|%3[0-9]){1,25}')
# Where a link can be: masked, in <…> (which can hold the tabs and newlines a browser drops), or bare up to a space
# or "<", whatever its host and path (see post).
LINK = re.compile(r'\[(?P<label>[^\]\n]{1,100})\]\(\s*<?(?P<masked>https?:[^\s<>()]*)>?\s*\)|'
                  r'<(?P<angled>https?:(?:[^\s<>]|[\t\n\r])*)>|<?(?P<bare>https?:[^\s<]*)', re.I)
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


def post(url):
    """The length of the post link at the start of `url`, through its message's ID, as written or as a browser reads
    it; None if `url` doesn't start with one. A tab or newline in it (only <…> holds one) counts as nothing."""
    url = re.sub(r'[\t\n\r]', '', url)
    if written := POST.match(url):
        return written.end()
    authority = AUTHORITY.match(url, url.index(':')+1)
    if not discord(authority[1].rpartition('@')[2].partition(':')[0]):
        return None
    path, names, at = [], [], authority.end()  # Its segments, and the ones that count: as written, "//" doesn't.
    while part := SEGMENT.match(url, at):
        segment, at = part[1], part.end()
        name = unquote(segment)
        if name == '..':
            if path and path.pop():
                names.pop()
        elif name != '.':
            if (len(names) == 3 and names[0].lower() == 'channels' and re.fullmatch(r'[0-9]{1,25}|@me', names[1], re.I)
                    and re.fullmatch(r'[0-9]{1,25}', names[2]) and (message := DIGITS.match(segment))):
                return part.start(1)+message.end()
            path.append(name)
            if name:
                names.append(name)
    return None


def opened(marks, text):
    """The paired markdown ("__", "~~") still open after `text`, given what was open before it."""
    for mark in re.findall(r'__|~~', text):
        marks ^= {mark}
    return marks


def once(text, allowed):
    """`text` with each post link in it kept, as <link>, or dropped (see link_posts), once."""
    parts, done, at, marks = [], 0, 0, frozenset()
    while match := LINK.search(text, at):
        url = match['masked'] or match['angled'] or match['bare']
        if (length := post(url)) is None:
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
    if not re.search(r'https?:', reply, re.I):
        return reply
    allowed = shown(context)
    result = reply
    for _ in range(8):  # Dropping one link can join the text around it into another, so go until nothing changes.
        filtered = once(result, allowed)
        if filtered == result:
            return result if result.strip() else empty  # Only a dropped link: nothing left to send.
        result = filtered
    return empty  # Still changing: send nothing it could have made.
