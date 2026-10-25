"""A bounded turn: tools return evidence, never authority for more tools."""

import asyncio
import json

import discord

from . import feedback as commands
from .fri_chat import ChatResult
from .fri_errors import ControllerRejected
from .fri_images import require_current as require_current_images
from .fri_outcomes import Outcome, fallback
from .fri_outcomes import receipt as tool_receipt

MAX_STEPS = 4
TERMINAL = {'gene_command', 'gene_diagnose', 'run_learning', 'image_search'}


async def run(fri, actions, text, context, result):
    progress = {'receipts': [], 'research': None, 'active': None}
    try:
        async with asyncio.timeout(300):
            return await _run(fri, actions, text, context, result, progress)
    except TimeoutError:
        if progress['active']:
            progress['receipts'].append(tool_receipt(progress['active'], {
                'uncertain': 'The turn timed out while waiting for that step.'}))
        if progress['receipts']:
            return (ChatResult(reply=fallback(progress['receipts']), status='completed'),
                    progress['receipts'], progress['research'])
        raise commands.UserFeedbackCheckFailure('That turn ran out of time. Saved work remains available; check its status before retrying an action.') from None


async def recheck(actions, context):
    """Before the model hears the turn again: a ping's own check, which covers its image, else the image's.
    A tool checks before it acts (Actions.execute) and the reply before it goes out, so only model calls
    need this. An outside ping's model calls check it themselves (see Fri.chat_once)."""
    invitation = getattr(actions, 'invitation', None)
    if invitation and (context.get('fence') or {}).get('invitation') is invitation:
        return
    if invitation:
        await invitation.require_current()
    await require_current_images(context, getattr(actions, 'event', None), checked=bool(invitation))


async def _run(fri, actions, text, context, result, progress):
    receipts, seen, research = progress['receipts'], set(), None
    offered = dict(context['actions'])
    for _ in range(MAX_STEPS):
        action = result.action
        if not action:
            break
        kind = action['kind']
        fingerprint = json.dumps(action, sort_keys=True)
        if kind not in offered or fingerprint in seen:
            if receipts:
                return ChatResult(reply=fallback(receipts), status='completed'), receipts, research
            raise commands.UserFeedbackCheckFailure('That step is unavailable or already ran. No action repeated.')
        seen.add(fingerprint)
        if kind == 'gene_command' and not actions.command_requested(action):
            receipt = tool_receipt(kind, {'blocked': 'The music request expired or was not requested in this conversation. Ask briefly for a fresh complete request.'})
            receipts.append(receipt)
            await recheck(actions, context)
            try:
                result = await fri.chat_once(text, {**context, 'actions': {}, 'action_result': receipt,
                    'action_results': list(receipts)})
            except commands.UserFeedbackCheckFailure:
                result = ChatResult(reply=fallback(receipts), status='completed')
            if not result.action:
                return result, receipts, research
            return ChatResult(reply=fallback(receipts), status='completed'), receipts, research
        progress['active'] = kind
        try:
            receipt = await actions.execute(action)
        except ControllerRejected as exc:
            receipt = {'blocked': str(exc)}
        except (commands.UserFeedbackCheckFailure, discord.HTTPException):
            receipt = {'uncertain': 'Completion was not confirmed. Do not claim success or retry automatically.'}
        progress['active'] = None
        receipt = tool_receipt(kind, receipt)
        receipts.append(receipt)
        if receipt.get('clarification'):
            return ChatResult(reply=receipt['clarification']['question'], status='completed',
                              clarification=receipt['clarification']), receipts, research
        if kind == 'preview_voice':
            preview = receipt.get('settings_preview')
            return ChatResult(reply='Voice settings preview prepared. Nothing is saved until you click Apply.' if preview
                else fallback(receipts), status='completed', settings_preview=preview), receipts, research
        if receipt.get('post_preview'):
            return ChatResult(reply='Post preview prepared. Nothing is sent until you click Send once.',
                status='completed', post_preview=receipt['post_preview']), receipts, research
        if receipt.get('social_preview'):
            return ChatResult(reply='Preview prepared. Nothing is scheduled or published until you confirm it.',
                status='completed', social_preview=receipt['social_preview']), receipts, research
        if kind in ('web_search', 'web_refine', 'image_search') and all(receipt.get(k) for k in ('query', 'answer', 'sources', 'checked_at')):
            if research:
                receipt['sources'] = list(dict.fromkeys(receipt['sources']+research['sources']))[:8]
            research = {k: receipt[k] for k in ('query', 'answer', 'sources', 'checked_at')}
            progress['research'] = research
        offered.pop(kind, None)
        if kind == 'history_search' and receipt.get('remaining_searches', 0) > 0:
            offered['history_search'] = {**context['actions']['history_search'],
                'remaining_searches': receipt['remaining_searches']}
        if kind == 'history_search':
            from .fri_history_search import READ_ONLY_AFTER_HISTORY
            offered = {key: value for key, value in offered.items() if key in READ_ONLY_AFTER_HISTORY}
        if kind == 'web_search' and research:
            offered['web_refine'] = {'description': 'If the lookup did not answer the actual question, investigate further '
                'using public sources. query="". One refinement only; no private context is exported.'}
        if kind in TERMINAL or receipt['outcome'] != Outcome.COMPLETED:
            offered = {}
        # Reading private member evidence cannot become a persistent draft or a public post.
        if kind in ('read_recent', 'learning_overview', 'run_learning', 'history_search'):
            offered.pop('work_save', None)
            offered.pop('prepare_post', None)
            offered.pop('clarify', None)
            offered.pop('reminder_prepare', None)
            offered.pop('plan_prepare', None)
            offered.pop('reminder_cancel', None)
            if getattr(actions, 'invitation', None):
                offered.pop('image_search', None)
        if len(seen) == MAX_STEPS:
            offered = {}
        await recheck(actions, context)
        try:
            result = await fri.chat_once(text, {**context, 'actions': offered,
                'action_result': receipts[-1], 'action_results': list(receipts)})
        except commands.UserFeedbackCheckFailure:
            result = ChatResult(reply=fallback(receipts), status='completed')
        if result.action and not offered:
            return ChatResult(reply=fallback(receipts), status='completed'), receipts, research
    if result.action:
        return ChatResult(reply=fallback(receipts), status='completed'), receipts, research
    return result, receipts, research
