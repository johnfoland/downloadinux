# Decisions

Settled questions, so they aren't reopened by accident. Each entry says what
was decided and why, and is cited from the docs and the code as `(DEC-n)`.

- Add new entries at the end, numbered one past the greatest existing ID,
  with the month they were settled.
- To change one, discuss it first, then edit the entry in place (don't delete
  it): say what replaced it, when, and why, and update every doc and code site
  that cites it. Never give a changed decision a new ID.

Each entry has this shape:

```markdown
## DEC-1: <short title> (YYYY-MM)

What was decided and why, in a paragraph or two: the alternatives it rules
out and the reason they lost.

*Where:* the docs that carry the rule, if any.
```

DEC-1 to DEC-6 are the agreed decisions from the 2026-09 architecture review.

## DEC-1: macOS and Linux hosts, downloads run in Docker (2026-09)

Support both macOS and Linux hosts, and keep download execution (yt-dlp,
gallery-dl, media tools, VPN) inside Docker. Native downloader execution on the
host and a remote-server control protocol are out of scope.

*Where:* [target design](target-design.md), *Proposed boundaries*.

## DEC-2: Host specifics stay out of operational logic (2026-09)

Host-specific setup and file locations (home directories, usernames, browser
paths, OS tooling) are resolved once at the boundary, not threaded through
operational code. The worker does not need to know the host username or
mirror the host home directory.

*Where:* [target design](target-design.md), *Configuration and paths*.

## DEC-3: Named cookie sources, selectable per job (2026-09)

Cookies come from named sources, each backed by a browser/profile export or an
imported Netscape file, and a job selects one, or runs anonymously as an
explicit choice. This replaces the single shared `cookies/cookies.txt`.

*Where:* [target design](target-design.md), *Named cookie sources*.

## DEC-4: Remove tmux entirely (2026-09)

tmux is not part of the worker or TUI architecture. Jobs outlive the UI
through persisted worker state, not a terminal multiplexer, and the TUI never
attaches to a terminal session.

*Where:* [target design](target-design.md), *Persistent jobs*;
[implementation plan](implementation-plan.md), *Stage 3*.

## DEC-5: The TUI is Python + Textual (2026-09)

The terminal UI is built in Python with Textual. Specific Textual APIs,
dependency versions, packaging, and keybindings are deferred to
implementation research.

*Where:* [target design](target-design.md), *Python + Textual TUI*.

## DEC-6: Existing download behavior is preserved as presets (2026-09)

The current `dlmp4` and `dlgal` naming, quality, metadata, archive, and
throttling choices carry over as explicit named presets and migration
defaults, rather than being redesigned.

*Where:* [current state](current-state.md), *Download behavior worth
preserving*; [target design](target-design.md), *Configuration and paths*.
