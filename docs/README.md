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
Private execution jars and D4 cutover remain pending; downloads/export/setup/doctor still use the Stage 1 shared jar
and ignore the preparatory registry default. Stages 3–4 (a persistent worker in place of tmux
and the Python + Textual TUI) remain proposals; batch runs still use tmux.

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

The [relay workflow](../AGENTS.md#relay-workflow) keeps its process files in
the git-ignored `.relay/` directory in the main working tree. They record how
work is done, not how the product behaves, and are not available in a clone:

- [Decision log](decisions.md): settled questions, cited as `(DEC-n)`.
- `.relay/status.md`: what is in flight and what to do next.
- `.relay/projects/` and `.relay/chunks/`: local projects and chunk briefs.
- `.relay/review-checklist.md` and `.relay/brief-checklist.md`.

Pull requests carry executor reports and review comments as the remote record.
