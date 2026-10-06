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

### §1. The brief names an external reference nobody checked exists

A brief that pins a third-party name (an action's floating tag, a package
version, a download URL, a CLI flag) by assumption hands the executor a
choice the brief claims to have made: the reference may not exist, and the
executor has to pick a substitute. Before committing a brief, check every
external reference it names (`gh api repos/<owner>/<repo>/tags`, the package
index, `--help`), or say "the current release" and let the executor record
which one it pinned.

*Found:* P0001-C01: the brief asked for `astral-sh/setup-uv` "at its current
major tag"; the action publishes no floating major tag, and the executor
pinned `v10.2.0`.

### §2. The brief retires a source but lists its readers by hand

A chunk that removes or replaces where something is read from (a file, a
variable, a path) must move every reader, but a brief that names the
readers it remembers leaves the rest pointing at the retired source, and
the executor, building the brief, has no reason to look further. Before
committing such a brief, grep the repo for the retired source (`git grep
-n '<name>'`) and give every hit a unit or an *Out of scope* line.

*Found:* P0001-C02: the goal said the `.env` coupling was gone, but the
brief moved only `doctor`'s jar path; its `.env` requirement and token read
survived, so after `just token` the recipe reported the old token.
