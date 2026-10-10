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
check. This count check covers serialization only: upstream extraction can
already have overwritten duplicate domain/path/name keys, including cookies
from different Firefox containers. Rejection before that loss is still pending
in PR #41; the present adapter must not be treated as proving lossless extraction.
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

Linux keyring IDs, in upstream declaration order: `KWALLET`, `KWALLET5`,
`KWALLET6`, `GNOMEKEYRING`, `BASICTEXT`. KWallet requires an accessible desktop
wallet and `dbus-send`; GNOME requires `secretstorage` in the host interpreter
and an unlocked accessible Secret Service. These remain browser-only optional
prerequisites, per [upstream dependencies](https://github.com/yt-dlp/yt-dlp/tree/2026.08.19#dependencies).
Imports require neither. The host dependency has no media extras.
