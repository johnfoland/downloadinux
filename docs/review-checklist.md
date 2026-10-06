# The review checklist

Every class of bug a review has missed at least once, kept so the next review
looks for it by default. Each entry is a *shape*, not a rule: the authority
docs remain the authority on what the code must do. This file only records
where a passing test suite has failed to catch a real defect.

**How it is used.** `/relay-next` takes the whole chunk diff through every
entry here, after the per-unit checks against the cited doc sections. A finding
that matches an entry cites it as `review-checklist §N`. `/relay-execute` does
not work the whole list; instead the chunk brief's *Kickoff notes* name the
entries a given chunk is most likely to trip, and the executor reads those
before building.

**How it grows.** When a review finds a *class* of bug rather than an
instance, `/relay-next` adds it here as the next numbered entry, with the
finding that revealed it. The provenance is the point: it is the evidence that
the shape is real. Entries are not renumbered or retired: a shape that has
stopped mattering is cheaper to skim than to re-derive after it bites again.

---

## The catalogue

### §1. An operating-system failure escapes the CLI's error contract

The CLI's `main` catches only the project's own error types, so an `OSError`
raised below an operation (a directory that cannot be created, a file that
cannot be read or written, an executable that is not installed, a hard link
the filesystem refuses) reaches the user as a Python traceback rather than
`downloadinux: error: <path>: <reason>` with exit 1. Tests that only use
writable temporary directories never see it. Look at every filesystem and
subprocess call an operation makes and ask what happens when it fails; a test
should drive at least one such failure per command through `cli.main` and
assert the message and exit status.

*Found:* P0001-C01 review: `dlx init --config <read-only dir>/x/config.toml`
printed a `PermissionError` traceback from `files.write_atomic`.
