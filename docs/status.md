# Status

Last updated 2026-10-07 by `relay-next`.
Last review: 2026-10-07 (P0001-C05, accepted with follow-ups).

## Where this stands

- **P0001** (Stage 1, portable configuration and shared operations) is
  active: DEC-7 to DEC-23, delivery items D1–D5.
- **F20261005-01** (maintenance, issue #3): accepted 2026-10-05, pull
  request #6. The gate is `just check` (it needs the Docker daemon running),
  CI runs it on every pull request, and `check` is required on `master`.
- **D1** done: P0001-C01, pull request #12. **D2** done: P0001-C02 and
  P0001-C03, pull requests #15 and #16. **D3** done: P0001-C04, pull
  request #19. **D4** done: P0001-C05, accepted with follow-ups
  2026-10-07, pull request #24.
- **P0001-C06** (D5: `just setup` over the CLI with DEC-23's VPN question,
  Stage 1 docs, the in-place migration proof; the C05 review's follow-ups):
  ready for an executor. Its unit d migrates the maintainer's real
  installation (their choice). Remaining after D5: the P0001 closeout
  (`/relay-project`).

## In flight

- **Chunk:** P0001-C06
- **State:** executing
- **Branch:** `chunk/p0001-c06`
- **Pull request:** pending
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

One line per chunk: the decision-log IDs it added.

- P0001-C01: DEC-14.
- P0001-C02 (cut): DEC-15.
- P0001-C03: DEC-16 (cut), DEC-17 and DEC-18 (executor), DEC-19 (review).
- P0001-C04 (cut): DEC-20, DEC-21.
- P0001-C05 (cut): DEC-22 (amended in its review).
- P0001-C06 (cut): DEC-23.
