# Status

Last updated 2026-10-08 by `relay-next`.
Last review: 2026-10-08 (P0002-C02, accepted after rework).

## Where this stands

- **P0001** (Stage 1, portable configuration and shared operations) is
  complete: DEC-7 to DEC-23, delivery items D1–D5, chunks P0001-C01 to
  P0001-C06.
- **P0002** (Stage 1 follow-up hardening) is active: DEC-24 to DEC-27,
  delivery items D1–D3. D1's portable atomic no-clobber writes are accepted
  in P0002-C01; D2's reliable cookie and setup refresh are accepted in
  P0002-C02 after rework. D3's runtime-home ownership and project completion
  evidence are ready for an executor in P0002-C03. Remaining: accept D3 and
  audit project completion with `/relay-project`.
- **F20261005-01** (maintenance, issue #3): accepted 2026-10-05, pull
  request #6. The gate is `just check` (it needs the Docker daemon running),
  CI runs it on every pull request, and `check` is required on `master`.

## In flight

- **Chunk:** P0002-C03
- **State:** executing
- **Branch:** `chunk/p0002-c03`
- **Pull request:** `#32` (draft)
- **Units committed:** a

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
- P0001-C03: DEC-16 (cut), DEC-17 and DEC-18 (executor), DEC-19 (review).
- P0001-C04 (cut): DEC-20, DEC-21.
- P0001-C05 (cut): DEC-22 (amended in its review).
- P0001-C06 (cut): DEC-23.
- P0002-C01-a: DEC-27.
