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
In configured channels, local privacy/usage utilities, exact controls and accepted
operational follow-ups retain their existing admission before any social allowance.
New conversation and on-demand surrounding context still need normal chat admission;
an outside mention always keeps its separate one-event social admission.

Each invitation can use at most twelve preceding messages from the same channel in
the last thirty minutes and a direct reference up to seven days old. A catch-up in a
configured channel uses its own read instead (see "Catching up"). At most one
image post is inspected, preferring the current attachments, then the reference,
then a single nearby candidate. Multiple candidates need clarification. Existing
limits remain two non-spoiler PNG/JPEG/WebP attachments, 4 MB each, twenty million
pixels; external embeds, GIFs and videos are not inspected by this path. Captions
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
notice), `image share withdrawn`, `reply withheld: <recheck>` when a recheck failed after
composing, and an exception type such as `Forbidden`. A burst's earlier messages are
logged under the reason its last message got. A reply, a notice, a preview or a reaction
counts as an answer. Bots, webhooks and DMs aren't logged. Fri ignores a copy of an event
that Discord delivers again while the first delivery, or the burst holding it, is still
being handled. Separately, it remembers each event ID for an hour from its first arrival
(at most 10,000 IDs, oldest dropped first) and ignores copies in that time, so the first
delivery answers or explains for every copy. A ping is logged at most once while it is among the last
10,000 pings watched.

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

## Member profiles

With a controller that advertises `member-profiles`, each member has one profile per server: up to
15 small preferences (three per category) that follow them into every conversation in that server,
DMs included. Fri decides where a note belongs when the turn is saved. Only a channel the server's
default role can view counts, with a thread judged by its parent, and only public or news threads.
Age-restricted channels never count, and neither does anything Fri can't resolve. Notes from DMs,
the `/fri` panel, restricted channels, private threads and outside invitations stay in that one
conversation, and the controller refuses a profile write from a private conversation. Notes saved
before this release stay where they were said. Background learning still keeps per channel.

Each turn also sees up to two of the speaker's own exchanges from the last 12 hours in the server's
other readable channels (`with_you_elsewhere_untrusted`). Fri re-checks that each channel is still
readable by everyone before showing it, and the turn inherits their provenance, so an erasure removes
derived copies here too. "What do you remember about me?" in public lists only what that channel can
see. In a DM it lists every note with where it shows, and My memory there edits any of them. Editing
a profile note clears the member's saved chats across the server. "Forget me" is unchanged and erases
everything. At startup Fri logs how many configured channels are readable by everyone. If that's
none, profiles never fill, and the server's channel permissions are the thing to look at.

Without the capability (an older controller), Fri sends no audience and memory stays per
conversation as before.
`python -m fri.fri_eval --suite requests` checks status answers with the real model: queued and
approved haven't started their next step, building isn't done, deployed means it shipped (the
controller doesn't mark a request a later rollback reverted), withdrawn isn't a rejection, untitled,
empty or unavailable lists aren't guessed, an open request isn't drafted again, and public banter
leaves requests alone. Each reply must be a plain reply with no draft or action, then a rubric judge
on the same model grades it.

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
`python -m gene.profile_downgrade <controller database>`. That removes profile notes and keeps every
conversation's own notes and chats. A Fri-only rollback needs nothing: an older Fri sends no audience,
so new notes stay per conversation, and existing profile notes keep showing until they expire or the
member removes them.

Rollback changes the image/configuration against **current** data. It never restores
an old controller database, reminder/post receipt, erased memory or subscription
session. Data snapshots are disaster-recovery evidence, not automatic rollback inputs.
Failed initial cutover restarts the preserved legacy singleton. Failed later releases
restore the previous independent image. A failed recovery requires operator attention.

Do not start the old Gene companion profile after migration. The shared data-directory
lock prevents overlapping processes, but repeatedly restarting a retired service is
still an operational error. Do not remove the legacy image/container until a later,
explicitly reviewed retirement step.

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
  member text;
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
