<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/logo-dark.svg">
  <img alt="downloadinux" src="docs/logo-light.svg" height="64">
</picture>

downloadinux runs yt-dlp and gallery-dl in a headless Debian container and
saves what they download to a folder on your computer (`~/Downloads` by
default). It can route downloads through NordVPN, which is off until you turn
it on. You drive it from a terminal UI.

It's a personal tool, shared as it is. I build and use it on macOS with Docker
Desktop, and CI runs its checks on Linux too.

## What you need

- Docker Desktop, running (`docker ps` works without an error)
- [`just`](https://github.com/casey/just), pinned to 1.40.0 in `.tool-versions`
- [`uv`](https://docs.astral.sh/uv/)
- For logged-in downloads (Instagram, members-only video): Firefox, logged in
  to those sites, and `yt-dlp` on the host to read its cookies
  (`brew install yt-dlp`)

## Set it up

```sh
git clone https://github.com/johnfoland/downloadinux.git
cd downloadinux
just setup
```

`just setup` first asks whether to use NordVPN. If you say yes, it asks for a
NordVPN access token
([how to get one](docs/reference.md#step-2--get-your-nordvpn-token)). Then it
offers to copy your Firefox cookies; skip that if you only download public
videos. It builds the image, which takes a few minutes the first time, starts
the container, and ends with a health check.

Your settings live in `~/.config/downloadinux/config.toml`, outside the
checkout, and you can change them from the TUI.

## Use it

```sh
just tui
```

If you install the CLI with `uv tool install --editable .`, plain `dlx` opens
the TUI too, from any directory.

The TUI has five tabs. Press `1` to `5` to switch between them and `?` for the
keys.

1. Jobs lists your downloads and follows the selected one's log as it runs.
   Cancel, retry, and exporting failed URLs to a queue file are keys here.
2. Submit takes URLs or a queue file, the preset (`video` or `gallery`), which
   cookies to use, and a user-agent. `ctrl+s` sends it.
3. Cookies lists your cookie sources. You can add one from a browser or a
   Netscape file, refresh one, or change the default.
4. Status shows the health checks and every setting. You edit settings here,
   store the NordVPN token, start and stop the container, and connect the VPN.
5. Activity records what you did this session. It isn't saved.

The header line shows how many jobs are running, queued, and failed, and
whether the VPN is connected.

Quitting closes the screen and nothing else. Downloads keep running, and the
next session shows the same jobs.

### Keys

| Where | Key | Action |
| --- | --- | --- |
| Everywhere | `1` to `5` | Show Jobs, Submit, Cookies, Status, Activity |
| | `ctrl+r` | Refresh the shown view and the VPN state now |
| | `q`, `ctrl+q` | Quit |
| | `?` | Key help |
| Jobs | `space` | Mark or unmark the cursor job |
| | `a` | Toggle every job and the default listing |
| | `c` | Cancel the marked jobs, else the cursor job |
| | `r` | Retry them, each keeping its own cookie rule and user-agent |
| | `R` | Retry them with a chosen cookie selection and user-agent |
| | `[`, `]` | Show the previous or next attempt's log |
| | `v`, `g` | Follow the video or gallery lane in the log pane |
| | `escape` | Return the log pane to the cursor job |
| | `e` | Export a preset's failed work to a queue file |
| Submit | `ctrl+s` | Submit the form |
| Cookies | `a` | Add a browser source |
| | `i` | Import a Netscape file |
| | `f` | Refresh the cursor source |
| | `d` | Make the cursor source the default |
| | `n` | Make anonymous the default |
| Status | `enter` | Edit the cursor setting |
| | `x` | Unset the cursor setting |
| | `t` | Store the NordVPN token |
| | `U`, `D`, `R` | Container up, down, restart |
| | `o` | Connect the VPN, or switch servers |
| | `O` | Disconnect the VPN |

In a text field, `q`, `1` to `5`, and `?` type themselves; `escape` leaves the
field, and `ctrl+q` quits from anywhere. The TUI asks before it stops or
restarts the container (naming how many running jobs that would interrupt) and
before it cancels a running job. It won't switch VPN servers or disconnect
while a job runs.

## Where downloads go

Everything lands under the downloads folder, `paths.downloads`:

| Preset | Folder |
| --- | --- |
| `video` | `dlmp4/<site>/<uploader>/` |
| `gallery` | `dlgal/` for Instagram, `gallery-dl/` for other sites |

To use another folder, such as one on an external drive, edit
`paths.downloads` in Status and restart the container with `R`. The folder has
to exist already.

## Turning the VPN on later

In Status, set `vpn.enabled` to `true`, store your token with `t`, and restart
the container with `R`. Then `o` connects; its *Choose a server* button lists
a city's least-loaded servers. While the VPN is on, queued downloads wait for
it to connect.

## What still needs the command line

- `just update` rebuilds the image with the latest yt-dlp and gallery-dl and
  restarts the container. Run it when a site that used to work stops
  downloading, and after you pull new code. Running downloads are interrupted;
  queued ones carry on. [#141](https://github.com/johnfoland/downloadinux/issues/141)
  would move this into the TUI.
- `just shell` opens a shell inside the container.
- `just nuke` deletes the container and its volumes, which logs NordVPN out.

Everything the TUI does also has a command, for scripts. `just` lists them.

## When something goes wrong

Check Status first. A failing check names its fix.

- Instagram downloads fail with 403 or come back empty: the session cookies
  have gone stale, which happens every few weeks. Log in to Instagram in
  Firefox again, then refresh the source in Cookies with `f`.
- Status says "worker not answering" right after you start the container: with
  the VPN on, the worker takes tens of seconds to come up. Wait, then press
  `ctrl+r` ([#83](https://github.com/johnfoland/downloadinux/issues/83)).

## More

[docs/reference.md](docs/reference.md) covers each setup step in detail, every
setting, cookie sources, the `dlx` and `just` commands, batch queues, and how
the container works. The planning documents behind the design start at
[docs/README.md](docs/README.md).

## License

MIT; see [LICENSE](LICENSE).
