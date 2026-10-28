# Contributing to Fri

Fri's checks run offline and need no credentials. [AGENTS.md](../AGENTS.md) holds the repository's
standing rules; this page explains the parts a newcomer trips over: the integration pin and the
offline checks.

## Quick checks

```sh
uv sync --frozen
uv run --no-sync pytest -q       # unit tests (tests/); no Gene needed
uv run --no-sync ruff check .
```

## The integration pin

Fri can't be tested alone: most of its behavior is a conversation with Gene's controller. So the
integration suites run against one exact Gene commit, recorded in
[`docs/integration-pair.json`](integration-pair.json):

| Field | Meaning |
| --- | --- |
| `contract` | The wire contract, `gene-fri/1` ([architecture](architecture.md#the-contract)). |
| `verified_controller_commit` | The Gene commit the integration suites run against. The offline checks read it, and a release needs it to be an ancestor of the Gene being released. |
| `minimum_controller_commit` | The oldest Gene this Fri needs. Usually the same commit. |
| `baseline_fixture_commit` | The Gene commit Fri was extracted from ([extraction.json](extraction.json)). It doesn't change. |

When a Fri change needs a controller change:

1. Make the Gene change first, in its own PR on [HumanMint/gene](https://github.com/HumanMint/gene).
2. Point both commit fields at that Gene PR's head and run the offline checks against it.
3. Re-pin whenever the Gene PR moves. Gene merges with merge commits, so a pinned branch commit stays
   valid after the merge.
4. A Fri PR merges only once its pin is an ancestor of Gene's `main`. Gene releases first, then Fri;
   a rollback goes Fri first, then Gene.

A Fri change that doesn't touch the contract leaves the pin alone.

## Offline checks

`scripts/offline-checks.sh` is what maintainers and reviewers run before a merge:

```sh
git clone https://github.com/HumanMint/gene.git ../gene     # once; any checkout that has the pinned commit
scripts/offline-checks.sh setup    # once per pinned Gene commit; the only step that uses the network
scripts/offline-checks.sh check    # tests, integration suites and ruff, network denied
```

- `setup` syncs Fri's `.venv`, then extracts the pinned Gene commit with `git archive` from `GENE_REPO`
  (default `../gene`) into `~/.cache/fri-offline/<gene sha>/`, with its own venv built from that
  commit's `uv.lock`. Your Gene checkout's branch and venv are never used. If the commit is missing,
  `git -C ../gene fetch origin` first.
- `check` clears the environment (no tokens, a temporary home), proves the network is denied with
  canaries, then runs `tests/`, `integration/compat_tests/` and `integration/replay/` against the real
  controller code, and `ruff`. Network denial comes from `sandbox-exec` on macOS or `unshare -rn` on
  Linux, plus a Python socket guard.
- Expect several minutes: the integration suites are a few thousand tests.

Every scenario uses invented members and messages, and Discord, the model provider and the network
are substituted. Passing checks don't prove live Discord rendering, audible music or how the real
model answers. The [operator runbook](operations.md#integration-checks) has the details and the
overrides (`GENE_SHA`, `GENE_REPO`, `FRI_VENV`, `FRI_OFFLINE_CACHE`, `OFFLINE_ISOLATION`).

## CI

GitHub Actions ([`checks.yml`](../.github/workflows/checks.yml)) runs the unit tests and ruff, then
builds the runtime image and imports every module in it as UID 10001 with no network, checking that
neither Gene nor Red is inside. It skips Markdown-only changes. The integration suites run through the
offline checks above, not in CI.

## Live model checks

`python -m fri.fri_eval --suite <name>` (suites such as `voice`, `controls`, `requests`, `sass`) asks
the real model about fictional conversations and scores the answers. It reads the same controller and
ChatGPT settings as Fri, makes real model calls on that plan and records their usage, but never posts
to Discord or creates member conversation history. It isn't part of the offline checks.

## Pull requests

- Branch from `main` and keep commits small, with imperative subjects.
- Every change gets an independent review of its exact head before it merges. Findings are reproduced
  before they're fixed; narrower ones become issues labelled `edge-case`.
- Maintainers' PR bodies end with a hidden block that the daily release reads:

  ```text
  <!-- release-notes
  type: feature | fix | chore
  severity: P0 | P1 | P2 | P3      (fixes only)
  roadmap: none
  codex_rounds: 0
  codex_verdict: pending
  items: N                          (fixes only)
  summary: one line a member would understand
  -->
  ```

## Keep it public-safe

This repository is public. Never commit or paste tokens, ChatGPT session files, `.env` files, real
Discord IDs or member messages, in code, tests, docs, commits, PRs, issues or logs. Tests use
invented data. Never send test dialogue into a live server's conversations; use `fri.fri_eval`.
