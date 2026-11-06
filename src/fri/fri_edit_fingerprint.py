"""What an edit took out of a message, as a fingerprint: hashed word runs and rare words, never its text (gene #150,
#246).

An edit withdraws the member's own saved exchange, but another member's saved reply whose request carried the message
may quote its old words. Retiring every reply that carried it would breadcrumb most of a busy channel's answers after a
typo fix, so one is retired only where it holds enough of what the edit took out. Fri makes the fingerprint from the
message as it stood and as it stands; Gene reads a saved reply the same way, and its own saved copy of the message too.
This module is the same in both repositories: Fri's integration tests check it word for word.

What's compared is each element of a text:
- every run of three words, not all COMMON, weighing 2, so a quote of it is enough alone;
- every pair of words, neither COMMON, weighing 1;
- every rare word: one with four digits or more, weighing 2 (a number, a code), or eight letters or more and not
  COMMON, weighing 1.

A text holds what an edit took out once the elements of it that the edit took out weigh ENOUGH: a quoted run, a
number, or a close paraphrase keeping two names or rare words, or a name and a pair. An element is hashed with a salt
(HMAC-SHA256, the first 16 hex digits), so the fingerprint can't be read back as words or matched against another's; it's
never written down, by Fri or Gene."""
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
probably definitely basically literally whatever honestly seriously""".split())
ENOUGH = 2
MOST = 3000  # Of each list sent: a 4000-character message has fewer elements than this.


def words(text):
    """The text's words: compatibility forms folded, case folded, a possessive's "'s" dropped and apostrophes inside a
    word too ("don't" is "dont"), then runs of letters and digits."""
    text = unicodedata.normalize('NFKC', text or '').casefold()
    return WORD.findall(APOSTROPHE.sub('', POSSESSIVE.sub('', text)))


def elements(text):
    """Each element of the text (see above), with its weight."""
    found, said = {}, words(text)
    for i in range(len(said)-2):
        if any(word not in COMMON for word in said[i:i+3]):
            found['3 '+' '.join(said[i:i+3])] = 2
    for i in range(len(said)-1):
        if said[i] not in COMMON and said[i+1] not in COMMON:
            found['2 '+said[i]+' '+said[i+1]] = 1
    for word in said:
        if sum(character.isdigit() for character in word) >= 4:
            found['1 '+word] = 2
        elif len(word) >= 8 and word.isalpha() and word not in COMMON:
            found['1 '+word] = 1
    return found


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
    """Whether the text holds enough of what an edit took out (`taken`, hashed with `key`): its elements among them
    weigh ENOUGH."""
    return sum(weight for element, weight in elements(text).items() if hashed(element, key) in taken) >= ENOUGH
