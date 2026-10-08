# cookies/

The cookie jar moved to the managed directory at `paths.cookies`, by default
`~/.local/share/downloadinux/cookies/cookies.txt`. The old `cookies.txt` here
is no longer read. `dlx config import-env` copies it once, leaving the original
in place. Cookie files hold live session credentials; treat them like passwords.
Once the import has run, delete the old jar here, or `chmod 600` it if you
keep it: it may be world-readable.

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
succeeds.

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
