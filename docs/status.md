# Status

Last updated 2026-10-05 by `relay-next`.
Last review: 2026-10-05 (F20261005-01, accepted).

## Where this stands

- **P0001** (Stage 1, portable configuration and shared operations) is
  active: DEC-7 to DEC-13 settled, delivery items D1–D5.
- **F20261005-01** (maintenance, issue #3): accepted 2026-10-05, pull
  request #6. The gate is `just check` (it needs the Docker daemon running),
  CI runs it on every pull request, and `check` is required on `master`.
- **P0001-C01** (D1: package, settings model, `config` commands): cut
  2026-10-05, ready for an executor. Remaining after it: D2–D5.

## In flight

- **Chunk:** P0001-C01
- **State:** awaiting review
- **Branch:** `chunk/p0001-c01`
- **Pull request:** `#12` (ready)
- **Units committed:** a, b, c, d

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
