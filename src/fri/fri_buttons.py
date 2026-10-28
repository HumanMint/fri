"""Fri's buttons either work or say why: someone else's, or one whose view ended (a timeout or a restart)."""

import discord

NOT_YOURS = 'not your button.'
EXPIRED = 'that one expired. ask again.'
UNCHECKED = 'couldn’t check you’re still here. try again.'
# Request drafts, owner settings previews, the memory panel and the old per-reply memory buttons: views kept in
# memory only, so a restart ends them. Persistent buttons (fri:memory:open:<member>, reviews, posts, plans) and
# modals aren't matched: those keep working on their own.
ENDED = (r'fri:(?:request:(?:preview:[a-f0-9]{32}|[a-f0-9]{32}:(?:back|next|submit|cancel))|'
         r'behavior:(?:back|next|submit|cancel)|'
         r'memory:(?:open|choose|remove|replace|share|confirm|keep|summary|clear):[a-f0-9]{32})')
ANSWERED = 'fri:answered'  # In a click's Interaction.extras: who answers it, Ended or the view it stands in for.


async def tell(interaction, text):
    """A short private line, gone after ten seconds (see fri_panel.notice). A failed send is just not said."""
    from .fri_panel import notice
    try:
        await notice(interaction, text)
    except discord.HTTPException:
        pass


async def not_yours(interaction):
    """A click on another member's Fri button: told so, instead of Discord's "This interaction failed"."""
    await tell(interaction, NOT_YOURS)


async def unchecked(interaction):
    """A member's own click on a live Fri view, or a memory form they sent, when the fresh check that they're still
    in the server comes back empty (a failed fetch, or they left): told so, instead of Discord's "This interaction
    failed" (fri #183)."""
    await tell(interaction, UNCHECKED)


def answers(interaction, who):
    """Whether `who` answers this click: 'ended' for Ended, 'view' for the view it stands in for. discord.py can queue
    both for one click (ViewStore.dispatch_view), and the view may end before either runs, so the first to ask takes
    it and the other stays out (fri #175). No await between the check and the claim. Without extras to mark, it
    answers, as before."""
    extras = getattr(interaction, 'extras', None)
    if not isinstance(extras, dict):
        return True
    return extras.setdefault(ANSWERED, who) == who


def live(interaction):
    """Whether a view Fri still runs takes this click, looked up as discord.py looks it up (ViewStore.dispatch_view),
    which hands the click to a matching dynamic item as well. If the store can't be read, it's taken as live, so
    nothing extra is said."""
    store = getattr(getattr(interaction.client, '_connection', None), '_view_store', None)
    views = getattr(store, '_views', None)
    if not isinstance(views, dict):
        return True
    data = interaction.data or {}
    key = (data.get('component_type'), data.get('custom_id'))
    return key in views.get(getattr(interaction.message, 'id', None), {}) or key in views.get(None, {})


class Ended(discord.ui.DynamicItem[discord.ui.Item], template=ENDED):
    """One of those buttons, clicked after its view ended: it says so and comes off Fri's message. Nothing is
    submitted, applied or shown from old state. While its view still runs, the view answers alone, and a click the
    view took stays the view's even once it ends (see answers)."""

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(item)

    async def callback(self, interaction):
        fri = interaction.client
        if (live(interaction) or
                getattr(getattr(interaction.message, 'author', None), 'id', None) != getattr(fri.user, 'id', None) or
                not answers(interaction, 'ended')):
            return
        try:
            await interaction.response.edit_message(view=None)
        except discord.HTTPException:
            pass  # Deleted, say: it's still told.
        await tell(interaction, EXPIRED)
