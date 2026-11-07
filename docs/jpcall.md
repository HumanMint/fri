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
| Translation and outcome | OpenAI text model, configurable, low reasoning effort for live lines |
| Discord audio | `discord.py[voice]` with DAVE (`davey`), libopus, send-only |

## Layout

- `src/fri/jpcall/contract.py`: the only shared surface between the engine and the Discord side.
- Engine: Twilio REST + webhook/media server, Realtime session, hold timers, translation, usage meter.
- Discord side: `/jpcall`, brief card, voice connection and mixer, transcript pages, question prompts,
  outcome and cost summary, and a standalone dev runner.

## Rules

- Calls open with a short AI-interpreter introduction and ask whether the callee will talk.
  The notice includes AI processing, group listening and retained transcription.
  Callee relay/text is suppressed until explicit permission; withdrawal fences
  queued output. Permission recognition remains model inference, not a perfect detector.
  A refusal ends the call with a plain decline outcome. Recording questions
  are answered truthfully: no audio is recorded, and the group gets a text transcript.
- Only allowlisted members can start calls. Numbers must be Japanese (`+81`) unless listed as test numbers.
  Emergency and premium-rate prefixes are refused.
- Fri only gathers information. It never books, pays, gives card numbers, or accepts fees or cancellation terms; group answers cannot expand that authority.
- Holds are app-timed: at most one check-in after 45 s of callee silence, with an unresolved
  ending after the hold limit. A delivered natural response replaces the check-in;
  raw VAD noise only postpones it. No automatic callback is made.
- Transcripts include third parties. By default they live only in Discord messages; jpcall
  persists no call content. The optional development timeline below retains text when enabled.
  Audio is never recorded or persisted. The usage ledger stores counts and costs only.
- Costs: every model call and the Twilio leg is metered. One compact final message shows
  the outcome and total; `cost details` shows component tokens/minutes and USD ephemerally,
  marking estimates and pending Twilio prices.

## Discord development

The runtime image needs libopus (for example, the `libopus0` system package on Debian).
For an Apple Silicon Homebrew installation, include `/opt/homebrew/lib` in
`DYLD_FALLBACK_LIBRARY_PATH` when launching the runner so discord.py can find libopus.
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
channel. Only the requester can confirm or cancel the brief. Only the requester may approve an answer; other members may suggest facts in chat.
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
Ctrl-C and SIGTERM both unwind the client's call cleanup before the runner exits.

`register(tree, client, engine, guild_ids, limits=limits)` returns the Discord call
manager; call its async `close()` on shutdown. Supply the configured `Limits` explicitly;
the default is deny-all. Main Fri enables voice-state intent and opens/registers the engine
only when its configuration is enabled. `/jpcall` and `jpcall:` component/modal IDs pass the
usual channel filter into their own allowlist, requester and voice membership checks. Call
operations do not use the Gene controller. Disabled configuration keeps the existing command
and channel behavior.

The public final report has the call duration, a compact outcome and cost embed.
The complete outcome/conditions/reference are attached as call-result.txt;
persistent controls retain costs, not the outcome. Unknown phone termination is
shown separately from local closure. Late price updates edit only the cost embed. Busy, no-answer and voicemail outcomes get a plain `call didn't connect` line. Exact
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

### Conversation settings

The voice defaults are `JPCALL_REALTIME_MODEL=gpt-realtime-2.1`,
`JPCALL_REALTIME_VOICE=coral`, and `JPCALL_REALTIME_SPEED=1.15` (valid range 0.25–1.5).
The session receives the original member goal alongside the English plan, as data.
Fri introduces itself once, waits for permission, then asks one concrete question at a time.
Hold lines run out of band with only the short phone rules and the exact line to say.

| Setting | Default | Accepted values |
|---|---|---|
| `JPCALL_HOLD_APOLOGY_EVERY_S` | `45` | Positive integer seconds; at most one proactive check-in per question |
| `JPCALL_VAD_THRESHOLD` | `0.7` | Finite number from 0 to 1, inclusive |
| `JPCALL_VAD_SILENCE_DURATION_MS` | `600` | Positive integer milliseconds |
| `JPCALL_VAD_NOISE_REDUCTION` | `near_field` | `near_field`, `far_field`, `off` |
| `JPCALL_REALTIME_REASONING` | unset | `minimal`, `low`, `medium`, `high`, `xhigh` |

Server VAD remains enabled, with automatic responses and interruptions disabled.
Higher activation threshold and near-field filtering reduce noise triggers. Replies and
barge-in wait for a final non-echo transcript; this adds transcription latency to genuine
interruptions, but keeps raw VAD noise and playback echo from cutting off Fri. Changed numbers, negations and conditions are preserved. Exact recent repeats
remain uncertain transcript evidence rather than being deleted; bounded recovery
avoids treating ambiguous echo as a confirmed answer. Interrupted generated text
is labelled uncertain; zero-played output contributes no spoken fact. Use headphones during development calls.

`JPCALL_REALTIME_REASONING` is omitted from the session when unset, retaining the provider's
documented `low` default. Greater effort can increase latency and output tokens; it does not
change the per-token price. No fixed cost multiplier is available. The default model is unchanged.
These fields were checked against the [GA client-event schema](https://developers.openai.com/api/reference/resources/realtime/client-events),
[VAD guide](https://developers.openai.com/api/docs/guides/realtime-vad), and
[Realtime 2.1 model documentation](https://developers.openai.com/api/docs/models/gpt-realtime-2.1).

### Private development timeline

`JPCALL_DEBUG_TIMELINE_PATH` is unset by default. Setting it to an operator-chosen JSONL
file enables development-only text diagnostics. Parent directories are created as needed;
the file is appended with mode `0600`, and symlinks at the file path are refused.
Each call has an anonymous correlation ID, UTC timestamps and elapsed seconds. Multiple
calls can append to the same file. Unsetting it disables writes for new sessions.

Events include VAD starts/stops, response creation/completion/cancellation, truncation with
played milliseconds, tool arguments/results, final transcripts, echo drops, and hold
start/check-in/answer. No audio bytes or API credentials are written. This file contains
private conversation text, including dropped echoes; keep it outside the checkout, restrict
access and delete it after diagnosis. It has no automatic retention policy. It is separate
from the counts/costs-only usage ledger. A write failure logs a fixed message and does not
end the call. Normal logs never include timeline transcripts or paths.

## Recovery and development validation

Calling requires explicitly initialized recovery state in the configured
private jpcall data directory. For a genuinely new installation, run
`python -m fri.jpcall.recovery <private-jpcall-data-directory>` before enabling
calling. Never delete/reset this record to unlock a pending attempt. Missing,
corrupt or unknown-version state disables admission. An installation fuse
prevents ordinary journal deletion from being treated as a new installation.

One process owns calling. Attempt intent and callback correlation are durable
before create. A failed hangup or lost create acknowledgement blocks another
dial until authenticated terminal evidence or owner reconciliation exists.
The engine's owner-only `recovery_status()` and `reconcile()` operations inspect
or terminate an existing call; they never originate one. An unknown SID remains
blocked. Local stop, TTL, restart and billing completion are not termination
evidence. Rollback preserves the record and must disable calling if older code
cannot honor it. No call resumes after restart.

Confirmation uses the exact original goal and approved facts without a paid
pre-call paraphrase. Request/brief equality and expiry still prevent stale
confirmation. Ringing is bounded at 30 seconds, permission/media readiness at
20 seconds, and the attempt at at most 600 seconds. Stop works during connection.
No verified dollar spending cap is claimed; estimates and actual posted prices
remain distinct.

Current development validation is simulated. Use the
[evaluation checklist and log schema](jpcall-evaluation.md) to distinguish fake
peers, model-simulated dialogue and later authorized live voice. Passing tests
does not establish Japanese naturalness or real-path latency.
