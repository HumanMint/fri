# Carried saved answers

With Gene's `saved-answer-ids` capability, Fri collects saved history's `answer_id` values after its final withholding checks, immediately before each model request. Requests in a tool sequence accumulate identities in the shared turn fence. `/chat/turn` reports them as optional `carried_answers`, alongside existing `drew_on` message IDs. Withheld or unattributed answers contribute no identities. Gene-retired breadcrumbs and summary-held placeholders have no answer identity to carry.

Without the capability Fri omits the field. Missing or malformed answer identities are ignored. Old Fri and old Gene retain today's behavior. No persistent Fri state format changes; rollback to `9637389` remains compatible. Gene's migration is additive, and rollback to `29d6791` preserves saved turns and retirement breadcrumbs.

The paired Gene PR must merge first. `docs/integration-pair.json` pins its exact head. Gene follows explicit answer identity edges, never words or the source IDs of a question alone. Valid ancestry beyond Gene's bounds succeeds without keeping an exchange rather than saving partial tracking.

Proof includes failing-first unit cases on Fri main, real-controller capture of saves with/without a withheld answer, capability omission, malformed identities, accumulation across requests, both rollback mixed pairs, and two killed mutations (missing reporting and collecting before withholding). The standard offline check exercises this exact pin with network denial and no credentials.

Round-1 correction: the pinned Gene backfills pre-upgrade retirement breadcrumbs and independently guards answer identity exposure. The integration regression checks that a question/breadcrumb-only request reports no carried answer and survives the later original-message withdrawal. Gene also inherits bounded original message dependencies separately from question-only source IDs, preserving retirement after middle-turn trimming. At over 100 inherited messages it marks ancestry unknown, preserves the original stored answer, and withholds current history/reference reuse. Rollback retains that data and uses the older behavior.
