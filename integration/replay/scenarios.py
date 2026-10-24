"""Synthetic replay scenarios. Members, messages and replies are invented; none are member history.

Checks marked scripted_only depend on scripted model output; the rest also apply to a live model.
A check with known_gap documents current behavior that should change; tests expect it to fail.
"""

import json

from .harness import (
    FRI,
    OUTSIDE,
    OWNER,
    Check,
    Deny,
    FailNextDelivery,
    Inspect,
    Say,
    Scenario,
    Wait,
    act,
    quiet,
    react,
    reply,
    web,
)

BO, ASH = 20, 30
PING = f'<@{FRI}>'


def route(replay, label):
    return replay.step(label).route()


def first_call(replay, label):
    return replay.step(label).conversation()[0]


def answered(replay, label):
    return replay.step(label).answered()


CLOSING = Scenario(
    'closing_after_completed_answer',
    'A completed answer followed by an exact closing gets a local reaction, not another model call.',
    [Say(BO, 'fri how do i see whats queued', label='ask'), Wait(20), Say(BO, 'gotcha', label='close')],
    {'conversation': [reply('say "fri controls" and hit queue', allow_closing=True)]},
    [Check('question gets a model text reply',
           lambda r: route(r, 'ask')['kind'] == 'model' and answered(r, 'ask')),
     Check('completed answer is stored as the reply text',
           lambda r: route(r, 'ask')['stored'] == ['reply-text'], scripted_only=True),
     Check('exact closing skips inference with one reaction',
           lambda r: route(r, 'close')['kind'] == 'local-closing' and route(r, 'close')['model_calls'] == 0
           and route(r, 'close')['reaction'] == '👍', scripted_only=True),
     Check('closing acknowledgment gets no text reply',
           lambda r: route(r, 'close')['outcome'] in ('react', 'silent'))])

OPEN_QUESTION = Scenario(
    'closing_after_open_question',
    'If the last reply asked something, "gotcha" must reach the model even if allow_closing was set.',
    [Say(BO, 'fri should i run the modpack on the vps', label='ask'), Wait(20), Say(BO, 'gotcha', label='close')],
    {'conversation': [reply('want me to check how much ram it has first?', allow_closing=True), quiet()]},
    [Check('open question disables the closing shortcut',
           lambda r: route(r, 'close')['kind'] == 'model' and route(r, 'close')['conversation_calls'] == 1,
           scripted_only=True),
     Check('the follow-up turn may stay quiet', lambda r: first_call(r, 'close')['quiet_allowed'],
           scripted_only=True)])

WEB_CLARIFICATION = Scenario(
    'web_clarification_followup',
    'One clarifying question, then an unaddressed answer continues the same public lookup.',
    [Say(BO, 'fri how are the lakers doing', label='ask'),
     Inspect('pending', lambda r: r.stored(BO)['pending'] is not None), Wait(30),
     Say(BO, 'this season', label='answer')],
    {'conversation': [act('clarify', goal='web', topic='how are the lakers doing',
                          question='this season or last night?'),
                      act('web_search', query='this season'),
                      reply('30-12 so far, third in the west')],
     'web': [web('The Lakers are 30-12 this season, third in the West.', 'https://www.nba.com/standings')]},
    [Check('ask gets a reply', lambda r: answered(r, 'ask')),
     Check('clarification is held for the follow-up',
           lambda r: route(r, 'ask')['clarification'] and r.inspections['pending'], scripted_only=True),
     Check('unaddressed answer continues without new social admission',
           lambda r: route(r, 'answer')['admission'] == ['/chat/operations/admit'], scripted_only=True),
     Check('continuation offers only web_search',
           lambda r: first_call(r, 'answer')['offered_actions'] == ['web_search'], scripted_only=True),
     Check('one public lookup, then a text answer',
           lambda r: route(r, 'answer')['tools'] == ['web_search'] and answered(r, 'answer')
           and sum(m['kind'] == 'web' for m in r.step('answer').model) == 1, scripted_only=True),
     Check('pending question is consumed and research retained',
           lambda r: r.stored(BO)['pending'] is None and bool(r.stored(BO)['web_research']), scripted_only=True)])

OUTSIDE_MENTION = Scenario(
    'outside_mention_forced_text',
    'An @mention outside configured channels gets one text reply with bounded nearby context.',
    [Say(ASH, 'that patch notes meme is so real', channel=OUTSIDE, label='before'), Wait(60),
     Say(BO, PING + ' thoughts on that', channel=OUTSIDE, label='ping'), Deny(OUTSIDE, BO), Wait(60),
     Say(BO, PING + ' you there?', channel=OUTSIDE, label='denied')],
    {'conversation': [react('😂')]},  # A provider ignoring the text-only schema still yields text.
    [Check('chatter outside configured channels is never observed',
           lambda r: route(r, 'before')['kind'] == 'ignored'),
     Check('ping is admitted once as a mention',
           lambda r: route(r, 'ping')['admission'] == ['/chat/mentions/admit']),
     Check('mention gets a generated text answer, never a reaction', lambda r: answered(r, 'ping')),
     Check('provider sees a text-only schema and the one nearby message',
           lambda r: first_call(r, 'ping')['text_only']
           and first_call(r, 'ping')['fields']['invited_channel_context'] == 1),
     Check('outside channel never joins ambient scope',
           lambda r: OUTSIDE not in r.agent.recent and all(key[0] != OUTSIDE for key in r.agent.active)),
     Check('nearby text is not stored', lambda r: 'patch notes' not in json.dumps(r.stored(BO, OUTSIDE))),
     Check('member without channel access gets nothing', lambda r: route(r, 'denied')['kind'] == 'ignored')])

KEYWORDS = Scenario(
    'keyword_routing',
    'Characterize which addressed messages local intercepts take before the conversation model.',
    [Say(OWNER, 'fri show my token usage', label='usage'),
     Say(OWNER, 'fri lol the token usage today is crazy', label='casual'),
     Say(BO, 'fri what do you remember about me?', label='memory'),
     Say(OWNER, 'fri have you been learning anything', label='learning'),
     Say(OWNER, 'fri make your tone a bit drier', label='tone')],
    {'conversation': [reply('wallet in shambles'), reply('a bit, mostly game chatter')],
     'internal': [{'explanation': 'drier voice', 'changes': [
         {'key': 'voice_notes', 'json_value': json.dumps('drier, fewer jokes')}]}]},
    [Check('explicit usage request is answered locally',
           lambda r: route(r, 'usage')['kind'] == 'local-usage-report' and route(r, 'usage')['model_calls'] == 0),
     Check('casual mention of usage reaches the model', lambda r: route(r, 'casual')['kind'] == 'model'),
     Check('memory question is answered locally',
           lambda r: route(r, 'memory')['kind'] == 'local-memory' and route(r, 'memory')['model_calls'] == 0),
     Check('learning chat is conversation, not a settings command',
           lambda r: route(r, 'learning')['kind'] == 'model'),
     Check('tone change becomes an owner settings preview',
           lambda r: route(r, 'tone')['kind'] == 'owner-control'
           and any(d.view for d in r.step('tone').deliveries))])

ERASURE = Scenario(
    'derived_content_survives_erasure',
    'Another member\'s recent message shapes a saved reply; that copy should not outlive their erasure.',
    [Say(ASH, 'heads up im moving to denver next month', label='share'), Wait(30),
     Say(BO, 'fri where did ash say they were moving', label='ask'), Wait(30),
     Say(ASH, 'fri forget me', label='erase')],
    {'conversation': [reply('ash said denver, next month')]},
    [Check('bo gets a generated answer', lambda r: answered(r, 'ask')),
     Check('unaddressed chatter is observed, not answered',
           lambda r: route(r, 'share')['kind'] == 'observed' and route(r, 'share')['outcome'] == 'silent'),
     Check('the answer saw the other member\'s recent message',
           lambda r: first_call(r, 'ask')['recent_authors'] == 2),
     Check('erasure is handled locally', lambda r: route(r, 'erase')['kind'] == 'local-privacy'),
     Check('the derived answer is stored verbatim', lambda r: route(r, 'ask')['stored'] == ['reply-text'],
           scripted_only=True),
     Check('erased member\'s details do not survive in another member\'s saved chat',
           lambda r: 'denver' not in json.dumps(r.stored(BO)).lower(),
           known_gap='Replies built from recent channel context are saved verbatim and survive the source '
                     'author\'s erasure; only reply-target/invited context is fenced (fri.py:390).')])

REPLY_TARGET = Scenario(
    'reply_target_turn_is_fenced',
    'Contrast case: the same answer to an explicit reply reference is stored as a breadcrumb.',
    [Say(ASH, 'heads up im moving to denver next month', label='share'), Wait(30),
     Say(BO, 'fri where is that', reply_to='share', label='ask')],
    {'conversation': [reply('ash said denver, next month')]},
    [Check('reply-target answer is generated and delivered', lambda r: answered(r, 'ask')),
     Check('reply-target turn is stored as a breadcrumb', lambda r: route(r, 'ask')['stored'] == ['breadcrumb']),
     Check('referenced details are not stored', lambda r: 'denver' not in json.dumps(r.stored(BO)).lower())])

DELIVERY_FAILURE = Scenario(
    'delivery_failure_after_save',
    'A Discord send failure after a model reply should not leave a stored turn claiming it was said.',
    [FailNextDelivery(), Say(BO, 'fri yo', label='yo')],
    {'conversation': [reply('yo')]},
    [Check('the reply was generated', lambda r: route(r, 'yo')['inference'] == ['completed']),
     Check('failed delivery is observed', lambda r: route(r, 'yo')['outcome'] == 'delivery-failed'),
     Check('no stored turn claims an undelivered reply',
           lambda r: 'yo' not in [turn['reply'] for turn in r.stored(BO)['turns']],
           known_gap='The turn is saved before Discord delivery (fri.py:378, fri_agent.py:432).')])

SCENARIOS = [CLOSING, OPEN_QUESTION, WEB_CLARIFICATION, OUTSIDE_MENTION, KEYWORDS, ERASURE, REPLY_TARGET,
             DELIVERY_FAILURE]
