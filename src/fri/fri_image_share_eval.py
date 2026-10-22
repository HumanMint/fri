"""Opt-in real provider/vision check. No Discord client, member context, or posting."""

from types import SimpleNamespace

from .fri_agent_model import analyze
from .fri_chat import ChatResult
from .fri_image_request import image_request
from .fri_image_share import ImageShare, descriptor

REQUEST = 'share a random sfw image featuring hatsune miku from danbooru'
DETAILED_REQUEST = ("let's try another one\n\n"
    'to represent a theoretical japanese word for gaming, can you share a link to a random sfw '
    'image featuring hatsune miku off of danbooru but with relevant tags and context that would '
    'be cool to "represent" the japanese word of gaming and be framed as a theoretical '
    '"miku word of the day"')


async def evaluate(chat, model, effort, voice_notes, background, *, detailed=False):
    request = DETAILED_REQUEST if detailed else REQUEST
    context = {'model': model, 'effort': effort, 'facts': [], 'turns': [],
        'actions': {'image_search': descriptor(), 'web_search': {'description': 'Public web lookup.'}},
        'voice_notes': voice_notes, 'requests_enabled': False}
    result = await chat.respond(request, context)
    action = result.action or {}
    brief = image_request(action.get('query'), request)
    ok = action.get('kind') == 'image_search' and brief == request
    yield {'name': 'requested_image_tool', 'expect': 'Select image_search with the current public request.',
           'passed': ok}, result
    if not ok:
        return
    role_model = background.get('meme_model', model)
    role_effort = background.get('meme_effort', effort)
    calls = []
    async def inspect(instruction, data, schema, **kwargs):
        value, counters, status = await analyze(chat, instruction, data, role_model, role_effort, schema, **kwargs)
        calls.append(({'name': 'image_vision' if kwargs.get('images') else 'image_subject',
            'expect': 'Complete the isolated image selection check.', 'model': role_model, 'effort': role_effort,
            'passed': status == 'completed', 'evidence': value}, ChatResult(status=status, counters=counters)))
        return value, counters, status
    share = ImageShare(SimpleNamespace())
    share.inspect = inspect
    async def permitted():
        return True  # Isolated public fixture; this evaluator has no Discord send path.
    share.current = permitted
    try:
        receipt = await share.find(brief)
    finally:
        for item in calls:
            yield item
    prepared = share.prepared
    success = prepared is not None and receipt.get('image_status') == 'prepared'
    context['actions'] = {}
    context['action_result'] = {'kind': 'image_search', **receipt}
    final = await chat.respond(request, context)
    yield {'name': 'checked_image_reply', 'expect': 'Relevant SFW bytes prepared, brief truthful reply, no Discord post.',
        'passed': success and final.status == 'completed' and final.response_kind == 'reply' and bool(final.reply),
        'evidence': {'prepared': success, 'bytes': len(prepared.body) if prepared else 0,
                     'receipt': receipt}}, final
    if prepared:
        prepared.body = b''
