# Codex counter-review of the 2026-10-23 architecture review

Reviewer: Codex (gpt-6.1-sol, xhigh), read-only fork `01a16c67-59ed-7192-814e-45a172bae21f` of the owner's Fri thread, run by Claude at the owner's request on 2026-10-23. Sandbox read-only; MCP servers, browser/computer-use plugins and Codex memory writes disabled. Verbatim output below. Peer review evidence, not owner approval.

---

**The review identifies real continuity problems, but it underestimates the purpose of the privacy boundaries and overstates how little orchestration already exists.** I would improve context assembly and observability before moving databases or replacing the tool protocol.

This is a context-rich counter-review, not fresh-candidate release approval. Everything below comes from offline reads and isolated, in-memory probes. No files or live systems were changed.

References use **F** for `fri/src/fri/` and **G** for the inspected Gene worktree’s `src/gene/`.

**Claim audit: sections 2a and 3**

- **§2a — MISSING CONTEXT.** Plumbing, continuity, admission, keyword, capability, learning, voice, and specification problems are recognizable historical themes. The approximate counts, dated classifications, and “most fixes add gates” conclusion lack a reproducible classification method. Current code cannot establish historical report counts. Some examples describe resolved failures: the owner’s “token usage is crazy” message now returns **false** from the usage detector; “show my token usage” returns **true**. See `F/fri_usage.py:24`. Admission changes also expanded capabilities while preserving boundaries, rather than merely adding exceptions.

- **§3.1 store inventory — CONFIRMED.** Six turns/seven days, ten notes/five categories/90 days, thirty buffered messages/fifteen minutes, 600-character/two-day summaries, twenty guild facts, three one-day web receipts, five-minute clarifications, historical observations, and recent command receipts are implemented. References: `G/store.py:575`, `G/store.py:587`, `G/store.py:593`, `F/fri_memory.py:7`, `F/fri_agent.py:170`, `G/fri_agent_store.py:113`, `G/fri_agent_store.py:323`, `G/fri_pending.py:20`, `G/fri_explore_store.py:42`, `G/control_store.py:68`.

- **§3.1 tool amnesia — CONFIRMED, with exceptions.** Most tool replies become breadcrumbs; clarification and certain preview replies are retained. Public research receipts remain available, so a later source request has a retrieval route. See [F/fri.py:339](/Users/danmacmini/Projects/vibing/fri/src/fri/fri.py:339) and `G/store.py:592`.

- **§3.1 “cannot see their own messages” — OVERSTATED.** Their gateway messages are excluded from the ambient buffer, but stored assistant turns are supplied to the model, and direct references can retrieve Fri’s messages. The missing shared conversational view is real. `F/fri_agent.py:285`, `F/fri_chat.py:357`, `F/fri_conversation.py:152`.

- **§3.1 channel isolation — CONFIRMED; “different stranger” — OVERSTATED.** Notes and turns are conversation-scoped, but identity, current display name, approved facts, and retrievable records still exist. `G/store.py:578`, `G/store.py:664`, `F/fri.py:287`.

- **§3.1 magic phrases — MISSING CONTEXT.** The **evidence substring**, not the whole message, must start with an accepted phrase. My probe accepted “by the way, I like Minecraft.” The grinding example fails personal-note validation, but can survive temporarily as conversation text or become a dated historical observation. A crashing server belongs in task state, not necessarily a preference profile. `F/fri_memory.py:13`, `G/fri_explore_store.py:204`.

- **§3.1 “no open loops” — WRONG literally.** Saved work has open/completed/cancelled states; Gene jobs, reminders, post receipts, and review notifications persist. What is missing is a unified conversational projection of that work and general commitments. `F/fri_workspace.py:53`, `G/fri_workspace_store.py:17`, `F/fri_requests.py:80`, `F/fri_reviews.py:150`.

- **§3.2 stacked routing — CONFIRMED; causal certainty — OVERSTATED.** The gates exist, but they serve different purposes and do not all run on every path. Clarifications extend attention to 300 seconds, and pending requests can survive the ordinary 120-second window. The closing shortcut requires the previous model response to permit closing; it excludes pending actions and explicit mentions. `F/fri_agent.py:339`, `F/fri_agent.py:435`, `F/fri_responses.py:28`.

- **§3.2 prompt measurements — PARTLY CONFIRMED.** I reproduced 3,087 words and 21 precisely date-bounded Gene prompt commits. “61 prohibitions” needs a counting definition: literal `never/don’t` occurrences total 44. “Repeated policy buys no safety” is **WRONG**: deterministic tool authorization cannot fully prevent semantic disclosure in ordinary text. Runtime voice preferences are also explicitly separated from protected rules. `F/fri_chat.py:209`, `F/fri_chat.py:398`.

- **§3.3 loop mechanics — CONFIRMED; interpretation — OVERSTATED.** One structured action, four sequential steps, and no carried reasoning items are implemented. However, subsequent calls receive accumulated tool receipts and conversation context; they do not reason from zero. JSON and bounded steps do not themselves make a system non-agentic. `F/fri_runner.py:15`, `F/fri_runner.py:117`, `F/fri_chat.py:357`.

- **§3.3 native-function/encrypted-reasoning availability — OVERSTATED.** The implementation demonstrates web-search requests, not function calls or encrypted-reasoning round trips. The adapter targets `https://api.openai.com/v1` using ChatGPT-plan authorization; API-key endpoint parity cannot be assumed. These capabilities remain unverified here. `F/subscription.py:14`, `F/fri_web.py:22`, `F/fri_chat.py:398`.

- **§3.4 monolith — CONFIRMED; explanation — OVERSTATED.** `talk()` spans lines 164–380, with another path at 414–474. Lazy imports do not generally prove cycles: my static graph found the panel/request cycle, not pervasive circularity. Extensive tests also protect authorization and recovery. `F/fri_panel.py:11`, `F/fri_requests.py:68`.

- **§3.5 controller dependence — CONFIRMED.** “Every memory change requires cross-repo contract changes” is **OVERSTATED**: compatible controller-only changes are possible. “Cannot run in another server” misses the bounded Lab configuration. Six-to-ten HTTP calls is an unmeasured, path-dependent estimate. `G/config.py:36`, `G/store.py:571`, `F/fri_actions.py:106`.

- **§3.6 no route to work — WRONG broadly.** Submitted Gene requests reach development and release review; automatic repairs and owner updates exist. Missing general-purpose provisioning and unified progress are valid findings. `F/fri_requests.py:80`, `F/fri_reviews.py:126`.

- **§3.7 evaluation — WRONG about only eight scored cases.** Images, continuity, and image sharing also have automated assertions. Model-facing evaluators generally bypass gateway admission, so the full-pipeline quality gap is **CONFIRMED**. Some integration tests exercise the real provider adapter with mocked transport, rather than stubbing all model code. `F/fri_eval.py:195`, `F/fri_continuity_eval.py:30`, `F/fri_image_share_eval.py:26`, `integration/compat_tests/test_fri_mentions.py:345`.

- **§3.8 privacy tradeoff — MISSING CONTEXT.** The owner explicitly anticipated prompt injection from friends. A private server does not establish consent to redistribute DMs or retain other members’ derived information.

**Proposal decisions: sections 4–7**

**§4: PARTIAL.** Agree with staged turns, budgeted retrieval, declarative tools, open-task views, scoped workers, and Gene becoming an integration. Admit and minimize **before durable event logging**. Keep source/audience/deletion provenance on episodes and summaries. Risk tiers also need object-level authorization, freshness, quotas, and idempotency. An 800-word prompt is an experiment, not a correctness criterion.

**§5 quick wins:**

1. **PARTIAL: retain safe replies.** Breadcrumbs prevent Alice’s words, paraphrased into Bob’s answer, surviving Alice’s erasure. Removing literal quotations cannot remove derived facts. Retain public-only or requester-owned results where provenance establishes that scope; otherwise retain source references and re-fetch. Dependencies must invalidate replies, summaries, caches, and reasoning blobs on erasure or access changes.
2. **PARTIAL: include Fri’s messages.** Include successfully delivered messages with source and dependency metadata; suppress self-triggered loops.
3. **DISAGREE: merge turns/notes across DMs behind existing opt-out.** Start with explicitly shareable, editable preferences per guild/member. Keep private episodes private; promotion between audiences needs explicit consent.
4. **PARTIAL: longer history/summaries.** Add a token budget, attribution, expiry, deletion dependencies, and correction handling before increasing retention.

**Database split: PARTIAL, later.** The documented transaction is real: revision checks, source deduplication, and pending-command consumption share a transaction. See `G/control_store.py:93` and `G/fri_operation_store.py:28`. First create logical memory/orchestration interfaces within the existing controller. Then choose an authoritative privacy/admission service and design fail-closed permits, durable erasure propagation, and cutover recovery. Avoid casual dual writes. Physical separation is a substantial control-plane migration, not a table copy.

**Judgment consolidation: PARTIAL.** Consolidate fuzzy settings/topic routing and expose decision reasons. Preserve deterministic privacy controls, addressing eligibility, budgets, and accepted-operation admission. Calling a model for every acknowledgment or ambient message adds usage and injection exposure; post-hoc validation cannot undo a leaked public reply.

**Native loop: PARTIAL.** Run a small capability probe early, separately from production. Preserve the existing adapter until function arguments/results, streaming, refusal handling, and encrypted-reasoning continuation actually pass. Carrying opaque reasoning also creates retention obligations.

**§6: PARTIAL.** Miss capture, triage, synthetic regressions, root-cause PRs, and category scores are useful. Do not store “everything the model saw” by default: that creates a second private-history database. Paraphrasing and replacing names do not guarantee anonymization. Use minimal, expiring reports and explicit capture consent. Reaction feedback supplies evidence, never coding or release approval.

Full-pipeline graded evaluation is feasible. Forty cases averaging three generation calls plus one judge call mean roughly **160 calls**, before extra research/retries. This consumes subscription allowance; recorded tokens do not establish its monetary or quota weighting. Use deterministic checks first, targeted live cases, calibrated judges, and a held-out set. More reported misses can also reflect more usage.

**§7: AGREE with separation; PARTIAL overall.** Never resume or steer the owner’s live thread from a peer agent. Context-rich forks are for discussion; candidate reviewers must start fresh with the SHA, product spec, and evidence. Pin model/effort, enforce read-only/offline tool access, and authenticate peer identity outside message text. GitHub comments under HumanMint are not owner approvals. Mandatory owner merges/deployments would contradict existing maintainer authorization and the standing repair policy. Agents can draft the product spec; the owner should correct priorities without maintaining another manual.

**What the review missed**

- **Reproduced privacy gap:** ordinary `recent` context can produce a retained third-party-derived answer, while identical reply-target output becomes a breadcrumb. `F/fri.py:390`.
- Conversation is saved before Discord delivery; retained “replies” can include undelivered output. `F/fri.py:378`, `F/fri_agent.py:432`.
- Heartbeat tasks execute serially, so slow learning can delay attention and delivery work. `F/fri_agent.py:638`.
- Replay needs controlled time and fresh, consistent message IDs; existing model fixtures contain fixed historical sources. `F/fri_eval.py:16`.

## Top disagreements

1. Claude proposes quotation scrubbing; I require provenance-aware retention and erasure.
2. Claude proposes cross-DM profiles behind opt-out; I require audience-specific consent.
3. Claude prioritizes physical database separation; I prioritize logical boundaries and continuity first.
4. Claude prefers one judgment decision; I prefer consolidated routing with deterministic eligibility and privacy controls.
5. Claude expects native reasoning support; I require an endpoint-specific capability test.
6. Claude describes mostly manual evaluation; automated suites exist, with a real gateway-plus-model coverage gap.
7. Claude adds owner merge gates; I preserve existing authorization and peer/owner separation.

## Agreed

Improve context assembly, expose existing work state, simplify the pipeline, measure conversational quality, retain scoped workers, and cross-review exact candidates. Sequence measurement → logical refactor → provenance-aware continuity → consented profiles; postpone physical migration and provisioning.

## Recommended first PR

- Add a synthetic full-handler replay harness with fake Discord/controller transports.
- Control clocks, source IDs, permissions, privacy revisions, and delivery failures.
- Cover clarification, closing, explicit mention, keyword routing, and derived-content erasure.
- Record route decisions, context sizes, model/prompt versions, and token usage without raw member history.
- Add a separately invoked, budgeted real-model evaluation mode; preserve production behavior and authority.

