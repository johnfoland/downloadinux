# The brief checklist

Every class of mistake a chunk *brief* has carried at least once, kept so the
next cut looks for it by default. Each entry is a *shape*, not a rule: the
authority docs remain the authority on what the code must do. This file
records only where a planner, holding the right decisions, still handed an
executor a brief that was wrong.

It is the cut-time companion to the review checklist. The division is by
*when* the mistake is made, not by who finds it: a defect in the code an
executor wrote belongs there, a defect in the instructions the planner wrote
belongs here. Most entries are found in review, because that is when a brief's
error becomes visible; the provenance names the chunk whose brief carried it.

**How it is used.** `/relay-next` takes each draft brief through every entry
here before committing it. A fix that matches an entry cites it as
`brief-checklist §N` in the review or the chunk ledger. `/relay-execute` does
not read this file: an executor builds the brief it is given.

**How it grows.** When a review finds that the *brief* was wrong rather than
the code (a decision that reached no unit, an instruction that contradicted
itself, a *Tests* line that invented a condition), `/relay-next` adds it here
as the next numbered entry with the finding that revealed it. Entries are not
renumbered or retired.

The boundary with `/relay-next` itself: the skill keeps the *procedure* (what
to read, decide, and write, in what order). This file keeps the recurring ways
the product of that procedure has come out wrong.

---

## The catalogue

None yet.
