"""Isolated multi-turn model checks, with public research but no Discord actions.

Inputs are fictional fixtures. The caller records only anonymous evaluation usage.
"""

import datetime as dt
import time

from .fri_chat import ChatResult
from .fri_pending import completion, research_query
from .fri_web import cite_reply, lookup


async def evaluate(chat, model, effort, voice_notes):
    def source(seconds=0):
        return {'kind': 'discord_message', 'id': str((int((time.time()+seconds)*1000)-1420070400000) << 22),
                'guild_id': '444', 'channel_id': '555', 'user_id': '101', 'display_name': 'Mint'}
    old = source(-60)
    current = source()
    actions = {
        'gene_command': {'commands': {'play': 'Queue a song through Gene with member voice checks.'}},
        'web_search': {'description': 'Research the exact public question from the current message or pending web topic.'},
        'clarify': {'goals': ['play', 'web'], 'description': 'Ask a short question, retaining this request for 5 minutes. '
                    'goal=play has topic=""; goal=web has topic=the original exact public question.'}}
    base = {'model': model, 'effort': effort, 'voice_notes': voice_notes, 'facts': [], 'turns': [],
            'actions': actions, 'requests_enabled': False, 'revision': 0,
            'speaker': {'user_id': '101', 'display_name': 'Mint'}, 'source': current,
            'personal_memory': {'enabled': True, 'notes': []}}

    def report(name, expect, passed, **extra):
        return {'name': name, 'expect': expect, 'passed': passed, **extra}

    first_text = 'can you join the VC and play music'
    first = await chat.respond(first_text, {**base, 'source': old})
    question = first.action if first.action and first.action.get('kind') == 'clarify' else {}
    yield report('music_initial', 'Ask which song through a held play request.', question.get('goal') == 'play'), first
    pending = {'goal': 'play', 'topic': '', 'question': question.get('question', 'what song?'),
               'request_text': first_text, 'source': old, 'revision': 0}
    context = {**base, 'pending': pending, 'turns': [{'input': first_text, 'reply': pending['question'], 'source': old}]}
    second = await chat.respond('blind melon no rain', context)
    action = second.action or {}
    allowed = action.get('kind') == 'gene_command' and completion(pending, 'blind melon no rain',
        action.get('action'), action.get('argument'), current, '444', 'public:555', '101', 0)
    yield report('music_answer', 'Same member song answer completes the play request with exact query.', allowed), second
    cancelled = await chat.respond('nah never mind', context)
    yield report('music_cancel', 'No music action after cancellation.', not cancelled.action), cancelled
    unrelated = await chat.respond('anyway what can you help with', context)
    yield report('music_topic_change', 'Do not treat unrelated chat as a song.', not unrelated.action), unrelated
    other = {**base, 'actions': {**actions, 'clarify': {**actions['clarify'], 'goals': ['web']}}, 'recent': [{'user_id': '202', 'text': first_text}], 'turns': []}
    confused = await chat.respond('blind melon no rain', other)
    yield report('other_member', 'No play authority from another member.',
                 not confused.action or confused.action.get('kind') not in ('gene_command', 'clarify')), confused

    topic = 'how are the bills doing nowadays'
    pending = {'goal': 'web', 'topic': topic, 'question': 'you mean the buffalo bills?',
               'request_text': topic, 'source': old, 'revision': 0}
    context = {**base, 'pending': pending, 'turns': [{'input': topic, 'reply': pending['question'], 'source': old}]}
    confirmed = await chat.respond('ya', context)
    selected = confirmed.action or {}
    query = research_query(selected.get('query'), 'ya', pending)
    yield report('research_confirmation', 'Confirmation researches the original question without asking twice.',
                 selected.get('kind') == 'web_search' and query == topic), confirmed
    qualifier_context = {**base, 'pending': {**pending, 'request_text': 'how fast is a jaguar',
        'topic': 'how fast is a jaguar', 'question': 'the animal or the car?'},
        'turns': [{'input': 'how fast is a jaguar', 'reply': 'the animal or the car?', 'source': old}]}
    qualified = await chat.respond('the car', qualifier_context)
    yield report('research_qualifier', 'Carry the new public qualifier into the original question.',
        (qualified.action or {}).get('kind') == 'web_search' and
        research_query((qualified.action or {}).get('query'), 'the car', qualifier_context['pending']) ==
        'how fast is a jaguar; clarification: the car'), qualified
    refine = {'web_refine': {'description': 'Investigate further when the first result did not answer the question. query="". Once only.'}}
    insufficient = {'kind': 'web_search', 'query': topic, 'answer': 'Only the league schedule was found. No Bills record or results established.',
                    'sources': ['https://www.nfl.com/schedules/'], 'checked_at': dt.datetime.now(dt.timezone.utc).isoformat()}
    retry = await chat.respond('ya', {**context, 'actions': refine, 'action_result': insufficient})
    yield report('research_bad_result', 'Refine an irrelevant schedule rather than stopping with a disclaimer.',
                 (retry.action or {}).get('kind') == 'web_refine'), retry

    # Actual public web access, isolated from member history, identifiers and credentials.
    evidence, counters, status = await lookup(chat, topic, model, effort)
    yield report('live_public_research', 'Completed cited public research, not a fabricated fixture.',
                 status == 'completed' and bool(evidence.get('sources')), evidence=evidence), ChatResult(status=status, counters=counters)
    if status != 'completed' or not evidence.get('sources'):
        return
    evidence['query'] = topic
    final = await chat.respond('ya', {**context, 'actions': refine, 'action_result': evidence})
    if final.action and final.action.get('kind') == 'web_refine':
        yield report('live_refinement_decision', 'Refinement stays within the same public question.', True), final
        more, counters, status = await lookup(chat, topic, model, effort, previous=evidence)
        yield report('live_refinement', 'At most one extra research pass.', status == 'completed', evidence=more), ChatResult(status=status, counters=counters)
        if more.get('sources'):
            evidence = {**more, 'query': topic, 'sources': list(dict.fromkeys(more['sources']+evidence['sources']))[:8]}
        final = await chat.respond('ya', {**context, 'actions': {}, 'action_result': evidence})
    final.reply = cite_reply(final.reply, evidence)
    yield report('live_research_reply', 'Short, natural, evidence-grounded answer; no default sources footer.',
                 final.status == 'completed' and not final.action and bool(final.reply) and 'http' not in final.reply), final
    sources = await chat.respond('where did you find that? sources?', {**base,
        'web_research': [evidence], 'actions': {'web_sources': {'description': 'Read saved research sources; query="" for latest.'}}})
    yield report('sources_followup', 'Retrieve retained sources without rerunning research.',
                 (sources.action or {}).get('kind') == 'web_sources'), sources
