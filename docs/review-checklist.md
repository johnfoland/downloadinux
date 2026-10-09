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

### §2. A test double fails differently from the call it replaces

A test that monkeypatches a library or OS call to raise builds the exception
by hand, and gives it attributes the real call never sets (a `filename` that
names the destination where the real `os.link` names the source, an errno
the real call cannot return). The code under test then handles the fake
correctly and the real failure wrongly, with every test green. When a test
fakes a failure, check what the real call raises (run it once against a real
failing input) and build the fake the same way; better, cause the real
failure (a read-only directory, an absent executable on an empty `PATH`)
where the platform allows.

*Found:* P0001-C02 review: `test_hard_link_failure` faked `os.link` raising
with the destination as `filename`; the real `os.link`, like `mkstemp`,
names the temporary file, so `dlx init` into a read-only directory reported
a `.config.toml.….tmp` file that no longer existed instead of the settings
file.

### §3. An "unreadable input" contract catches only the parser's documented exceptions

A rule says that input which cannot be read or parsed is handled gracefully
(a warning, a `fail` check, a default), and the code wraps the parser in an
`except` naming the exception types the library documents. Parsers raise
others on inputs their authors did not anticipate, and those escape as a
traceback with every test green, because the tests feed only the malformed
inputs the documented types cover. Where the contract is "cannot be read →
graceful", treat any exception from parsing that input as unreadable (the
input, not the code, is what failed), and test at least one malformed input
of a shape the documented types do not name.

*Found:* P0001-C04 review: `useragent.chrome_version` caught `OSError`,
`ValueError`, and `ExpatError`; `plistlib` raises `AttributeError` on a
`<date>` it cannot parse, so `dlx config user-agent` printed a traceback
instead of DEC-21's warning (issue #23).

### §4. Duplicate validation uses keys from before library normalization

A wrapper rejects duplicate raw input keys, then a library changes those keys
before inserting records into its own keyed collection. Two inputs that passed
the wrapper's uniqueness test can silently overwrite each other. Validate
uniqueness in the representation the consumer stores, and test normalization
collisions alongside distinctions that must remain case-sensitive. Sweep every
aggregate insertion boundary and preserve independent normalization fixes.

*Found:* P0003-C01 review: `cookiejar.parse` accepted two rows with the same
path/name for `Example.com` and `example.com`, then `MozillaCookieJar` lowercased
the domains and retained only one record. Dot-domain, HttpOnly, and empty-name
variants reproduced the loss; distinct paths/names remained separate. The only
aggregate insertion boundary is shared by reads, serialization, and publication.

### §5. Error locations apply a transformed-input offset to the original input

A decoder or parser removes a prefix or transforms its input, and its error
position refers to that transformed buffer. Error rendering counts lines in
the original buffer using that offset, pointing the user at the wrong line.
Use the buffer the error describes or map its position back explicitly. Test
errors immediately after boundaries, with and without every accepted prefix
and line-ending form, rather than testing prefixes only on valid inputs.

*Found:* P0003-C01 review: `utf-8-sig` removed the BOM before reporting a
`UnicodeDecodeError.start`, but `cookiejar.parse` sliced the original bytes.
Invalid bytes on lines 2–4 reported line 1 with a BOM and the right line
without it. The sweep of decoder-error handlers found no sibling offset
calculation requiring another fix.
