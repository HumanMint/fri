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

So a quoted run with a name or a number in it is enough alone, and so is a number, or a paraphrase keeping two names or
rare words; one name alone, or a common phrase, isn't. An element is hashed with a salt (HMAC-SHA256, the first 16 hex
digits), so the fingerprint can't be read back as words or matched against another's; it's never written down, by Fri
or Gene."""
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


def words(text):
    """The text's words: compatibility forms folded, case folded, a possessive's "'s" dropped and apostrophes inside a
    word too ("don't" is "dont"), then runs of letters and digits, digits next to each other joined ("555 0199" is
    "5550199", as a number written either way)."""
    text = unicodedata.normalize('NFKC', text or '').casefold()
    said = []
    for word in WORD.findall(APOSTROPHE.sub('', POSSESSIVE.sub('', text))):
        if said and word.isdigit() and said[-1].isdigit():
            said[-1] += word
        else:
            said.append(word)
    return said


def rare(word):
    """How much a word alone says: 2 for one with four digits or more (a number, a code), 1 for one of six characters or
    more not COMMON (a name, a place), 0 otherwise."""
    if sum(character.isdigit() for character in word) >= 4:
        return 2
    return 1 if len(word) >= 6 and word not in COMMON else 0


def runs(said):
    """The text's runs of three words, by where each starts, not all COMMON."""
    return {i: '3 '+' '.join(said[i:i+3]) for i in range(len(said)-2) if any(w not in COMMON for w in said[i:i+3])}


def pairs(said):
    """Its pairs of words, by where each starts, neither COMMON."""
    return {i: '2 '+said[i]+' '+said[i+1] for i in range(len(said)-1)
            if said[i] not in COMMON and said[i+1] not in COMMON}


def elements(text):
    """Each element of the text (see above): its runs, pairs and rare words."""
    said = words(text)
    return {*runs(said).values(), *pairs(said).values(), *('1 '+word for word in said if rare(word))}


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
    copies of the message as it stood) has that the fingerprint's kept text doesn't."""
    taken, kept = set(fingerprint.get('removed') or ()), set(fingerprint.get('kept') or ())
    for before in befores:
        taken |= {hashed(element, fingerprint['salt']) for element in elements(before)} - kept
    return taken


def holds(text, key, taken):
    """Whether the text holds enough of what an edit took out (`taken`, hashed with `key`): what it shares of that
    weighs ENOUGH (see above), each element counted once."""
    said, counted, covered, weight = words(text), set(), set(), 0

    def shared(element):
        if element in counted or hashed(element, key) not in taken:
            return False
        counted.add(element)
        return True
    for i, run in runs(said).items():
        if shared(run):
            weight += 2 if any(rare(word) for word in said[i:i+3]) else 1
            covered |= {i, i+1, i+2}
    for i, pair in pairs(said).items():
        if not {i, i+1} <= covered and shared(pair):
            weight += max(1, rare(said[i])+rare(said[i+1]))
            covered |= {i, i+1}
    for i, word in enumerate(said):
        if i not in covered and rare(word) and shared('1 '+word):
            weight += rare(word)
    return weight >= ENOUGH
