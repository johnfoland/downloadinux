# Downloadinux planning documents

These documents capture the repository review of 2026-09-27 and the refactor
it set in motion. Stage 1, portable configuration and shared operations
(project P0001), is implemented: a host CLI, a settings file outside the
checkout, and the `just` recipes as wrappers over it. Stage 2, named cookie
sources, is settled in active project [P0003](projects/P0003-named-cookie-sources.md)
with its internal parser and atomic source-store foundation implemented in
P0003-C01. Public source operations, legacy migration, and private execution
jars remain pending. Stages 3–4 (a persistent worker in place of tmux
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

The [relay workflow](../AGENTS.md#relay-workflow) keeps these alongside the
planning documents. They record how work is done, not how the product behaves:

- [Decision log](decisions.md): settled questions, cited as `(DEC-n)`.
- [Status](status.md): what is in flight and what to do next.
- [Projects](projects/index.md) and [chunk briefs](chunks/).
- [Review checklist](review-checklist.md) and
  [brief checklist](brief-checklist.md).
