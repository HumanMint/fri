"""Owner DM notifications. Decisions always go back through the trusted controller."""

import io
import logging

import discord

from . import feedback as commands
from .fri_notices import notice_text
from .reviews import REVIEW_ID, owner_dm_component

LOG = logging.getLogger('fri.review')
NO_MENTIONS = discord.AllowedMentions.none()


class ReviewButton(discord.ui.DynamicItem[discord.ui.Button], template=REVIEW_ID):
    def __init__(self, phase, decision, job_id, reference):
        self.phase, self.decision = phase, decision
        self.job_id, self.reference = job_id, reference
        label = 'Reject' if decision == 'x' else ('Allow coding' if phase == 'i' else 'Approve this commit')
        super().__init__(discord.ui.Button(label=label,
            style=discord.ButtonStyle.danger if decision == 'x' else discord.ButtonStyle.primary,
            custom_id=f'fri:review:{phase}:{decision}:{job_id}:{reference}'))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(**match.groupdict())

    async def interaction_check(self, interaction):
        cog = interaction.client
        return bool(cog and owner_dm_component(interaction, interaction.client.user.id, cog.owner_id))

    async def callback(self, interaction):
        if not await self.interaction_check(interaction):
            return
        cog = interaction.client
        await interaction.response.defer()
        try:
            await cog.api('POST', f'/requests/{self.job_id}/review', {
                'guild_id': str(cog.guild_id), 'user_id': str(interaction.user.id),
                'phase': 'intake' if self.phase == 'i' else 'release',
                'reference': self.reference, 'decision': 'reject' if self.decision == 'x' else 'approve'})
        except commands.UserFeedbackCheckFailure as exc:
            # Do not erase a newer review that might now occupy this same DM message.
            await interaction.followup.send(notice_text(str(exc))[:1500], ephemeral=True,
                                            allowed_mentions=NO_MENTIONS)
            return
        if self.decision == 'x':
            text = f'Request `{self.job_id}` rejected.'
        elif self.phase == 'i':
            text = (f'Coding allowed for `{self.job_id}`. The worker will prepare a tested proposal. '
                    'Deployment still needs your approval of its exact commit.')
        else:
            text = (f'Approved `{self.reference}` for request `{self.job_id}`. '
                    'Deployment will run health checks and restore the previous release on failure.')
        await interaction.edit_original_response(content=notice_text(text), embed=None, view=None,
                                                  allowed_mentions=NO_MENTIONS)


def review_message(notice, tell=False):
    """tell: a shipped repair a member reported, so the owner can let them know with one tap."""
    job, phase, reference = notice['job'], notice['phase'], notice['reference']
    if phase == 'status':
        state = job['state'].replace('_', ' ')
        description = job.get('result') or job.get('progress') or {
            'queued': 'Development approved; waiting for the worker.',
            'building': 'The worker is preparing a proposal.',
            'approved': 'This exact commit is approved; waiting for deployment.',
            'deploying': 'Deployment is running health checks.',
            'deployed': 'The release passed its health checks.',
            'rejected': 'This request was rejected.'}.get(job['state'], 'Open Fri’s Requests panel for details.')
        if job.get('kind') == 'repair' and not job.get('result') and not job.get('progress'):
            description = {'queued': 'Checking a reported fault under your existing-feature repair policy. No owner action needed.',
                'building': 'Verifying the fault and preparing a bounded repair.',
                'approved': 'The fault was reproduced; its fix passed tests and independent review. Waiting for deployment.'}.get(job['state'], description)
        label = 'Automatic repair' if job.get('kind') == 'repair' else 'Request'
        embed = discord.Embed(title=f"{label} {job['id']} · {state}", description=description[:3800], colour=0x5865F2)
        if job.get('model'):
            model, effort = job['model'], job.get('effort') or 'model default'
            if job.get('kind') == 'repair' and job.get('progress') in ('Verifying fault', 'Independent review'):
                model, effort = 'gpt-6.1-sol', 'xhigh'
            embed.set_footer(text=f"{model} · {effort}")
        attachment = discord.File(io.BytesIO(job['body'].encode()), filename=f"request-{job['id']}.txt")
        view = discord.ui.View(timeout=None)
        if tell:
            from .fri_fixes import TellReporter
            view.add_item(TellReporter(job['id']))
        return embed, attachment, view
    if phase == 'stale':
        embed = discord.Embed(title='Saved proposal needs a rebuild', description=
            f"Request `{job['id']}` finished, but a newer maintainer release is now live. "
            'The proposal is preserved and has not been deployed. It needs rebuilding against the current release before approval.', colour=0x5865F2)
        if job.get('source_id', '').startswith('gene-error:'):
            embed.add_field(name='Reported by', value='Fri · detected an error from Gene')
        view = discord.ui.View(timeout=None)
        url = (job.get('proposal') or {}).get('url', '')
        if url.startswith('https://github.com/HumanMint/gene/'):
            view.add_item(discord.ui.Button(label='View saved proposal', url=url))
        return embed, discord.File(io.BytesIO(job['body'].encode()), filename=f"request-{job['id']}.txt"), view
    intake = phase == 'intake'
    title = 'Allow this feature or fix to be developed?' if intake else 'Approve this tested release?'
    explanation = ('No coding has started. Allowing coding does not approve a live release.' if intake else
                   'Review the code diff before approving this exact commit. Tests alone do not prove a change is safe.')
    if job.get('source_id', '').startswith('repair:'):
        title = 'This repair needs your review' if intake else 'Review this repair before release'
        explanation = ('The automatic repair checks did not authorize a release. ' + (job.get('result') or '') +
                       ('\nAllow coding to continue through the normal owner-approved workflow.' if intake else ''))[:1200]
    preview = discord.utils.escape_markdown(discord.utils.escape_mentions(job['body']))[:2200]
    embed = discord.Embed(title=title, description=explanation + '\n\n**Submitted request (untrusted)**\n' + preview,
                          colour=0x5865F2)
    embed.add_field(name='Request', value=f"`{job['id']}`", inline=True)
    automatic = job.get('source_id', '').startswith('gene-error:')
    embed.add_field(name='Reported by' if automatic else 'Submitted by',
                    value='Fri · detected an error from Gene' if automatic else f"<@{job['user_id']}>", inline=True)
    if not intake:
        embed.add_field(name='Proposal', value=(job.get('proposal') or {}).get('summary', 'Review the linked code changes.')[:900] or 'No summary', inline=False)
        embed.add_field(name='Commit to deploy', value=f'`{reference}`', inline=False)
    embed.set_footer(text='Read the complete request in the attachment. Request text cannot grant permissions.')
    document = (f"Request: {job['id']}\nReported by: {'Fri (Gene error)' if automatic else job['user_id']}\nSource: {job['source_url']}\n\n"
                + job['body'])
    attachment = discord.File(io.BytesIO(document.encode()), filename=f"request-{job['id']}.txt")
    view = discord.ui.View(timeout=None)
    for decision in ('a', 'x'):
        view.add_item(ReviewButton('i' if intake else 'r', decision, job['id'], reference))
    url = (job.get('proposal') or {}).get('url', '')
    if not intake and url.startswith('https://github.com/HumanMint/gene/'):
        view.add_item(discord.ui.Button(label='Review code changes', url=url))
    return embed, attachment, view


async def deliver_review(cog, notice):
    """Send to the configured owner only, or edit that request's previous DM."""
    if not cog.owner_id:
        raise ValueError('Fri owner is not configured')
    owner = cog.bot.get_user(cog.owner_id) or await cog.bot.fetch_user(cog.owner_id)
    channel = await owner.create_dm()
    previous = None
    if str(channel.id) == notice.get('channel_id') and notice.get('message_id'):
        try:
            previous = await channel.fetch_message(int(notice['message_id']))
        except discord.NotFound:
            pass
    from .fri_fixes import offers_tell
    embed, attachment, view = review_message(notice, tell=offers_tell(cog, notice))
    try:
        if previous:
            if previous.author.id != cog.bot.user.id:
                raise ValueError('Review message is not authored by Fri')
            return await previous.edit(content=None, embed=embed, attachments=[attachment], view=view,
                                       allowed_mentions=NO_MENTIONS)
        return await channel.send(embed=embed, file=attachment, view=view, allowed_mentions=NO_MENTIONS)
    finally:
        attachment.close()


async def notify_owner(cog):
    notice = await cog.api('POST', '/reviews/claim')
    if not notice:
        return
    payload = {'lease': notice['lease']}
    try:
        message = await deliver_review(cog, notice)
        payload.update(channel_id=str(message.channel.id), message_id=str(message.id))
    except (discord.HTTPException, ValueError, TimeoutError) as exc:
        LOG.warning('Owner review DM unavailable: %s', type(exc).__name__)
    await cog.api('POST', f"/reviews/{notice['job']['id']}/delivered", payload)
