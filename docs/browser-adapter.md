# Host browser adapter evidence

Validated on 2026-10-10 for [PR #41](https://github.com/johnfoland/downloadinux/pull/41) against the locked PyPI release **2026.08.19**
(`yt-dlp==2026.8.19`, `uv.lock`). Updates require revalidating these boundaries.

The installed [cookie module](https://github.com/yt-dlp/yt-dlp/blob/2026.08.19/yt_dlp/cookies.py)
exposes `extract_cookies_from_browser(browser_name, profile=None,
logger=YDLLogger(), *, keyring=None, container=None)`. The adapter calls only
that entry point, without constructing `YoutubeDL`, loading extractors, or
submitting a URL. Logger signatures are `debug(message)`, `info(message)`,
`warning(message, only_once=False)`, `error(message, *, is_error=True)`, and
optional `progress_bar()`. Warnings/errors and Chromium's info-level failed
cookie count refuse publication; diagnostics and unexpected exception text
are consumed internally. Python warnings and stdout/stderr are contained too.

`YoutubeDLCookieJar.save(filename=None, ignore_discard=True,
ignore_expires=True)` accepts an in-memory stream without reading an existing
destination. However, it mutates session expiry to zero and its `_really_save`
omits available HttpOnly metadata. The adapter therefore passes the real jar
through the shared Mozilla/Netscape serializer and strict parser, retaining
session/expired/secure/domain/available HttpOnly semantics with a record-count
check. That check covers serialization only. Both locked Firefox and Chromium
extractors construct `YoutubeDLCookieJar` and call `set_cookie` for each extracted
row; its usual keyed insertion can overwrite credentials from different Firefox
containers or Chromium partitions before returning a jar.

The adapter temporarily substitutes a subclass at that constructor boundary.
Before each insertion it rejects repeated domain/path/name keys, normalizing
domains with the shared parser's lowercase rule while preserving case-sensitive
paths/names and distinct host/dot-domain keys. It checks both the jar's stored
key and the shared serializer's Netscape key (including empty names). A collision
is recorded before raising, so even an upstream handler that catches the exception
cannot turn it into a successful export. Errors expose only source/browser/selector
context and container/import remedies; they never include a colliding key or value.
Extraction, profile SQL, and decryption remain delegated to yt-dlp.

The locked API has no injectable jar factory. An adapter lock serializes the
temporary constructor override and diagnostic containment, with unconditional
constructor/context restoration on success or failure. A context variable limits
duplicate rejection to the active adapter call; unrelated yt-dlp threads retain
their normal insertion behavior. This boundary must be revalidated when updating
the dependency, together with the existing extraction and serialization contracts.
Browser extractors do not expose HttpOnly from every database; the
adapter preserves metadata that they provide, without adding private browser SQL.

`tests/test_cookie_browser.py` pins the exact signature/keyring set, real-jar
serialization differences, clean empty exports, normalized-key collision
rejection, and safe warning/error/debug/exception paths. Synthetic Firefox
SQLite profiles drive real extraction, including named/none/all containers,
missing profiles and schema warnings. Synthetic Chrome/Chromium SQLite profiles
on both platform selectors drive real extraction and AES decryption; only the
OS key retrieval boundary is replaced with synthetic keys. Missing keys drive
the real warning/partial-export paths. Network, YoutubeDL/extractor entry points,
profile discovery outside fixtures, and real keychains are blocked.

`tests/test_cookie_browser_collisions.py` drives the real extraction entry point
with Firefox schema-15 container rows and Chrome/Chromium schema-23 partition
rows on both macOS/Linux selectors. It rejects same-key, domain-case, and
empty-name collisions in both row orders; distinct case-sensitive paths/names,
host/dot-domain keys, and explicit Firefox named/none selectors succeed. Public
add/replace/refresh tests preserve the anonymous migration baseline or existing
registry, default, immutable material, settings, and external profiles after
failure, and the prior copy remains inspectable. Additional boundary tests pin
caught-insertion-error refusal, unrelated-thread behavior, simultaneous adapter
calls, and restoration of the constructor and diagnostic streams. The old
returned-jar collision double tests serialization only; these real profiles
prove rejection before extraction can lose records.

Linux keyring IDs, in upstream declaration order: `KWALLET`, `KWALLET5`,
`KWALLET6`, `GNOMEKEYRING`, `BASICTEXT`. KWallet requires an accessible desktop
wallet and `dbus-send`; GNOME requires `secretstorage` in the host interpreter
and an unlocked accessible Secret Service. These remain browser-only optional
prerequisites, per [upstream dependencies](https://github.com/yt-dlp/yt-dlp/tree/2026.08.19#dependencies).
Imports require neither. The host dependency has no media extras.
