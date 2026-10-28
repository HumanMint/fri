# Try Fri in your own server

This runs Fri and a local [Gene controller](https://github.com/HumanMint/gene) on your machine, with
Fri answering in one channel of a Discord server you own. Gene's Red bot, music and the production
setup aren't part of it; see [What's left out](#whats-left-out).

## What you need

| You need | Why |
| --- | --- |
| `git`, and [`uv`](https://docs.astral.sh/uv/getting-started/installation/) | `uv` builds the environments and fetches Python 3.11 if you don't have it. |
| A Discord application with a bot user, just for Fri | Fri's own identity and token. |
| A test server you own, with a text channel for Fri | Fri serves one server, and you are its owner. Don't start in a real community. |
| A ChatGPT plan that can sign in Fri ([below](#4-connect-model-access)) | Fri's only model access. There is no API-key mode. |
| A Gene controller | It stores everything Fri remembers. You run it locally in step 2. |

**About model access.** Fri calls OpenAI's Responses API with a *Sign in with ChatGPT* plan token,
never an API key. `scripts/connect_chatgpt.py` registers Fri as an agent on your ChatGPT account
through a browser sign-in. That's the only supported path; whether every account or plan can
authorize a self-hosted agent this way isn't established. If yours can't, the script saves
nothing and says so. Without a sign-in Fri still logs in, reaches the controller and answers
`fri show settings`, but every reply that needs the model fails: members get a short notice, and the
owner gets one DM. Fri asks for `gpt-6.1-sol` for conversation and `gpt-6-luna` for background work,
so your plan has to offer those models.

## 1. Create Fri's Discord application

1. In the [Discord Developer Portal](https://discord.com/developers/applications), choose
   **New Application** and name it, for example "Fri (test)".
2. Under **Bot**, choose **Reset Token** and keep the token for step 3. It's a password: never paste it
   in chat, an issue or a commit.
3. Still under **Bot**, turn on **Message Content Intent**. **Server Members Intent** is only used by
   the optional history exploration (off by default); Fri never needs Presence Intent.
4. Copy the **Application ID** from **General Information**. It isn't secret; `check` turns it into an
   invite link.
5. In Discord, turn on **User Settings > Advanced > Developer Mode** so you can right-click to copy IDs.
   Copy your test server's ID, your own user ID, and the ID of the channel Fri should answer in.

If you also run Gene's Red bot, it needs a separate application of its own.

## 2. Start a local Gene controller

Clone both repositories side by side:

```sh
git clone https://github.com/HumanMint/gene.git
git clone https://github.com/HumanMint/fri.git
```

Then, in `gene/` (Gene's README has the details):

```sh
cd gene
uv sync --frozen
uv run python scripts/dev.py init     # writes gene/.env.local from Gene's template
```

`init` lists Gene's four service tokens. Run `uv run python scripts/dev.py token` once for each and paste
what it prints after that token's `=` in `gene/.env.local`. Gene's script never writes a token itself.

Put the server, owner and channel IDs from step 1 into `gene/.env.local` as `GENE_GUILD_ID`,
`GENE_OWNER_ID` and `GENE_WORKSHOP_CHANNEL_ID`, then:

```sh
uv run python scripts/dev.py check
uv run python scripts/dev.py fri-env      # prints the controller's share of Fri's settings
uv run python scripts/dev.py controller   # leave this running
```

`fri-env` prints the lines Fri and the controller must share (the three IDs and the controller's
address) for Fri's git-ignored `.env.local`, which `setup` creates in step 3. It never prints the
token: copy `GENE_FRI_TOKEN` from `gene/.env.local` yourself. The controller listens on
`http://127.0.0.1:8787` and keeps its SQLite database in `gene/.state/`.

## 3. Set up Fri

In a second terminal:

```sh
cd fri
scripts/local.sh setup
```

`setup` checks for `git` and `uv`, runs `uv sync --frozen`, creates the private `.state/fri-data` and
`.secrets` directories, then writes `.env.local` from [`.env.example`](../.env.example) with local
paths (mode `0600`). An existing `.env.local` keeps every value it has; only missing settings are
added, so paste the lines from `fri-env` before or after, as you like. It ends with a check that
lists what's still missing; it never prints a value:

```text
error  DISCORD_TOKEN             missing: Fri's bot token: Discord Developer Portal > your application > Bot > Reset Token
ok     GENE_FRI_TOKEN            set (hidden); must equal the controller's GENE_FRI_TOKEN
ok     GENE_GUILD_ID             a Discord ID; must match the controller's GENE_GUILD_ID
...
warn   FRI_CHATGPT_AUTH          no ChatGPT sign-in yet: Fri connects, but every model reply fails ...
```

Open `.env.local` in an editor and fill in the rest:

| Setting | Value |
| --- | --- |
| `DISCORD_TOKEN` | Fri's bot token from step 1. |
| `FRI_APPLICATION_ID` | Optional: the application ID, so `check` prints an invite link. |
| `GENE_FRI_TOKEN` | Copy the controller's `GENE_FRI_TOKEN` from `gene/.env.local`, at least 32 characters. |
| `GENE_GUILD_ID`, `GENE_OWNER_ID`, `GENE_WORKSHOP_CHANNEL_ID` | Printed by `fri-env`. They must equal the controller's values. |
| `GENE_CONTROLLER_URL` | `http://127.0.0.1:8787` for the local controller. |
| `FRI_PRESENCE` | `invisible` (the default) or `online`. |
| `FRI_DATA`, `FRI_CHATGPT_AUTH` | Already set to `.state/fri-data` and `.secrets/fri-chatgpt.json`. |

Optional extras: `FRI_TIMEZONE` (an IANA zone for reminders, default `America/Los_Angeles`),
`FRI_CHAT_CHANNEL_ID` (a channel where every message counts as talking to Fri), and `GENE_BOT_ID` and
`GENE_PREFIX` if Gene's Red bot shares the server. The `GENE_*` names are kept for compatibility; they
don't grant Fri anything Gene's controller doesn't.

## 4. Connect model access

```sh
uv run --no-sync python scripts/connect_chatgpt.py
```

It opens a browser page to sign in with ChatGPT and approve Fri, and waits up to 10 minutes for the
answer on a local `127.0.0.1` port. On success it saves three private files in `.secrets/`:
`fri-chatgpt.json` (the session Fri reads), `fri-chatgpt-registration.json` and
`fri-hostinger-id.json` (Fri's agent registration and host identity, kept for reconnecting). Fri
renews the session itself and rotates its refresh token, so never run two Fri processes on copies of
the same session. Fri sets no token budget: use counts against your plan's limits.

## 5. Check everything and invite Fri

With the controller still running:

```sh
scripts/local.sh check --controller
```

`--controller` reads the controller's `/fri/contract` with Fri's token, exactly as Fri's startup does:

```text
ok     controller                controller contract gene-fri/1, every required capability offered

Invite Fri to your test server (bot and slash commands, only the permissions Fri uses):
https://discord.com/oauth2/authorize?client_id=...&scope=bot%20applications.commands&permissions=274878024768&guild_id=...

.env.local looks ready. Start Fri with: scripts/local.sh run
```

Open the invite link to add Fri to your test server. Without `FRI_APPLICATION_ID`, build it yourself:
`https://discord.com/oauth2/authorize?client_id=<application ID>&scope=bot%20applications.commands&permissions=274878024768`.
Those permissions are View Channels, Send Messages, Send Messages in Threads, Embed Links, Attach Files,
Read Message History and Add Reactions; Fri needs no Administrator, Manage Server or voice permission.
Make sure Fri can see and post in the workshop channel.

## 6. Run Fri

```sh
scripts/local.sh run
```

It checks `.env.local` again, then starts Fri with those settings. Fri logs in to Discord, reads the
controller's contract (and refuses to start on an incompatible one), registers `/fri` in your server
and starts its background heartbeat. It stays invisible unless `FRI_PRESENCE=online`. Ctrl-C stops it.
`.state/fri-data/fri-health.json` shows whether it's connected.

## 7. Say hi

In the workshop channel, as the owner:

1. `fri show settings`: Fri posts its current behavior settings. It needs no model call, so it proves
   Discord, the controller and the IDs line up.
2. A fresh controller starts with Fri **switched off**: it answers only your settings requests. Say
   `fri change your settings: turn yourself on`. Fri drafts a preview with an **Apply** button (one
   model call); nothing changes until you press Apply.
3. Now talk to them: say "fri", @mention them or reply to them. Short follow-ups keep the thread going.
   `/fri` opens the private panel with settings and memory controls. "what do you remember about me?"
   and "forget me" work at any time.

Plain "fri" works in the workshop and in channels the controller lists (`FRI_CHANNEL_IDS` in Gene's
env). Elsewhere in the server, an @mention gets a single reply in a channel Fri and the member can read.

## What's left out

- **Music and Gene's commands.** Fri hands `play`, `skip`, `queue` and similar to Gene's Red bot
  through the controller. Without Red and its audio node, those requests don't play anything. Gene's
  README covers running Red locally.
- **Public web research** is a controller setting (`FRI_WEB_SEARCH_ENABLED` in Gene's env) and uses
  the same ChatGPT plan.
- **Production.** Server installs, releases and rollbacks are maintainer operations in the
  [operator runbook](operations.md) and Gene's `ops/maintainer/`. Nothing here deploys anything.

## Troubleshooting

| You see | Do this |
| --- | --- |
| `no controller answered at http://127.0.0.1:8787` | Start the controller (step 2) or fix `GENE_CONTROLLER_URL`. |
| `the controller refused GENE_FRI_TOKEN` | Copy the controller's `GENE_FRI_TOKEN` from `gene/.env.local` again, exactly. |
| `discord.errors.LoginFailure: Improper token has been passed.` | `DISCORD_TOKEN` is wrong or was reset; copy a fresh one from **Bot > Reset Token**. |
| `PrivilegedIntentsRequired` | Turn on **Message Content Intent** for Fri's bot (step 1). |
| `another fri process already owns this data directory` | A Fri is already running on this `FRI_DATA`; stop it first. |
| Fri is online but silent | Check you're in the workshop channel, that Fri can read it, and that you turned Fri on (step 7). Fri logs why a ping went unanswered: `Mention <channel>/<message> not answered: <reason>`. |
| Fri answers that they need a ChatGPT sign-in, or couldn't renew it | Run step 4 again. |
| `/fri` doesn't appear | Re-invite with the `applications.commands` scope, then restart Fri. |
