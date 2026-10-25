# Fri

Fri is a private Discord companion: casual conversation, selective reactions,
small attributed memories, public research, inspected image sharing, reminders,
and access to Gene's existing commands. Fri uses they/them. They have their own
Discord application, Python package, dependency lock and release image.

**This first split still requires a compatible [Gene controller](https://github.com/HumanMint/gene).**
The controller holds conversation state, settings, requests, approvals and delivery
records. Fri does not install Red, import Gene, or receive deployment credentials.
Physical database separation is deliberately deferred.

## Using Fri

Say “fri”, mention them, or reply in an enabled channel. Short follow-ups retain
context; closing messages can get a reaction or silence. DMs require verified server
membership and have bounded conversation allowances. The `/fri` panel provides
private settings, memory controls and request reviews.

- Ask to play music through Gene; normal member, voice and DJ checks still apply.
- Ask what you missed. Fri reads what others said in that channel since your last
  message there (up to 100 messages or 12 hours) and gives you the short version.
- Ask for current information. Relevant requested links are included; research
  receipts support later requests for sources without exposing internal reasoning.
- Explicit Danbooru/Safebooru image requests can return an attached still image.
  Fri inspects bounded candidates for relevance and SFW suitability before posting.
- Ask what Fri remembers, edit a specific note, or ask to forget you.
- The owner can adjust voice, attention and learning with a versioned preview and
  Apply action. Read access never silently becomes posting permission.
- Reminders and one-off posts use scoped confirmation and persistent delivery
  receipts. A message never grants shell or deployment access.

Models and effort remain configurable in the existing controller settings.
Conversation uses GPT-6.1 Sol/high by default; lighter attention/learning work uses
Luna. Usage receipts are retained separately from conversation history. The existing
subscription connection is preserved; no API key is introduced by this migration.

## Setup

Use Python 3.11 and `uv`. Access to both private repositories is required for the
current controller integration.

```sh
uv sync --frozen
uv run --no-sync pytest -q
uv run --no-sync ruff check .
```

Follow [installation and operations](docs/operations.md) before connecting Discord.
Existing installations should retain the current bot token, owner/server IDs, data
and subscription directories. Do not run the setup or login scripts again just to
move repositories.

For a new installation, configure the Gene controller first, then create a separate
Discord bot application and a private environment file using `.env.example`.
Enable **Message Content Intent** and **Server Members Intent** in the portal for
member-history roster access. Fri's gateway subscribes to messages and guilds;
it does not subscribe to member/presence events. Presence Intent is unnecessary.
Grant only the channel permissions needed: view/history, send in approved channels,
embed links, attach files and add reactions where those features are enabled.
No Administrator, Manage Server or voice permission is required for Fri.

`scripts/connect_chatgpt.py` retains the existing account registration flow. Its
availability for unrelated accounts is not established; a new installer must verify
that the account can authorize this self-hosted agent. The script fails without
replacing a valid session if authorization is unavailable. Never paste tokens in chat.

## Development and compatibility

Runtime entry point: `fri` or `python -m fri.fri`. Isolated model checks use
`python -m fri.fri_eval`; these make real model/provider calls and record evaluation
usage, but do not post to Discord or create member conversation history.

`tests/` checks that the package imports without Gene or Red, contract validation,
failure categories and duplicate-process prevention. `integration/compat_tests/`
contains the extracted workflow regressions, retargeted to `fri.*`, against the real
Gene controller with synthetic databases and substituted network/Discord transports.
See [running integration checks](docs/operations.md#integration-checks). Passing checks
do not prove audible music, model judgment or Discord client rendering.

The source originated from Gene commit `7bdf6af858a5231c6e6af0e10662aa126aa4eb2a`.
[Extraction provenance](docs/extraction.json) lists original module hashes and the
small dependency cuts. Pure controller contracts are versioned as `gene-fri/1`.
Unknown controller versions stop startup before background work or Discord views;
each subsequent controller call carries the required version.

Gene remains the only release authority. Existing Gene job approvals still target
Gene, and its automatic-repair policy cannot modify this repository. Fri releases
are maintainer operations with independent images and current-state-preserving rollback.
