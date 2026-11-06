"""What an edit took out of a message, as a fingerprint: hashed word runs and rare words, never its text (gene #150,
#246).

An edit withdraws the member's own saved exchange, but another member's saved reply whose request carried the message
may quote its old words. Retiring every reply that carried it would breadcrumb most of a busy channel's answers after a
typo fix, so one is retired only where it holds enough of what the edit took out. Fri makes the fingerprint from the
message as it stood and as it stands; Gene reads a saved reply the same way, and its own saved copy of the message too.
This module is the same in both repositories: Fri's integration tests check it word for word.

What's compared is each element of a text: every run of three words, not all COMMON; every pair of words, neither
COMMON; and every rare word (see rare). A text holds what an edit took out once what it shares of that weighs ENOUGH:
- a shared run weighs 2 where it has a rare word (a quote), 1 otherwise;
- a shared pair no shared run covers weighs its rare words, at least 1;
- a rare word no shared run or pair covers weighs its own (see rare).
A year, a time or a date (see rare) is a rare word that weighs 1, in a pair too, but never makes a run a quote, and a
run whose only words not COMMON are such numbers counts as one of them; all of them that no run or pair covers weigh at
most 1 together, so they need words beside them (gene #261 round 1).

So a quoted run with a name or a distinctive number in it is enough alone, and so is a distinctive number (a phone or a
house number, a code), or a paraphrase keeping two names or rare words, or a year with a name beside it; one name alone,
a year, a time or a date alone, or a common phrase, isn't. What the message says now (kept) is never counted as taken
out, whoever says it was (see removed).

An element is hashed with a salt (HMAC-SHA256, the first 16 hex digits), which keeps words out of the request, and the
fingerprint is never written down, by Fri or Gene. It's not secret from whoever receives it: the salt travels with it, so
an element from a set small enough to try (a year, a time, a short number, a word or name from a dictionary, a run of
them) can be found by hashing each candidate. Fri salts every edit while it runs with one salt (so two queued edits of a
message merge), so within a process the same element hashes alike in every request, and two fingerprints can be matched
against each other. Only the controller receives it, which holds the saved exchanges themselves."""
import hashlib
import hmac
import re
import secrets
import unicodedata

QUOTES = "'‘’ʼ＇"
POSSESSIVE = re.compile(f"[{QUOTES}]s\\b")
APOSTROPHE = re.compile(f"[{QUOTES}]")
WORD = re.compile(r'[^\W_]+')
COMMON = frozenset("""
a about above after again against all also am an and any are as at back be because been before being below between
both but by can cant could did didnt do does doesnt doing done dont down during each even few for from further get gets
getting got had has have having he her here hers herself him himself his how i if im in into is isnt it its itself ive
just know like lol me more most much my myself no nor not now of off on once only or other our ours out over own really
same say said she should so some such than that thats the their theirs them then there these they thing think this those
through to too under until up us very was we well were what when where which while who whom why will with would yeah
yes you your youre yours yourself yourselves ok okay oh hey hi lmao haha gonna wanna gotta tho though bro dude guys
something anything everything nothing someone anyone everyone nobody tomorrow yesterday tonight today because actually
probably definitely basically literally whatever honestly seriously please tell told thanks thank pretty little better
people around already another almost always anyway enough sorry sure good great nice cool want need going make time
still also last next first never maybe every thats theres whats""".split())
ENOUGH = 2
MOST = 3000  # Of each list sent: a 4000-character message has fewer elements than this.


SUFFIXED = re.compile(r'(\d+)[^\W\d_]+')  # A number with letters after it: "1990s", "0930am", "4217b".


def words(text):
    """The text's words: compatibility forms folded, case folded, a possessive's "'s" dropped and apostrophes inside a
    word too ("don't" is "dont"), then runs of letters and digits, digits next to each other joined ("555 0199" is
    "5550199", as a number written either way)."""
    return weighed(text)[0]


def weighed(text):
    """The text's words (see words), what each says alone, and whether each is a year, a time or a date (see rare)."""
    text = unicodedata.normalize('NFKC', text or '').casefold()
    said, groups = [], []
    for word in WORD.findall(APOSTROPHE.sub('', POSSESSIVE.sub('', text))):
        if said and word.isdigit() and said[-1].isdigit():
            said[-1] += word
            groups[-1].append(word)
        else:
            said.append(word)
            groups.append([word])
    weights = [rare(word, written) for word, written in zip(said, groups)]
    return said, [alone for alone, _ in weights], [plain for _, plain in weights]


def rare(word, groups=None):
    """How much a word alone says, and whether it's a year, a time or a date: for a number, by its groups of digits as
    written ("555 0199" is two groups, "5550199" one, "12:30" two; groups, from weighed: the word alone otherwise) or one
    with letters after it ("1990s", "0930am"), 0 for one of fewer than four digits, 1 for a year, a time or a date (see
    dated), 2 for any other (a phone or a house number, a PIN, a code); 2 for any other word with four digits or more (a
    code), 1 for one of six characters or more not COMMON (a name, a place), 0 otherwise."""
    groups = groups or [word]
    suffixed = SUFFIXED.fullmatch(word)
    digits = groups if all(group.isdecimal() for group in groups) else [suffixed.group(1)] if suffixed else None
    if digits is not None:
        if len(''.join(digits)) < 4:
            return 0, False
        return (1, True) if dated(digits) else (2, False)
    if sum(character.isdigit() for character in word) >= 4:
        return 2, False
    return (1 if len(word) >= 6 and word not in COMMON else 0), False


def dated(groups):
    """Whether groups of digits are a year (1900 to 2100), a time ("12:30", "12:30:15", "1530"), a date ("2024-05-01",
    "5/1/2024", "01.05.24", "may 5, 2024", "20240501"), or years next to each other: what many messages name."""
    if len(groups) == 1:
        [digits] = groups
        return year(digits) or (len(digits) == 4 and clock(digits[:2], digits[2:])) or (
            len(digits) == 8 and day(digits[:4], digits[4:6], digits[6:]))
    return all(year(group) for group in groups) or clock(*groups) or day(*groups)


def year(digits):
    return len(digits) == 4 and 1900 <= int(digits) <= 2100


def clock(hour, minute, second='00', *rest):
    return (not rest and 1 <= len(hour) <= 2 and int(hour) <= 23 and len(minute) == 2 and int(minute) <= 59
            and len(second) == 2 and int(second) <= 59)


def day(*groups):
    """A date, year first ("2024-05-01") or last ("1/5/2024", "5/1/24"), or a day and a month ("31 12"; a month and a
    day of four digits is a time too), or a year and a month, or a day or month and a year ("may 5, 2024" has the
    groups 5 and 2024)."""
    def month(digits):
        return len(digits) <= 2 and 1 <= int(digits) <= 12

    def date(digits):
        return len(digits) <= 2 and 1 <= int(digits) <= 31
    if len(groups) == 3:
        first, middle, last = groups
        return ((year(first) and month(middle) and date(last)) or
                ((year(last) or len(last) == 2) and ((date(first) and month(middle)) or (month(first) and date(middle)))))
    if len(groups) == 2:
        first, last = groups
        return (year(first) and month(last)) or (year(last) and date(first)) or (date(first) and month(last))
    return False


def runs(said):
    """The text's runs of three words, by where each starts, not all COMMON."""
    return {i: '3 '+' '.join(said[i:i+3]) for i in range(len(said)-2) if any(w not in COMMON for w in said[i:i+3])}


def pairs(said):
    """Its pairs of words, by where each starts, neither COMMON."""
    return {i: '2 '+said[i]+' '+said[i+1] for i in range(len(said)-1)
            if said[i] not in COMMON and said[i+1] not in COMMON}


def elements(text):
    """Each element of the text (see above): its runs, pairs and rare words."""
    said, alone, _ = weighed(text)
    return {*runs(said).values(), *pairs(said).values(), *('1 '+word for word, weight in zip(said, alone) if weight)}


def hashed(element, salt):
    return hmac.new(bytes.fromhex(salt), element.encode(), hashlib.sha256).hexdigest()[:16]


def salt():
    return secrets.token_hex(16)


def fingerprint(before, after, key):
    """An edit from `before` to `after` (None for either: not known), salted with `key`: the hashes of what `before`
    had that `after` doesn't (removed; none where `before` isn't known), and of everything `after` has (kept; none
    where it isn't known), so the controller can work out what was removed from its own copy of `before` too."""
    old = elements(before) if before is not None else {}
    new = elements(after) if after is not None else {}
    return {'salt': key, 'removed': sorted({hashed(element, key) for element in old if element not in new})[:MOST],
            'kept': sorted({hashed(element, key) for element in new})[:MOST]}


def removed(fingerprint, befores=()):
    """What the edit took out, as hashes: what the fingerprint says, and what each of `befores` (the controller's own
    copies of the message as it stood) has, less all the fingerprint's kept text, whichever says it was taken out (gene
    #261 round 1: what the message says now never was, however the fingerprint was put together)."""
    taken, kept = set(fingerprint.get('removed') or ()), set(fingerprint.get('kept') or ())
    for before in befores:
        taken |= {hashed(element, fingerprint['salt']) for element in elements(before)}
    return taken - kept


def holds(text, key, taken):
    """Whether the text holds enough of what an edit took out (`taken`, hashed with `key`): what it shares of that
    weighs ENOUGH (see above), each element counted once."""
    (said, alone, plain), counted, covered, weight, numbers = weighed(text), set(), set(), 0, 0

    def shared(element):
        if element in counted or hashed(element, key) not in taken:
            return False
        counted.add(element)
        return True
    for i, run in runs(said).items():
        if shared(run):
            span = range(i, i+3)
            if any(alone[j] and not plain[j] for j in span):
                weight += 2
            elif any(said[j] not in COMMON and not plain[j] for j in span):
                weight += 1
            else:  # Only a year, a time or a date among common words: as it alone.
                numbers = 1
            covered |= set(span)
    for i, pair in pairs(said).items():
        if not {i, i+1} <= covered and shared(pair):
            weight += max(1, alone[i]+alone[i+1])
            covered |= {i, i+1}
    for i, word in enumerate(said):
        if i not in covered and alone[i] and shared('1 '+word):
            if plain[i]:
                numbers = 1
            else:
                weight += alone[i]
    return weight+numbers >= ENOUGH
