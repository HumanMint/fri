# jpcall

`/jpcall` lets a member in a Discord voice channel ask Fri to phone a business
for the group. Fri speaks the configured call language, plays the call audio
(the callee and Fri) into the voice channel, posts a live original/English
transcript in the invoking channel, asks the group when it needs an answer
while politely holding the callee, and posts the outcome and a cost summary
when the call ends. Members' voices never go into the call.

jpcall is Fri-only. It does not use the Gene controller.

## Pieces

| Piece | Choice |
|---|---|
| Phone line | Twilio Programmable Voice, outbound, `<Connect><Stream>` |
| Call audio | Twilio Media Streams, bidirectional, 8 kHz mono PCMU |
| Voice | OpenAI Realtime (`gpt-realtime-2.1`), g711_ulaw in/out, tools for asking the group and ending the call |
| Callee transcript | Realtime input audio transcription with a named model, language from `JPCALL_CALL_LANGUAGE` |
| Translation, brief, outcome | OpenAI text model, configurable, low reasoning effort for live lines |
| Discord audio | `discord.py[voice]` with DAVE (`davey`), libopus, send-only |

## Layout

- `src/fri/jpcall/contract.py`: the only shared surface between the engine and the Discord side.
- Engine: Twilio REST + webhook/media server, Realtime session, hold timers, translation, usage meter.
- Discord side: `/jpcall`, brief card, voice connection and mixer, transcript pages, question prompts,
  outcome and cost summary, and a standalone dev runner.

## Rules

- Calls open with a short AI-interpreter introduction and ask whether the callee will talk.
  Fri waits for consent; a refusal ends the call with a plain decline outcome. Recording questions
  are answered truthfully: no audio is recorded, and the group gets a text transcript.
- Only allowlisted members can start calls. Numbers must be Japanese (`+81`) unless listed as test numbers.
  Emergency and premium-rate prefixes are refused.
- Fri never pays, gives card numbers, or accepts fees or cancellation terms on its own; those become questions.
- Holds are app-timed: an apology about every 15 s and a call-back fallback after the hold limit.
- Transcripts include third parties. They live only in the Discord messages; jpcall persists no call content.
  The usage ledger stores counts and costs only.
- Costs: every model call and the Twilio leg is metered. One compact final message shows
  the outcome and total; `cost details` shows component tokens/minutes and USD ephemerally,
  marking estimates and pending Twilio prices.

## Discord development

The runtime image needs libopus (for example, the `libopus0` system package on Debian).
The pinned discord.py voice extra supplies PyNaCl and DAVE support; `davey` is also pinned
to 0.1.7 explicitly: discord.py 2.7.1 does not implement davey 0.2's explicit MLS
transition API. The incompatible pair leaves encryption keys inactive even after connection
completes. See the [upstream compatibility update](https://github.com/Rapptz/discord.py/pull/10548).
Playback waits for the negotiated DAVE session to become ready before consuming audio.
PCM sources use the normal `VoiceClient.play` path: the player encodes PCM
to Opus, then `_get_voice_packet` applies DAVE encryption before transport encryption.
This path was checked against the installed discord.py 2.7.1 source; a live encrypted
voice session still needs a manual development-guild check.

Run the standalone development identity with `JPCALL_DEV_DISCORD_TOKEN` and
`JPCALL_DEV_GUILD_ID` supplied by the environment:

```sh
uv run python -m fri.jpcall.devbot --fake-engine --allow-user MEMBER_ID
```

In fake mode, repeat `--allow-user` for each member who may start a call; the default
allows nobody. The brief, transcript, questions, outcome and costs go in the channel where
`/jpcall` was invoked (including a voice channel's own chat). Fri joins the requester's voice
channel. Only the requester can confirm or cancel the brief. Anyone currently in that voice channel may answer call questions.
The bot needs guild and voice-state intents, the applications.commands scope, and view
channel, send messages and embed links permissions in the invoking channel, plus connect
and speak permissions in the test voice channel. It connects self-deafened and never receives or forwards member audio.
The fake engine scripts original/English lines, one group question, synthetic tones,
an outcome and estimated component costs followed by a sample posted phone price.
Fake mode uses no phone/model credentials and never imports engine configuration or
transports. Fri's generated speech can arrive in bursts, so the audio source buffers up
to 60 seconds for Fri and 2 seconds for the callee; barge-in clears the unplayed Fri backlog.

To use the real engine, provide its settings and `JPCALL_ALLOWED_USER_IDS` in process env,
then run `uv run python -m fri.jpcall.devbot`. No env files are loaded. Disabled configuration
exits with setting names only. The runner lazily constructs `JpcallEngine(load_config())`,
opens it before command registration, and closes it at shutdown. Real mode uses the engine's
configured allowlist; `--allow-user` is only for fake mode.

`register(tree, client, engine, guild_ids, limits=limits)` returns the Discord call
manager; call its async `close()` on shutdown. Supply the configured `Limits` explicitly;
the default is deny-all. Main Fri enables voice-state intent and opens/registers the engine
only when its configuration is enabled. `/jpcall` and `jpcall:` component/modal IDs pass the
usual channel filter into their own allowlist, requester and voice membership checks. Call
operations do not use the Gene controller. Disabled configuration keeps the existing command
and channel behavior. The isolated Discord branch also treats an absent engine configuration
module as unavailable; real mode requires the separately implemented engine modules.

The public final report has the call duration, one to three short outcome lines and a cost
total. Busy, no-answer and voicemail outcomes get a plain `call didn't connect` line. Exact
phone prices edit that same message; repeated finish events never create additional reports.
`cost details` opens the per-component/model units and USD privately, including input/output
token totals across models. Cached counts are included once, with `~` for estimates and
`pending` for unposted prices. A stable Discord nonce also deduplicates initial send retries.

EventSink admission does not await Discord HTTP. An owned worker serializes the display work,
so the engine's delivery deadline cannot cancel an accepted message before its ID is stored.
Shutdown drains display work again after engine close, before closing Discord. No call content
is written to disk by the display worker.

### Audio diagnostic

```sh
uv run python -m fri.jpcall.devbot --fake-engine --audio-check --allow-user MEMBER_ID
```

Join a voice channel, invoke `/jpcall` in the desired text chat (any number and a goal
such as `audio check`), then press `call`. This mode plays a five-second 440 Hz tone at
about -12 dBFS peak, without calling a phone/model API or asking a group question. The
script holds the source open for the 250 frames plus a short scheduling margin.

Before starting the player, the console logs a `ready` state with zero frames read.
The console then reports play calls, player state, frames read, non-silent frames, EOF reads, UDP packets sent/failed/blocked,
speaking updates, mute/suppression flags, and DAVE protocol/readiness/encryption counts.
It logs no packet contents, tokens, keys, privacy codes or channel/member identifiers. Expect
250 non-silent frames and at least 250 sent packets, a speaking-on update and DAVE encryption
successes when DAVE is negotiated. A connected voice transport alone does not establish MLS
readiness. Opus must be installed and loadable by discord.py on the development machine.

Offline tests drive the installed player and send path with a synthetic codec/socket; live
audibility, the actual codec and the server's handshake still need a development-guild check.

### Call language and brief

`JPCALL_CALL_LANGUAGE=ja` is the default; `ko` supports development calls in Korean.
Fake mode reads the same setting without importing the real engine. Transcript messages
show `fri` or `callee`, the original line, then the English translation on the next line
with an arrow; they do not name the source language.

The brief shows the short opener separately from the member's goal and share/never facts.
The goal is context for the conversation, rather than a script to read aloud. The outcome
renderer also preserves a callee's refusal to talk with an AI interpreter plainly. Discord
UI references to the bot use `fri`; spoken-name pronunciation belongs to the engine.
