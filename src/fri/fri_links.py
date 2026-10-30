"""Discord post links in Fri's replies: one goes out only if the model was shown that exact link."""

import bisect
import functools
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
SLASHES = re.compile(r'[/\\]*')
MARK = re.compile(r'[/\\?#]')  # Where an address ends.
SEGMENT = re.compile(r'[/\\]([^/\\?#]*)')
DIGITS = re.compile(r'(?:[0-9]|%3[0-9]){1,25}')
# A browser drops a tab or newline anywhere in a link, its scheme too ("htt\tps:"), and a control character before
# it (fri #208).
SCHEME = r'h[\t\n\r]*t[\t\n\r]*t[\t\n\r]*p[\t\n\r]*(?:s[\t\n\r]*)?:'
LINKED = re.compile(SCHEME, re.I)
DROPPED = re.compile(r'[\t\n\r]')
# A space as Discord's markdown reads one (JavaScript's \s): not a control character Python also calls a space, which a
# browser keeps in a link (encoded), as it does any of these but the plain space inside <…>.
SPACE = '\t\n\v\f\r \xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff'
# Markdown undoes a masked link's escapes before a browser reads its target (fri #231): a "\\" before anything but a
# letter, digit or space goes, so "x\\(y/.." is "x(y/..", which resolves away, where "\\" read as written splits the
# path as "/" does. Its scheme's ":" may be escaped too, and in a target an escaped ")", ">" or space (a "\\" and any
# character but a line break, as Discord's link reads one) doesn't end it.
ESCAPE = re.compile(r'\\[^0-9A-Za-z'+SPACE+r']')
ESCAPED = SCHEME[:-1]+r'\\?:'
SCHEMES = re.compile(ESCAPED, re.I)
PAIR = r'\\[^\n\r\u2028\u2029]'
# Where a link can be: masked (an empty label too), in <…>, or bare, whatever its host and path. A masked link's
# target can hold a "<", as Discord's can, and escapes (see ESCAPE). One in <…> runs to a plain space or ">", a "<"
# too, as Discord's autolink does (fri #220), and a bare one to a space or "<": each run (ANGLED, RUN) is found once for
# the links that start inside it, and read once for all of them (see ends).
LINK = re.compile(r'\[(?P<label>[^\]\n]{0,100})\]\(\s*<?(?P<masked>[\x00-\x1f]*'+ESCAPED+r'(?:'+PAIR+r'|[^'+SPACE+r'>()\\])*)'
                  r'>?\s*\)|<(?P<angled>[\x00-\x1f]*'+SCHEME+r')|<?(?P<bare>'+ESCAPED+r')', re.I)
BARE = re.compile(r'<?(?P<bare>'+SCHEME+r')', re.I)
ANGLED = re.compile(r'[^ >]*')
RUN = re.compile(r'[^'+SPACE+r'<]*')
# Right after a "(", one may be the target of a masked link LINK doesn't take (a label holding a newline, or a "]"
# Discord allows), which a "<" doesn't end either: it's read to a space instead (see aimed), or past an escaped one.
TARGET = re.compile(r'(?:'+PAIR+r'|[^'+SPACE+r'])*')
# What ends a masked link right after its target (see Runs.close): its ")", or a title (a quote, then one right before
# the ")"); and where a target may end, so where to look for one.
SHUT = re.compile(r'['+SPACE+r']*\)')
TITLED = re.compile(r'['+SPACE+r']+[\'"]')
TITLE_END = re.compile(r'[\'"]['+SPACE+r']*\)')
SPECIAL = re.compile(r'[()>\\'+SPACE+r']')
GAPS = re.compile(r'['+SPACE+r']')
# A masked link's "(", and where its target starts (see Runs.frame).
FRAME = re.compile(r'\](\()['+SPACE+r']*')
# A link's start, as the receipt reply's cut keeps it whole (see linked): a scheme with an address after it.
ADDRESS = re.compile(SCHEME[:-1]+r'(?P<escape>\\)?:[/\\]+[^'+SPACE+r'/\\]', re.I)
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
# A web lookup's receipt lists the pages it cited as plain strings: each is a verified source too (fri #223). Its
# answer's words aren't.
WEB = ('web_search', 'web_refine', 'web_sources', 'image_search')
EMPTY = 'can’t link that one.'


def shown(context):
    """Every post link the model was shown as a post's source or url this call."""
    found = set()

    def walk(value, key=None):
        if isinstance(value, dict):
            if value.get('kind') in WEB and isinstance(value.get('sources'), list):
                walk([{'url': source} for source in value['sources']])
            for name, item in value.items():
                walk(item, name)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item, key)
        elif key in ('source', 'url') and isinstance(value, str) and POST.fullmatch(value):
            found.add(value)
    walk({key: context.get(key) for key in SHOWN})
    return found


@functools.lru_cache(maxsize=1024)
def discord(host):
    """Whether a browser takes `host` for Discord's: percent-decoded, then mapped as the URL standard maps a domain."""
    try:
        return bool(HOST.fullmatch(idna.uts46_remap(unquote_to_bytes(host).decode(), std3_rules=False,
                                                    transitional=False)))
    except UnicodeError:  # Not a host a browser opens (idna's errors are UnicodeErrors too).
        return False


def ends(span, plain=True):
    """Where the post link starting at each scheme in `span` ends, through its message's ID, as written or as a
    browser reads it to the end of `span`: {where it starts: (where it ends, where it ends if whole)}, None if it names
    none. A tab or newline in it counts as nothing (`span` holds one only in a scheme or escaped, or is a <…> link's,
    which can hold one anywhere). `plain`: as written; otherwise as markdown hands a masked link's target to a
    browser, its escapes undone (see ESCAPE), where an escaped ":" can make a scheme. `whole`: `span` ends the link
    (its <…> does), so as a browser reads it, its path's "." and ".." resolve to the end before it names a post or not
    (fri #209). Otherwise its end is Discord's to find (a bare link never ends on ".", see TAIL), so any start of it
    that names a post is one.

    Each link's path is the span's from where its own address ends, so the path is read once for all of them (see
    paths), not once for each (fri #223)."""
    kept, escapes = None, () if plain else {escape.start() for escape in ESCAPE.finditer(span)}
    if escapes or DROPPED.search(span):
        kept = [at for at, char in enumerate(span) if char not in '\t\n\r' and at not in escapes]
        span = ''.join(span[at] for at in kept)
    found, starts, mark = {}, [], -1
    for scheme in LINKED.finditer(span):
        at = scheme.start()
        if written := POST.match(span, at):
            found[at] = (written.end(),)*2
            continue
        found[at] = (None, None)
        address = SLASHES.match(span, scheme.end()).end()
        if mark < address:  # Its address's end: the next start's is after it, so each is found once.
            mark = next_mark.start() if (next_mark := MARK.search(span, address)) else len(span)
        host = span.rfind('@', address, mark)+1 or address  # After the last "@", up to a port.
        port = span.find(':', host, mark)
        if discord(span[host:mark if port == -1 else port]):
            starts.append((mark, at))
    paths(span, starts, found)
    return {kept[at] if kept else at: tuple(None if end is None else kept[end-1]+1 if kept else end for end in ending)
            for at, ending in found.items()}


def paths(span, starts, found):
    """Each start's (where its path starts, where the link starts) post (see ends): through its message's ID as soon
    as the first four segments that count name one, or once all of them are read (whole). "." and ".." resolve as a
    browser resolves them, and as written "//" doesn't count.

    Read once for every start along one run of segments: as the first one reads it (`stack`), each later start's path
    is the top of that from where it started (its floor), lower once a ".." pops below it, so starts whose floors meet
    read the same from then on. A start names a post when the four names at the top of the stack do and exactly those
    are above its floor."""
    index = 0
    while index < len(starts):
        at, stack, names, waiting, everyone = starts[index][0], [], [], [], []
        while True:
            while index < len(starts) and starts[index][0] == at:
                join(waiting, len(stack), starts[index][1])
                join(everyone, len(stack), starts[index][1])
                index += 1
            if not (part := SEGMENT.match(span, at)):
                break
            segment, at = part[1], part.end()
            name = unquote(segment)
            if name == '..':
                if stack:
                    if stack.pop()[0]:
                        names.pop()
                    lower(waiting, len(stack))
                    lower(everyone, len(stack))
            elif name != '.':  # Its segments, and the ones that count, each with where its message's ID would end.
                stack.append((name, part.start(1)+message.end() if (message := DIGITS.match(segment)) else None))
                if name:
                    names.append(len(stack)-1)
                    if len(names) >= 4 and (end := message_end([stack[k] for k in names[-4:]])):
                        low = bisect.bisect_right(waiting, names[-5] if len(names) > 4 else -1, key=floor)
                        high = bisect.bisect_right(waiting, names[-4], key=floor)
                        for _, group in waiting[low:high]:
                            for start in group:
                                found[start] = (end, None)
                        del waiting[low:high]
        for bottom, group in everyone:  # Whole: the first four names above its floor, once all of it is read.
            first = bisect.bisect_left(names, bottom)
            end = message_end([stack[k] for k in names[first:first+4]])
            for start in group:
                found[start] = (found[start][0], end)


def floor(group):
    return group[0]


def join(groups, height, start):
    """Add a start whose path starts at the top of the stack (at `height`) to `groups`, kept in floor order."""
    if groups and groups[-1][0] == height:
        groups[-1][1].append(start)
    else:
        groups.append([height, [start]])


def lower(groups, height):
    """The stack popped to `height`: every start whose floor was above it reads from there now."""
    merged = []
    while groups and groups[-1][0] >= height:
        merged.append(groups.pop()[1])
    if merged:
        merged.sort(key=len)
        group = merged.pop()
        for other in merged:
            group += other
        groups.append([height, group])


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


def escaped(text, at):
    """Whether the character at `at` is escaped: an odd number of backslashes right before it."""
    start = at
    while start and text[start-1] == '\\':
        start -= 1
    return (at-start) % 2 == 1


def targeted(text, at):
    """Whether what starts at `at` may be a masked link's target: only spaces, as JavaScript reads them, after a "("."""
    while at and (text[at-1].isspace() or text[at-1] == '\ufeff'):
        at -= 1
    return text[at-1:at] == '('


def aimed(text, at):
    """targeted, for the scheme at `at`: control characters and a "<" may come before it too."""
    while at and text[at-1] <= ' ':
        at -= 1
    return targeted(text, at-(text[at-1:at] == '<'))


def framed(text, at):
    """aimed, after a "](": the link at `at` may be a masked link's target, which runs on to its ")"."""
    while at and text[at-1] <= ' ':
        at -= 1
    at -= text[at-1:at] == '<'
    while at and (text[at-1].isspace() or text[at-1] == '\ufeff'):
        at -= 1
    return at >= 2 and text[at-2:at] == ']('


def past(runs, end):
    """Whether a masked link's target holding a link that ends at `end` may run on past it (fri #236): unless it closes
    right there (the link's own ">" is the one before its ")"), its target goes on over that ">" and what follows, to
    wherever it closes (see Runs.close)."""
    return end <= runs.closing and runs.closer(end, False) is None and runs.close(end) is not None


def linked(text, start, stop):
    """Whether a link starts in text[start:stop] as the filter reads one: a scheme with an address after it, its ":"
    escaped only where a masked link's target may start (review of fri #236)."""
    return any(not found['escape'] or aimed(text, found.start()) for found in ADDRESS.finditer(text, start, stop))


class Runs:
    """A text's masked-target runs (see TARGET), each read once for every start inside it."""

    def __init__(self, text):
        self.text, self.closing, self.last, self.closes, self.titled = text, text.rfind(')'), (1, 0), {}, None

    def run(self, at):
        """Where TARGET's run from `at` ends. A start inside the last run read is on its own step (nothing but a
        "\\" escapes the character after it, and no start follows one), so that run's end is its end too."""
        first, end = self.last
        if not first <= at <= end:
            end = TARGET.match(self.text, at).end()
            self.last = (at, end)
        return end

    def reach(self, at):
        """How far a masked link's target from `at` may run at the most. Discord's target takes a "(…)" group whole, a
        space in it too (fri #235): an unescaped "(" after the run's last ")" runs on to the next one, past the run,
        and the target may go on from there."""
        text = self.text
        while True:
            run = self.run(at)
            opened = self.unescaped('(', max(at, text.rfind(')', at, run)+1), run)
            if opened < 0 or opened > self.closing:
                return run
            at = text.find(')', run)+1

    def unescaped(self, char, start, end):
        """The last `char` in text[start:end] that no "\\" escapes, or -1."""
        found = self.text.rfind(char, start, end)
        while found >= 0 and escaped(self.text, found):
            found = self.text.rfind(char, start, found)
        return found

    def closer(self, at, mark=True):
        """Where a masked link ends if its target ends at `at`: past a ">" (`mark`), its ")" or a title; None if
        neither."""
        text = self.text
        at += mark and text.startswith('>', at)
        if shut := SHUT.match(text, at):
            return shut.end()
        if quote := TITLED.match(text, at):
            if self.titled is None:
                self.titled = [end.start() for end in TITLE_END.finditer(text)]
            ending = bisect.bisect_left(self.titled, quote.end())
            if ending < len(self.titled):
                return TITLE_END.match(text, self.titled[ending]).end()
        return None

    def close(self, at):
        """Where the masked link whose target starts at `at` ends, as Discord's markdown reads it, or None if it
        doesn't: at the first place its target may end (see closer), taking a "(…)" group whole before a "(" alone,
        a space in it too (fri #235). Each place is read once, whatever start reaches it."""
        text, memo, stack = self.text, self.closes, [at]
        while stack:
            place = stack[-1]
            if place in memo:
                stack.pop()
                continue
            char = text[place:place+1]
            if not char or GAPS.match(char):  # Only a ")" or a title may follow it here.
                memo[place] = self.closer(place)
                stack.pop()
                continue
            if char in ')>' and (end := self.closer(place)) is not None:
                memo[place] = end
                stack.pop()
                continue
            if char == '\\':  # An escape takes the character after it, but not a line break.
                options = [] if text[place+1:place+2] in ('', '\n', '\r', '\u2028', '\u2029') else [place+2]
            elif char == '(':  # Its group whole, if a ")" comes after it, then the "(" alone.
                options = ([text.find(')', place+1)+1] if place < self.closing else []) + [place+1]
            elif char in ')>':
                options = [place+1]
            else:  # Nothing ends the target before the next mark.
                found = SPECIAL.search(text, place+1)
                options = [found.start() if found else len(text)]
            for option in options:  # The first that closes, in order; one not read yet is read first.
                if option not in memo:
                    stack.append(option)
                    break
                if memo[option] is not None:
                    memo[place] = memo[option]
                    stack.pop()
                    break
            else:
                memo[place] = None
                stack.pop()
        return memo[at]


def assemble(runs, edits):
    """`text` with `edits` (where each starts and ends, what goes there, whether a link was dropped) made, but a link
    dropped inside a masked link's target that may close takes all of that target, the outermost one's, with the label
    kept as a dropped masked link's is (review of fri #236). What's left of a target can never be joined into
    another link. `edits` are in order and apart, and so are the targets, so each is read once."""
    text, regions = runs.text, []
    if dropped := [edit[0] for edit in edits if edit[3]]:
        frames, index = [(found.start(1), found.end()) for found in FRAME.finditer(text)], 0
        for begin in dropped:
            while index < len(frames) and frames[index][0] < begin:  # The outermost that holds it, in "(" order.
                opening, start = frames[index]
                if (end := runs.close(start)) is not None and end > begin:
                    if regions and opening < regions[-1][1]:
                        regions[-1][1] = max(regions[-1][1], end)
                    else:
                        regions.append([opening, end, opening])
                    break
                index += 1
    merged, index = [], 0
    for region in regions:  # An edit across a region's edge goes with it, and regions that meet are one.
        while index < len(edits) and edits[index][1] <= region[0]:
            index += 1
        while index < len(edits) and edits[index][0] < region[1]:
            region[0], region[1] = min(region[0], edits[index][0]), max(region[1], edits[index][1])
            index += 1
        if merged and region[0] < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], region[1])
        else:
            merged.append(region)
    starts = [edit[0] for edit in edits]

    def edited(place):
        at = bisect.bisect_right(starts, place)-1
        return at >= 0 and place < edits[at][1]
    pieces, before = [], 0
    for start, end, opening in merged:
        pieces.append((start, end, ''))
        bracket = text.rfind('[', before, opening-1)  # A plain label's brackets go, as a dropped masked link's do.
        if (start == opening and bracket >= 0 and text.find(']', bracket+1, opening-1) < 0 and not edited(bracket) and
                not edited(opening-1)):
            pieces += [(bracket, bracket+1, ''), (opening-1, opening, '')]
        before = end
    index = 0
    for begin, end, kept, _ in edits:
        while index < len(merged) and merged[index][1] <= begin:
            index += 1
        if not (index < len(merged) and merged[index][0] <= begin):
            pieces.append((begin, end, kept))
    parts, done = [], 0
    for start, end, kept in sorted(pieces):
        parts += [text[done:start], kept]
        done = end
    return ''.join(parts)+text[done:]


def once(text, allowed, found):
    """`text` with each post link in it kept, as <link>, or dropped (see link_posts), once. `found`: what ends found for
    each run of text read so far."""
    edits, done, at, marks, channels, runs, targets = [], 0, 0, frozenset(), set(), {}, Runs(text)
    bare, angled, target = (1, 0), (1, 0), (1, 0)

    def length(scheme, start, stop, whole, plain=True):
        """The length of the post link starting at `start` (its scheme at `scheme`), whose run ends at `stop`; None
        if it names none. Read once for every link starting in that run (see ends), as written or, not `plain`, with
        its escapes undone."""
        first, ending = runs.get((stop, plain), (None, None))
        if first is None or scheme-first not in ending:
            span = text[scheme:stop]
            if (span, plain) not in found:
                found[span, plain] = ends(span, plain)
            first, ending = runs[stop, plain] = scheme, found[span, plain]
        end = ending[scheme-first][whole]
        return None if end is None else first+end-start

    while match := LINK.search(text, at):
        name, begin = next(group for group in ('masked', 'angled', 'bare') if match[group]), match.start()
        if name == 'angled':  # To a plain space or ">": where an earlier one's run ends, if it starts inside that run.
            if not angled[0] <= match.end() <= angled[1]:
                angled = (match.end(), ANGLED.match(text, match.end()).end())
            if text.startswith('>', angled[1]):
                start, stop = match.start(name), angled[1]
            else:  # Never closed, so a bare one, as Discord reads it.
                match, name = BARE.search(text, begin), 'bare'
                begin = match.start()
        undone = name == 'masked' or name == 'bare' and aimed(text, match.start(name))  # Its escapes may be undone.
        if name == 'bare' and undone:  # Maybe a masked link's target: to a space, or past one through a group.
            if not target[0] <= match.end() <= target[1]:
                target = (match.end(), targets.reach(match.end()))
            start, stop = match.start(name), target[1]
        elif name == 'bare' and '\\' in match[name]:  # "https\\:" out of a target: markdown shows it as text, no link.
            at = begin+1
            continue
        elif name == 'bare':  # To a space or "<": where an earlier one's run ends, if it starts inside that run.
            if not bare[0] <= match.end() <= bare[1]:
                bare = (match.end(), RUN.match(text, match.end()).end())
            start, stop = match.start(name), bare[1]
        elif name == 'masked':
            start, stop = match.span(name)
        url, scheme = text[start:stop], SCHEMES.search(text, start)
        # Only <…> ends a link for certain, so only then is its whole path its own (see ends). Not a masked link's
        # target out of <…>: Discord can refuse the masked link and read it bare. Nor <…> right after a "(": it may be
        # the target of a masked link LINK doesn't take, and end at a ")" in it. Nor one with a "\\" in it, which may
        # escape its ">". Nor one whose "<" is escaped ("\\<"): only text, so the link is a bare one. Nor one without a
        # "/" right after its scheme, which Discord's autolink needs, nor a masked one whose scheme a tab or newline
        # splits, which Discord may not take for a link at all.
        whole = '\\' not in url and text.startswith('/', scheme.end()) and (
            name == 'masked' and text[start-1] == '<' and text.startswith('>', stop) and
            not DROPPED.search(scheme[0]) or
            name == 'angled' and not targeted(text, begin) and not escaped(text, begin))
        plain, scheme = '\\' not in scheme[0], scheme.start()  # As written, "https\\:" is no scheme.
        size = length(scheme, start, stop, whole) if plain and scheme not in channels else None
        if size is None and undone and '\\' in url and scheme not in channels:  # As a browser gets it from markdown.
            size = length(scheme, start, stop, False, False)
        if size is None:
            if whole:
                channels.add(scheme)  # Not a post link, so not one read bare from the same start either.
            at = begin+1  # Not a post link: one may still start inside it (…?next=https://discord.com/…).
            continue
        marks, end = opened(marks, text[done:begin]), match.end() if name == 'masked' else stop+1
        if name == 'masked':
            label = once(match['label'], allowed, found)  # A label's own post links follow the same rule.
            shown = url in allowed
            kept = (f'[{label}](<{url}>)' if label.strip() else f'<{url}>') if shown else label
        else:
            if name == 'bare':
                tail = TAILS[marks].match(text, start+size).end()
                url, end = text[start:tail], tail+text.startswith('>', tail)
            # Exactly a shown link, and a masked link's whole target if it may be one ("<A>(x)/../.." isn't A).
            shown = url in allowed and not (framed(text, start) and past(targets, end))
            kept = f'<{url}>' if shown else ''
        edits.append((begin, end, kept, not shown))
        done = at = end
    return assemble(targets, edits)


def link_posts(reply, context, empty=EMPTY):
    """A post link the model was shown stays, as <link> so it shows no preview. Any other one (made up, mangled,
    another form of a shown one, or copied from someone's words) goes, and a masked one keeps its label. Other
    links are left as they are. `empty`: what to send when only a dropped link was there."""
    if not SCHEMES.search(reply):
        return reply
    allowed, found = shown(context), {}
    result = reply
    for _ in range(8):  # Dropping one link can join the text around it into another, so go until nothing changes.
        filtered = once(result, allowed, found)
        if filtered == result:
            return result if result.strip() else empty  # Only a dropped link: nothing left to send.
        result = filtered
    return empty  # Still changing: send nothing it could have made.
