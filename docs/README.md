# Downloadinux planning documents

These documents capture the repository review of 2026-09-27 and the refactor
it set in motion. Stage 1, portable configuration and shared operations
(project P0001), is implemented: a host CLI, a settings file outside the
checkout, and the `just` recipes as wrappers over it. Stage 2, named cookie
sources, is settled under [issue #34](https://github.com/johnfoland/downloadinux/issues/34)
with its internal parser and atomic source-store foundation implemented in
P0003-C01 and locked legacy migration plus public list/inspect/default operations
implemented in P0003-C02. [PR #41](https://github.com/johnfoland/downloadinux/pull/41) adds direct host browser extraction,
support reporting, file/browser registration/replacement, and explicit refresh.
[PR #45](https://github.com/johnfoland/downloadinux/pull/45) adds internal groundwork for private execution jars: submission
pinning, exact pinned reads, lease-held snapshot allocation, and lease-safe cleanup.
PRs #49–#55 ship the container runner and the remaining fronts, and
[PR #62](https://github.com/johnfoland/downloadinux/pull/62) is the D4 cutover: downloads, batches, `just cookies`, setup,
and doctor use named sources, and the container's download shortcuts are
retired. [PR #63](https://github.com/johnfoland/downloadinux/pull/63) adds Stage 2's completion evidence (D5), so Stage 2
is implemented. Stage 3, a persistent worker and jobs in place of tmux, is
settled under [issue #64](https://github.com/johnfoland/downloadinux/issues/64)
(DEC-41 to DEC-45). Only its internal `downloadinux.job_store` foundation is
implemented: private versioned SQLite ownership, atomic pinned submissions,
exact active duplicates, and typed record reads. Lifecycle, scheduling,
execution/cancellation/shutdown, worker service and commands, and cutover
remain unimplemented; batch runs still use tmux. Stage 4,
the Python + Textual TUI, remains a proposal.

- [Current state](current-state.md): architecture, behavior worth preserving,
  portability constraints, and implementation observations.
- [Target design](target-design.md): agreed decisions, proposed boundaries,
  configuration, cookie sources, jobs, and the Python + Textual TUI.
- [Implementation plan](implementation-plan.md): stages, acceptance criteria,
  migration considerations, and decisions still to resolve.

The existing [project README](../README.md) remains the guide to running the
current implementation. Update these planning documents as decisions become
implementation, keeping proposals distinguishable from completed work.

## Process files

The [decision log](decisions.md) holds settled questions, cited as `(DEC-n)`.

The [relay workflow](../AGENTS.md#relay-workflow) keeps its process files in
the git-ignored `.relay/` directory in the main working tree. They record how
work is done, not how the product behaves, and are not available in a clone:

- `.relay/status.md`: what is in flight and what to do next.
- `.relay/projects/` and `.relay/chunks/`: local projects and chunk briefs.
- `.relay/review-checklist.md` and `.relay/brief-checklist.md`.

Pull requests carry executor reports and review comments as the remote record.
