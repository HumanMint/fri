"""Isolated time-for-everyone checks: a fictional channel, a real model reply, then a rubric judge.

Each case fixes when the speaker's message was sent, so the rendered Discord timestamp has one right
epoch, checked without a model. No Discord client, controller writes or member context. The caller
records only anonymous usage.
"""

import datetime as dt
import re

from .fri_agent_model import STRING, analyze, obj
from .fri_chat import ChatResult
from .fri_times import ASK
from .privacy import model_json

VERDICT = obj({'verdict': {'type': 'string', 'enum': ['pass', 'fail']}, 'reason': STRING})
JUDGE = '''Grade one Discord reply from Fri, a deadpan, sassy bot in a friend group, against the case
rubric. <t:EPOCH:F> and <t:EPOCH:R> in the reply are Discord timestamps: each reader sees them in their
own timezone, and their numbers were checked separately, so don't judge the numbers. Fail a reply that
guesses a timezone nobody stated, gives the time in one named zone as if it were everyone's, invents a
time, answers about a different time than the one discussed, or breaks the rubric. The voice is
lowercase, short and dry: fail cheerfulness, exclamation marks or assistant filler. Return pass or fail
and one short reason.'''
TOKEN = re.compile(r'<t:(-?\d+)(?::([tTdDfFR]))?>')
ZONE_WORDS = re.compile(r'time ?zone|\bzone\b|\btz\b|\bwhere\b|\bwhose\b', re.I)


def at(value):
    return dt.datetime.fromisoformat(value).timestamp()


def snowflake(value):
    """A Discord id sent at that time, so a post's id and its at agree, as Discord's do."""
    return str((int(at(value)*1000)-1420070400000) << 22)


def source(now):
    mid = snowflake(now)
    return {'kind': 'discord_message', 'user_id': '101', 'display_name': 'Mint', 'id': mid,
            'channel_id': '555', 'guild_id': '444', 'at': now,
            'url': f'https://discord.com/channels/444/555/{mid}'}


def post(user, name, when, text):
    """A live-view row as Fri hears it: its at is the epoch it arrived."""
    return {'id': snowflake(when), 'user_id': user, 'name': name, 'text': text, 'at': at(when), 'gene_error': False}


def target(user, name, when, text):
    """The post they replied to, as Discord's fetch gives it: no at, only its id."""
    return {'source': {'kind': 'discord_message', 'id': snowflake(when), 'user_id': user, 'display_name': name,
                       'channel_id': '555', 'guild_id': '444'}, 'text': text, 'privacy_revision': 0}


NOW = '2026-11-12T18:00:00+00:00'  # A Thursday: 13:00 in New York, 10:00 in Los Angeles, 19:00 in Berlin.


def cases():
    return [
        {'name': 'zone_in_the_post_before', 'text': 'fri what time is that for everyone',
         'recent': [post('202', 'Bo', '2026-11-12T17:58:00+00:00', 'raid tonight 9pm EST dont be late')],
         'epoch': 1794535200,  # Thursday 21:00 in New York.
         'expect': 'Gives the raid time as a Discord timestamp for everyone. Short, no zone guessed.'},
        {'name': 'zone_a_few_posts_up', 'text': "fri what time's movie night for everyone else",
         'recent': [post('203', 'Ash', '2026-11-12T17:40:00+00:00', 'movie night saturday 7pm PT'),
                    post('202', 'Bo', '2026-11-12T17:45:00+00:00', 'bet'),
                    post('204', 'Cy', '2026-11-12T17:50:00+00:00', 'ill bring snacks'),
                    post('205', 'Dee', '2026-11-12T17:55:00+00:00', "who's picking the movie tho")],
         'epoch': 1794711600,  # Saturday 19:00 in Los Angeles.
         'expect': "Finds Ash's movie night time further up and gives it as a Discord timestamp. Short."},
        {'name': 'reply_to_the_post_with_the_time', 'text': "fri what's that in my time",
         'recent': [post('202', 'Bo', '2026-11-12T17:30:00+00:00', 'lunch at 1 was mid'),
                    post('204', 'Cy', '2026-11-12T17:50:00+00:00', 'anyone up for duos')],
         'reply_target': [target('203', 'Ash', '2026-11-12T17:00:00+00:00', 'tournament check-in is friday 18:00 CET')],
         'epoch': 1794589200,  # Friday 18:00 in Berlin.
         'expect': "Converts the check-in time from the post they replied to, not Bo's lunch. Short."},
        {'name': 'tomorrow_from_when_it_was_said', 'text': 'fri when is that for us', 'now': '2026-11-12T03:05:00+00:00',
         'recent': [post('206', 'Min', '2026-11-12T03:00:00+00:00', 'tomorrow 9pm KST we queue ranked')],
         'epoch': 1794571200,  # Friday 21:00 in Seoul: Min posted at noon on Thursday there.
         'expect': "Gives Min's ranked time as a Discord timestamp for everyone. Short."},
        {'name': 'no_zone_asks', 'text': 'fri what time is that for everyone',
         'recent': [post('202', 'Bo', '2026-11-12T17:50:00+00:00', 'game night tomorrow at 8, bring snacks'),
                    post('204', 'Cy', '2026-11-12T17:55:00+00:00', 'im in')],
         'asks': True,
         'expect': 'Nobody said a timezone: one short question about which zone Bo meant. No time given, '
                   'no zone assumed.'},
        {'name': 'not_about_time', 'text': 'fri is pineapple on pizza a crime',
         'recent': [post('202', 'Bo', '2026-11-12T17:58:00+00:00', 'raid tonight 9pm EST dont be late')],
         'expect': 'A short take on pineapple pizza. No mention of the raid time.'},
    ]


def checked(case, result):
    """What a model can't be trusted to judge: the epoch, and that nothing raw or broken went out."""
    reply = result.reply or ''
    stamps = TOKEN.findall(reply)
    if not (result.status == 'completed' and result.response_kind == 'reply' and reply and
            result.action is None and result.draft is None and '[[' not in reply and reply != ASK):
        return False
    if 'epoch' in case:  # The full time, and nothing pointing anywhere else.
        return f'<t:{case["epoch"]}:F>' in reply and all(int(epoch) == case['epoch'] for epoch, _ in stamps)
    if stamps or '<t:' in reply:
        return False
    return not case.get('asks') or bool('?' in reply or ZONE_WORDS.search(reply))


async def evaluate(chat, model, effort, voice_notes, *, judge=None):
    async def grade(data):
        return await analyze(chat, JUDGE, data, model, effort, VERDICT, encode=model_json)  # Same in --baseline.
    judge = judge or grade
    for case in cases():
        now = case.get('now', NOW)
        context = {'model': model, 'effort': effort, 'voice_notes': voice_notes, 'facts': [], 'turns': [],
                   'actions': {}, 'requests_enabled': True, 'personal_memory': {'enabled': False, 'notes': []},
                   'display_name': 'Mint', 'speaker': {'user_id': '101', 'display_name': 'Mint'},
                   'source': source(now), 'recent': case['recent'], 'reply_target': case.get('reply_target', [])}
        result = await chat.respond(case['text'], context)
        if not checked(case, result):
            yield {'name': case['name'], 'expect': case['expect'], 'passed': False,
                   'evidence': {'checks_passed': False}}, result
            continue
        verdict, counters, status = await judge({'rubric': case['expect'], 'message': case['text'], 'sent_at': now,
                                                 'recent': case['recent'], 'reply_target': case.get('reply_target', []),
                                                 'reply': result.reply})
        yield {'name': case['name'], 'expect': case['expect'],
               'passed': status == 'completed' and verdict.get('verdict') == 'pass',
               'evidence': {'checks_passed': True, 'judge': verdict}}, result
        yield ({'name': case['name']+'_judge', 'expect': 'Complete the rubric judgment.', 'passed': status == 'completed'},
               ChatResult(status=status, counters=counters))
