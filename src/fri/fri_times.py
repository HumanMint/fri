"""Discord timestamps for a time the chat mentions, worked out by Fri, never by the model.

The reply model writes [[time 2026-10-27T20:00 America/New_York]] (or reltime) for a time someone
said, in the zone they said it in. Fri turns each into <t:EPOCH:F> or <t:EPOCH:R>, which Discord
shows in every reader's own timezone. One that doesn't parse never goes out raw: the reply asks for
the time and zone instead.
"""

import datetime as dt
import re
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

ASK = "couldn't pin that time down. what time and timezone was it"
SPAN = dt.timedelta(days=400)  # Further from when it was said than this is a misread date, not a plan.
STYLES = {'time': 'F', 'reltime': 'R'}
# Anything that starts like a placeholder. One left after rendering was malformed.
OPENING = re.compile(r'\[\[\s*(?:rel)?time', re.I)
PLACEHOLDER = re.compile(r'\[\[(?P<kind>time|reltime) (?P<date>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})'
                         r'T(?P<hour>\d{2}):(?P<minute>\d{2}) (?P<zone>[^\s\[\]]{1,64})\]\]', re.I)
OFFSET = re.compile(r'(?:UTC|GMT)(?P<sign>[+-])(?P<hours>\d{1,2})(?::?(?P<minutes>[0-5]\d))?', re.I)
# Abbreviations people type, for when the model passes one through. Ambiguous ones (CST, IST, BST, MST,
# AEST) are left out: they need the model's reading of the chat, or a question.
ALIASES = {
    **dict.fromkeys(('et', 'est', 'edt'), 'America/New_York'),
    **dict.fromkeys(('ct', 'cdt'), 'America/Chicago'),
    **dict.fromkeys(('mt', 'mdt'), 'America/Denver'),
    **dict.fromkeys(('pt', 'pst', 'pdt'), 'America/Los_Angeles'),
    **dict.fromkeys(('akst', 'akdt'), 'America/Anchorage'),
    'hst': 'Pacific/Honolulu', 'kst': 'Asia/Seoul', 'jst': 'Asia/Tokyo',
    **dict.fromkeys(('cet', 'cest'), 'Europe/Berlin'),
    **dict.fromkeys(('utc', 'gmt', 'z', 'etc/utc', 'etc/gmt'), 'UTC'),
}


@lru_cache(maxsize=1)
def regions():
    """Region zones by lowercase name. Etc/GMT+5 style names (their sign is backwards), leap-second
    right/ copies and the posix/ duplicates are left out."""
    return {name.casefold(): name for name in available_timezones()
            if '/' in name and not name.startswith(('Etc/', 'posix/', 'right/', 'SystemV/'))}


def zone(name):
    """A zone the poster's words name, or None."""
    offset = OFFSET.fullmatch(name)
    if offset:
        hours, minutes = int(offset['hours']), int(offset['minutes'] or 0)
        if hours > 14 or (hours == 14 and minutes):
            return None
        delta = dt.timedelta(hours=hours, minutes=minutes)
        return dt.timezone(-delta if offset['sign'] == '-' else delta)
    key = ALIASES.get(name.casefold()) or regions().get(name.casefold())
    try:
        return ZoneInfo(key) if key else None
    except (ZoneInfoNotFoundError, ValueError):
        return None


def instant(match, now):
    """The epoch a placeholder names, or None for a date, zone or wall time that doesn't hold up.

    A wall time that a daylight-saving change skips or repeats is refused rather than guessed, like the
    controller's reminders."""
    where = zone(match['zone'])
    if where is None:
        return None
    try:
        wall = dt.datetime(*(int(match[key]) for key in ('date', 'month', 'day', 'hour', 'minute')))
    except ValueError:
        return None
    # Roughly in range first, so the zone arithmetic below never meets year 1 or 9999.
    if abs(wall - now.astimezone(dt.timezone.utc).replace(tzinfo=None)) > SPAN + dt.timedelta(days=1):
        return None
    stamps = {wall.replace(tzinfo=where, fold=fold).timestamp() for fold in (0, 1)
              if dt.datetime.fromtimestamp(wall.replace(tzinfo=where, fold=fold).timestamp(), where)
              .replace(tzinfo=None) == wall}
    if len(stamps) != 1:
        return None
    stamp = stamps.pop()
    if abs(stamp - now.timestamp()) > SPAN.total_seconds():
        return None
    return int(stamp)


def said_at(context):
    """When the current message was sent, from its harness source: what relative times resolve against."""
    at = ((context or {}).get('source') or {}).get('at')
    try:
        when = dt.datetime.fromisoformat(at) if isinstance(at, str) else None
    except ValueError:
        when = None
    if when is None or when.tzinfo is None:
        return dt.datetime.now(dt.timezone.utc)
    return when


def render(text, now=None):
    """Every placeholder as a Discord timestamp. Text without one comes back unchanged."""
    if not OPENING.search(text):
        return text
    now = now or dt.datetime.now(dt.timezone.utc)
    broken = []

    def stamp(match):
        epoch = instant(match, now)
        if epoch is None:
            broken.append(match[0])
            return ''
        return f'<t:{epoch}:{STYLES[match["kind"].lower()]}>'

    rendered = PLACEHOLDER.sub(stamp, text)
    return ASK if broken or OPENING.search(rendered) else rendered


def render_tree(value, now=None):
    """Every string in a model's structured output (an action's question, a draft's body) rendered the same
    way, and whether all of them could be: a broken one there can't fall back to the ask in place."""
    if isinstance(value, str):
        if not OPENING.search(value):
            return value, True
        rendered = render(value, now)
        return rendered, rendered != ASK
    if isinstance(value, list):
        items = [render_tree(item, now) for item in value]
        return [item for item, _ in items], all(ok for _, ok in items)
    if isinstance(value, dict):
        items = {key: render_tree(item, now) for key, item in value.items()}
        return {key: item for key, (item, _) in items.items()}, all(ok for _, ok in items.values())
    return value, True
