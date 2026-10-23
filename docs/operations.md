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
the last thirty minutes and a direct reference up to seven days old. At most one
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

Offline synthetic integration checks cover handler replies, image provenance,
privacy/access revocation, expiry, allowances and the real controller wire contract.
They do not prove Discord delivery or live model interpretation of a particular meme.

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

Use the Gene checkout named in the integration pairing file and install its test
requirements (`uv sync --frozen --extra red`). This is a **test-only** dependency;
the Fri wheel and runtime image contain neither Gene nor Red.

Run with both source trees available, credentials removed and network access denied:

```sh
PYTHONPATH="$PWD/src:$PWD/integration:/path/to/gene/src:/path/to/gene" \
  /path/to/gene/.venv/bin/python -m pytest -q -p no:cacheprovider integration/compat_tests
```

The maintainer runs this inside an OS network-denied environment or disposable
container without credential mounts. Ordinary runtime tests need only Fri's own
`uv sync --frozen` environment. CI builds/imports the standalone image without network
access at execution time. Live model checks are opt-in and separate from offline tests.

Review source/schema changes for member identity, permission revocation, pending
music requests, privacy erasure, owner approvals and one-attempt delivery. Use fresh
independent review before release, and report untested Discord rendering/playback.
