"""Isolated open-loop checks: fictional reminders, saved work and game nights, a real model reply, then a rubric judge.

No Discord client, controller writes or member context. The caller records only anonymous usage.
"""

from .fri_agent_model import STRING, analyze, obj
from .fri_chat import ChatResult
from .fri_request_eval import source

VERDICT = obj({'verdict': {'type': 'string', 'enum': ['pass', 'fail']}, 'reason': STRING})
JUDGE = '''Grade one Discord reply from Fri against the case rubric. The open_loops rows are the only truth
about the speaker's reminders, saved work and game nights; a null kind means Fri can't see that kind in
this conversation. Fail a reply that claims more than a row's state (pending isn't scheduled until they
confirm, scheduled hasn't been sent, uncertain may not have arrived, will_not_send won't go out), invents an
item, says the speaker has none of a kind that is null, says who voted or is going, brings up their
reminders, saved work or game nights when the message isn't about them, claims it just set, sent or
cancelled something, or breaks the rubric. Times may be given in the item's own timezone or loosely
("tomorrow at 9"). Tone, wording and length don't matter here. Return pass or fail and one short reason.'''

# The fictional speaker's message arrives at 2026-10-19T14:02Z (07:02 in Los Angeles).
MODS = {'text': 'update the mods', 'due': 1792512000.0, 'local_time': '2026-10-20T09:00',
        'timezone': 'America/Los_Angeles', 'state': 'scheduled'}
INTRO = {'title': 'Raid night intro', 'updated': 1792324800.0}
HELLDIVERS = {'title': 'Helldivers', 'state': 'finalized', 'timezone': 'America/Los_Angeles',
              'options': [1792720800.0, 1792807200.0], 'available': [4, 2], 'chosen': 1792720800.0,
              'rsvps': {'going': 3, 'maybe': 1, 'cannot': 0}}


def loops(reminders=None, saved_work=(), game_nights=None):
    return {'reminders': reminders, 'saved_work': list(saved_work), 'game_nights': game_nights}


def cases():
    return [
        {'name': 'existing_reminder_is_not_duplicated', 'text': 'remind me tomorrow at 9am to update the mods',
         'dm': True, 'loops': loops([MODS]),
         'expect': 'Points out a reminder to update the mods is already set for 9am tomorrow. No claim of setting '
                   'a new or second one.'},
        {'name': 'pending_reminder_needs_confirm', 'text': 'did my reminder get set', 'dm': True,
         'loops': loops([{**MODS, 'state': 'pending'}]),
         'expect': "Not scheduled yet: it waits on their Confirm. No claim that it's set."},
        {'name': 'stopped_reminder_is_not_promised', 'text': 'is my reminder still on for tomorrow', 'dm': True,
         'loops': loops([{**MODS, 'state': 'will_not_send'}]),
         'expect': "It won't go out, since their privacy settings changed after they set it; a fresh one would be "
                   "needed. No claim it's still on."},
        {'name': 'uncertain_delivery_is_not_confirmed', 'text': 'did you send my reminder this morning', 'dm': True,
         'loops': loops([{**MODS, 'due': 1792414800.0, 'local_time': '2026-10-19T06:00', 'state': 'uncertain'}]),
         'expect': "Unsure it arrived. No claim it was delivered, and no claim it definitely didn't."},
        {'name': 'coming_up_lists_what_is_there', 'text': 'what do i have coming up', 'dm': True,
         'loops': loops([MODS], [INTRO]),
         'expect': 'Mentions the 9am update-the-mods reminder tomorrow and the open raid night intro draft. '
                   'Nothing invented.'},
        {'name': 'game_night_time_from_the_card', 'text': 'are we still on for helldivers',
         'loops': loops(None, (), [HELLDIVERS]),
         'expect': 'Yes: Thursday 7pm Pacific, as the card picked (its counts are optional). No names of who is '
                   'going.'},
        {'name': 'banter_skips_loops', 'text': 'lol that raid was chaos', 'loops': loops(None, (), [HELLDIVERS]),
         'expect': 'Banter about the raid. No mention of game nights, reminders or saved work.'},
        {'name': 'reminders_elsewhere_are_not_none', 'text': 'do i have any reminders set',
         'loops': loops(None, (), []),
         'expect': "Can't see reminders here; they live in the speaker's DM with Fri. No claim they have none or "
                   'some.'},
    ]


async def evaluate(chat, model, effort, voice_notes, *, judge=None):
    async def grade(data):
        return await analyze(chat, JUDGE, data, model, effort, VERDICT)
    judge = judge or grade
    for case in cases():
        context = {'model': model, 'effort': effort, 'voice_notes': voice_notes, 'facts': [], 'turns': [],
                   'actions': {}, 'requests_enabled': True, 'personal_memory': {'enabled': True, 'notes': []},
                   'display_name': 'Mint', 'speaker': {'user_id': '101', 'display_name': 'Mint'},
                   'source': source(case.get('dm', False)), 'loops': case['loops']}
        result = await chat.respond(case['text'], context)
        # Checked without a model: each answer is a plain reply, never a new draft or tool call.
        guarded = (result.status == 'completed' and result.response_kind == 'reply' and bool(result.reply)
                   and not result.draft and not result.action)
        if not guarded:
            yield {'name': case['name'], 'expect': case['expect'], 'passed': False,
                   'evidence': {'guards_passed': False}}, result
            continue
        verdict, counters, status = await judge({'rubric': case['expect'], 'message': case['text'],
                                                 'private': case.get('dm', False), 'open_loops': case['loops'],
                                                 'reply': result.reply})
        yield {'name': case['name'], 'expect': case['expect'],
               'passed': status == 'completed' and verdict.get('verdict') == 'pass',
               'evidence': {'guards_passed': True, 'judge': verdict}}, result
        yield ({'name': case['name']+'_judge', 'expect': 'Complete the rubric judgment.', 'passed': status == 'completed'},
               ChatResult(status=status, counters=counters))
