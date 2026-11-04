"""Outcome and per-component usage, including late phone-line prices."""

import asyncio
import logging

import discord

from .contract import CallFinished, UsageUpdated
from .discord_text import chunks

LOG = logging.getLogger(__name__)


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


class CostSummary:
    """Update the same message on UsageUpdated. Overflow gets bounded companion pages."""

    def __init__(self, send, edit):
        self.send, self.edit = send, edit
        self.finished = None
        self.usage = ()
        self.usage_final = False
        self.messages = []
        self._lock = asyncio.Lock()

    async def emit(self, event):
        async with self._lock:
            if isinstance(event, CallFinished):
                self.finished = event
                self.usage, self.usage_final = event.usage, event.usage_final
            elif isinstance(event, UsageUpdated):
                self.usage, self.usage_final = event.usage, event.usage_final
            else:
                return
            if self.finished is None:
                return
            text = (f'call ended · {self.finished.duration_s / 60:.1f} min\n'
                    f'{self.finished.outcome_en}\n\n'
                    f'{render_usage(self.usage, self.usage_final)}')
            pages = chunks(text)
            pages += ['costs continued above.'] * (len(self.messages) - len(pages))
            try:
                for index, content in enumerate(pages):
                    if index == len(self.messages):
                        self.messages.append(await self.send(content))
                    else:
                        await self.edit(self.messages[index], content)
            except discord.HTTPException as exc:
                LOG.warning('jpcall cost write failed: HTTP %s', exc.status)
