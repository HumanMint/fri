# Serial reminder checkpoints

Base: `8fba1adf9c36158055daaf0551cdb84c4937c213` (Fri main).
Controller: `2336d2d59f79d8a35b8ba96bbe5f64b94c4b43f1`, the integration pin.

The heartbeat keeps its original stage order. At each optional-stage boundary it
checks whether 30 seconds have elapsed since its last delivery poll. An eligible
checkpoint awaits channel withdrawal settlement (including its existing retry),
then posts, then reminders. The initial poll still runs every tick, and there is
also a checkpoint after the final stage. Failures still use the existing stage
error handling. No worker or concurrent stage was added.

Thirty seconds matches the heartbeat's existing polling interval: fast idle ticks
make the same controller calls, while long ticks gain bounded polls. This is a
polling bound, not a delivery deadline: a long optional stage must finish first;
short stages can share the 30-second interval, and controller/Discord outages can
still delay delivery. A stage can itself contain several model calls. The existing
600-second per-call model limit is unchanged. There is no model-input change.

## Proof

`integration/compat_tests/test_fri_delivery_checkpoints.py` uses virtual elapsed
time and event barriers, real pinned-controller claims, and synthetic Discord
sends. It covers all seven potentially long stage positions, verifies delivery
before the next stage and exactly once across later checkpoints/ticks, and verifies
the idle tick's complete controller-call list. Thirty seconds of optional work
permits just one extra poll of each delivery queue.

The applicable #405 failures are exercised without recreating its concurrent
architecture: settlement is blocked after a transient failure, with no reminder
claim to expire or summary-derived text sent meanwhile; its retry cancels a
summary-dependent reminder and allows an unrelated reminder exactly once. Later
readers wait. The actual reply-check adapter claims after settlement, and the
withdrawn chat pair never reaches its model. This does not add a fence for unrelated
Discord events racing inside an existing stage.

A scratch-source differential using main's heartbeat failed all seven latency
cases (main makes no second claim that tick). A short mutation pass killed all
three mutants: removing intermediate checkpoints (6 failures; the final-stage
case still passes), omitting settlement (2 failures), and disabling the poll
throttle (1 failure). Scratch source copies were used; candidate source stayed
unchanged during the pass.

Validation: `uv sync --frozen`, then
`GENE_REPO=../reminder-checkpoints-gene scripts/offline-checks.sh setup`, then
`scripts/offline-checks.sh check`. Checks run with sandbox-exec plus the Python
network guard and passing denial canaries. No live model or Discord checks.
