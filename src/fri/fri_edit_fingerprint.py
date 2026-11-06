"""What an edit took out of a message, as a fingerprint: hashed word runs and numbers, never its text (gene #150, #246).

An edit withdraws the member's own saved exchange, but another member's saved reply whose request carried the message
may quote its old words. Retiring every reply that carried it would breadcrumb most of a busy channel's answers after a
typo fix, so one is retired only where it quotes what the edit took out. Fri makes the fingerprint from the message as
it stood and as it stands; Gene reads a saved reply the same way, and its own saved copy of the message too. This module
is the same in both repositories: Fri's integration tests check it word for word.

Precision first (Sol round 2 of #261): a reply retired by mistake is a saved answer lost, and one missed is only as main
leaves it, so a reply holds what an edit took out only where it has, verbatim once normalized, one of:
- a run of four words with a rare word in it (see rare): six letters or more, not COMMON, and no calendar word;
- a distinctive number (see numbers): seven digits or more in one group, or grouped as a phone number is, that is no
  date, time or timestamp, written in one group, as a date with a time, or over lines.
Nothing else counts: no pairs, no lone names or rare words, no sums of them. A house number, a year, a time or a date
counts only inside a run of four words with a rare word in it. What the message says now (kept) is never counted as
taken out, whoever says it was, and the controller's own copy of the message is read only where the kept list is whole
(complete; see removed).

An element is hashed with a salt (HMAC-SHA256, the first 16 hex digits), which keeps words out of the request, and the
fingerprint is never written down, by Fri or Gene. It's not secret from whoever receives it: the salt travels with it, so
an element from a set small enough to try (a short number, a run of dictionary words or names) can be found by hashing
each candidate. Fri salts every edit while it runs with one salt (so two queued edits of a message merge), so within a
process the same element hashes alike in every request, and two fingerprints can be matched against each other. Only the
controller receives it, which holds the saved exchanges themselves."""
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
# Months, weekdays, ordinal days and times of day: a run of them, with a date or a time, says when, not what (Sol round 2
# of #261, R2-B1: "is on may 1, 2024" in two unrelated messages).
CALENDAR = frozenset("""
january february march april may june july august september october november december jan feb mar apr jun jul aug sep
sept oct nov dec monday tuesday wednesday thursday friday saturday sunday mon tue tues wed thu thur thurs fri sat sun
first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth fifteenth
sixteenth seventeenth eighteenth nineteenth twentieth thirtieth twenty thirty morning mornings evening evenings
afternoon afternoons midnight midday noon tonight weekend weekends weekday weekdays oclock hour hours minute minutes
second seconds daily weekly monthly yearly annual today tomorrow yesterday""".split())
RUN = 4
MOST = 3000  # Of each list sent: a 4000-character message has fewer elements than this, and `complete` says if not.
# A number as written on one line: digits, and the separators a phone number or a date or time is written with.
WRITTEN = re.compile(r'[\d(+][\d()\-. +:/]*\d')
DIGITS = re.compile(r'\d+')


def words(text):
    """The text's words: compatibility forms folded, case folded, a possessive's "'s" dropped and apostrophes inside a
    word too ("don't" is "dont"), then runs of letters and digits."""
    return WORD.findall(APOSTROPHE.sub('', POSSESSIVE.sub('', folded(text))))


def folded(text):
    return unicodedata.normalize('NFKC', text or '').casefold()


def rare(word):
    """A word that says something: six letters or more, not COMMON, and no calendar word."""
    return sum(character.isalpha() for character in word) >= 6 and word not in COMMON and word not in CALENDAR


def runs(said):
    """The text's runs of four words with a rare word in them."""
    return {f'{RUN} '+' '.join(said[i:i+RUN]) for i in range(len(said)-RUN+1) if any(rare(w) for w in said[i:i+RUN])}


def numbers(text):
    """The text's distinctive numbers, as their digits ("555-0199" and "5550199" alike): written on one line, seven
    digits or more in one group, or grouped as a phone number is (two to six groups, each after the first of two digits
    or more, the last of three or more), and no date, time or timestamp (see calendar)."""
    found = set()
    for written in WRITTEN.finditer(folded(text)):
        groups = DIGITS.findall(written.group())
        whole = not calendar(written.group(), groups)
        if whole and 2 <= len(groups) <= 6 and len(''.join(groups)) >= 7 and len(groups[-1]) >= 3 and all(
                2 <= len(group) <= 4 for group in groups[1:]) and len(groups[0]) <= 4:
            found.add(''.join(groups))
        found |= {group for group in groups if len(group) >= 7 and not calendar(group, [group])}
    return found


def calendar(written, groups):
    """Whether a number as written is a date, a time or a timestamp, or holds one: a colon or a slash in it (12:30,
    5/1/2024), three groups in a row that are a date (2024-05-01, 01.05.2024, 05-01-2024), groups that are all years, or
    one group that is a date, a date and a time (20240501, 202405011230, 20240501123015) or a Unix time in seconds or
    milliseconds (1714521600)."""
    if ':' in written or '/' in written:
        return True
    if any(day(*groups[i:i+3]) for i in range(len(groups)-2)) or all(year(group) for group in groups):
        return True
    if len(groups) == 1:
        [digits] = groups
        if len(digits) in (8, 12, 14) and day(digits[:4], digits[4:6], digits[6:8]):
            return True
        return (len(digits) == 10 and 10**9 <= int(digits) <= 21*10**8) or (
            len(digits) == 13 and 10**12 <= int(digits) <= 21*10**11)
    return False


def year(digits):
    return len(digits) == 4 and 1900 <= int(digits) <= 2100


def day(first, middle, last):
    """Three groups that are a date, year first ("2024-05-01") or last ("1.5.2024", "05-01-24")."""
    def month(digits):
        return len(digits) <= 2 and 1 <= int(digits) <= 12

    def date(digits):
        return len(digits) <= 2 and 1 <= int(digits) <= 31
    return ((year(first) and month(middle) and date(last)) or
            ((year(last) or len(last) == 2) and ((date(first) and month(middle)) or (month(first) and date(middle)))))


def elements(text):
    """Each element of the text (see above): its runs of four words with a rare word, and its distinctive numbers."""
    return runs(words(text)) | {'n '+digits for digits in numbers(text)}


def hashed(element, salt):
    return hmac.new(bytes.fromhex(salt), element.encode(), hashlib.sha256).hexdigest()[:16]


def salt():
    return secrets.token_hex(16)


def fingerprint(before, after, key):
    """An edit from `before` to `after` (None for either: not known), salted with `key`: the hashes of what `before`
    had that `after` doesn't (removed; none where `before` isn't known), and of everything `after` has (kept; none
    where it isn't known), so the controller can work out what was removed from its own copy of `before` too; and
    whether the kept list is all of it (complete: not where `after` isn't known, or had more than MOST)."""
    old = elements(before) if before is not None else set()
    new = elements(after) if after is not None else set()
    return {'salt': key, 'removed': sorted({hashed(element, key) for element in old if element not in new})[:MOST],
            'kept': sorted({hashed(element, key) for element in new})[:MOST],
            'complete': after is not None and len(new) <= MOST}


def removed(fingerprint, befores=()):
    """What the edit took out, as hashes: what the fingerprint says, and, where its kept list is whole (complete), what
    each of `befores` (the controller's own copies of the message as it stood) has; less all the fingerprint's kept
    text, whichever says it was taken out. A kept list cut short, or one a fingerprint doesn't say is whole, would read
    what the message still says as taken out of the controller's copy (Sol round 2 of #261, R2-B3)."""
    taken, kept = set(fingerprint.get('removed') or ()), set(fingerprint.get('kept') or ())
    if fingerprint.get('complete') is True:
        for before in befores:
            taken |= {hashed(element, fingerprint['salt']) for element in elements(before)}
    return taken - kept


def holds(text, key, taken):
    """Whether the text holds what an edit took out (`taken`, hashed with `key`): any of its elements (see above)."""
    return any(hashed(element, key) in taken for element in elements(text))
