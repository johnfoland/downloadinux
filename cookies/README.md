# cookies/

Everything in this directory is gitignored. `cookies.txt` holds live session
credentials for every site you're logged in to — treat it like a password file.

## Just use the recipe

```sh
just cookies
```

Reads your host Firefox profile and writes `cookies.txt` here in Netscape
format, chmod 600, then reports which sites it actually found sessions for.
Multiple profiles:

```sh
ls ~/Library/Application\ Support/Firefox/Profiles/
just cookies widfsx0p.default-release
```

## Why this exists

The host configs used `--cookies-from-browser firefox` (yt-dlp) and
`"cookies": ["firefox"]` (gallery-dl). Neither works in the container: there is
no Firefox profile in there, and on macOS the profile is encrypted against the
local keychain regardless. Export on the host, use the file in here.

Both configs point at `~/cookies/cookies.txt` inside the container, which is
this directory.

## Doing it by hand

Any Firefox extension that emits Netscape format works — "cookies.txt" or
"Get cookies.txt LOCALLY". Export while logged in, save as `cookies.txt` here.

The recipe is just this, which is worth knowing if you ever need to vary it:

```sh
yt-dlp --cookies-from-browser firefox --cookies cookies/cookies.txt \
       --simulate --quiet 'https://www.youtube.com/watch?v=aqz-KE-bpKQ'
```

yt-dlp writes its cookie jar back to `--cookies` on exit, which is what turns
a live browser profile into a portable file. The URL is only there to give it
something to do.

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

Consistency matters more than which browser you claim to be. All three places
that send a user-agent are pinned to the same string:

- `config/yt-dlp/dlmp4.conf` → `--user-agent`
- `config/gallery-dl/config.json` → `extractor.user-agent`
- `config/gallery-dl/config.json` → `extractor.instagram.user-agent`

That third one is not redundant. gallery-dl's Instagram extractor declares its
own `useragent`, and gallery-dl deliberately ignores the *global*
`extractor.user-agent` for any extractor that does — only an extractor-specific
key overrides it.

`just cookies` runs `just useragent` automatically, which reads your host
Chrome version and rewrites all three. Run `just useragent` on its own after a
Chrome update; `just doctor` warns if they drift apart, fall behind the host
version, or stop being Chrome.

If Instagram ever does challenge the session, the fix is the same as for
expiry: clear it in Firefox, then re-run `just cookies`.

## Expiry

Instagram and YouTube sessions go stale in days-to-weeks. When downloads start
403ing or silently returning nothing, re-export first.

`just doctor` checks for the real `sessionid` cookie rather than just the file's
existence, so it tells you the truth about whether you're actually logged in.
Note that yt-dlp rewrites this file on every run, so its size and timestamp
mean nothing on their own.
