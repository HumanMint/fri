"""Compact owner learning DMs with persistent, read-only detail navigation."""

import re

import discord

from . import feedback as commands
from .previews import pages
from .privacy import clean

NO_MENTIONS = discord.AllowedMentions.none()
REPORT_ID = re.compile(r'^fri:learning:(?P<report_id>[a-f0-9]{32}):(?P<page>summary|[0-9]{1,2})$')


def safe(text, limit=200):
    return discord.utils.escape_markdown(discord.utils.escape_mentions(
        clean(' '.join(str(text).split()), limit)))


def summary(report):
    data = report.get('card') or {}
    if not data:
        interrupted = 'interrupted' in report['body'].casefold()
        return ('**Learning check · interrupted**\n\nNo new findings confirmed. Open Details for the recorded issue.'
                if interrupted else '**Learning update**\n\nThis earlier report is available under Details.')
    historical, preferences = data['historical_notes'], data['saved_preferences']
    total = historical + preferences
    heading = 'couldn’t finish' if data['error'] else (f'{total} new note'+('s' if total != 1 else '') if total else 'no new notes')
    lines = [f'**Learning check · {heading}**']
    channel = str(data['channel_id'])
    where = f'<#{channel}>' if channel.isdigit() else safe(channel, 40)
    lines += [f"**{safe(data['member'], 80)}** · {where}" if data['member'] else where, '']
    if data['term']:
        lines.append(f"Searched for “{safe(data['term'], 80)}”.")
    scanned, eligible, checked = data['scanned'], data['eligible'], data['already_checked']
    if data['error']:
        lines.append('The check didn’t finish. No new notes were saved; details have the issue.')
    elif data.get('rotation', {}).get('exhausted'):
        lines.append('No unread history left for this member in the approved channels.')
    elif scanned and checked == scanned:
        lines.append(f'{scanned} match'+('es' if scanned != 1 else '')+' found, all already checked.')
    elif not scanned:
        lines.append('No matching messages in this slice.')
    elif not eligible:
        lines.append(f'{scanned} messages checked; none eligible for learning.')
    else:
        lines.append(f'{scanned} messages checked · {eligible} eligible'+
                     (f' · {checked} already seen' if checked else ''))
        if data['summary']:
            lines += ['', safe(data['summary'], 240)]
        elif historical and data.get('findings'):
            lines += ['', safe('; '.join(data['findings']), 240)]
        if historical:
            lines.append(f'Saved {historical} historical note'+('s' if historical != 1 else '')+'.')
        if preferences:
            lines.append(f'Saved {preferences} stated preference'+('s' if preferences != 1 else '')+'.')
    if data.get('oldest') is not None and data.get('newest') is not None:
        lines.append(f"History: <t:{int(data['oldest'])}:d> to <t:{int(data['newest'])}:d>")
    rotation = data.get('rotation') or {}
    if rotation:
        lines += ['', f"Round {rotation['round']} · member {rotation['position']}/{rotation['total']}"]
    lines.append(f"Next check <t:{int(data['next_check'])}:R>")
    return '\n'.join(lines)


def report_component(interaction, bot_id, owner_id):
    if not bot_id or not owner_id or getattr(interaction, 'guild_id', None) is not None:
        return False
    kind = getattr(interaction, 'type', None)
    custom_id = (getattr(interaction, 'data', None) or {}).get('custom_id', '')
    return (getattr(kind, 'value', kind) == 3 and
            getattr(getattr(interaction, 'user', None), 'id', None) == owner_id and
            getattr(getattr(getattr(interaction, 'message', None), 'author', None), 'id', None) == bot_id and
            isinstance(custom_id, str) and REPORT_ID.fullmatch(custom_id) is not None)


class ReportButton(discord.ui.DynamicItem[discord.ui.Button], template=REPORT_ID):
    def __init__(self, report_id, page, label='Details'):
        self.report_id, self.page = report_id, str(page)
        super().__init__(discord.ui.Button(label=label, style=discord.ButtonStyle.secondary,
                         custom_id=f'fri:learning:{report_id}:{page}'))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(**match.groupdict())

    async def callback(self, interaction):
        fri = interaction.client
        if not report_component(interaction, getattr(fri.user, 'id', None), fri.owner_id):
            return
        await interaction.response.defer()
        if not await fri.agent.member(fri.owner_id, fresh=True):
            await interaction.followup.send('server access is needed to open this report.', ephemeral=True,
                                             allowed_mentions=NO_MENTIONS)
            return
        try:
            report = await fri.api('POST', f'/agent/reports/{self.report_id}/view',
                                  {'guild_id': str(fri.guild_id), 'user_id': str(fri.owner_id)})
        except commands.UserFeedbackCheckFailure:
            await interaction.followup.send('i couldn’t load that report. it may have expired or been cleared; '
                                             'if it’s recent, try again in a moment.',
                                             ephemeral=True, allowed_mentions=NO_MENTIONS)
            return
        content, view = report_message(report, self.page)
        await interaction.edit_original_response(content=content, embed=None, attachments=[], view=view,
                                                   allowed_mentions=NO_MENTIONS)


def report_message(report, page='summary'):
    view = discord.ui.View(timeout=None)
    if page == 'summary':
        content = summary(report)
        view.add_item(ReportButton(report['id'], 0))
    else:
        detail = pages(report['body'].replace('Untrusted summary:', 'Conversation summary:'))
        index = min(max(int(page), 0), len(detail)-1)
        content = f"**Learning details · {report['id'][:8]}**\n\n{detail[index]}\n\nPage {index+1}/{len(detail)}"
        view.add_item(ReportButton(report['id'], 'summary', 'Summary'))
        if index:
            view.add_item(ReportButton(report['id'], index-1, 'Back'))
        if index+1 < len(detail):
            view.add_item(ReportButton(report['id'], index+1, 'Next'))
    return content, view
