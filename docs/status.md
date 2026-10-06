# Status

Last updated 2026-10-06 by `relay-next`.
Last review: 2026-10-06 (P0001-C01, accepted with follow-ups).

## Where this stands

- **P0001** (Stage 1, portable configuration and shared operations) is
  active: DEC-7 to DEC-15, delivery items D1–D5.
- **F20261005-01** (maintenance, issue #3): accepted 2026-10-05, pull
  request #6. The gate is `just check` (it needs the Docker daemon running),
  CI runs it on every pull request, and `check` is required on `master`.
- **P0001-C01** (D1: package, settings model, `config` commands): accepted
  with follow-ups 2026-10-06, pull request #12. D1 is done.
- **P0001-C02** (D2, first part: Compose files and layer, image and
  entrypoint, lifecycle commands, recipe rewiring, smoke test; the C01
  error-handling follow-up): cut 2026-10-06, ready for an executor.
  Remaining after it: the rest of D2 (`shell`, `vpn`, `servers`, login on
  set-token, their wrappers, the side-by-side check), then D3–D5.

## In flight

- **Chunk:** P0001-C02
- **State:** executing
- **Branch:** `chunk/p0001-c02`
- **Pull request:** #15 (draft)
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

One line per chunk: the decision-log IDs it added.

- P0001-C01: DEC-14.
- P0001-C02 (cut): DEC-15.
