# jpcall

`/jpcall` lets a member in a Discord voice channel ask Fri to phone a Japanese
business for the group. Fri speaks Japanese on the call, plays the call audio
(the callee and Fri) into the voice channel, posts a live Japanese/English
transcript in the voice channel's chat, asks the group when it needs an answer
while politely holding the callee, and posts the outcome and a cost summary
when the call ends. Members' voices never go into the call.

jpcall is Fri-only. It does not use the Gene controller.

## Pieces

| Piece | Choice |
|---|---|
| Phone line | Twilio Programmable Voice, outbound, `<Connect><Stream>` |
| Call audio | Twilio Media Streams, bidirectional, 8 kHz mono PCMU |
| Voice | OpenAI Realtime (`gpt-realtime-2.1`), g711_ulaw in/out, tools for asking the group and ending the call |
| Callee transcript | Realtime input audio transcription with a named model, language `ja` |
| Translation, brief, outcome | OpenAI text model, configurable, low reasoning effort for live lines |
| Discord audio | `discord.py[voice]` with DAVE (`davey`), libopus, send-only |

## Layout

- `src/fri/jpcall/contract.py`: the only shared surface between the engine and the Discord side.
- Engine: Twilio REST + webhook/media server, Realtime session, hold timers, translation, usage meter.
- Discord side: `/jpcall`, brief card, voice connection and mixer, transcript pages, question prompts,
  outcome and cost summary, and a standalone dev runner.

## Rules

- Every call opens with two notices in Japanese: Fri is an AI assistant calling for the group, and the group
  is listening and the call is transcribed.
- Only allowlisted members can start calls. Numbers must be Japanese (`+81`) unless listed as test numbers.
  Emergency and premium-rate prefixes are refused.
- Fri never pays, gives card numbers, or accepts fees or cancellation terms on its own; those become questions.
- Holds are app-timed: an apology about every 15 s and a call-back fallback after the hold limit.
- Transcripts include third parties. They live only in the Discord messages; jpcall persists no call content.
  The usage ledger stores counts and costs only.
- Costs: every model call and the Twilio leg is metered; the summary shows tokens/minutes and USD per component,
  marking estimates and pending Twilio prices.

## Discord development

The runtime image needs libopus (for example, the `libopus0` system package on Debian).
The pinned discord.py voice extra supplies PyNaCl and DAVE support; `davey` is also pinned
explicitly. PCM sources use the normal `VoiceClient.play` path: the player encodes PCM
to Opus, then `_get_voice_packet` applies DAVE encryption before transport encryption.
This path was checked against the installed discord.py 2.7.1 source; a live encrypted
voice session still needs a manual development-guild check.

Run the standalone development identity with `JPCALL_DEV_DISCORD_TOKEN` and
`JPCALL_DEV_GUILD_ID` supplied by the environment:

```sh
uv run python -m fri.jpcall.devbot --fake-engine --allow-user MEMBER_ID
```

In fake mode, repeat `--allow-user` for each member who may start a call; the default
allows nobody. The brief is public in the voice channel's chat; only the requester can
confirm or cancel it. Anyone currently in that voice channel may answer call questions.
The bot needs guild and voice-state intents, the applications.commands scope, and view
channel, send messages, embed links, connect and speak permissions in the test voice
channel. It connects self-deafened and never receives or forwards member audio.
The fake engine scripts Japanese/English lines, one group question, synthetic tones,
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

The cost summary lists units and USD per component/model, then sums input and output tokens
across models. Cached modality counts are included once; explicit aggregate token counts take
precedence over breakdowns. Prices retain `~` for estimates and `pending` until posted.
