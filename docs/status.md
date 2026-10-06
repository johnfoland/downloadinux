# Status

Last updated 2026-10-06 by `relay-next`.
Last review: 2026-10-06 (P0001-C04, accepted with follow-ups).

## Where this stands

- **P0001** (Stage 1, portable configuration and shared operations) is
  active: DEC-7 to DEC-22, delivery items D1–D5.
- **F20261005-01** (maintenance, issue #3): accepted 2026-10-05, pull
  request #6. The gate is `just check` (it needs the Docker daemon running),
  CI runs it on every pull request, and `check` is required on `master`.
- **D1** done: P0001-C01, pull request #12. **D2** done: P0001-C02 and
  P0001-C03, pull requests #15 and #16. **D3** done: P0001-C04, accepted
  with follow-ups 2026-10-06, pull request #19.
- **P0001-C05** (D4: `doctor [--json]`, its wrapper, the smoke test's
  `doctor --json`; issues #23 and #21 from the C04 review): ready for an
  executor. Remaining after D4: D5.

## In flight

- **Chunk:** P0001-C05
- **State:** executing
- **Branch:** `chunk/p0001-c05`
- **Pull request:** `#24` (draft)
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
- P0001-C05 (cut): DEC-22.
