# cookies/

Downloads read cookies from **named cookie sources** kept in the managed
directory at `paths.cookies` (by default `~/.local/share/downloadinux/cookies/`),
never from this checkout directory. The old `cookies.txt` here is no longer
read. `dlx config import-env` copies it once into the managed directory,
leaving the original in place, and the first cookie-aware command then
registers that copy (below). Cookie files hold live session credentials; treat
them like passwords. Once the import has run, delete the old jar here, or
`chmod 600` it if you keep it: it may be world-readable.

To import a jar that only exists here without `import-env`, register it and
select it:

```sh
dlx cookies import mine cookies/cookies.txt
dlx cookies default mine
```

## Just use the recipe

```sh
just cookies
```

This is the account switch. It runs `dlx cookies firefox`: it reads your host
Firefox profile (Firefox's default discovery, all containers), creates or
replaces the source named `firefox` with that export, and makes it the default,
all in one atomic publication. It prints the same summary as `dlx cookies
inspect`: counts by domain and whether a recognized session cookie is
present, never a cookie name or value. A failed export or parse exits 1 with
`source firefox: …` and changes nothing, default included. It does not touch
`downloads.user_agent`, and it never requests a site.

Multiple profiles:

```sh
ls ~/Library/Application\ Support/Firefox/Profiles/
just cookies widfsx0p.default-release
```

`just setup`'s cookie step is `dlx cookies setup`. With a named default it
keeps it, prints `Keeping the default cookie source NAME; it was not
refreshed.` and its summary, and asks nothing. With an anonymous default it
asks `Export Firefox cookies now? [y/N] `: only `y` or `yes` (any case) runs
the Firefox export; anything else, or end of input, keeps anonymous. A
failed step is reported and setup carries on with the build.

## Sources, the default, and downloads

```sh
dlx cookies list                      # every source, and the default
dlx cookies inspect firefox           # counts and session-cookie presence
dlx cookies default work              # the default for later downloads
dlx cookies default --anonymous       # later downloads use no cookies
dlx cookies refresh firefox           # reread the stored origin
```

`dlx dl` and `dlx batch` use `--cookie-source NAME`, or `--anonymous`, or
else the default; with no default they run anonymously. The host pins the
source's current revision when you run the command, and the container's
runner copies exactly that revision into a private jar for that one call or
that whole batch, then deletes it. A refresh or a new default afterwards
applies to the next command, never to a run already submitted. `gallery`
passes the cookies to Instagram only.

Register other origins without a browser switch:

```sh
dlx cookies browsers --json
dlx cookies import imported "~/Exports/account cookies.txt"
dlx cookies add-browser personal --browser firefox --profile "work profile" --container none
dlx cookies add-browser work --browser chrome --profile "Default"
# Explicit replacement may change origin or kind without switching defaults:
dlx cookies import personal "~/Exports/replacement.txt" --replace
```

`sources.json`, `.lock`, and `sources/<name>/<revision>.txt` live below
`paths.cookies`. Managed directories are 0700; published files and the lock
are 0600. Registration validates before atomic publication, retaining old
revisions and the current default; it never selects a default itself (only
`just cookies` / `dlx cookies firefox` and `dlx cookies default` do). Imports
are immutable managed copies, never links to the external origin; imports and
profiles are never chmodded or overwritten. Warnings, partial exports,
parser/publication errors, and concurrent changes fail safely. Clean empty
files and exports are valid. Imports need no installed browser.

Refresh is explicit, never automatic. It always publishes a fresh revision and
time on success, including identical or valid empty material. A missing origin
does not invalidate the last managed copy: inspect still works. Restore or
re-export the origin, check the profile, keyring or keychain, or replace the
origin explicitly. No registration or refresh edits settings or the
user-agent.

The browser adapter uses the locked yt-dlp 2026.08.19 directly, without a URL
or probe. Firefox accepts containers (`none` = outside, omitted = all);
Chrome/Chromium accept Linux keyrings only. Profile names stay names;
path-like or `~` profiles and import paths become absolute without resolving
symlinks. See [browser capabilities](../docs/reference.md#browser-capabilities) for
the exact keyring set and Linux desktop dependencies.

Duplicate domain/path/name keys fail before browser extraction can overwrite a
record, including collisions between Firefox containers or Chromium
partitions. For Firefox, choose an explicit named container or `--container
none`, or import a Netscape file with distinct keys.

`dlx cookies cleanup` asks the running container's runner to remove private
snapshots a hard kill or a stopped container left behind, skipping any still
in use or unverifiable, and prints both counts. It never reads the source
store or migrates.

## The Stage 1 jar and migration

On the first source access (any `cookies` command but `browsers` and
`cleanup`, a `dl` or `batch` without `--anonymous`, `setup`, or `doctor`),
when there is no `sources.json`, a valid nonempty `paths.cookies/cookies.txt`
becomes the file source `legacy` and the default, in one atomic publication.
A missing or header-only jar creates an anonymous empty registry; malformed or
unreadable material fails with a repair/move-aside hint and publishes nothing
(`--anonymous` still works). Migration never changes either old jar's bytes or
modes, and nothing writes to them afterwards. An existing registry is kept
unchanged, and a damaged one is never reset automatically.

## Why this exists

The host configs used `--cookies-from-browser firefox` (yt-dlp) and
`"cookies": ["firefox"]` (gallery-dl). Neither works in the container: there is
no Firefox profile in there, and on macOS the profile is encrypted against the
local keychain regardless. Export on the host into a managed source; the
container gets a private copy per download.

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
   throttling in the `video` batch.
3. **A UA that keeps changing** — which is why it's pinned rather than derived
   live.

Consistency matters more than which browser you claim to be. One setting,
`downloads.user_agent` in your `config.toml`, holds the string (a single
line; downloads refuse one with a line break), and every download passes it
to all three places that send a user-agent:

- yt-dlp → its `User-Agent` header
- gallery-dl → `extractor.user-agent`
- gallery-dl → `extractor.instagram.user-agent`

That third one is not redundant. gallery-dl's Instagram extractor declares its
own `useragent`, and gallery-dl deliberately ignores the *global*
`extractor.user-agent` for any extractor that does — only an extractor-specific
key overrides it.

Neither `just cookies` nor setup changes the user-agent. Set an exact literal
with `just useragent "Mozilla/5.0 …"` (`dlx config user-agent "…"`), or, on
macOS, run `just useragent` with no argument to derive one from your host
Chrome's version. `dlx dl --user-agent=VALUE` overrides it for one run.
`just doctor` only checks that the setting is a nonempty single line.

If Instagram ever does challenge the session, the fix is the same as for
expiry: clear it in Firefox, then re-run `just cookies`.

## Expiry

Instagram and YouTube sessions go stale in days-to-weeks. When downloads start
403ing or silently returning nothing, re-export (`just cookies`) or refresh
the source you use (`dlx cookies refresh NAME`) first.

`just doctor` checks the default source for the session cookie itself
(Instagram's `sessionid`; YouTube's `__Secure-3PSID` or `SAPISID`) and warns
when only an expired one is recorded. It cannot tell whether the site still
accepts an unexpired one, so downloads failing is what tells you to
re-export. Downloader writeback goes to the private per-download copy, never
to the source.
