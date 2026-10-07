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

### §3. A kickoff note restates a pinned rule more strongly than the rule

Kickoff notes compress the authority into a few lines, and a compression
can turn a narrow rule into a broader one ("the token in no environment"
where the rule said "this variable only in that process"). The broader
reading then contradicts a contract an earlier chunk pinned, and the
executor has to choose between the note and the doc. Before committing a
brief, check each kickoff and *Tests* line that paraphrases a rule against
the rule's own words and against the contracts already in force, and
quote the rule where a paraphrase would widen it.

*Found:* P0001-C03: the note said the fake token must be in no environment
when the container is stopped, "nothing runs then"; the running check does
run, and under C02's invocation contract every VPN-on Compose call carries
`DOWNLOADINUX_NORDVPN_TOKEN`. The executor implemented the contract and
reported the conflict.

### §4. A *Done when* grep that the brief's own content fails

A grep in a unit's *Done when* ("finds nothing outside …") is written from
the paths being retired, but the brief itself keeps some matches: a line
another unit owns and has not changed yet, an assertion the brief pins that
names the retired path on purpose, a file another delivery item owns. The
executor then reports a check it cannot pass, or worse, edits out-of-scope
files to pass it. Before committing a brief, run each *Done when* grep
against the tree as it will stand after that unit (the files the chunk
leaves alone are already there to grep) and give every expected hit an
explicit exclusion, or say "finds only …" and list them.

*Found:* P0001-C04: unit b's grep matched the `dl` recipe line the brief
gave to unit c; unit d's matched the smoke test's pinned absence loop
(excluded from c's grep but not d's) and `.env.example`, which D5 owns.

### §5. A pinned rule's reason reaches further than the rule

A cut pins a rule with its reason ("skip X when Y, since X would pass for the
wrong reason"), and names only the case in front of it. The reason applies
just as well to a sibling the rule leaves out, and the executor, building
the rule rather than the reason, builds the sibling the old way. Before
committing a pinned rule that carries a "since" or "because", read every
sibling of the case it names (the other checks, the other mounts, the other
commands) and ask whether the reason holds there too; extend the rule, or
say why it stops.

*Found:* P0001-C05: DEC-22 skipped each `mount.<m>.writable` check after a
failed `mounted` because the probe would pass for the wrong reason, but left
`mount.config.readonly` running; on a container with no `/config`, `test -w`
failed for that reason and `doctor` reported `/config is read-only` as `ok`.

### §6. A cleanup guarantee includes failures that make cleanup unknowable or impossible

A brief says every failed path removes the operation's residue, then also asks
tests to fail the calls that identify or remove that residue. Those requirements
cannot both hold: after an identity check fails, cleanup cannot prove that the
path is still its own; after deletion fails, no implementation can make the
entry disappear. Before promising cleanup for every failure, enumerate failures
of the cleanup mechanism itself. Preserve anything whose ownership cannot be
proved, report cleanup failures, and state exactly which residue may remain.

*Found:* P0002-C01: unit a required every failed path to leave no owned
reservation while its OS-error coverage also failed `fstat`, `lstat`, and
`unlink`. DEC-27 and the reviewed brief now qualify cleanup by verifiable
identity and a working removal call.

### §7. A staged output path is allocated without checking whether the callee reads it first

An external command uses one path as both optional input and eventual output,
but a brief says to allocate a temporary file and pass it without pinning
whether that path must exist when the command starts. A test double that only
writes the path then validates the happy path while the real command reads and
rejects the empty file before it can produce anything. Before specifying a
staged external-command output, exercise both an absent path and an allocated
empty path against the real tool (or inspect its source), state the required
entry state in the unit, and require the double to reproduce any pre-write
read or validation.

*Found:* P0002-C02: the brief required a mode-0600 temporary passed to
yt-dlp's `--cookies`; yt-dlp loads any readable existing path as a Netscape
jar, so the empty `mktemp` file was rejected while the stub overwrote it.
