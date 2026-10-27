# Fri operations

## Explicit mention invitations

The `event-mention-chat` controller capability is required. A real, visible Discord
mention can invite a reply in an accessible text channel or public thread in the
configured main guild. Quoted/code/URL tokens, webhooks, bots, stale events and mass
mentions do not qualify. The separate Lab scope remains its exact configured channel.
Outside configured chat channels, plain `fri` and unmentioned follow-ups do nothing.

An outside invitation permits one textual conversation reply and optional public web
research or retained public sources. It grants no operations, admin, workspace,
posting or learning permission, and never adds a channel to an ambient allowlist.
The controller applies the existing member allowance. Disabled mode and in-progress
reply notices keep their existing behavior. The runtime rechecks the current event,
member roles, channel/thread permissions, source contents and privacy before output.
After the model answers, a ping is rechecked before each tool acts, before the model hears
the turn again (once signed in for that request, with every nearby post its read showed, text-only
included), and once right before sending; checks repeated with nothing in between are
gone. That last check reads the ping again after its privacy read, and also covers its image
post, including a photo the author posted just before the ping.
In configured channels, local privacy/usage utilities, exact controls and accepted
operational follow-ups retain their existing admission before any social allowance.
New conversation and on-demand surrounding context still need normal chat admission;
an outside mention always keeps its separate one-event social admission.

Each invitation can use at most twelve preceding messages from the same channel in
the last six hours and a direct reference up to seven days old. A catch-up in a
configured channel uses its own read instead (see "Catching up"). Up to four still
images are inspected in one model call, the current attachments and the reference
first, then the newest nearby posts' own attachments; Fri picks among several instead
of asking which one. A speaker's own post keeps the two-image limit; a nearby gallery
post can use the whole budget. Each image stays a non-spoiler PNG/JPEG/WebP, 4 MB,
twenty million pixels. A GIF link (Tenor, Giphy) counts as one image through the still preview
Discord's own proxy made of it; nothing decodes an animation, so an uploaded GIF or an animated
preview is not inspected. Other external embeds and videos are not inspected by this path.
A pasted message link or a replied-to post is opened only when the speaker and Fri can
read it now (fetched overwrites and roles); from a server channel, only when it is this
channel or one that keeps no role or member out. Every post a read or a link showed is
found again unchanged before each later model request and before delivery. Asked what's
pinned, Fri reads up to twenty of a channel's pins, any age, with the same channel, audience
and author checks as any read; each must still be pinned and unchanged before the next request. Captions
and filenames are never visual evidence. Public research only exports an exact
public question from the current request, never surrounding chat or image captions.

Context from other members keeps its actual author and privacy revision and stays
ephemeral. Saved conversation uses a neutral breadcrumb; no derived personal notes,
drafts or posts are retained. Configured-channel mentions start with their normal
capabilities. The existing `read_recent` tool can fetch the bounded nearby text and
image context on demand; only after that read are persistent writes and external
image selection removed for that turn. Explicit reply references load only their
direct target initially, and keep their source and persistence guards. The current
attachments and existing immediate own-photo pairing remain available directly.

Freshness checks compare author and event IDs, never display names. A gateway event shows
the member's server nickname, but a fetched copy of the same message shows their global
name, because Fri has no members intent or member cache.

Every ping to Fri from a member (a mention, a reply ping or Fri's role pill) that gets no
lasting reply is logged once at INFO, with only its channel and message IDs:
`Mention <channel>/<message> not answered: <reason>`. A reason is a gate or an error type,
never content or names. Examples: `not a visible user mention` (the role pill, or a mention
in quotes or code), `message changed`, `stale event`, `missing permissions`, `fri disabled`,
`author privacy changed`, `burst too long`, `earlier reply still running` (its waiting
notice is deleted once that reply lands), `social cooldown` (after the cooldown's one
notice), `image share withdrawn`, `message deleted`, `nearby message deleted`, `reply withheld:
<recheck>` when a recheck failed after composing, and an exception type such as `Forbidden`. A burst's earlier messages are
logged under the reason its last message got. A reply, a notice, a preview or a reaction
counts as an answer. Bots, webhooks and DMs aren't logged. Fri ignores a copy of an event
that Discord delivers again while the first delivery, or the burst holding it, is still
being handled. Separately, it remembers each event ID for an hour from its first arrival
(at most 10,000 IDs, oldest dropped first) and ignores copies in that time, so the first
delivery answers or explains for every copy. A ping is logged at most once while it is among the last
10,000 pings watched.

A message to Fri waits 1.5 s for the rest of a burst before Fri answers. A message that is only
Fri's name or a hello ("fri", "yo fri", "fri?", a bare ping) waits up to 8 s instead, because the
question usually comes next, and in a channel that next message gets in without "fri". If it comes
within the 5 s burst span, one reply answers both. If it comes later, it is answered on its own with
the bare line in Fri's view, and a bare ping is logged as `rest came separately`. One sent within
the 8 s whose checks end after them still gets in, answered after Fri's reply to the bare line: a
follow-up window counts as of when the message came in. A reply to another message, or one with an
image or sticker, doesn't wait.

Fri shows typing only once words are coming: for a message addressed to it (its name, a ping, a
reply to Fri, a DM), once the turn is admitted, so never before a cooldown, a privacy or music
control, or the local closing reaction; for anything else (a follow-up without "fri", ambient
attention), only once an action starts. A refused typing request never stops the reply. An ambient
reply is dropped, unsaid and unsaved (`room moved on`), if anyone other than the member it answers
spoke in the channel while Fri composed, or the channel can't be read to check. Fri reads the next
20 messages for that; when all 20 are bot posts or the member's own, it counts as moved on too.

Fri's live view of a configured channel is its last 15 minutes (at most 30 messages). A deleted
message leaves it, and so does a reply of Fri's that someone deletes. Gene's error posts stay until
they expire, so a quick delete can't hide one from the repair investigation. A deleted message
also leaves a burst that is still gathering, and the burst's last message answers for the rest; if
the last one is deleted, the whole burst is dropped. A bare "fri" deleted, during its 8 s wait or after it, stops
letting the member's next message in without "fri", even one sent during the wait that's still
coming in; a follow-up window from an earlier reply stays open. A deleted "forget me" or other privacy control
isn't carried out: deleting it may be taking it back, and an erasure can't be undone. A reply
still being composed for a deleted
message is neither sent nor saved (`message deleted`), and one deleted before the model is
asked never reaches it. Delivery checks again after each of its own waits (an image's last check,
a reaction's turn): a message it answers, or one the model saw, deleted meanwhile stops it, and
one deleted while the reply, reaction or card goes out leaves it delivered but unsaved. Another member's message deleted before then is left out of what the
model sees; deleted after, the reply is withheld (`nearby message deleted`), with the "things
changed while i was typing" notice when someone addressed Fri; a reaction or quiet decision is
dropped too, unsaved and without a notice. An edit replaces a member's row
with the edited text and what the post still carries (an image edited out leaves the row's kinds too); an edit Discord sends without its author drops the row. An edit to a member's
message Fri is still handling (still coming in, or being answered) withdraws it instead: it leaves,
or never enters, the view, and a reply to it that hasn't reached the model yet is refused. Fri's and Gene's
rows, bursts and the reply fences keep comparing against what arrived. Fri's own reply to a
message that was later deleted stays in the view as long as it stays in the channel.

Offline synthetic integration checks cover handler replies, image provenance,
privacy/access revocation, expiry, allowances and the real controller wire contract.
They do not prove Discord delivery or live model interpretation of a particular meme.

## Catching up

"fri what did i miss", or any ask to be caught up, has the model call `read_recent` with
`window=since_my_last_message`. Fri reads that channel back from the request (the message, or the
panel interaction), up to 100 messages or 12 hours before it, and stops at the speaker's last
message there. Their own messages from the five minutes before they asked ("back", "yo") and ones
only bots answered count as them coming back, not where they left. Both windows are measured from
the request, so a slow model call doesn't change what the read covers. Rows carry the author, minute and text cut to 300
characters, without jump links, and stop once the serialized rows would pass 20,000 characters, so
emoji-heavy chat keeps the newest stretch instead of growing the prompt. The receipt says where the
read stopped (`your_last_message`, `size_limit`, `message_limit` or `time_limit`) and when the
speaker last spoke, so Fri can say a recap only covers the latest stretch.

It is the same tool, scope and fences as any `read_recent`: one channel the member can read (the
current one in public, an approved one from a DM), no bots, webhooks, empty messages or members with
memory off, each author's privacy revision rechecked before delivery, the history-days and
dedicated-channel start floors, no images, no extra model call, only the conversation allowance,
and a neutral breadcrumb in saved history. Before the reply goes out Fri reads the same window
again, and withholds the reply if any message it showed was edited or deleted meanwhile. A
configured-channel mention that asks to catch up gets this read, and that recheck, rather than the
twelve-message nearby context. Outside invitations don't offer it.
`python -m fri.fri_eval --suite controls` includes catch-up cases: choosing the window, a short
attributed recap, an empty read and a partial one.

## Conversation continuity and provenance

Requires the controller's `turn-provenance` capability. Fri starts only against a controller that
advertises it, so the controller must be released first.

- **One reply per message.** A message's direct reply holds it until Discord accepts the reply and
  the turn is recorded. A redelivered event, or ambient attention picking the same message, is
  ignored meanwhile. From the moment Fri starts sending until the turn is recorded, the member's
  next message waits (up to 30 seconds), so an answer to a question Fri just asked still finds it
  pending. Waiting messages still group into bursts by when they arrived. They are dropped if their
  author's privacy revision moves meanwhile: "forget me", the panel's "Forget my chats", or Gene/Red
  erasure. Privacy commands, plain or @mentioned, never wait.
- **Memory controls in send order.** A member's "forget me" and "remember me again" take effect in the
  order they were sent, not the order their arrival reads finish: a later one waits (up to 240
  seconds) while an earlier one is still coming in. One that arrives after a newer, different one
  already went through (e.g. delivered late) isn't carried out; Fri says so and asks them to send it
  again if they still mean it.
- **DMs at delivery.** A DM reply, reaction or quiet decision is neither sent nor saved once its author
  has left the server (read fresh from Discord) or DMs were turned off while Fri wrote it, as before
  the model request. A memory control that already went through still says so.
- **Saved only after delivery.** A turn is staged while Fri composes the reply. Just before
  delivery, `/chat/turn/check` runs the save's privacy fence without writing: the speaker's
  revision, plus every referenced member's revision and memory setting. Only after Discord
  accepts the reply (or Fri records a reaction or quiet decision) is the turn saved. A failed send
  leaves no record claiming Fri said it. A refused check stops the reply. A failed save is
  logged; it never retracts a delivered message.
- **Provenance.** Each saved turn lists the other members whose messages shaped it, each with the
  privacy revision Fri observed. Every context source counts:
  - recent channel rows, including Fri's own replies with their sources;
  - saved history turns;
  - a pending clarification;
  - the channel summary's authors;
  - reply targets. A referenced Fri message is resolved through `/chat/messages/sources` to its
    original sources and speaker. If that's unknown, or the message kept only a breadcrumb, its text
    is withheld from the model before inference and the new turn is stored as a neutral breadcrumb.
  - configured-channel nearby context and read evidence.

  More than 200 sources refuses the reply rather than dropping any. So does one member appearing with
  two revisions: their privacy changed while the turn was read, and Fri never relabels the older
  read. Repeated reads in one turn (history pages, channel reads) keep the first revision they saw,
  so a change between them fails the final access check.
- **Unattributed history.** Replies saved before this release, or by a legacy companion during
  rollout or after a Fri-only rollback, may quote other members with no record of whom. The
  controller marks them, and Fri shows the model only that an earlier reply existed. Pending
  questions saved without provenance are ignored. A legacy command receipt shows that a command ran,
  not its argument or Gene's output, which can echo it. A request draft carries its
  turn's fence: preview, every page turn and submission re-run `/chat/turn/check` and refuse once
  any source changed. A submitted request carries those sources and the requester's revision. The
  controller checks them as it creates the job and withdraws the job, even mid-build, if any source
  later erases. Preview cards are recorded as soon as Discord accepts them, even if binding fails. The controller rejects stale provenance, deletes dependent turns
  when any listed member turns memory off, is erased or clears chats, and drops turns whose
  sources' revisions moved. Over-inclusion is deliberate: anyone in the context can erase the
  derived copy.
- **Real replies.** With provenance, Fri keeps what they actually said instead of placeholder
  breadcrumbs. This covers plain chat, reply targets, public research, image shares, Gene
  command results and the member's own saved work or reminders. Reads of other channels,
  archives, learning reports and diagnostics still keep neutral breadcrumbs, including a diagnosis
  nested in an uncertain Gene command. The recorded and echoed text is what Discord actually
  showed, for example an attachment fallback. Outside invitations
  keep the controller's mention placeholder.
- **Other stores take only the member's own words.** Command receipts, saved public work and
  game-night cards don't track provenance, so another member's words never enter them. A song
  argument must use only words from the member's message or verified clarification answer whenever
  other members' words are in context. Case, accents and punctuation may differ. A 0-100 volume is exempt unless it came from read evidence.
- **Gene's errors.** Gene's error text can quote whoever ran the command, and there's no way to
  attribute it. Conversations see only that Gene posted an error. The owner's repair investigation
  still reads the full text.
- **Not yet covered.** Repair requests Fri files after noticing a Gene error quote Gene's raw error
  text, and `gene_diagnose` reads them back. Replies built from that read keep only a breadcrumb,
  but the stored repair text isn't attributed or erased yet. It's tracked for a follow-up.
- **Group view.** Fri's delivered public replies join the in-memory channel view, tagged with the
  speaker and their provenance. A reply leaves the view, and no later turn can inherit from it,
  once anyone it drew on erases or changes privacy. Fri's own rows never trigger background
  attention.
- **Attention's last check.** After its model slot and sign-in, attention checks that every row it's
  about to send is still in the live view, and that each author it draws on (provenance included)
  still has memory on at the revision it was read under, then checks the live view again. With a
  controller that offers `author-fence`, the authors are one `/agent/authors/check` call that the
  controller reads under its lock, so an erasure through Gene or Red can't land between two authors'
  reads (fri #84). Any refusal (a stale author, a channel out of scope, a malformed check) keeps the
  batch quiet, and so do more than 200 author revisions; it's offered again on the next heartbeat
  window, rebuilt from the live view. An unreachable controller fails the attention stage, as before.
  An older controller gets the per-author profile reads, which an erasure through Gene during them
  can still slip past.
- **Text the model reads.** Every JSON block sent to a model (conversation, internal tasks, web
  research) goes through `privacy.model_json`. Emoji and Korean arrive as written, so
  an emoji is one character instead of a 12-character `\u` escape and a Hangul syllable one instead
  of six. Only a pinned allowlist is raw (`privacy.RAW`): Emoji_Presentation emoji from Unicode 14.0
  except the regional indicators (🇫🇷 and Clipperton's 🇨🇵 are the same flag), plus ❤, Hangul
  syllables and the modern Hangul compatibility jamo such as ㅋ. None of them looks like another or
  like ASCII, alone or in a row, so a row of lookalikes can't spell bits a
  person reading the log won't see. Everything else stays escaped exactly as before: other scripts
  and symbols (Latin A and Cyrillic А, the Kelvin sign and K, look-alike combining marks, and ’”
  and ”’ render alike), combining marks, every variation selector (even an emoji's own U+FE0F: 👍 and 👍️
  look the same), controls, format and invisible characters, spaces and separators, surrogates,
  private-use and unassigned code points. Widening the list needs the same test: no member may look
  like another raw character. `json.dumps` still does the traversal, key conversion and errors. To
  compare against the old escaping, run `python -m fri.fri_eval --suite model-text` with and without
  `--baseline` (same cases and settings, model-graded: boundaries, attribution, tone, Korean and
  Korean chat shorthand, and hidden carriers (thumbs, doubled shaddas, overlay order, Kelvin signs,
  Cyrillic A, French-looking flags)). The baseline escapes all of Fri's own model input, internal tasks such as image
  selection included; the graders pin `privacy.model_json`, so they read both runs the same way. A
  case whose reply fails, or whose grade fails, fails the run.

## Members' own requests

Each turn's chat context includes up to five of the speaker's own requests that are still open or
settled within the last week, with their controller state (`your_gene_requests` for the model).
Fri uses them to answer "is my fix done?" and to avoid duplicate drafts, never claims progress
beyond the recorded state, and in public mentions them only when the speaker asks. Request text
appears only in the member's private conversation (DM or `/fri` panel), never for drafts built from
other members' words, and not at all while their memory is off. Requests typed in the panel's form
are sent with an empty source list, so they count as the member's own words. A request withdrawn
after a privacy change is marked `withdrawn`, so Fri doesn't call it the owner's rejection.
Public rows have no titles, so Fri ties a state to a feature the member names only when a title or id
shows it's that request (or the member asks generally and one shows); otherwise they say what they can
see without calling it that feature, or ask. With no list (an outside invitation or an older
controller) they point to Requests instead of guessing.
`python -m fri.fri_eval --suite requests` checks status answers with the real model: queued and
approved haven't started their next step, building isn't done, deployed means it shipped (the
controller doesn't mark a request a later rollback reverted), withdrawn isn't a rejection, untitled,
empty or unavailable lists aren't guessed, an open request isn't drafted again, and public banter
leaves requests alone. Each reply must be a plain reply with no draft or action, then a rubric judge
on the same model grades it.

## Open loops

Each turn's chat context also carries the speaker's own unfinished things that belong to that
conversation (`your_open_loops` for the model), so Fri knows them without a tool call:

- **Reminders**, only in the member's DM with Fri: upcoming ones and any due in the last day, with
  their delivery state. A confirmed reminder that a later privacy change will stop at delivery shows as
  `will_not_send`, not `scheduled`. The controller sends an empty list for any private conversation,
  and Fri nulls it unless the turn came from a DM message, since the `/fri` panel is private too.
- **Saved work**, only in the conversation it was saved in: open items' titles and last update.
- **Game nights**, only in the card's channel: cards the member organized that haven't happened yet,
  with the card's own counts and never who chose what.

Each item already shows to the member in that same place through `reminder_list`, `work_list` or the
posted card, so nothing reaches a new audience. A kind the conversation can never show is null, so
an empty list there doesn't read as "none". Memory off sends none. Fri uses them when the member asks
what's coming up or about one of them, and before preparing a duplicate, and doesn't bring them up
unprompted. An older controller sends no `loops`, which the model reads as unavailable.
`python -m fri.fri_eval --suite loops` checks these answers with the real model and a rubric judge,
offering the DM cases the reminder tools a DM offers, and fails any tool call or draft:
an existing reminder isn't set again, pending, `will_not_send` and uncertain aren't promised, a
game night's time comes from the card without naming who's going, banter leaves loops alone, and
reminders outside DMs aren't called none.

## Member profiles

With a controller that advertises `member-profiles`, Fri asks for profiles on its context and memory
requests, and each member has one profile per server: up to 15 small preferences (three per
category) that follow them into every conversation in that server, DMs included. Fri decides where a
note belongs when the turn is saved. Only a channel the server's default role can view counts, with a
thread judged by its parent, and only public or news threads. Age-restricted channels never count,
and neither does anything Fri can't resolve. Right before each model request Fri also reads the
channel from Discord (fetched overwrites and roles, the ping's own check for a mention), so an
overwrite the cache hasn't seen yet keeps the turn in that channel too. The same read confirms that a
configured-channel message's author can still read the channel; if not, the request isn't sent. Notes from DMs, the `/fri` panel, restricted channels,
private threads and outside invitations stay in that one conversation, and the controller refuses a
profile write from a private conversation. Notes saved before this release stay where they were said.
Background learning still keeps per channel.

Each turn also sees what the speaker last said to Fri in the server's other readable channels: up to
two of their messages from the last 12 hours (`with_you_elsewhere_untrusted`). Only their own words
travel, never Fri's replies, because a reply can draw on a note or exchange that stays in its own
conversation. Fri re-checks that each channel is still readable by everyone right before each model
request and again before posting the reply; if one closed meanwhile, the reply stays unsaid. The turn
inherits their provenance, since a member's words can repeat someone else's: an erasure removes what
was built on them here too. Every model request is signed in first and then runs the delivery privacy
check (for an outside @mention, its invitation check), so the words of someone who erased or changed
their privacy while Fri gathered context are never sent.
"What do you remember about me?" in public lists only what that channel can see. In a DM it lists
every note with where it shows, and My memory there edits any of them; the panel shows the first 25.
An edited note keeps its place: the new wording shows wherever the note did, as its label says.
Editing a profile note clears the member's saved chats and research receipts across the server, removes
the same words, or an excerpt containing or contained in them, wherever a single channel kept them
(under any category), and stops background learning from using anything the member said before the
edit, anywhere in the server: no note, archive observation or channel summary comes from those
messages, however it's phrased. A message sent before the edit but answered after it gets its reply
and keeps nothing: Fri's short-lived channel chatter and reply echoes skip it too. Whether a message
counts as said in front of everyone is decided when Fri first sees it, so a channel opened while Fri
replies can't widen it; a channel closed meanwhile narrows it: its excerpts drop out right before each
model call, and the turn is saved as staying in its channel. "Forget me" is unchanged and erases everything. At
startup Fri logs how many configured channels are readable by everyone. If that's none, profiles
never fill, and the server's channel permissions are the thing to look at.

Without the capability (an older controller), Fri sends no audience and doesn't ask for profiles,
and memory stays per conversation as before.

## Member summaries

With a controller that advertises `member-summaries` (and profiles), Fri keeps one short summary per
member of what they talked about with it lately, so a topic that fell out of the six recent exchanges
can come back. It is Fri's own text, the only model-written thing the controller keeps about a member,
and the reply model sees it as `your_summary_of_speaker_untrusted`: Fri's earlier note, never the
member's words or instructions. Fri adds it only to the member's own turns, in their DMs or /fri panel
and in channels everyone in the server can read, and only while that channel and every channel it came
from still are (checked again right before each model call and before delivery). A reply built on it
inherits its provenance, so it goes when anyone the summary drew on erases. In a public channel, a reply
that had a summary from another channel in view is saved as a neutral breadcrumb and kept out of the live
view, like a read of another channel; the member still gets the answer itself.

The `member_summaries` heartbeat stage builds them while background work and learning are on. It
claims one member at a time once the model slot is free and Fri is signed in
(`/agent/member-summary/claim`): their own recent messages from channels everyone could read, never
Fri's replies, DMs, the panel or restricted channels, plus the earlier summary. Fri drops anything from a
channel that isn't readable by everyone now, sends the rest to the summary model (`summary_model` /
`summary_effort`, Luna at medium by default) and reports the result, recorded as `summary` usage. The
controller allows one call per member a day and 50 a day in all, and keeps the result only if nobody's
privacy changed since the claim. A summary lasts 30 days after its last update. "What do you remember"
shows it in a DM and, in a public channel everyone can read, only says one exists. My memory shows it
with Edit summary and Clear summary; either clears the member's saved chats across the server, like a
server-wide note edit. "Forget me", memory off and Forget my chats wipe it.

Without the capability (an older controller), Fri doesn't ask for summaries, runs no builder and
adds nothing to the model's input.

## Reply checks

Needs a controller that advertises the optional `reply-checks` capability at startup; against an
older one Fri still starts and simply never claims a batch. If the controller refuses a claim while
Fri runs (it was rolled back underneath), Fri stops asking until it restarts. Off until the owner
applies `reply_check_enabled` through the usual settings preview (“enable reply checks”). The
controller's operations guide describes the settings, retention, export and rollback purge.

On the heartbeat, Fri waits for its background model slot and signs in to the subscription, then
claims at most one batch of up to 20 pairs and sends it straight away, so nothing erased before the
claim is ever sent. Each pair is Fri's verbatim reply, then the same member's next turn in that
conversation within 15 minutes.
Pairs carry no member, channel or message identifiers, and Discord mentions, links and ids in the text
arrive as neutral placeholders. Luna/low (configurable) flags only clear cases
of a member being unhappy with the reply and returns four labels from fixed lists: category, severity,
topic and suggested fix. There is no free-text field. Banter and roasting Fri back don't count. Usage
is recorded as attention with a `reply_check` purpose: this controller reports it as “reply checks”,
and an older one ignores the purpose and counts attention, so a receipt still queued when both sides
roll back is delivered rather than blocking the queue. The receipt is saved and left to the usage loop,
so a slow backlog never delays finishing the batch. Usage delivery and owner notifications retry
independently. Nothing is posted to Discord and no chat text is stored by Fri; the controller decides
what is kept and fences it by everyone's privacy revision.

The owner can DM “show reply checks” for the last seven days: exact counts by category and the top
recurring themes. In a server channel Fri only points the owner to DMs.

Offline checks cover the controller wire contract, privacy fences and failure retries with a
substituted model. They don't measure how well a live model separates banter from real complaints;
`python -m fri.fri_eval` has no reply-check suite yet.

## Background and owner call costs

Calls whose result can't change what members see are skipped before inference.

- **Attention.** Messages the direct path has taken when attention reads the channel (addressed
  to Fri, in the dedicated channel, or a follow-up in an active window) stay in the attention
  model's context but are never offered to it as fresh. A message Fri already answered, reacted to
  or chose to skip isn't decided twice. A message still being routed (say, the controller hasn't
  answered whether it continues a conversation) is offered as before; attention checks again that
  the direct path hasn't taken its pick before sending an ambient reply. The direct path can still
  take a message after that last check, as before this change.
- **Owner settings.** An owner line that matches the settings words first goes to a yes/no check on
  the attention model with only the owner's words, outside the background model slot, recorded as
  `owner_settings` usage. Its receipt and the Sol call's are saved for the usage loop to deliver, so
  a slow usage report never holds up the preview (a normal reply still delivers pending receipts
  first, as it always has). Only a yes (or a failed check, or one still running after 10 seconds)
  runs the Sol/high settings call, with typing shown. Anything else takes the normal reply path,
  even while another settings preview is being prepared. Requests queue as they reach the settings
  handling, before its controller reads; a request gets the busy note only if an earlier one is
  still being decided or prepared when it's ready. A line the owner erased after it arrived ("forget
  me", or a privacy change from the panel, Gene or Red, during the controller reads, the sign-in,
  the check, the model slot wait or the Sol call) gets no reply, preview or busy note and never
  reaches a model after the erasure is seen, like any waiting message. Asking what Fri remembers, or
  Fri clearing cached rows from an earlier erasure, doesn't count. While Fri is disabled the check
  is skipped, since the normal path can't answer then.
- Meme checks are unchanged: an inspection still runs while the reaction budget is closed, because
  the budget can reopen (or the owner can raise it) before the model answers.
- When a meme check reacts, Fri keeps in memory, never on disk, the emoji, the post's author ID, their
  privacy revision, the post's edit time and up to 200 characters of what the model saw (never the
  caption), for up to three days or until Fri restarts; a rollback leaves nothing behind. A read or a
  mention's nearby rows show it as `fri_reacted` on that post only while the author's revision and the
  edit time still match, and drop it when they don't. Forgetting a member drops theirs; every heartbeat
  expires old ones (before it waits on the controller) and, every ten minutes, refetches each noted
  post and drops those whose post was deleted or edited or whose author changed privacy outside Fri,
  whether or not meme checks are on. Every note's privacy is read from Gene before any post is
  refetched, so an opt-out goes whatever Discord does; a post Discord can't show right now (refused,
  rate-limited, connection failed) keeps its note until a later check. The heartbeat waits up to 10 s
  on the refetches, then moves on to its stages without cancelling them, since discord.py can't recover
  from a cancel inside its global rate-limit wait; a pass still running at the next check is left to
  finish rather than doubled, and that check's privacy reads don't wait on it.
- **Chat meme channels.** In a meme channel that's also one of Fri's chat channels (like #fri-chat),
  the meme check still judges posts with images but never a post that's only text. A line of text is
  judged only once two other members laugh at it. A laugh is a whole message from a short fixed list
  (lol, lmao, lmfao, haha, ㅋㅋ, "im dead", "crying", 😂 🤣 💀 😭). It counts for the newest message before
  it that isn't a laugh too, within the next 8 messages in Fri's live view and 10 minutes. A laughing
  reply counts for the line it replies to, anywhere in those 10 minutes. The author's own laughs,
  bots, webhooks and members with memory off don't count, and a laugher who changes their privacy
  or leaves the server before the check is taken back out. The second laugh starts the check right
  away, in the background. The line is judged as a meme post is: same model, quiet by default, same fences on its
  author, edits and deletes, receipts and kept notes. The model also gets up to 5 lines before it from
  Fri's privacy-fenced live view as untrusted context, but not who laughed. These checks share the
  daily check and reaction budgets and the hourly gap between reactions. Unlike a meme check, one
  doesn't call the model while no reaction could follow: that's checked before it reads anything and
  again right before the model call, when every line it would show must also still be in the live view
  as it was, from authors who still allow it. One runs at a time; a laugh while one runs starts nothing,
  though a later member's laugh can. Each line is considered once, and Fri only reacts, never posts.

## Deployment boundary

The companion runs in its own `fri` Compose project using `fri-runtime:<full Fri SHA>`.
It connects to Gene's private Docker network and exposes no public ports. It receives
only its Discord token, existing Fri controller credential and subscription directory.
There is no Docker socket, source checkout, SSH key, GitHub credential or controller
SQLite mount inside the companion.

The trusted release coordinator is `gene.fri_release` in the Gene repository. The
copy of `ops/compose.yml` here documents the runtime; the coordinator uses its own
reviewed `Gene/ops/fri-compose.yml`. Fri's feature code cannot select service mounts.
The Gene feature deployer still activates only Red and does not read Fri release pins.

## Watching Gene and the controller

Fri can't restart anything. It only notices and says so.

- **Owner DM per outage.** Fri's own watch task reads `/controls/health` every 2 minutes, with
  no model call, apart from the agent heartbeat and owner DM loop. It's a plain asyncio task
  timed on the event loop's monotonic clock, not a `tasks.loop`, which times its sleeps by the
  wall clock: a host clock set back an hour mid-pass would have stalled it for an hour. Once Fri
  has read in this process, the spacing between reads and the bad-read streak also follow the
  monotonic clock, so a stepped wall clock can't skip a read or break a streak. It doesn't wait
  for the gateway: reads and the DM go over Discord's HTTP API. After 3 bad reads in a row it
  DMs the owner once. A bad read is one of: Gene's heartbeat missing or at least 60 s old, Gene
  not connected to Discord, Gene's command runner stopped, the controller unreachable (transport
  error, 5xx, or no answer in 10 s), or the controller rejecting the read (4xx, such as a contract
  mismatch). A stale command poll alone is a busy Gene, not an outage. Recovery edits the same DM
  with the outage length. A new outage within 30 minutes of the last DM edits that DM again instead
  of sending another. State lives in the private `fri-watch.json` in the data directory, so a
  restart mid-outage neither repeats nor forgets the DM. Fri saves what the DM should say before
  it touches Discord. Each pass waits up to 30 s for the delivery, then moves on, so a slow send
  never delays the next read. No Discord call is ever cancelled, because discord.py can't recover
  from a cancel inside its global rate-limit wait and every later Discord call would stall. A
  delivery still running is left to finish (aiohttp's 5-minute request timeout still ends a hung
  call), and no second one starts beside it. A send or edit that fails is retried on the next
  pass, even after a restart. A send that may have landed is looked up in the DM before it's sent
  again under the same nonce. Every DM from a minute before the attempt to a minute after that
  send settled (or after Fri restarted, if it died mid-send) is checked, and a lookup that fails
  proves nothing, so it's retried. A relapse keeps a DM whose delivery is still unknown rather than
  starting a second one. The 30-minute relapse window starts when Discord made the DM (not when its reply reached Fri), and a
  DM whose last edit hasn't landed still reads as down, so it carries the next outage too. An edit
  whose reply is lost counts as not landed, so it's redone, even back to the text shown before. Only Fri or Discord can delete Fri's message in a DM. If it's gone
  when Fri goes to edit it mid-outage, Fri sends it again. Once the outage is over, nothing is sent
  late. If what the DM should say changed while Discord was taking it (Gene recovered while a slow
  send was underway, say), the same delivery edits it to the new text at once instead of leaving
  it for the next pass.
- **Startup with the controller down.** Fri retries its startup controller reads (`/fri/contract`,
  then `/agent/state`) every 10 s before it joins the gateway, instead of exiting. Discord's HTTP API already works then, so the same watch reads,
  once per 2 minutes, still DM the owner. Reads stay about 2 minutes apart across that wait, the
  loop's first pass and restarts, so a restart never shortens the 3-read debounce: the loop's
  first pass waits until an interval after the last read, rather than skipping a read. Across a
  restart only the saved wall time dates the last read, so a last read dated in the future, or a
  clock that went back during the first read itself, starts a fresh streak instead of muting the
  watch or counting toward an alert. Members see Fri offline until the controller answers, and
  `fri health` waits too. A rejected or incompatible contract still stops Fri at once, and so does
  a success that isn't JSON (a wrong URL or a proxy's page), since waiting won't fix it.
- **Quick controls.** Before charging the operations allowance, a quick control checks Gene's
  command catalog, which lists only what Gene polled for in the last 20 s. If the command isn't
  there, the member gets a fixed "gene isn't taking commands right now" at once, plus "owner
  already knows" while the owner's DM shows an open Gene outage (not while a relapse's edit is
  still pending on a DM that says Gene is back), and nothing is queued. When Gene is polling but
  Red has that one command disabled, the notice says Gene isn't offering it. A command Gene accepts and
  then hangs on still waits out the controller's 115 s; that needs a controller change.
- **Controller outage notice.** A message Fri would answer (a member DM, or a message addressed
  to Fri in a configured channel) whose controller read fails, before or while replying, gets one
  fixed notice per channel per 5 minutes instead of silence. A mention Fri already admitted only
  needs its Discord-side checks for this notice, since the notice carries no evidence. The notice
  stands in for the whole burst, so every earlier message in it must also be as it arrived, and
  each @mention among them still gets those checks, even when the reply answers a later message.
  discord.py edits its cached copy in place, so only a fresh fetch shows an edit. A privacy
  recheck that can't reach the controller leads to the notice, not silence, and so does the access
  recheck before a reply drawn from recent Gene receipts (it used to say "my access changed
  mid-check"). Rechecks after a tool ran keep their receipt reply. Every notice first
  fetches the message as Discord has it now: it must still exist and never have been edited, since
  discord.py updates its cached copy in place. Before a reply has started, a channel message must
  also still be addressed, and an @mention must pass its channel and access checks with Fri
  enabled. A posted notice counts as Fri's answer to a ping. A notice that
  fails to post, for any reason, doesn't use up those 5 minutes, and they count from when Discord
  took the notice, not from when Fri started sending it (a slow send under a rate limit would
  otherwise leave a second notice due moments after the first appeared). Ambient chimes stay quiet, and so
  does a message Fri already answered. Mentions outside configured channels keep their own gates
  and stay silent. The same text replaces the old "workshop controller is
  temporarily unavailable" wherever that error was shown, and a controller 5xx's detail goes to
  the log only.
- **`fri health`.** Owner DM only; in a server channel Fri says to DM. It works while the
  controller is down and records no turn. It shows Fri's release, Discord connection, heartbeat
  status and age, loop states, waiting usage receipts, live `/status` (Gene's deployed release, and
  whether deploys are paused) and `/controls/health`
  (4 s each, failures shown as "unreachable" or "rejected"), and the watch state.

## When Gene gets fixed

- **Gene errors Fri files.** When attention files a Gene error (an automatic repair check, or an
  owner-review request), Fri puts a 👀 on Gene's message and watches that job in the private
  `gene-fixes.json` in the data directory. It holds only the job, channel and message ids. Each
  error is watched once, for 3 days from when Gene posted it: filing the same error again through
  the other path opens a second job, and that one isn't watched. The `fixes` heartbeat stage reads
  each watched job. Once the job is deployed and its commit is still the live release, Fri replies
  to Gene's message with one dry line, but only while proactive posts are on, the channel is still
  configured and sendable (an archived thread isn't reopened), nobody spoke there in the last
  60 s, and Gene's message still exists. The release and these gates are checked again after
  Discord returns Gene's message, so a rollback or a config change meanwhile still stops it. While
  the controller reports Gene's runtime as unknown (`runtime_unknown`: a failed restore never proved
  healthy, so the live release may not be running), the line waits, and goes out if a maintainer or
  a healthy release makes it known again within the 3 days. An older controller doesn't report it. A job
  that's rejected, failed or needs attention, and one a rollback or newer release beat to it, just
  stop being watched. A hand-off to the owner isn't announced, and neither are failures. The job is
  marked done before the send, so a line Discord didn't confirm is never sent twice. It goes
  through the same non-retrying send as one-off posts. A heartbeat waits up to 10 s on a pass and
  never cancels one stuck in a Discord rate limit: the stage reports `TimeoutError`, and the next
  heartbeat waits on that same pass.
- **Tell reporter.** The owner's deployed DM for a repair a member reported (including one the owner
  finished after its checks handed it over) gets a **Tell reporter** button, when the controller
  offers `requester-notices` and the report is still on record. A tap first reads Discord: Fri must
  be on with proactive posts on, the member still in the server, and the report still there and
  still theirs, in a configured channel Fri can post in and not an archived thread, or in their DM
  while support DMs are on. A thread Fri had to fetch is read again after the report, since no
  archive update reaches an uncached copy, and the member is checked last of all. Fri then checks
  the live release itself, and that no other release is going out (an older controller checks
  neither), and claims the controller's mark, which refuses an erased report, a rollback
  since the repair, any newer release now live, or another release still going out (and, from
  gene #64, an unknown runtime). With the claim
  held it reads the live release again (a newer one, a rollback or one going out frees the claim and
  stops the reply) and fetches a thread it had to fetch once more, then checks the config and channel
  with nothing awaited before the send. Controller data of the wrong
  shape, or a thread whose channel was deleted, gets the owner a plain "didn't send it" and frees
  any claim. So a change while Discord answered stops
  the reply. It replies to the report once with a fixed line, mentions off and no request text, and
  records the outcome as the owner. A send Discord refused (a 4xx, rate limits included) can be
  tapped again. One that may have posted is never retried. The owner sees the outcome privately, and
  the button goes away once the reply is sent.
- **The 👀.** Filing a Gene error waits up to 5 s on the reaction, then lets attention move on. A
  reaction stuck in a Discord rate limit finishes on its own and is never cancelled. A 👀 a shutdown
  cut off is added by the next pass while the watch is open; one Discord refused is not retried.

## Existing installation migration

1. Deploy a reviewed Gene controller supporting `/fri/contract` and `gene-fri/1`.
   Keep the old companion image/configuration available. The compatibility companion
   in that Gene release also has the shared data-directory singleton lock.
2. Build this repository's reviewed commit with:

   ```sh
   docker build -f ops/Dockerfile --build-arg FRI_RELEASE_SHA=<full-reviewed-sha> \
     -t fri-runtime:<full-reviewed-sha> .
   ```

   The image makes its root-owned checked-in `/app/src` readable and traversable
   by UID `10001`, including when the source archive was extracted with restrictive
   permissions. CI builds from owner-only source and imports the image as that UID
   without network access. Auth and state mounts retain their separate permissions.

3. Create `/srv/fri/install.json`, owned by the trusted maintainer and mode `0600`.
   On the current installation the values are:

   ```json
   {
     "env_file": "/srv/gene/secrets/fri.env",
     "data_dir": "/srv/gene/data/fri",
     "auth_dir": "/srv/gene/secrets/fri-chatgpt",
     "network": "gene_default",
     "controller_url": "http://controller:8787",
     "legacy_container": "gene-fri-1"
   }
   ```

4. After independent exact-commit review, run the trusted coordinator as the
   maintainer on the host:

   ```sh
   sudo /srv/gene/venv/bin/python -m gene.fri_release <full-reviewed-sha>
   ```

   Ensure that host environment contains the reviewed Gene coordinator version.
   A source-based invocation can instead set `PYTHONPATH=/srv/gene/source/src`.
   The command verifies the image's revision label, stops the old singleton and
   disables its restart policy, snapshots data, starts only Fri, checks fresh health,
   then records the Fri SHA in the controller audit and `/srv/fri/release.json`.

The complete data directory stays in place: `usage/`, `post-receipts/`,
`reminder-receipts/`, status/meme state and health files. Keep the complete writable
subscription directory and lock inode, including session, registration and host-ID
records. Never make independent active copies of a rotating refresh token.

The same application, owner, guild, channel allowlists, model settings, preferences,
pending reviews and persistent button IDs are preserved. Controller database tables
are not migrated. Pure client validation supplements the controller's authorization;
it does not replace it.

## Rollback and future releases

`/srv/fri/release.env` is separate from Gene's `release.env`. A later reviewed Fri
commit uses the same coordinator. To roll back an independent Fri release, run it
with the previously retained, compatible full Fri SHA/image.

Before rolling the Gene controller back past member profiles, stop it and run
`python -m gene.profile_downgrade <controller database>` in the controller's environment. That
removes profile notes, copies their removals to every configured channel so an older controller saves
no note from the member's earlier messages, and keeps every conversation's own notes and chats. It
refuses to run when it can't tell the channels. It never migrates the database, so going below both member
profiles and turn provenance, it and `gene.provenance_downgrade` can run in either order. A Fri-only rollback needs nothing: an older Fri sends no audience
and doesn't ask for profiles, so the controller gives it only each conversation's own notes, within the
limits it was built for. Profile notes stay stored, unseen, until a newer Fri returns or they expire, and
"forget me" still erases them.

Before rolling the controller back past member summaries, stop it and run
`python -m gene.fri_member_summary purge <controller database>` (`batch-release.sh` records and runs it
as a downgrade). A Fri-only rollback needs nothing: an older Fri doesn't ask for summaries, so it never
gets one and builds none. Stored summaries stay unseen until a newer Fri returns, they expire after 30
days, or "forget me", memory off or Forget my chats removes them; run the purge too if they should go now.

Rollback changes the image/configuration against **current** data. It never restores
an old controller database, reminder/post receipt, erased memory or subscription
session. Data snapshots are disaster-recovery evidence, not automatic rollback inputs.
Failed initial cutover restarts the preserved legacy singleton. Failed later releases
restore the previous independent image. A failed recovery requires operator attention.

Do not start the old Gene companion profile after migration. The shared data-directory
lock prevents overlapping processes, but repeatedly restarting a retired service is
still an operational error. Do not remove the legacy image/container until a later,
explicitly reviewed retirement step.

## Release patch notes

After a release, Fri drafts that release's patch notes from the maintainer's facts and posts them in
the server's #fri-chat, for everyone there. The owner's DM stays for owner-only notes (no release
today, a rollback).

```sh
docker exec -i fri-runtime-1 python -m fri.fri_patch_notes draft 0.7.0 < facts.md > notes.md
docker exec -i fri-runtime-1 python -m fri.fri_patch_notes preview 0.7.0 < notes.md  # no send
docker exec -i fri-runtime-1 python -m fri.fri_patch_notes send 0.7.0 --channel fri-chat < notes.md
docker exec -i fri-runtime-1 python -m fri.fri_patch_notes pin 0.7.0 --channel fri-chat  # the pin alone
docker exec -i fri-runtime-1 python -m fri.fri_patch_notes send 0.7.0 < note.md  # the owner's DM
```

`draft` runs one model call with Fri's voice, the owner's voice notes and the controller's chat model:
talk to the server, call the owner mint or bossman, use only the given facts, and aim for one message
(under 1,500 characters) with only the highlights, since the full changelog is attached; a draft that
needs more than one message is refused. It prints
the post and sends nothing; its usage is reported like an eval's. The maintainer checks every line
against the facts before sending, and edits the file if a line isn't backed by them.

`send --channel <name>` posts in the one text channel of `GENE_GUILD_ID` with that name (refused if
there are none or several); without `--channel` the recipient is `GENE_OWNER_ID`. A channel post is one
message (owner request, 2026-10-27): the notes file holds that short summary, then a `<!-- changelog -->`
line, then the full changelog, which goes out attached to the same message as `fri-<version>-changelog.md`.
A summary that needs two messages, or a new channel post without a changelog, is refused before login; a
changelog in the owner's DM is refused too. The version's `sha256` stays the summary's digest, as older
code computes it, so a rollback can still finish an unconfirmed post from its summary; the changelog's hash
is kept beside it, so the same summary with another changelog counts as different notes, a retry that
might resend refuses to go without the changelog, and recovery only takes a message that carries the file. A version posted before this (1.0.0) keeps working without one. There is no way to
name anyone else. It logs in over REST only, so the running Fri's gateway session is untouched, and
sends with mentions and link embeds off. Notes are split at blank lines into at most four messages of
2000 characters, counted the way Discord counts them (UTF-16 units, so most emoji count 2).
`/data/patch-notes-server.json` (the channel) and `/data/patch-notes.json` (the DM) record each
version's confirmed parts, so a rerun finishes a partial send and never repeats a part: a part whose
send was attempted but never confirmed is looked for there first (Fri's own messages since the attempt
with that text, ignoring leading and trailing whitespace, skipping any the record already gives
another part), and every part carries a
fixed nonce. If two versions each have an unconfirmed part with the same text once its ends are
trimmed (kept per version under `trimmed`), the history can't say whose a message is, so both are refused
until someone checks and fixes the record by hand; an unconfirmed part older code recorded, without
trimmed text, counts as possibly the same.

Once every part of a channel post is confirmed, Fri pins its first part in that channel (owner request,
2026-10-27); the DM is never pinned. The record keeps the pinned message's id under `pinned`, so a rerun
after a failed pin only pins and never posts again, and a message already pinned counts. Every rerun asks
Discord rather than trusting the record, and pins again a post someone unpinned. `pin <version>
--channel <name>` pins a version already posted, from the record alone and without the notes. Fri never
unpins, deletes or reposts anything: missing Pin Messages or Read Message History, Discord's 250-pin
limit (error 30003), a deleted post or a recreated channel leave the post as it is and are reported.
Pinning uses `PUT /channels/{id}/messages/pins/{message}` (discord.py 2.7's `Message.pin`).

Exit codes: 0 sent (or already sent) or drafted; 2 refused: the same version with different notes
(send a correction as a new version), notes that don't fit, a damaged record, a missing or doubled
channel, or missing settings; 1 failed: Discord or the model refused, answered badly or the connection
dropped. Every confirmed part is on record, so rerunning the same command after a 1 is safe; 3 posted
but not pinned, with Discord's reason (the post stands, and a rerun only tries the pin).

## Background work and health

The 30-second heartbeat fetches controller state first, and defers the whole tick
when that fails. Every later stage (presence, posts, reminders, owner reports,
learning, attention, gene fixes, memes, reply checks, member summaries) runs on its own: a failure is logged with
the stage and exception class only, and never skips the stages after it. `fri-health.json` keeps
`agent_heartbeat.running/failed/status` for the release coordinator and adds
`agent_heartbeat.stages`, one `{at, last_ok, error}` entry per enabled stage.

Attention waits up to 5 s on its own Discord work, then moves on and never cancels it, since
discord.py can't recover from a cancel inside its global rate-limit wait. That covers an ambient
reply (reading its pick, the reply itself) and, before an automatic repair, the read of Gene's error.
An ambient reply still going holds back its channel's next attention pass until it finishes. A
Gene error whose read is still going is filed for the owner's review instead of as an automatic
repair.

A usage receipt the controller refuses as invalid (HTTP 400 or 422), or one that
can't be parsed, moves to `usage/rejected/` and stops blocking later receipts.
`rejected_usage` in the health file counts them. Credential or contract refusals
keep every receipt queued.

The owner's usage report ("fri token usage", "show usage by model") is cumulative unless
the ask names a rolling window: "last 24h", "past 3 days", "last week", "this month" and the
like, up to 999 of a unit. "today" means the last 24 hours and "this week" the last 7 days,
not calendar periods, and the reply names the exact window. Fri sends the window as `since`
only to a controller that offers `usage-window`, and names it only when every row the
controller returns carries that `since`. An older controller, including one rolled back
while Fri kept running, gets or returns a plain report, and the reply says it's all-time. Attempts count by when the controller recorded
them, so a receipt held through an outage lands in the window it arrives in.

A failed subscription renewal logs its cause: exception class, HTTP status and OAuth
error code, never the response or tokens. Only a refused grant (HTTP 400/401), a
renewal without the plan scope, or a missing or incomplete saved sign-in is reported
as needing a reconnect. Each refusal sends the owner one DM; a token that works in
between, even briefly, makes the next refusal a new one.
Network, rate-limit, provider and disk failures, including a malformed renewal
response, ask members to retry. A renewal that can't be saved still keeps a rotated
refresh token. A controller 4xx whose JSON body can't be read keeps its status. A 4xx that
isn't JSON at all, such as a proxy's HTML or text page, or whose body is cut off or stalls (5 s), is
still a refusal (startup stops, and the watch reports a rejection, not an outage), but with no
status, so no receipt is set aside.

## New installations

Install a compatible Gene controller first. Create a private Fri environment file
from `.env.example` and directories owned by runtime UID/GID `10001:10001`. Obtain
subscription authorization on a trusted machine only if supported for the account;
transfer the entire identity/session bundle securely. Set `legacy_container` to an
empty string in the installation JSON when no earlier companion exists. Adapt all
paths/network names to the installation; do not expose controller access publicly.

For a **new subscription connection**, run `uv run --no-sync python
scripts/connect_chatgpt.py` on the trusted workstation. Its local outputs map to the
private host directory as follows:

| Workstation file | Host file inside `auth_dir` |
| --- | --- |
| `.secrets/fri-chatgpt.json` | `session.json` (the runtime reads this exact name) |
| `.secrets/fri-chatgpt-registration.json` | `fri-chatgpt-registration.json` |
| `.secrets/fri-hostinger-id.json` | `fri-hostinger-id.json` |

Transfer these over authenticated SSH, set the directory to `0700`, files to `0600`,
and ownership to `10001:10001`. Keep the registration and host identity for future
reconnection. When reconnecting, stop only Fri, securely copy the **current** host
session/identity bundle back to the corresponding workstation filenames, and reuse
those identities during login. Install the verified replacement atomically as
`session.json`, retain its existing lock inode, then restart Fri and check health.
Do not run two processes against copies of the same rotating session or replace a
working connection when login fails. This procedure is unnecessary for the repository
migration: the existing auth directory is mounted in place.

Start with `FRI_PRESENCE=invisible` in a test server. Configure read, write, learning,
meme and Lab channel scope in the Gene controller, then explicitly enable behavior
and visibility when ready. The bot's Discord permissions alone do not enable access.

## Integration checks

Maintainers and review agents run the same script:

```sh
scripts/offline-checks.sh setup   # once per paired Gene commit; the only networked step
scripts/offline-checks.sh         # credential-free, network-denied checks
```

`setup` reads `verified_controller_commit` from the integration pairing file. It extracts that
exact Gene commit from a local Gene repository with `git archive`, and builds a dedicated venv
from that commit's `uv.lock` (`uv sync --frozen --extra red`). Both live under
`~/.cache/fri-offline/<gene-sha>/`, so an unrelated Gene branch or venv is never borrowed. Gene
and Red are **test-only** dependencies; the Fri wheel and runtime image contain neither.

`check` runs with a cleared environment (no tokens, temporary home/config):

- Fri's `tests`;
- `integration/compat_tests` and `integration/replay` against the pinned controller;
- `ruff`.

The guarantee comes from an OS layer. On macOS, `sandbox-exec` denies outbound IP and the
`mDNSResponder` resolver socket; an IP-only rule still let names resolve. On Linux, `unshare -rn`
does the same.

A Python guard adds a second layer in each Python process:

- an audit hook denies non-loopback connect/sendto/sendmsg and the socket lookup functions;
- wrappers on `socket.socket.connect`, `connect_ex`, `sendto`, `sendmsg` and `bind` reject
  hostnames before CPython resolves them in C.

The guard covers only Python's socket layer. Non-Python subprocesses and native code, such as
uvloop's resolver, depend on an OS layer. OS-level DNS denial was verified manually: `example.com`
resolved under the IP-only profile and failed with the resolver socket denied. It has no per-run
canary, because proving it needs a real lookup.

If no OS layer can be applied here (for example nested inside a Codex reviewer's sandbox), the
script refuses to run unless `OFFLINE_ISOLATION=python` explicitly accepts the guard alone as best
effort, relying on the outer sandbox.

Every run first proves each active layer with canaries. These target RFC 5737/6761 test
addresses, and a backstop hook stops any probe the guard misses before it sends traffic. The run
prints the isolation it used and exits non-zero on any failure. No Python starts with the
caller's environment before `check` clears it.

Overrides:

- `GENE_SHA`: another controller commit, such as `minimum_controller_commit`;
- `GENE_REPO`: Gene repository path (default `../gene`);
- `FRI_OFFLINE_CACHE`: cache location;
- `FRI_VENV`: Fri venv, for a fresh review checkout without `.venv`;
- `OFFLINE_ISOLATION=python`: accept the Python guard alone (best effort) inside an outer network-denied sandbox.

The equivalent manual command for the integration suites is
`PYTHONPATH="$PWD/src:$PWD/integration:<gene>/src:<gene>" <gene venv>/bin/python -m pytest -q
-p no:cacheprovider integration/compat_tests integration/replay`.

### Replay harness

`integration/replay/` drives synthetic multi-message scenarios through the unmodified
`Fri.on_message` pipeline and the real Gene controller in-process. It fakes Discord, the
controller transport (an aiohttp-shaped adapter, so `Fri.api` runs unchanged) and the provider
transport. A virtual wall clock controls message IDs, follow-up windows and controller TTLs:
`time.time()` and every loaded `fri`/`gene` module's `dt.datetime.now()` follow it, while
monotonic time stays real for asyncio. `Inspect` steps can record state mid-run.

Each step records:

- the observed route: local intercept, admission, model, tools, outcome, and how the turn was
  stored;
- a context manifest: field sizes, offered actions, prompt version, model and effort, with no raw
  member text; `input_chars` counts the characters of text the model reads, not the request's own
  JSON escaping;
- token usage.

Production source is unchanged. Routes come from observable effects plus a pass-through spy on
`Actions.execute`, so tools that return early (for example a clarification) or are cancelled by
the turn deadline are traced too. All
scenario members and messages are invented.

Answer checks require completed inference, taken from Fri's own usage receipts. A provider
error notice is never counted as an answer. A turn counts as stored only if it's read back from
the synthetic store by its source event. A run is unsuccessful, and the CLI exits non-zero, if
it has:

- a controller 5xx;
- a controller 4xx the scenario didn't declare in `expected_rejections`;
- a rejected save;
- a failed inference;
- unused scripted responses;
- a scenario skipped because the call budget ran out;
- a failed ordinary check.

pytest and the CLI share this single rule (`failures()`). Checks run before real time is restored.
Live transports are created and closed per scenario from a factory, and subscription expiry
bookkeeping stays on real time.

`known_gap` checks run as strict xfails. They count only when the final gap assertion raises
`KnownGapPresent`, after every other precondition has passed. Gap checks inspect stored turns by
source event, not by model wording. Only the exact neutral placeholders written by Fri and the
controller count as non-derived storage. A gap check that errors fails the run. When a fix makes one pass, the suite
fails until the marker is removed, so a documented gap can't silently disappear or come back.

Print reports offline with `python -m replay` (run from `integration/` with the same
`PYTHONPATH`). `--live-model --auth SESSION --max-calls N` runs the same scenarios against the
real provider with a hard call budget, and is never part of offline checks. It needs network access
and a subscription session dedicated to evaluation, because refreshes rewrite the session. Never
use the production session or a copy of its rotating refresh token. Nothing is posted to Discord,
and live controller state is never touched.

The maintainer runs this inside an OS network-denied environment or disposable
container without credential mounts. Ordinary runtime tests need only Fri's own
`uv sync --frozen` environment. CI builds/imports the standalone image without network
access at execution time. Live model checks are opt-in and separate from offline tests.

Review source/schema changes for member identity, permission revocation, pending
music requests, privacy erasure, owner approvals and one-attempt delivery. Use fresh
independent review before release, and report untested Discord rendering/playback.
