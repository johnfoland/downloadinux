# Downloadinux planning documents

These documents capture the repository review and agreed direction as of
2026-09-27. They describe a proposed refactor; the implementation still uses the
current shell, `just`, and tmux workflow.

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
