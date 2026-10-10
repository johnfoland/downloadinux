# cookies/

The cookie jar moved to the managed directory at `paths.cookies`, by default
`~/.local/share/downloadinux/cookies/cookies.txt`. The old `cookies.txt` here
is no longer read. `dlx config import-env` copies it once, leaving the original
in place. Cookie files hold live session credentials; treat them like passwords.
Once the import has run, delete the old jar here, or `chmod 600` it if you
keep it: it may be world-readable.

## Preparatory named sources

`dlx cookies list [--json]`, `dlx cookies inspect NAME [--json]`, and
`dlx cookies default [NAME] [--anonymous]` are implemented. **Downloads,
`just cookies`, setup, and doctor still use the Stage 1 managed jar and ignore
this source-store default, even when it is anonymous.** File/browser registration
and explicit refresh are implemented. Private execution jars (PR #45) and the
container runner that creates them (PR #49) are shipped, and the public
cleanup front is implemented (PR #52), but no download or recipe creates
snapshots until the pending download cutover. `dlx cookies cleanup` asks the running
container's runner to remove abandoned ones (a hard-killed download's, once
downloads use them), skipping any still in use or unverifiable, and prints
both counts. It needs the running container from an image built with the
runner, and never reads the source store or migrates.

On first source access, a valid nonempty managed `paths.cookies/cookies.txt`
is copied into immutable source `legacy` and selected as the future default.
Missing or header-only material creates an anonymous empty registry; malformed
or unreadable material fails with a repair/move-aside hint and publishes nothing.
Migration never changes either old jar's bytes or modes. An existing registry
is kept unchanged, and a damaged one is never reset automatically.

```sh
dlx cookies list --json
dlx cookies default
# When list reports a migrated legacy source:
dlx cookies inspect legacy
dlx cookies default legacy
dlx cookies default --anonymous
```

`sources.json`, `.lock`, and `sources/<name>/<revision>.txt` live below
`paths.cookies`. Managed directories are 0700; published files and lock are
0600. List shows origin/revision/time/default metadata; inspect adds expiry
counts and domain/session-cookie presence without individual cookie names or
values, and never asserts login validity. Default changes retain revisions and
timestamps. List/inspect/default/import need no browser. Source operations need no Docker,
token, or user-agent operation.
A later `just cookies` export changes the old Stage 1 jar; the immutable
`legacy` inspection copy remains unchanged until explicitly refreshed or replaced.

```sh
dlx cookies browsers --json
dlx cookies import imported "~/Exports/account cookies.txt"
dlx cookies add-browser personal --browser firefox --profile "work profile" --container none
dlx cookies add-browser work --browser chrome --profile "Default"
dlx cookies refresh imported  # reread the stored file origin
dlx cookies refresh personal  # reuse the stored browser selectors
dlx cookies default imported  # explicit selection for the future cutover
# Explicit replacement may change origin or kind without switching defaults:
dlx cookies import personal "~/Exports/replacement.txt" --replace
```

Registration validates before atomic publication, retaining old revisions and
the current anonymous/named default. Imports are immutable managed copies, never
links to the external origin; imports/profiles are never chmodded or overwritten.
Warnings, partial exports, parser/publication errors, and concurrent changes to
the original source fail safely. If legacy migration succeeded first, that
committed baseline remains. Clean empty files/exports are valid.

The browser adapter uses locked yt-dlp 2026.08.19 directly without a URL/probe.
Firefox accepts containers (`none` = outside, omitted = all); Chrome/Chromium
accept Linux keyrings only. Profile names stay names; path-like/tilde profiles
and import paths become absolute without resolving symlinks. See the
[capabilities/prerequisites](../README.md#browser-capabilities-preparatory-stage-2)
for the exact keyring set and Linux desktop dependencies. Netscape import is
the browser-independent alternative; these commands do not alter user-agent.

Duplicate domain/path/name keys fail before browser extraction can overwrite a
record, including collisions between Firefox containers or Chromium partitions.
For Firefox, choose an explicit named container or `--container none`; omitted
container still exports all containers when their keys are distinct. Alternatively,
import a Netscape file with distinct keys. A failed export leaves the committed
source, default, and managed copies unchanged.

Refresh is explicit, never automatic. It always publishes a fresh revision/time
on success, including identical or valid empty material. A missing origin does
not invalidate the last managed copy: inspect still works. Restore/export the
origin, check the profile/keyring/keychain, or explicitly replace the origin.
Failures preserve source/default/material, and a source changed during extraction
refuses with retry guidance. No registration or refresh invokes the optional
Chrome user-agent lookup or edits settings.

## Just use the recipe

```sh
just cookies
```

Reads your host Firefox profile and writes `paths.cookies`/`cookies.txt` in
Netscape format (0600 in a 0700 directory), then reports cookie counts by site.
It stages the export beside the managed jar, validates the Netscape header,
and atomically replaces the jar only when that staged output is usable. A
missing, empty, or malformed export leaves the previous jar untouched. The
fixed YouTube probe may still fail after Firefox cookies were exported; in
that case the recipe warns, publishes the usable jar, prints its counts, and
continues successfully.

Multiple profiles:

```sh
ls ~/Library/Application\ Support/Firefox/Profiles/
just cookies widfsx0p.default-release
```

## Why this exists

The host configs used `--cookies-from-browser firefox` (yt-dlp) and
`"cookies": ["firefox"]` (gallery-dl). Neither works in the container: there is
no Firefox profile in there, and on macOS the profile is encrypted against the
local keychain regardless. Export on the host, use the managed file.

The container mounts the managed directory at `/cookies`, and every download
is given `/cookies/cookies.txt` as an argument.

## Doing it by hand

Any Firefox extension that emits Netscape format works — "cookies.txt" or
"Get cookies.txt LOCALLY". Export while logged in, save as `cookies.txt` under
the configured `paths.cookies` directory, with mode 0600.

For a manual export you can run the underlying yt-dlp command directly when
you need to vary it:

```sh
yt-dlp --cookies-from-browser firefox --cookies ~/.local/share/downloadinux/cookies/cookies.txt \
       --simulate --quiet 'https://www.youtube.com/watch?v=aqz-KE-bpKQ'
```

yt-dlp writes its cookie jar back to `--cookies` on exit, which is what turns
a live browser profile into a portable file. The URL is only there to give it
something to do. Unlike `just cookies`, this manual command writes the managed
jar directly: it does not stage and validate the replacement or preserve the
previous jar when the export is unusable.

## Cookies come from Firefox, the user-agent says Chrome

This mismatch is deliberate. Instagram only serves its 1080p video ladder to
Chrome user-agents — with a Firefox UA both downloaders cap at 720p, and
gallery-dl prints `Potentially lowered video quality due to non-Chrome
User-Agent` to tell you so.

It is also the combination gallery-dl ships by default: its Instagram extractor
hardcodes a Chrome UA, while its documented cookie workflow is to pull them
from whichever browser you actually use. Chrome-UA-plus-Firefox-cookies is what
most gallery-dl users are running without knowing it.

Instagram's `sessionid` is not bound to a user-agent. What actually draws
attention, roughly in order:

1. **Exit IP** — a datacenter/VPN address carrying a residential session is a
   far stronger signal than any UA string. This is the one to think about.
2. **Request rate** — already handled by the `sleep` settings here and the
   throttling in `dlmp4_batch`.
3. **A UA that keeps changing** — which is why it's pinned rather than derived
   live.

Consistency matters more than which browser you claim to be. One setting,
`downloads.user_agent` in your `config.toml`, holds the string, and every
download passes it to all three places that send a user-agent:

- yt-dlp → its `User-Agent` header
- gallery-dl → `extractor.user-agent`
- gallery-dl → `extractor.instagram.user-agent`

That third one is not redundant. gallery-dl's Instagram extractor declares its
own `useragent`, and gallery-dl deliberately ignores the *global*
`extractor.user-agent` for any extractor that does — only an extractor-specific
key overrides it.

`just cookies` runs `just useragent` (`dlx config user-agent`) automatically,
which reads your host Chrome version and writes the matching user-agent into
the setting; no file in the checkout changes. Run `just useragent` on its own
after a Chrome update; `just doctor` warns if the setting falls behind the
host version or stops being Chrome. Without Chrome in `/Applications` (any
Linux host) it warns and leaves the setting alone, so the export still
succeeds. To pin an exact literal instead (on Linux, say), pass it as one
argument: `just useragent "Mozilla/5.0 …"` (`dlx config user-agent "…"`).
That form never reads Chrome; see the main README.

`just setup` attempts the same refresh exactly once before its build when it
keeps an existing Instagram session or publishes a usable export. The existing
session branch runs `just useragent` directly; the export branch relies on
`just cookies`. An unusable export skips the refresh and setup carries on.

If Instagram ever does challenge the session, the fix is the same as for
expiry: clear it in Firefox, then re-run `just cookies`.

## Expiry

Instagram and YouTube sessions go stale in days-to-weeks. When downloads start
403ing or silently returning nothing, re-export first.

`just doctor` checks for the session cookie itself (Instagram's `sessionid`;
YouTube's `__Secure-3PSID` or `SAPISID`) rather than just the file's
existence, so it tells you whether a session cookie is *present*. It cannot
tell whether the site still accepts it: an expired session looks the same in
the jar, so downloads failing is what tells you to re-export. Note that
yt-dlp rewrites this file on every run, so its size and timestamp mean
nothing on their own.
