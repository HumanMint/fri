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
  queued output. Only a reply that begins after the complete notice was acknowledged
  as played can be classified; cleared or interrupted audio never counts. Speech over
  the notice or an unclear reply grants nothing and gets at most one repeat of the
  notice per consent phase, within the existing deadlines.
  Permission recognition remains model inference, not a perfect detector.
  A refusal, before or during the inquiry, is one permission withdrawal: it ends the call with a
  plain decline outcome and the fixed goodbye. Recording questions
  are answered truthfully: no audio is recorded, and the group gets a text transcript.
- Twilio webhooks never decompress request bodies; encoded or undecodable status bodies
  are refused before parsing and signature checks.
- Only allowlisted members can start calls. Numbers must be Japanese (`+81`) unless listed as test numbers.
  Emergency and premium-rate prefixes are refused.
- Fri only gathers information. It never books, pays, gives card numbers, or accepts fees or cancellation terms; group answers cannot expand that authority.
- Holds are app-timed, and the app tells the callee to wait; the model only asks the group.
  At most one check-in follows after 45 s of callee silence, with an unresolved
  ending and the fixed goodbye after the hold limit. A delivered natural response replaces the
  check-in; raw VAD noise only postpones it. No automatic callback is made.
- An ordinary end (the callee hangs up, `end_call`, a limit, or the requester's Stop) is not a
  withdrawal. Live callee relay, new capture and new turns stop at once; text captured under
  consent, its final transcription (up to 2 s), its translations and queued transcript writes
  still finish inside the 8-second cleanup bound. A refusal, transfer or other withdrawal, even
  while closing, still revokes queued text, translations and the summary before the final
  report is built. Fri says exactly one goodbye: the one spoken with `end_call`, otherwise
  the fixed farewell (at most 5 s). A callee hang-up and failures end without one. The
  requester's Stop says none and blocks new Fri audio to both destinations at once. It clears
  the group's queue and starts clearing the phone buffer without waiting for the model's cancel.
  Speech already played stays in the transcript, marked interrupted; actual phone cutoff latency
  still needs live measurement.
- Transcripts include third parties. By default they live only in Discord messages; jpcall
  persists no call content. The optional development timeline below retains text when enabled.
  Audio is never recorded or persisted. The usage ledger stores counts and costs only.
- Costs: every model call and the Twilio leg is metered. One compact final message shows
  the outcome and total; `cost details` shows component tokens/minutes and USD ephemerally,
  marking estimates and pending Twilio prices.

## Discord development

The runtime image installs `libopus0` and checks an Opus encode/decode as the runtime user during the build.
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

### Main Fri group trial

Main Fri is the selected group-trial identity. Keep the normal `/fri` command, chat,
memory controls and controller contract; `/jpcall` is enabled only with its complete
configuration and an explicit caller allowlist. Missing or invalid call configuration
disables calling without disabling ordinary Fri. A call does not take over an existing
voice connection, and only its requester can confirm, answer or stop it.

Before enabling the first trial, qualify the combined source against current main and
its pinned controller. Build the selected runtime image, check its codec/DAVE path,
signed callback routing, durable recovery directory and shutdown, then exercise the
full group flow with a consenting test recipient. The owner-only recording profile
does not disclose group sharing and must not be used for that flow. Group recording
stays off. Keep the existing brief notice covering AI processing, group listening and
retained original/English text; show it before dialing.

Release main Fri through the existing Gene maintainer batch and maintenance window.
Do not run a second process with main Fri's token. The call and chat paths share the
container's resource limit, so a live trial must also check ordinary Fri responsiveness.
Offline tests and a phone-only owner test do not establish that shared-runtime behavior.
The main entrypoint handles Docker's stop signal through Discord's normal cleanup;
Compose allows 30 seconds before forced termination. This is a shutdown allowance,
not proof of remote call termination.

Run the standalone development identity with `JPCALL_DEV_DISCORD_TOKEN` and
`JPCALL_DEV_GUILD_ID` supplied by the environment:

```sh
uv run python -m fri.jpcall.devbot --fake-engine --allow-user MEMBER_ID
```

In fake mode, repeat `--allow-user` for each member who may start a call; the default
allows nobody. The brief, transcript, questions, outcome and costs go in the channel where
`/jpcall` was invoked (including a voice channel's own chat). Fri joins the requester's voice
channel. Only the requester can confirm or cancel the brief; it names them by mention without notifying anyone.
Only the requester answers a question, with its buttons;
other members may suggest facts in chat. Chat isn't passed to the call. In main Fri, a reply to a running call's
post isn't chat with Fri, and Fri's own chat channel stops answering unaddressed lines while a call posts there.
Privacy controls ("forget me", "remember me again", "what do you remember about me?") keep their usual path.
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
only when its configuration is enabled. `/jpcall` skips the `/fri` panel's channel check and
relies on its own allowlist, requester and voice membership checks, as every `jpcall:` control does. Call
operations do not use the Gene controller. Disabled configuration keeps the existing command
and channel behavior.

The public final report has the call duration, a compact outcome and cost embed.
The summary runs beside the goodbye and hang-up and must finish within the same cleanup bound.
If it is unavailable, the outcome says `Summary unavailable; see the transcript.`, adding
`end_call`'s own description only as `Fri’s unverified note`. The summary is dropped if consent
is withdrawn before the final report is built; the reader is then stopped, so consent cannot
change during report delivery. A transcript line whose English never arrived is marked
`translation unavailable` in the final transcript.
The complete outcome/conditions/reference are attached as call-result.txt;
persistent controls retain costs, not the outcome. Unknown phone termination is
shown separately from local closure. Late price updates edit only the cost embed. Busy, no-answer and voicemail outcomes get a plain `call didn't connect` line;
a failure after the call was answered keeps `call ended`, its duration and the partial outcome. Exact
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
The goal is context for the conversation, rather than a script to read aloud. A refusal is
reported plainly as the recipient declining to share the call. Discord
UI references to the bot use `fri`; spoken-name pronunciation belongs to the engine.

### Conversation settings

The voice defaults are `JPCALL_REALTIME_MODEL=gpt-realtime-2.1`,
`JPCALL_REALTIME_VOICE=coral`, and `JPCALL_REALTIME_SPEED=1.15` (valid range 0.25–1.5).
The session receives the original member goal once, with the approved share/never facts, as data.
Fri introduces itself once, waits for permission, then asks one concrete question at a time.
Fixed lines (the notice, hold, check-in and fixed goodbye) carry only the short literal-speech
prefix and the exact text; conversation and authority rules stay in the session. Consent is
classified only by the permission classifier, where anything not explicit is `unclear` and grants nothing.

| Setting | Default | Accepted values |
|---|---|---|
| `JPCALL_HOLD_APOLOGY_EVERY_S` | `45` | Positive integer seconds; at most one proactive check-in per question |
| `JPCALL_VAD_THRESHOLD` | `0.7` | Finite number from 0 to 1, inclusive |
| `JPCALL_VAD_SILENCE_DURATION_MS` | `600` | Positive integer milliseconds |
| `JPCALL_VAD_NOISE_REDUCTION` | `near_field` | `near_field`, `far_field`, `off` |
| `JPCALL_REALTIME_REASONING` | unset | `minimal`, `low`, `medium`, `high`, `xhigh` |

Server VAD remains enabled, with automatic responses and interruptions disabled.
During an approved inquiry the app stops phone and Discord playback on speech-start,
cancels queued speech and truncates the old response to its acknowledged played duration.
Only a current final non-echo transcript releases the next reply, in the same conversation.
The prepermission notice keeps its existing interruption behavior. A lost, empty or echoed
transcript gets one short request to repeat (before consent: the notice's one repeat per
phase); silence afterwards, noise and echo never end the call. Only a second failed or missing
transcription without a valid one in between ends it as failed. Higher activation
threshold and near-field filtering reduce noise triggers, but echo or noise can still
cause a cutoff; actual fluidity needs a controlled voice test. Changed numbers, negations
and conditions are preserved. Exact recent repeats
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
start/check-in/answer. No audio bytes or API credentials are written. Text and tool
content are written only when the consent captured with them is still current;
pre-consent, refused and withdrawn speech keeps its timing event without content.
This file still contains private conversation text; keep it outside the checkout, restrict
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
or terminate an existing call; they never originate one. A Stop during call creation keeps
waiting for the create within the cleanup bound and hangs up the call it returns. A create
counts as never made only when it was provably not sent (no connection) or was refused as
unauthenticated (HTTP 401): the attempt is then closed and calling stays available. Every
other create failure (other 4xx and 5xx, timeouts, lost acknowledgements) stays blocked,
because Twilio does not document that it created nothing. While the runtime runs, a signed
status callback for such an attempt records its SID; a non-final one also asks Twilio to end
that call.
An unknown SID remains blocked. Local stop, TTL, restart and billing completion are not
termination evidence. Rollback preserves the record and must disable calling if older code
cannot honor it. No call resumes after restart.

Owner reconciliation, with every calling runtime stopped and the same process environment
and `FRI_DATA` (it never dials and prints status only):

```sh
python -m fri.jpcall.engine
```

It takes the journal's exclusive lock, so it refuses while a runtime runs. An attempt still
`reserved` (creation was never started) is closed as not created. An attempt with a known SID
is checked with Twilio and ended if needed, which records termination evidence. An attempt in
`creating`/`uncertain` with `sid_known: false` stays blocked: start the runtime where Twilio
can reach it so a signed callback can record the call, then reconcile again. Never delete or
reset the record.

Confirmation uses the exact original goal and approved facts without a paid
pre-call paraphrase. Request/brief equality and expiry still prevent stale
confirmation. Ringing is bounded at 30 seconds and media readiness at 20 seconds.
Notice delivery has a 30-second bound; the recipient then has 20 seconds to
answer after the complete notice is acknowledged as played. A transfer gets a new notice and response
deadline. The whole attempt has one deadline, from reservation: `JPCALL_MAX_CALL_S`
(default and maximum 600 seconds; larger values are a configuration error). The brief shows it,
Twilio's `TimeLimit` uses it, and the engine starts closing 8 seconds before it, for one goodbye
and the hang-up. Stop works during connection and never waits for a goodbye.
No verified dollar spending cap is claimed; estimates and actual posted prices
remain distinct.

Current development validation is simulated. Use the
[evaluation checklist and log schema](jpcall-evaluation.md) to distinguish fake
peers, model-simulated dialogue and later authorized live voice. Passing tests
does not establish Japanese naturalness or real-path latency.

### Owner-only Korean recording test

`scripts/jpcall_owner_recording.py` is an import-only operator helper for a separately
approved owner test. It never loads credentials, creates a client or dials. Its context
requires explicit go and recording consent, an exact allowlisted owner destination and
an existing matching call, Korean language, and a call limit of at most 120 seconds.
The run-local recording language must be supplied through the existing configuration's
`language` property before planning; ordinary no-recording prompts remain unchanged.
Use this profile only for the owner's isolated phone test without Discord relay or
publication. Its short opener identifies fri as AI and asks to start the previously
consented recording test; recording failure must still be reported accurately.
The helper starts one provider recording, downloads verified completed dual-channel WAV
into a new private directory outside Git, and offers separate manual stop/deletion.
Provider-side media is not proof of handset audibility; Stream channel contents need a
controlled check. Recording/storage add costs; the helper does not enforce a total dollar
budget. A supplied number or recording consent alone never authorizes starting a call.

### Prepaid trial budget

Calling additionally requires `budget.json` in the existing private calling directory.
Missing, unreadable, duplicate-key or incompatible state blocks admission. The budget
is cumulative across callers and restarts, capped at $5, and never resets with the day.
Initialize it only while calling is stopped, after recovery initialization:

```sh
python -m fri.jpcall.budget PRIVATE_CALL_DIRECTORY --spent-usd PRIOR_TOTAL --tariff APPROVED_TARIFF_JSON
```

`PRIOR_TOTAL` must include previous trial consumption and outstanding charges. Unknown
prior consumption is not zero. Initialization refuses an existing budget or installation
marker; deleting the ledger cannot reset it. Preserve both files through rollback, and
keep calling disabled on an older artifact that cannot enforce them.

The operator-approved tariff JSON binds exact `text_model`, `realtime_model` and
`transcribe_model` names. It requires positive decimal USD ceilings for `text_input`,
`text_output`, `realtime_input`, `realtime_output` (per million tokens), `phone_minute`,
`transcription_minute` and `phone_attempt`. Realtime ceilings must cover **both** text
and audio at the highest applicable rate, including uncached inputs. Phone ceilings
must cover every admitted destination, stream charges, rounding, fees and account
pricing; attempt charges include detection fees. Do not use an unverified list estimate
as an account ceiling. Unknown rates, billing minima or model changes block enablement
until an approved covering tariff is supplied. This command does not verify a provider's
account rates or authorize spending.

Before dialing, reserve rounded whole-attempt phone/transcription exposure plus an extra
billing minute. Input audio is byte-limited to the prepaid PCMU duration. Every Responses
request and Realtime response reserves its worst-case tokens synchronously before send.
Text inputs and voice instructions/overrides are limited to 32 KiB of serialized UTF-8;
bytes conservatively bound text tokens, with another 1,024 tokens of framing margin.
Realtime post-instruction context is capped at 4,096 tokens and output at 512 tokens per
response; text output is capped at 1,200. The current
[Realtime schema](https://developers.openai.com/api/reference/resources/realtime/client-events)
defines these output and truncation limits. They can shorten long conversations; exhaustion
stops new paid work rather than silently extending exposure. No paid retry is automatic.
Cleanup/status/hang-up remain available without a new paid model request.

Reservations are fsynced before action and are never refunded, even on cancellation,
timeout or a lost acknowledgment. This may exhaust the trial allowance before actual
invoices reach $5; it prevents uncertain work being spent twice. Metered Discord costs
remain separate from these upper bounds. Health exposes `jpcall_budget` admission,
reserved USD and the cap; it reports blocked on corrupt or unknown state. No dialogue,
destination or credentials enter the budget ledger. Provider adherence to approved
rates, maximum tokens, truncation and call time limits remains a live prerequisite.

Seeding is a **production change**, performed only in the maintainer maintenance window
and as part of the owner's explicit decision to enable a trial. This PR ships no seeded
ledger or tariff. Merely enabling the feature cannot admit a call without seeded state.
The stopped-runtime operator sequence is: verify the approved remaining allowance and
account/destination rate ceilings; initialize recovery only for a genuinely new install;
run the budget command above with the actual prior consumption and approved tariff;
then check health before admitting the explicitly authorized trial. Never initialize or
reset state as a workaround for an unreadable ledger.

Health distinguishes `unseeded`, `seeded` and `unreadable` ledger state. Remaining funds
and reservations are `null` for unknown state, never a fabricated zero. `open_reservations`
shows counts and upper-bound USD by call, text and Realtime component. These are funds
still encumbered by the conservative policy, including completed work: there is no
automatic invoice settlement or release. Prior seeded consumption is excluded from
those reservation counts, and all encumbered funds remain deducted after restart.

A revoked model completion is discarded before event publication and cannot reach
Discord, call evidence, memory or learning. The private append-only debug timeline
never records Responses completion text. Previously authorized timeline entries may
remain: each carries its consent generation, and withdrawal appends a content-free
`consent_revoked` marker identifying the revoked generation. Matching earlier entries within that call
are revoked evidence, not current facts; this marker is not a promise of deletion.

Budget reads are capped at 64 KiB and reject non-regular files, non-object JSON,
excessive nesting and other malformed state. The health writer also contains an
unexpected budget-contribution failure, reports unreadable/blocked state and continues
its ordinary heartbeat instead of leaving stale health.

On permission revocation, Realtime cancels old generation work, clears the input buffer,
and waits for the buffer-clear and outstanding-response acknowledgments. It then deletes
all tracked default-conversation items (audio, text and tool items), waiting for deletion
acknowledgments before any subsequent response. Incoming audio and tool results wait for
that barrier; send ordering prevents a delayed old write overtaking the clear. Failure to
confirm cleanup blocks inference and follows bounded call cleanup. Only content-free
item tombstones remain locally to reject late events; transcript/echo caches are cleared.
Notices use an out-of-band response with empty input and no tools, never the prior
conversation. Spoken notice wording is unchanged. These use the documented
[Realtime deletion and response events](https://developers.openai.com/api/reference/resources/realtime/client-events).
