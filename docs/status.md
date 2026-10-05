# Status

Last updated 2026-10-05 by `relay-next`.
Last review: none yet.

## Where this stands

- No project is active. The repo has no gate or CI yet.
- **F20261005-01** (maintenance, issue #3): adds `just check` and a CI
  workflow running it. Brief cut 2026-10-05; ready for an executor. Its
  executor needs the Docker daemon running, since the gate builds the image.
- **After it:** the planner adds the gate to AGENTS.md and requires the CI
  check on `master`; then `/relay-project` shapes Stage 1 of
  `docs/implementation-plan.md`.

## In flight

- **Chunk:** F20261005-01
- **State:** ready for an executor
- **Branch:** —
- **Pull request:** —
- **Units committed:** —

(Fixed shape. `State` ∈ `ready for an executor` · `executing` ·
`awaiting review` · `none`; `Branch`, `Pull request`, and `Units committed` are
`—` until set; the pull request carries `(draft)` or `(ready)`.
`/relay-execute` creates `chunk/<id>` from the integration branch, opens a
draft pull request, sets `executing`, updates `Units committed` per unit, and
sets `awaiting review` when its report lands; `/relay-next` reviews that pull
request on its branch, cuts the next chunk, and squash-merges. While a chunk
pull request is open, the copy of this file on its head branch is the
authoritative handoff.)

## Follow-ups not yet scheduled

Follow-ups are filed as GitHub issues (see *Roadmap* in AGENTS.md), not
kept here.

## Decisions by chunk

None yet. One line per chunk: the decision-log IDs it added.
