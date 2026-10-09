# Status

Last updated 2026-10-10 by `relay-execute`.
Last review: 2026-10-10 (P0003-C01, accepted).

## Where this stands

- **P0001** (Stage 1, portable configuration and shared operations) is
  complete: DEC-7 to DEC-23, delivery items D1–D5, chunks P0001-C01 to
  P0001-C06.
- **P0002** (Stage 1 follow-up hardening) is complete: DEC-24 to DEC-27,
  delivery items D1–D3, chunks P0002-C01 to P0002-C03 (PRs #30–#32;
  `9b3251b`, `47b82da`, `2350732`). Its completion audit records the accepted
  tests, current-user documentation, and closure of #14, #20, #22, and #26
  in [the project closeout](projects/P0002-stage1-followups.md#8-closeout).
- **P0003** (Stage 2, named cookie sources) is active: DEC-28 to DEC-33
  settle its source store, browser/file operations, parser, submission-pinned
  revisions/private startup jars, independent user-agent policy, and host-command
  migration. DEC-31 and DEC-33 were amended on 2026-10-09 to the maintainer's
  answers: pin revisions at submission and retire container download shortcuts.
  [P0003-C01](chunks/P0003-C01.md) implements D1's strict parser/summaries,
  immutable registry reader, and atomic host writer, accepted through PR #39
  after normalized-domain duplicate and BOM/UTF-8 diagnostic rework (649 tests
  and all seven gate steps green).
  [P0003-C02](chunks/P0003-C02.md) implements locked legacy migration and
  host-only `cookies list`/`inspect`/`default` commands, finishing D1's build.
  Registration, refresh, and private execution jars remain unimplemented;
  Stage 1 download/export/setup/doctor routes are unchanged and ignore the
  preparatory source-store default. D2–D5 remain unstarted.
  The next project ID is P0004. Source lifecycle and canonical revision pruning
  remain deliberately outside Stage 2 (issue #35).
- **F20261005-01** (maintenance, issue #3): accepted 2026-10-05, pull
  request #6. The gate is `just check` (it needs the Docker daemon running),
  CI runs it on every pull request, and `check` is required on `master`.

## In flight

- **Chunk:** P0003-C02
- **State:** awaiting review
- **Branch:** `chunk/p0003-c02`
- **Pull request:** `#40` (ready)
- **Units committed:** a, b

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
