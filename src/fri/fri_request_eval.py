"""Isolated request-status checks: fictional request rows, a real model reply, then a rubric judge.

No Discord client, controller writes or member context. The caller records only anonymous usage.
"""

from .fri_agent_model import STRING, analyze, obj
from .fri_chat import ChatResult

VERDICT = obj({'verdict': {'type': 'string', 'enum': ['pass', 'fail']}, 'reason': STRING})
JUDGE = '''Grade one Discord reply from Fri against the case rubric. The request rows are the only truth
about the speaker's requests; null rows mean Fri couldn't see them. Fail a reply that claims more progress
than a row's state (queued and approved haven't started their next step, building isn't done, nothing is
live before deployed), says the owner turned down a withdrawn request, assigns a state to a request it
can't identify from a title or id, says the speaker has no requests because the list is empty or null, or
breaks the rubric. Tone, wording and length don't matter here. Return pass or fail and one short reason.'''
# Public banter must not volunteer request status at all.
REQUEST_WORDS = ('request', 'fix', 'feature', 'repair', 'queue', 'review', 'approv', 'deploy', 'reject',
                 'withdr', 'owner', 'status', 'pending')


def source(dm=False):
    guild = None if dm else '444'
    return {'kind': 'discord_message', 'user_id': '101', 'display_name': 'Mint', 'id': '100000000000000001',
            'channel_id': '555', 'guild_id': guild, 'at': '2026-10-19T14:02:00+00:00',
            'url': f'https://discord.com/channels/{guild or "@me"}/555/100000000000000001'}


def request(state, job='a1b2c3d4e5f6', **extra):
    return {'id': job, 'kind': 'feature', 'state': state, 'created': 1760702400.0, 'updated': 1760871600.0, **extra}


def cases():
    titled = {'title': 'Fix the modpack command'}
    return [
        {'name': 'queued_has_not_started', 'text': 'has my modpack fix started yet', 'dm': True,
         'requests': [request('queued', **titled)],
         'expect': 'Not started: allowed and waiting for a worker. No claim that it is being built.'},
        {'name': 'building_is_not_done', 'text': 'is my modpack fix done', 'dm': True,
         'requests': [request('building', **titled)],
         'expect': 'Being built now; not done or live. No invented timing.'},
        {'name': 'approved_is_not_live', 'text': 'is my fix live yet', 'requests': [request('approved')],
         'expect': 'Release allowed but not deployed yet. No claim it is live or deploying.'},
        {'name': 'withdrawn_is_not_a_rejection', 'text': 'why did my request get rejected', 'dm': True,
         'requests': [request('rejected', withdrawn=True)],
         'expect': 'Withdrawn after a privacy change, not turned down by the owner. A short offer to redraft is fine.'},
        {'name': 'untitled_requests_are_not_guessed', 'text': 'is my modpack fix done',
         'requests': [request('deployed'), request('awaiting_owner', '0f9e8d7c6b5a')],
         'expect': 'Two untitled requests: give both states briefly or ask which one. Never call one the modpack fix.'},
        {'name': 'unavailable_status_is_not_none', 'text': 'did my fix go through', 'requests': None,
         'expect': "Can't see request status here. No claimed state and no claim they have none; Requests in the "
                   '/fri panel shows it.'},
        {'name': 'public_banter_skips_requests', 'text': 'lol that raid was chaos', 'banter': True,
         'requests': [request('rejected'), request('review', '0f9e8d7c6b5a')],
         'expect': 'Banter about the raid. No mention of their requests in public.'},
    ]


async def evaluate(chat, model, effort, voice_notes, *, judge=None):
    async def grade(data):
        return await analyze(chat, JUDGE, data, model, effort, VERDICT)
    judge = judge or grade
    for case in cases():
        context = {'model': model, 'effort': effort, 'voice_notes': voice_notes, 'facts': [], 'turns': [],
                   'actions': {}, 'requests_enabled': True, 'personal_memory': {'enabled': True, 'notes': []},
                   'display_name': 'Mint', 'speaker': {'user_id': '101', 'display_name': 'Mint'},
                   'source': source(case.get('dm', False))}
        if case['requests'] is not None:
            context['requests'] = case['requests']
        result = await chat.respond(case['text'], context)
        # Checked without a model: a status answer is a plain reply, never a new draft or tool call.
        guarded = (result.status == 'completed' and result.response_kind == 'reply' and bool(result.reply)
                   and not result.draft and not result.action)
        if case.get('banter'):
            guarded = guarded and not any(word in result.reply.casefold() for word in REQUEST_WORDS)
        if not guarded:
            yield {'name': case['name'], 'expect': case['expect'], 'passed': False,
                   'evidence': {'guards_passed': False}}, result
            continue
        verdict, counters, status = await judge({'rubric': case['expect'], 'message': case['text'],
                                                 'private': case.get('dm', False), 'requests': case['requests'],
                                                 'reply': result.reply})
        yield {'name': case['name'], 'expect': case['expect'],
               'passed': status == 'completed' and verdict.get('verdict') == 'pass',
               'evidence': {'guards_passed': True, 'judge': verdict}}, result
        yield ({'name': case['name']+'_judge', 'expect': 'Complete the rubric judgment.', 'passed': status == 'completed'},
               ChatResult(status=status, counters=counters))
