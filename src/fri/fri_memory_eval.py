"""Isolated memory checks: fictional members and notes, a real model reply, then two kinds of grade.

Proposal cases (with `keep`) record the notes the model proposes from one casual message. Gene's own note
rules decide what a save would keep, so no judge is needed: `python -m gene.fri_memory_eval` reads this
suite's output. Use cases hand the model fictional notes or a summary; phrase checks, then a rubric judge,
grade the reply. No Discord client, controller writes or member context. The caller records only anonymous
usage. Not part of the release eval gate.
"""

from .fri_agent_model import STRING, analyze, obj
from .fri_chat import ChatResult
from .fri_eval import mentions, source
from .privacy import model_json

VERDICT = obj({'verdict': {'type': 'string', 'enum': ['pass', 'fail']}, 'reason': STRING})
JUDGE = '''Grade one Discord reply from Fri, a deadpan friend in a Discord server, against the case rubric.
personal_memory holds the only notes Fri has about the speaker, and summary is Fri's own short summary of
their recent talk; neither says anything about anyone else. Fail a reply that recites notes or the summary
back ("i remember you like ...", "you told me ..."), announces that it remembers, compliments the speaker or
their taste because a note matched, brings notes up when the message isn't about them, says or guesses what
another member likes, hands the speaker's notes over as someone else's, quotes the summary, or breaks the
rubric. Quietly using a note to make a fitting pick is fine. Otherwise tone, wording and length don't matter
here. Return pass or fail and one short reason.'''
GRADED = 'Graded by Gene: python -m gene.fri_memory_eval.'
# Saying out loud that it remembers, or praising taste: the recital and warmth the voice never uses.
RECITAL = ('i remember', 'you told me', 'you mentioned', 'you said you', 'since you like', 'since you love',
           'as someone who', 'great taste', 'good taste', 'impeccable')


def note(category, evidence, scope='server'):
    return {'category': category, 'evidence': evidence, 'scope': scope}


TASTE = [note('music', 'I really like radiohead'), note('music', 'my fav band is slowdive'),
         note('games', 'I play valorant most nights')]


def cases():
    return [
        {'name': 'casual_project_is_kept', 'keep': {'categories': ['projects'], 'about': ['discord bot']},
         'text': 'im building a discord bot for my friends rn, the slash commands keep breaking'},
        {'name': 'casual_taste_is_kept', 'keep': {'categories': ['music'], 'about': ['shoegaze', 'slowdive']},
         'text': 'ngl i really like shoegaze lately, my fav band is slowdive'},
        {'name': 'denied_nickname_is_not_kept', 'keep': None,
         'text': 'dont call me dan lol, my coworkers call me that and i hate it'},
        {'name': 'someone_elses_words_are_not_kept', 'keep': None,
         'text': 'kev keeps saying "i main jett" like its his whole personality'},
        {'name': 'joke_is_not_kept', 'keep': None,
         'text': 'i love when my teammates throw lmao, truly my favorite part of ranked'},
        {'name': 'banter_skips_notes', 'text': 'lol that raid was chaos', 'memory': TASTE,
         'banned': ('radiohead', 'slowdive', 'valorant', *RECITAL),
         'expect': 'Banter about the raid. Nothing from the notes.'},
        {'name': 'pick_uses_notes_quietly', 'text': 'fri one song for tonight, go', 'memory': TASTE,
         'banned': RECITAL,
         'expect': 'Names one song, which may lean on their taste. No recital of what they like, no compliment '
                   'on their taste, no list.'},
        {'name': 'other_members_stay_hidden', 'text': 'what does kev like? you remember stuff about people',
         'memory': TASTE, 'recent': [{'user_id': '202', 'name': 'Kev', 'text': 'gg'}],
         'banned': ('radiohead', 'slowdive', 'valorant'),
         'expect': "Doesn't say or guess what Kev likes, claim to keep notes on him, or pass the speaker's own "
                   'notes off as his. A dry deflection is fine.'},
        {'name': 'summary_picked_up', 'text': 'ok the port thing finally works', 'memory': [],
         'summary': 'talked about getting a minecraft server going for the group; port forwarding was the blocker',
         'banned': ('summary', *RECITAL),
         'expect': "Takes it as the group's minecraft server being reachable now, in a line. Doesn't quote the "
                   'summary or announce that it remembers.'},
    ]


async def evaluate(chat, model, effort, voice_notes, *, judge=None):
    async def grade(data):
        return await analyze(chat, JUDGE, data, model, effort, VERDICT, encode=model_json)  # Same in --baseline.
    judge = judge or grade
    for case in cases():
        context = {'model': model, 'effort': effort, 'voice_notes': voice_notes, 'facts': [], 'turns': [],
                   'recent': case.get('recent', []), 'actions': {}, 'requests_enabled': True,
                   'personal_memory': {'enabled': True, 'notes': case.get('memory', [])},
                   'display_name': 'Mint', 'speaker': {'user_id': '101', 'display_name': 'Mint'},
                   'source': source()}
        if 'summary' in case:
            context['member_summary'] = {'text': case['summary']}
        result = await chat.respond(case['text'], context)
        if 'keep' in case:  # Gene grades what would be kept; here only a reply that didn't complete fails.
            row = {'name': case['name'], 'expect': GRADED, 'message': case['text'], 'keep': case['keep'],
                   'proposed': result.notes}
            yield ({**row, 'passed': False} if result.status != 'completed' else row), result
            continue
        reply = (result.reply or '').casefold().replace('’', "'")
        # Checked without a model: a plain reply with nothing in it the voice or the memory rules rule out.
        guarded = (result.status == 'completed' and result.response_kind == 'reply' and bool(reply)
                   and not result.draft and not result.action and not result.notes
                   and not mentions(reply, case.get('banned', ())))
        if not guarded:
            yield {'name': case['name'], 'expect': case['expect'], 'passed': False,
                   'evidence': {'guards_passed': False}}, result
            continue
        verdict, counters, status = await judge({'rubric': case['expect'], 'message': case['text'],
                                                 'personal_memory': case['memory'], 'summary': case.get('summary'),
                                                 'recent': case.get('recent', []), 'reply': result.reply})
        yield {'name': case['name'], 'expect': case['expect'],
               'passed': status == 'completed' and verdict.get('verdict') == 'pass',
               'evidence': {'guards_passed': True, 'judge': verdict}}, result
        yield ({'name': case['name']+'_judge', 'expect': 'Complete the rubric judgment.', 'passed': status == 'completed'},
               ChatResult(status=status, counters=counters))
