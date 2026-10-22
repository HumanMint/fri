# Fri operations

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
