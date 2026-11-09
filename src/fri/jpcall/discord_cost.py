"""Outcome and per-component usage, including late phone-line prices."""

import asyncio
import io
import logging
import re
import textwrap
import uuid
from dataclasses import replace

import discord

from .contract import CallFinished, CallState, StatusChanged, UsageUpdated
from .discord_text import chunks

LOG = logging.getLogger(__name__)
FINAL_REPORT_TIMEOUT_S = 5
FINAL_SEND_TIMEOUT_S = 2
WITHHELD = 'Call details are withheld: sharing permission was withdrawn.'


class _Withdrawn(Exception):
    """Consent changed between renders: render the withheld result again before sending."""


def token_totals(units):
    # Explicit aggregate counts already include their modality/cache breakdowns.
    incoming = units.get('input_tokens')
    if incoming is None:
        incoming = sum(count for kind, count in units.items() if kind.endswith('_in_tokens')
                       and kind != 'unknown_cached_in_tokens')
    outgoing = units.get('output_tokens')
    if outgoing is None:
        outgoing = sum(count for kind, count in units.items() if kind.endswith('_out_tokens'))
    # unknown_cached_in_tokens describes an overlapping, unsplit cache, not extra input.
    return incoming, outgoing


def render_usage(usage, usage_final):
    lines, total, estimate, pending = [], 0.0, False, False
    incoming = outgoing = 0
    for row in usage:
        units = ', '.join(f'{key.replace("_", " ")} {value:g}'
                          for key, value in row.units.items()) or 'no units reported'
        price = 'pending' if row.usd is None else f'{"" if row.exact else "~"}${row.usd:.4f}'
        lines.append(f'{row.component} · {row.model} · {units} · {price}')
        total += row.usd or 0
        row_in, row_out = token_totals(row.units)
        incoming += row_in
        outgoing += row_out
        estimate |= row.usd is not None and not row.exact
        pending |= row.usd is None
    if not usage:
        pending = True
        lines.append('usage pending')
    suffix = ' + pending' if pending or not usage_final else ''
    tokens = f'input {incoming:g} / output {outgoing:g} tokens' if usage else 'tokens pending'
    lines.append(f'total · {tokens} · {"~" if estimate else ""}${total:.4f}{suffix}')
    return '\n'.join(lines)


class CostDetails(discord.ui.View):
    def __init__(self, receipt):
        super().__init__(timeout=None)
        self.receipt = receipt

    @discord.ui.button(label='cost details', custom_id='jpcall:cost_details')
    async def details(self, interaction, button):
        pages = chunks(render_usage(self.receipt.usage, self.receipt.usage_final))
        for index, content in enumerate(pages):
            send = (interaction.response.send_message if index == 0
                    else interaction.followup.send)
            await send(content, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def on_error(self, interaction, error, item):
        LOG.warning('jpcall cost details failed: %s', type(error).__name__)


def failure_reason(finished, failure):
    text = finished.outcome_en.strip().casefold()
    # Only recognize an explicit voicemail outcome, not a passing reference to it.
    if re.match(r'^(?:(?:the call |call )?(?:went (?:straight )?to |reached ))?'
                r'voicemail(?:[.!;:]|$)', text):
        return 'went to voicemail'
    if failure is None and finished.duration_s > 0:
        return None
    detail = (failure.detail if failure is not None else finished.outcome_en).casefold()
    if 'busy' in detail:
        return 'busy'
    if any(word in detail for word in ('no answer', 'no-answer', 'did not answer', "didn't answer")):
        return 'no answer'
    return 'connection failed' if failure is not None else None


def render_final(finished, usage, usage_final, failure=None):
    reason = failure_reason(finished, failure)
    if reason:
        lines = [f"call didn't connect · {reason}"]
    else:
        lines = [f'call ended · {finished.duration_s / 60:.1f} min']
        outcome = ' '.join(finished.outcome_en.split()) or 'no confirmed outcome.'
        # Keep reference codes and names intact while using Fri's sentence style.
        outcome = re.sub(r'^(The|They|It|We|I|No|Booked|Confirmed|Declined|Call|Fri)\b',
                         lambda match: match.group().lower(), outcome)
        short = textwrap.wrap(outcome, width=100)
        lines.extend(short[:3])
        if len(short) > 3:
            lines[-1] = lines[-1][:-1] + '…'
    if finished.termination_confirmed is False:
        lines.insert(0, 'phone termination unconfirmed; calling remains blocked.')
    return '\n'.join(lines)


def render_cost(usage, usage_final):
    known = [row.usd for row in usage if row.usd is not None]
    pending = not usage or len(known) != len(usage)
    estimated = pending or not usage_final or any(not row.exact for row in usage)
    label = 'estimated cost total' if estimated else 'cost total'
    price = f'${sum(known):.2f}' if known else 'pending'
    suffix = ' (pending prices)' if pending and known else ''
    return discord.Embed(description=f'{label}: {price}{suffix}')


class UsageReceipt:
    """Late billing retains HTTP + numeric locators and metering, never call content."""

    def __init__(self, http=None, channel_id=None):
        self.http, self.channel_id, self.message_id = http, channel_id, None
        self.usage, self.usage_final = (), False
        self.revision = 0
        self._lock = asyncio.Lock()

    async def emit(self, event):
        if not isinstance(event, UsageUpdated):
            return
        self.usage, self.usage_final = event.usage, event.usage_final
        self.revision += 1
        await self.publish()

    async def publish(self):
        async with self._lock:
            if self.http is None or self.message_id is None:
                return
            try:
                # No content/view argument: Discord preserves the outcome and its controls.
                with discord.http.handle_message_parameters(
                        embed=render_cost(self.usage, self.usage_final)) as params:
                    await self.http.edit_message(self.channel_id, self.message_id, params=params)
            except Exception as exc:
                LOG.warning('jpcall cost update failed: %s', type(exc).__name__)


class CostSummary:
    """One public report; the breakdown lives in the ephemeral details view."""

    def __init__(self, send, edit, *, http=None, channel_id=None, allowed=None):
        self.send, self.edit = send, edit
        self.allowed = allowed  # The consent check for an outcome that carries call content.
        self.finished = self.failure = self.message = None
        self.receipt = UsageReceipt(http, channel_id)
        self.view = CostDetails(self.receipt)
        self._nonce = uuid.uuid4().int >> 64
        self._revision = 0
        self._event_type = 'CallFinished'
        self._send_attempts = 0
        self._publish_task = None
        self._text_fallback = False
        self._fallback_page = 0

    def _fence(self):
        """Withdrawn consent replaces the outcome before any render or send; never restored."""
        finished = self.finished
        if (finished is None or finished.consent_generation is None or self.allowed is None
                or self.allowed(finished.consent_generation)):
            return False
        self.finished = replace(finished, outcome_en=WITHHELD, consent_generation=None)
        return True

    @property
    def usage(self):
        return self.receipt.usage

    @property
    def usage_final(self):
        return self.receipt.usage_final

    async def emit(self, event):
        if isinstance(event, StatusChanged):
            if event.state == CallState.FAILED:
                self.failure = event
            return
        if isinstance(event, CallFinished):
            # Replayed terminal events must not overwrite a newer posted phone price.
            if self.finished is None:
                self.finished = event
                if self.receipt.revision == 0:
                    self.receipt.usage, self.receipt.usage_final = event.usage, event.usage_final
        elif isinstance(event, UsageUpdated):
            self.receipt.usage, self.receipt.usage_final = event.usage, event.usage_final
            self.receipt.revision += 1
        else:
            return
        self._revision += 1
        self._event_type = type(event).__name__
        if self.finished is None:
            return
        if self._publish_task is None or self._publish_task.done():
            self._publish_task = asyncio.create_task(self._publish())
        # A cancelled waiter must not orphan an already accepted Discord message.
        await asyncio.shield(self._publish_task)

    async def _send_retry(self, content, *, nonce, outcome=None, **kwargs):
        for attempt in range(2):
            if self._fence():
                raise _Withdrawn
            try:
                async with asyncio.timeout(FINAL_SEND_TIMEOUT_S):
                    if nonce == self._nonce:
                        self._send_attempts += 1
                    if outcome is None:
                        return await self.send(content, nonce=nonce, **kwargs)
                    with io.BytesIO(outcome.encode('utf-8')) as body:
                        attachment = discord.File(body, filename='call-result.txt')
                        try:
                            return await self.send(content, nonce=nonce, file=attachment, **kwargs)
                        finally:
                            attachment.close()
            except discord.HTTPException as exc:
                if exc.status == 403 or attempt == 1:
                    raise
            except Exception:
                if attempt == 1:
                    raise

    async def _initial_report(self, text, embed):
        outcome = self.finished.outcome_en or 'no confirmed outcome.'
        if not self._text_fallback:
            try:
                return await self._send_retry(text, nonce=self._nonce, outcome=outcome,
                                              embed=embed, view=self.view)
            except discord.HTTPException as exc:
                if exc.status != 403:
                    raise
                self._text_fallback = True
        # No attachment permission: post the complete result, with stable identity per page.
        pages = chunks(outcome, 1900)
        while self._fallback_page < len(pages):
            index = self._fallback_page
            await self._send_retry(f'full result · {index + 1}/{len(pages)}\n{pages[index]}',
                                   nonce=(self._nonce + index + 1) % (2 ** 64))
            self._fallback_page += 1
        return await self._send_retry(text, nonce=self._nonce, embed=embed, view=self.view)

    async def _publish(self):
        event_type = self._event_type
        try:
            # Recovery belongs to final publication: late billing holds only the receipt.
            async with asyncio.timeout(FINAL_REPORT_TIMEOUT_S):
                while True:
                    revision = self._revision
                    event_type = self._event_type
                    self._fence()
                    text = render_final(self.finished, self.usage, self.usage_final, self.failure)
                    embed = render_cost(self.usage, self.usage_final)
                    if self.message is None:
                        try:
                            self.message = await self._initial_report(text, embed)
                        except _Withdrawn:
                            continue
                        self.receipt.message_id = getattr(self.message, 'id', None)
                        if self._send_attempts > 1:
                            # A nonce retry may recover an accepted message with an older price.
                            await self.edit(self.message, text, embed=embed, view=self.view)
                    else:
                        await self.edit(self.message, text, embed=embed, view=self.view)
                    self.receipt.message_id = getattr(self.message, 'id', None)
                    # A price may arrive while the initial Discord send is pending.
                    if self.receipt.revision:
                        await self.receipt.publish()
                    if revision == self._revision:
                        return
        except Exception as exc:
            # Never replace a known message after a failed edit; nonce retry is bounded above.
            LOG.warning('jpcall final report failed: %s %s', event_type, type(exc).__name__)

    async def flush(self):
        if self._publish_task is not None:
            await asyncio.shield(self._publish_task)
