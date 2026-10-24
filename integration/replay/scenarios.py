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
    placeholder,
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


def placeholder_only(replay, label):
    """No retained turn for this event keeps a derived reply; only neutral placeholders remain."""
    return all(placeholder(turn['reply']) for turn in replay.retained(label))


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
           lambda r: route(r, 'ask')['tools'] == ['clarify'] and route(r, 'ask')['clarification']
           and r.inspections['pending'], scripted_only=True),
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
     Check('controller keeps only a neutral mention placeholder', lambda r: route(r, 'ping')['stored'] == ['breadcrumb']),
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
    'derived_content_removed_on_erasure',
    'Another member\'s recent message shapes a saved reply; their erasure removes that derived copy.',
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
     Check('forget me persisted: memory off, own chats erased',
           lambda r: r.stored(ASH)['personal_memory']['enabled'] is False and not r.stored(ASH)['turns']),
     Check('the derived answer is stored verbatim', lambda r: route(r, 'ask')['stored'] == ['reply-text'],
           scripted_only=True),
     Check('erased member\'s details do not survive in another member\'s saved chat',
           lambda r: placeholder_only(r, 'ask'))])

REPLY_TARGET = Scenario(
    'reply_target_continuity_and_erasure',
    'An answer to an explicit reply reference is kept verbatim for continuity until its source erases.',
    [Say(ASH, 'heads up im moving to denver next month', label='share'), Wait(30),
     Say(BO, 'fri where is that', reply_to='share', label='ask'),
     Inspect('kept', lambda r: [turn['reply'] for turn in r.retained('ask')]), Wait(30),
     Say(ASH, 'fri forget me', label='erase')],
    {'conversation': [reply('ash said denver, next month')]},
    [Check('reply-target answer is generated and delivered', lambda r: answered(r, 'ask')),
     Check('the real answer is kept for follow-ups', lambda r: route(r, 'ask')['stored'] == ['reply-text']
           and r.inspections['kept'] == [r.step('ask').deliveries[0].text]),
     Check('the source member\'s erasure removes the derived answer', lambda r: placeholder_only(r, 'ask')),
     Check('referenced details are not stored after erasure',
           lambda r: 'denver' not in json.dumps(r.stored(BO)).lower(), scripted_only=True)])

DELIVERY_FAILURE = Scenario(
    'failed_delivery_is_not_saved',
    'A Discord send failure after a model reply leaves no stored turn claiming it was said.',
    [FailNextDelivery(), Say(BO, 'fri yo', label='yo')],
    {'conversation': [reply('yo')]},
    [Check('the reply was generated', lambda r: route(r, 'yo')['inference'] == ['completed']),
     Check('failed delivery is observed', lambda r: route(r, 'yo')['outcome'] == 'delivery-failed'),
     Check('no stored turn claims an undelivered reply', lambda r: not r.retained('yo'))])

TOOL_CONTINUITY = Scenario(
    'tool_answer_is_remembered',
    'A public lookup answer is kept verbatim, so the next turn knows what Fri actually said.',
    [Say(BO, 'fri who won the last world series', label='ask'), Wait(40),
     Say(BO, 'fri and who did they beat', label='follow')],
    {'conversation': [act('web_search', query='who won the last world series'), reply('the dodgers won it'),
                      reply('the yankees, in five')],
     'web': [web('The Dodgers won the most recent World Series.', 'https://www.mlb.com/postseason')]},
    [Check('lookup answer is delivered', lambda r: answered(r, 'ask') and route(r, 'ask')['tools'] == ['web_search']),
     Check('the delivered answer, not a breadcrumb, is stored', lambda r: route(r, 'ask')['stored'] == ['reply-text'],
           scripted_only=True),
     Check('the follow-up sees the earlier turn', lambda r: first_call(r, 'follow')['prior_turns'] >= 1),
     Check('stored history holds what Fri said', lambda r: r.step('ask').deliveries[0].text in
           [turn['reply'] for turn in r.stored(BO)['turns']], scripted_only=True)])

GROUP_VIEW = Scenario(
    'group_context_includes_fri',
    'Fri\'s own delivered reply joins the shared channel view and leaves it when its source erases.',
    [Say(BO, 'fri whats a good starter modpack', label='ask'), Wait(20),
     Say(ASH, 'fri what did you tell bo', label='peer'),
     Inspect('peer_kept', lambda r: bool(r.retained('peer'))), Wait(20),
     Say(BO, 'fri forget me', label='erase'), Wait(20),
     Say(ASH, 'fri and what did bo ask', label='after')],
    {'conversation': [reply('all the mods 9 is a fine start'), reply('i said all the mods 9'),
                      reply('not sure, i dont have that anymore')]},
    [Check('the peer\'s turn sees Fri\'s reply to bo', lambda r: first_call(r, 'peer')['recent_authors'] == 3),
     Check('after bo erases, neither bo nor Fri\'s reply to bo remains in view',
           lambda r: first_call(r, 'after')['recent_authors'] == 1),
     Check('the peer\'s answer drew on bo transitively, so bo\'s erasure removed it too',
           lambda r: r.inspections['peer_kept'] and not r.retained('peer'))])



def erase_ash_mid_reply(replay):
    replay.store.personal_control(str(ASH), 'disable')
    return reply('ash said denver, next month')


MIDTURN_ERASURE = Scenario(
    'erasure_during_reply_blocks_delivery',
    'If a referenced member erases while Fri is composing, the pre-delivery check stops the reply.',
    [Say(ASH, 'heads up im moving to denver next month', label='share'), Wait(30),
     Say(BO, 'fri where is ash moving', label='ask')],
    {'conversation': [erase_ash_mid_reply]},
    [Check('the composed answer is never delivered',
           lambda r: all('denver' not in d.text for d in r.step('ask').deliveries)
           and route(r, 'ask')['outcome'] == 'reply', scripted_only=True),
     Check('nothing derived is saved', lambda r: not r.retained('ask'))],
    expected_rejections=['/chat/turn/check'])

KIT = 40
SHARE = Say(ASH, 'heads up im moving to denver next month', label='share')
EXPIRE = Wait(901)  # Past the 15-minute live channel view: only saved records remain.


def erase_ash_then(text):
    def respond(replay):
        replay.store.personal_control(str(ASH), 'disable')
        return reply(text)
    return respond


HISTORY = Scenario(
    'follow_up_inherits_saved_history',
    'A follow-up built only on an earlier derived answer inherits its sources from saved history.',
    [SHARE, Wait(30), Say(BO, 'fri where is ash moving', label='ask'), EXPIRE,
     Say(BO, 'fri when again', label='follow'), Inspect('kept', lambda r: bool(r.retained('follow'))), Wait(30),
     Say(ASH, 'fri forget me', label='erase')],
    {'conversation': [reply('ash is moving to denver'), reply('denver, next month')]},
    [Check('the follow-up saw no live channel rows, only saved history',
           lambda r: first_call(r, 'follow')['recent_authors'] == 1 and first_call(r, 'follow')['prior_turns'] == 1),
     Check('both derived answers are removed by the source member\'s erasure',
           lambda r: r.inspections['kept'] and not r.retained('ask') and not r.retained('follow'))])

HISTORY_MIDTURN = Scenario(
    'erasure_during_follow_up_blocks_delivery',
    'An erasure while composing a history-derived follow-up stops that reply before delivery.',
    [SHARE, Wait(30), Say(BO, 'fri where is ash moving', label='ask'), EXPIRE,
     Say(BO, 'fri when again', label='follow')],
    {'conversation': [reply('ash is moving to denver'), erase_ash_then('denver, next month')]},
    [Check('the composed follow-up is never delivered',
           lambda r: all('denver' not in d.text for d in r.step('follow').deliveries), scripted_only=True),
     Check('nothing derived remains', lambda r: not r.retained('ask') and not r.retained('follow'))],
    expected_rejections=['/chat/turn/check'])


def seed_summary(replay):
    import time
    with replay.store.connection() as db:
        db.execute('INSERT INTO agent_summaries(channel_id,body,authors,at,revisions) VALUES (?,?,?,?,?)',
                   ('1', 'ash is moving to denver next month', json.dumps([str(ASH)]), time.time(),
                    json.dumps({str(ASH): 0})))
    return True


SUMMARY = Scenario(
    'summary_derived_answer_follows_summary_authors',
    'An answer built from the channel summary carries the summary authors and goes with their erasure.',
    [Inspect('seeded', seed_summary), Say(BO, 'fri anything new about ash', label='ask'),
     Inspect('kept', lambda r: bool(r.retained('ask'))), Wait(30), Say(ASH, 'fri forget me', label='erase')],
    {'conversation': [reply('ash is moving to denver next month')]},
    [Check('the answer saw the summary', lambda r: first_call(r, 'ask')['fields']['channel_summary_untrusted'] > 0),
     Check('the summary author\'s erasure removes the derived answer',
           lambda r: r.inspections['kept'] and not r.retained('ask'))])

FRI_REFERENCE = Scenario(
    'reply_to_fri_inherits_its_sources',
    'Replying to Fri\'s message resolves that message to the members it drew on.',
    [SHARE, Wait(30), Say(BO, 'fri where is ash moving', label='ask'), EXPIRE,
     Say(KIT, 'fri wait where?', reply_to='@ask', label='peer'),
     Inspect('kept', lambda r: bool(r.retained('peer'))), Wait(30), Say(ASH, 'fri forget me', label='erase')],
    {'conversation': [reply('ash is moving to denver'), reply('denver, next month')]},
    [Check('kit\'s answer is kept verbatim at first', lambda r: route(r, 'peer')['stored'] == ['reply-text'],
           scripted_only=True),
     Check('the original source\'s erasure removes kit\'s derived answer',
           lambda r: r.inspections['kept'] and not r.retained('peer'))])

PENDING = Scenario(
    'derived_clarification_follows_its_source',
    'A clarifying question shaped by another member\'s words is dropped when they erase.',
    [SHARE, Wait(30), Say(BO, 'fri how are the lakers doing', label='ask'),
     Inspect('armed', lambda r: bool(r.stored(BO)['pending'])), Wait(30), Say(ASH, 'fri forget me', label='erase')],
    {'conversation': [act('clarify', goal='web', topic='how are the lakers doing',
                          question='before or after ash moves to denver?')]},
    [Check('the clarification was armed', lambda r: r.inspections['armed']),
     Check('the derived question is gone after the erasure', lambda r: r.stored(BO)['pending'] is None)])

def answer_without_seeing_denver(replay):
    shown = json.dumps(replay.transport.last_payload['input'][-1]).lower()
    assert 'denver' not in shown, 'erased content reached the model through a Fri reference'
    return reply('which one? i dont have that anymore')


REFERENCE_AFTER_ERASURE = Scenario(
    'reply_to_fri_after_source_erasure_hides_it',
    'A reference to Fri\'s reply whose source erased is withheld from the model before inference.',
    [SHARE, Wait(30), Say(BO, 'fri where is ash moving', label='ask'), Wait(30),
     Say(ASH, 'fri forget me', label='erase'), Wait(30),
     Say(KIT, 'fri wait where?', reply_to='@ask', label='peer')],
    {'conversation': [reply('ash is moving to denver'), answer_without_seeing_denver]},
    [Check('kit gets an answer without the erased content', lambda r: answered(r, 'peer')
           and all('denver' not in d.text for d in r.step('peer').deliveries)),
     Check('nothing derived is kept for kit', lambda r: placeholder_only(r, 'peer'))])

SCENARIOS = [CLOSING, OPEN_QUESTION, WEB_CLARIFICATION, OUTSIDE_MENTION, KEYWORDS, ERASURE, REPLY_TARGET,
             DELIVERY_FAILURE, TOOL_CONTINUITY, GROUP_VIEW, MIDTURN_ERASURE, HISTORY, HISTORY_MIDTURN, SUMMARY,
             FRI_REFERENCE, PENDING, REFERENCE_AFTER_ERASURE]
