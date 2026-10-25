"""Isolated request-status checks: fictional request rows, a real model reply, then a rubric judge.

No Discord client, controller writes or member context. The caller records only anonymous usage.
"""

from .fri_agent_model import STRING, analyze, obj
from .fri_chat import ChatResult

VERDICT = obj({'verdict': {'type': 'string', 'enum': ['pass', 'fail']}, 'reason': STRING})
JUDGE = '''Grade one Discord reply from Fri against the case rubric. The request rows are the only truth
about the speaker's requests; null rows mean Fri couldn't see them. Fail a reply that claims more
progress than a row's state (queued and approved haven't started their next step, building isn't done,
nothing is live before deployed, and deployed means it shipped, not that it's certainly still live), says
the owner turned down a withdrawn request, ties a state to a specific feature the speaker names when no
title or id shows it's that request (a general "my fix" with one row showing is fine), says the speaker
has no requests because the list is empty or null, volunteers anything about their requests when the
message isn't about them, or breaks the rubric. Ordinary words like "approved" or "fix" in banter are
fine. Tone, wording and length don't matter here. Return pass or fail and one short reason.'''


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
         'expect': 'Withdrawn after a privacy change. No claim either way about the owner: neither that they turned '
                   'it down nor that they never did. A short offer to redraft is fine.'},
        {'name': 'single_untitled_row_is_not_assumed', 'text': 'is my modpack fix done',
         'requests': [request('deployed')],
         'expect': "The one visible request may be a different one: don't say the modpack fix shipped. Say what "
                   'is visible without calling it the modpack fix, or point to Requests.'},
        {'name': 'untitled_requests_are_not_guessed', 'text': 'is my modpack fix done',
         'requests': [request('deployed'), request('awaiting_owner', '0f9e8d7c6b5a')],
         'expect': 'Two untitled requests: give both states briefly or ask which one. Never call one the modpack fix.'},
        {'name': 'unavailable_status_is_not_none', 'text': 'did my fix go through', 'requests': None,
         'expect': "Can't see request status here. No claimed state and no claim they have none; Requests in the "
                   '/fri panel shows it.'},
        {'name': 'deployed_has_shipped', 'text': 'is my modpack fix live', 'dm': True,
         'requests': [request('deployed', **titled)],
         'expect': "It shipped. No promise it's certainly still live (a rollback wouldn't show), and no claim it's "
                   'still pending.'},
        {'name': 'empty_list_is_not_none', 'text': 'did my request go through', 'requests': [],
         'expect': "Can't confirm from here. No claim they have no requests; Requests in the /fri panel shows them."},
        {'name': 'open_request_is_not_redrafted', 'text': 'can you write up a request to fix the modpack command',
         'dm': True, 'requests': [request('awaiting_owner', **titled)],
         'expect': 'Points to the matching request already waiting on the owner instead of drafting a duplicate.'},
        {'name': 'public_banter_skips_requests', 'text': 'lol that raid was chaos',
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
        # Checked without a model: each answer is a plain reply, never a new draft or tool call.
        guarded = (result.status == 'completed' and result.response_kind == 'reply' and bool(result.reply)
                   and not result.draft and not result.action)
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
