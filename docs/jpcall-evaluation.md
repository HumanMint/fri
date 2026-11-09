# Call evaluation

Development uses synthetic providers and fictional dialogue. No test below
authorizes phone calls, API spending, recording, or production changes.

## Evidence labels

- `scripted`: fake peers and synthetic audio. Proves application mechanics.
- `model_simulated`: dialogue generated without a real phone recipient. Record
  the actual model; it does not establish the deployed voice model's quality.
- `live_voice`: separately approved calls to consenting test recipients.

Never combine these modes into one completion or latency score. A missing
measurement is `null`, not zero or a pass. Successful offline tests cannot
establish natural speech, real audibility, provider retention, or a spending cap.

## Scenario checklist

Before collecting results, declare the requested facts and material conditions.
Cover an ordinary inquiry, corrected time/price/negation, missing group fact,
hold chatter and no answer, interruption/noise, and transfer/refusal. Add a
regression whenever a concrete failure is found. A safe abort passes its safety
oracle but does not complete the inquiry.

Check both phone legs, no member microphone transmission, permission before
relay/text, current requester approval, absolute question expiry, Stop while
connecting, no stale output after withdrawal, truthful partial transcripts,
visible translation failure, full conditions/reference, and uncertain hangup.

## Evaluation log

Keep logs outside the repository. Capture fictional or separately consented
test content only; no credentials, real destination numbers or member history.
Raw audio capture is off unless separately approved. Record retention/cleanup
before any capture. Store a record per scenario using this shape:

```json
{
  "run": "synthetic-001",
  "source_sha": "exact candidate commit",
  "mode": "scripted",
  "scenario": "corrected-price",
  "call_language": "ja",
  "actual_models": [],
  "expected": {"price": 1200, "currency": "JPY"},
  "observed": null,
  "outcome": "not_run",
  "critical_failures": [],
  "original_transcript_evidence": null,
  "english_transcript_evidence": null,
  "delivery_provenance_evidence": null,
  "original_latency_ms": null,
  "english_latency_ms": null,
  "response_latency_ms": null,
  "interruption_stop_ms": null,
  "japanese_comprehension_1_to_5": null,
  "politeness_1_to_5": null,
  "conversation_fluidity_1_to_5": null,
  "english_faithfulness_1_to_5": null,
  "reviewer": null,
  "termination_evidence": null,
  "estimated_usd": null,
  "posted_usd": null,
  "limitations": ["scripted peers do not prove spontaneous conversation"]
}
```

Measure transcript and ordinary-response delay from the recipient's utterance
end. Measure interruption to playback cessation, not merely a cancel command.
Identify acoustic timestamps versus ingress/publication acknowledgements and
clock uncertainty. Keep missed/failed outputs in the denominator. Report each
mode, scenario class and speaker separately; turns within a call are correlated.

Initial design targets: original text 2 seconds, English 4 seconds, ordinary
task-relevant spoken reply 3 seconds, interruption 500 ms. These are proposed
targets, not achieved measurements. Six smoke calls cannot establish p95 or
general business-call readiness. Further collection needs a declared cohort,
evidence rule and approved budget; insufficient evidence stays inconclusive.

## Fresh scoring review

Each round uses one fresh reviewer at the frozen candidate SHA. Keep stable
finding IDs and record the exact checks, fixes, declined scope and evidence.
Review later rounds only for changed behavior and open findings.

Assess these categories and every listed subcategory separately:

| Category | Subcategories |
| --- | --- |
| Reliability | turn liveness, interruption, question races/deadlines, bounded shutdown |
| Authorization/privacy | permission/withdrawal, requester approval, no microphone forwarding, content lifetime |
| Recovery | durable admission, ambiguous create/hangup, authenticated reconciliation, restart/rollback |
| Transcript/results | chronology, delivery provenance, translation failure, corrected/conditional facts |
| Resources | content retention, pending tasks, cost transparency, measured latency |
| Usability | confirmation, connecting Stop, group questions, Japanese fluidity/politeness, full results |
| Maintainability | existing seams/dependencies, focused regression proof, proportional complexity |

An overall score cannot exceed the lowest verified category/subcategory.
Unobserved behavior is `not_verified`; it cannot inherit 9.5 from a plan score
or mocked test. A critical failure blocks acceptance regardless of averages.

Japanese behavioral evaluation needs a qualified Japanese/English evaluator
and actual selected-mode output. Minor imperfections without changed meaning
or avoidable rescue can earn 4/5; repeated unnecessary repair cannot. Keep
missing facts, conflicting facts and established conditional answers distinct.
