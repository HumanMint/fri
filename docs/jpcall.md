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
