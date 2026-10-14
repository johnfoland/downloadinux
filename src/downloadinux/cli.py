"""The `downloadinux` / `dlx` command line (DEC-7).

The CLI only parses arguments and prints; each subcommand is a thin front for
an operation the TUI will call (target design, *Host application*).
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import getpass
import json
import os
import re
import signal
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from downloadinux import (
    cookie_selectors, cookie_store, health, host_follow, host_jobs, launch, migrate, operations, presets, servers, worker,
)
from downloadinux.errors import DownloadinuxError, _Detached

PROG = "downloadinux"
# Parser nesting depth: the top level, a command, a command's subcommand.
_MAX_DEPTH = 2


@dataclass(frozen=True)
class GlobalOptions:
    """`--config PATH` and repeatable `--set key=value`, from any position."""

    config: str | None
    overrides: tuple[str, ...]


def _add_global_options(parser: argparse.ArgumentParser, level: int) -> None:
    # Each parser level stores into its own attributes: argparse copies a
    # subparser's namespace over its parent's, so a shared `dest` would let
    # `--set` after the subcommand discard the ones before it.
    group = parser.add_argument_group("global options")
    group.add_argument(
        "--config",
        metavar="PATH",
        dest=f"_config_{level}",
        default=argparse.SUPPRESS,
        help="settings file (default: $DOWNLOADINUX_CONFIG, then "
        "$XDG_CONFIG_HOME/downloadinux/config.toml)",
    )
    group.add_argument(
        "--set",
        metavar="KEY=VALUE",
        dest=f"_set_{level}",
        action="append",
        default=argparse.SUPPRESS,
        help="override a setting for this invocation (repeatable)",
    )


def _global_options(namespace: argparse.Namespace) -> GlobalOptions:
    config: str | None = None
    overrides: list[str] = []
    for level in range(_MAX_DEPTH + 1):
        # Deeper levels were typed later: their --config wins, their --set
        # values apply after the earlier ones.
        config = getattr(namespace, f"_config_{level}", config)
        overrides.extend(getattr(namespace, f"_set_{level}", []))
    return GlobalOptions(config=config, overrides=tuple(overrides))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Run the downloadinux download container from the host.",
    )
    _add_global_options(parser, 0)
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    cookies = commands.add_parser('cookies', help='register and inspect cookie sources; select their future default; clean up snapshots')
    _add_global_options(cookies, 1)
    cookie_commands = cookies.add_subparsers(dest='cookie_command', metavar='SUBCOMMAND', required=True)
    cookie_browsers = cookie_commands.add_parser('browsers', help='report supported browser selectors without probing installation')
    _add_global_options(cookie_browsers, 2)
    cookie_browsers.add_argument('--json', action='store_true', help='print JSON')
    cookie_browsers.set_defaults(handler=_cookies_browsers)
    cookie_import = cookie_commands.add_parser('import', help='register a managed copy of a Netscape file')
    _add_global_options(cookie_import, 2)
    cookie_import.add_argument('name', metavar='NAME', type=_source_name)
    cookie_import.add_argument('file', metavar='FILE')
    cookie_import.add_argument('--replace', action='store_true', help='explicitly replace an existing source')
    cookie_import.set_defaults(handler=_cookies_import)
    cookie_browser = cookie_commands.add_parser('add-browser', help='directly export and register browser cookies')
    _add_global_options(cookie_browser, 2)
    cookie_browser.add_argument('name', metavar='NAME', type=_source_name)
    cookie_browser.add_argument('--browser', required=True, choices=cookie_selectors.BROWSERS)
    cookie_browser.add_argument('--profile', metavar='PROFILE')
    cookie_browser.add_argument('--keyring', metavar='KEYRING')
    cookie_browser.add_argument('--container', metavar='CONTAINER')
    cookie_browser.add_argument('--replace', action='store_true', help='explicitly replace an existing source')
    cookie_browser.set_defaults(handler=_cookies_add_browser)
    cookie_firefox = cookie_commands.add_parser(
        'firefox', help='create or replace the source firefox from a Firefox profile and make it the default')
    _add_global_options(cookie_firefox, 2)
    cookie_firefox.add_argument('profile', nargs='?', metavar='PROFILE',
                                help='Firefox profile name or path (default: Firefox default discovery)')
    cookie_firefox.set_defaults(handler=_cookies_firefox)
    cookie_setup = cookie_commands.add_parser(
        'setup', help="setup's cookie step: keep a named default, or offer a Firefox export")
    _add_global_options(cookie_setup, 2)
    cookie_setup.set_defaults(handler=_cookies_setup)
    cookie_refresh = cookie_commands.add_parser('refresh', help='reread a stored origin and publish a fresh immutable revision')
    _add_global_options(cookie_refresh, 2)
    cookie_refresh.add_argument('name', metavar='NAME', type=_source_name)
    cookie_refresh.set_defaults(handler=_cookies_refresh)
    cookie_list = cookie_commands.add_parser('list', help='list source metadata')
    _add_global_options(cookie_list, 2)
    cookie_list.add_argument('--json', action='store_true', help='print JSON')
    cookie_list.set_defaults(handler=_cookies_list)
    cookie_inspect = cookie_commands.add_parser('inspect', help='summarize a source without exposing cookies')
    _add_global_options(cookie_inspect, 2)
    cookie_inspect.add_argument('name', metavar='NAME', type=_source_name)
    cookie_inspect.add_argument('--json', action='store_true', help='print JSON')
    cookie_inspect.set_defaults(handler=_cookies_inspect)
    cookie_default = cookie_commands.add_parser('default', help='report or select the future source default')
    _add_global_options(cookie_default, 2)
    selectors = cookie_default.add_mutually_exclusive_group()
    selectors.add_argument('name', nargs='?', metavar='NAME', type=_source_name)
    selectors.add_argument('--anonymous', action='store_true', help='select anonymous as the future default')
    cookie_default.set_defaults(handler=_cookies_default)
    cookie_cleanup = cookie_commands.add_parser(
        'cleanup', help='remove abandoned cookie snapshots in the running container',
        description='Ask the running container to remove abandoned private cookie '
        'snapshots (those no process holds) and report the counts. Needs the '
        'container running, from an image that ships downloadinux-runner.')
    _add_global_options(cookie_cleanup, 2)
    cookie_cleanup.set_defaults(handler=_cookies_cleanup)

    config = commands.add_parser(
        "config", help="inspect and migrate configuration"
    )
    _add_global_options(config, 1)
    config_commands = config.add_subparsers(
        dest="config_command", metavar="SUBCOMMAND", required=True
    )

    show = config_commands.add_parser(
        "show", help="effective settings, their sources, and the mounts"
    )
    _add_global_options(show, 2)
    show.add_argument("--json", action="store_true", help="print JSON")
    show.set_defaults(handler=_config_show)

    set_token = config_commands.add_parser(
        "set-token",
        help="validate and store the NordVPN token; log in if the container runs",
        description="Store the NordVPN token in the credentials file. Without "
        "TOKEN it prompts on a terminal, else reads one line from stdin. With "
        "the VPN enabled and the container running, it then logs the daemon "
        "in (and connects when vpn.autoconnect is true).",
    )
    _add_global_options(set_token, 2)
    set_token.add_argument("token", nargs="?", metavar="TOKEN")
    set_token.set_defaults(handler=_config_set_token)

    import_env = config_commands.add_parser(
        "import-env",
        help="migrate an existing .env (VPN on)",
        description="Write the settings and credentials files from an "
        "existing .env and copy cookies/cookies.txt beside it into the "
        "managed cookies directory. The .env and the old jar stay in place.",
    )
    _add_global_options(import_env, 2)
    import_env.add_argument(
        "--env", metavar="PATH", help="the .env to import (default: the checkout's)"
    )
    import_env.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing settings and credentials files "
        "(an existing cookie jar is always kept)",
    )
    import_env.set_defaults(handler=_config_import_env)

    user_agent = config_commands.add_parser(
        "user-agent",
        help="set downloads.user_agent to VALUE, or to the host Chrome's major version",
        description="With VALUE, write that literal as downloads.user_agent in "
        "the settings file (nonempty, one line; put a VALUE beginning with - "
        "after --). Without VALUE, read the host Chrome's version (macOS, "
        "/Applications) and write the matching Chrome user-agent; without a "
        "readable Chrome it warns and changes nothing.",
    )
    _add_global_options(user_agent, 2)
    user_agent.add_argument("value", nargs="?", metavar="VALUE")
    user_agent.set_defaults(handler=_config_user_agent)

    init = commands.add_parser(
        "init", help="write a starter settings file (VPN off) if none exists"
    )
    _add_global_options(init, 1)
    init.set_defaults(handler=_init)

    compose = commands.add_parser("compose", help="run Compose for the configured project")
    _add_global_options(compose, 1)
    compose.add_argument("args", nargs=argparse.REMAINDER, help="Compose arguments after --")
    compose.set_defaults(handler=_compose)

    for command, help_text in (
        ("build", "build the container image"),
        ("up", "start the configured container in the background"),
        ("down", "stop and remove the configured container"),
        ("restart", "recreate the container with current settings"),
        ("update", "rebuild without cache and start the container"),
    ):
        lifecycle = commands.add_parser(command, help=help_text)
        _add_global_options(lifecycle, 1)
        lifecycle.set_defaults(handler=_lifecycle)

    dl = commands.add_parser(
        "dl",
        help="download with a preset",
        description="Run a preset's downloader in the container, with a private "
        "copy of the pinned cookie source (or none) and the user-agent from the "
        "settings. PRESET is video (v: yt-dlp) or gallery (gal, g: gallery-dl). "
        "Without --cookie-source or --anonymous the cookie default applies. "
        "Options go before PRESET or after the last URL.",
    )
    _add_global_options(dl, 1)
    _add_launch_options(dl)
    dl.add_argument("preset", choices=list(presets.ALIASES), metavar="PRESET")
    dl.add_argument("urls", nargs="+", metavar="URL")
    dl.set_defaults(handler=_dl)

    batch = commands.add_parser(
        "batch",
        help="run a queue file in a detached tmux session",
        description="Start the batch loop on a queue file under "
        "paths.downloads in a detached tmux session in the container, with one "
        "private copy of the pinned cookie source (or none) for the whole "
        "batch. The queue defaults to dlmp4_queue (video) or dlgal_queue "
        "(gallery) in paths.downloads. Options are dl's.",
    )
    _add_global_options(batch, 1)
    _add_launch_options(batch)
    batch.add_argument("preset", choices=list(presets.ALIASES), metavar="PRESET")
    batch.add_argument("queue", nargs="?", metavar="QUEUE")
    batch.set_defaults(handler=_batch)

    watch = commands.add_parser(
        "watch",
        help="attach to a batch session (Ctrl-b d detaches)",
        description="Attach to a preset's batch session; any other word is "
        "taken as a tmux session name.",
    )
    _add_global_options(watch, 1)
    watch.add_argument("target", nargs="?", default=presets.VIDEO, metavar="PRESET",
                       help="video (default), gallery, or a tmux session name")
    watch.set_defaults(handler=_watch)

    shell = commands.add_parser("shell", help="interactive zsh in the container as dl")
    _add_global_options(shell, 1)
    shell.set_defaults(handler=_shell)

    vpn = commands.add_parser(
        "vpn",
        help="the container's NordVPN helper (status when no arguments)",
        description="Run the container's vpn helper: on [server], off, "
        "status, ip, or any nordvpn subcommand. Refuses when vpn.enabled is "
        "false.",
    )
    _add_global_options(vpn, 1)
    vpn.add_argument("args", nargs=argparse.REMAINDER, help="vpn helper arguments")
    vpn.set_defaults(handler=_vpn)

    servers_parser = commands.add_parser(
        "servers",
        help="least-loaded NordVPN servers in a city",
        description="Rank a city's NordVPN servers by load, fetched from "
        "inside the container. An underscore in CITY stands for a space; an "
        "unknown city lists the available ones.",
    )
    _add_global_options(servers_parser, 1)
    servers_parser.add_argument(
        "city", nargs="?", default=servers.DEFAULT_CITY,
        help=f"default: {servers.DEFAULT_CITY}",
    )
    servers_parser.add_argument(
        "country", nargs="?", default=servers.DEFAULT_COUNTRY,
        help=f"Nord's numeric country id (default: {servers.DEFAULT_COUNTRY}, "
        "the United States)",
    )
    servers_parser.set_defaults(handler=_servers)

    jobs = commands.add_parser(
        "jobs", help="list, inspect, follow, cancel, retry, and export the worker's jobs",
        description="Act on the jobs in the worker. "
        "Every subcommand needs the container running (dlx up).",
    )
    _add_global_options(jobs, 1)
    job_commands = jobs.add_subparsers(dest="job_command", metavar="SUBCOMMAND", required=True)
    jobs_list = job_commands.add_parser(
        "list", help="queued, running, failed, and interrupted jobs in ID order")
    _add_global_options(jobs_list, 2)
    jobs_list.add_argument("--all", action="store_true", help="every job")
    jobs_list.add_argument("--json", action="store_true", help="print JSON")
    jobs_list.set_defaults(handler=_jobs_list)
    jobs_show = job_commands.add_parser("show", help="one job's record and its log paths")
    _add_global_options(jobs_show, 2)
    jobs_show.add_argument("id", metavar="ID", type=_job_id)
    jobs_show.add_argument("--json", action="store_true", help="print JSON")
    jobs_show.set_defaults(handler=_jobs_show)
    jobs_logs = job_commands.add_parser(
        "logs", help="print an attempt's log (the latest by default)",
        description="Print an attempt's log, the latest by default. With --follow, "
        "stream it until the attempt ends; Ctrl-C detaches, and the job carries on.")
    _add_global_options(jobs_logs, 2)
    jobs_logs.add_argument("id", metavar="ID", type=_job_id)
    jobs_logs.add_argument("--attempt", metavar="N", type=_attempt, help="this attempt's log")
    jobs_logs.add_argument("--follow", action="store_true", help="stream until the attempt ends")
    jobs_logs.set_defaults(handler=_jobs_logs)
    jobs_cancel = job_commands.add_parser("cancel", help="cancel queued or running jobs")
    _add_global_options(jobs_cancel, 2)
    jobs_cancel.add_argument("ids", nargs="+", metavar="ID", type=_job_id)
    jobs_cancel.set_defaults(handler=_jobs_cancel)
    jobs_retry = job_commands.add_parser(
        "retry", help="retry failed, cancelled, or interrupted jobs",
        description="Retry failed, cancelled, or interrupted jobs as one new "
        "submission. Each keeps its own cookie source, re-pinned to its current "
        "revision, or stays anonymous, and keeps its user-agent, unless an option "
        "replaces them. Options are dl's.")
    _add_global_options(jobs_retry, 2)
    _add_launch_options(jobs_retry)
    jobs_retry.add_argument("ids", nargs="+", metavar="ID", type=_job_id)
    jobs_retry.set_defaults(handler=_jobs_retry)
    jobs_export = job_commands.add_parser(
        "export", help="print a preset's failed, cancelled, and interrupted URLs: a queue file")
    _add_global_options(jobs_export, 2)
    jobs_export.add_argument("preset", choices=list(presets.ALIASES), metavar="PRESET")
    jobs_export.set_defaults(handler=_jobs_export)

    doctor = commands.add_parser(
        "doctor", help="check configuration, Docker, the container, and VPN"
    )
    _add_global_options(doctor, 1)
    doctor.add_argument("--json", action="store_true", help="print JSON")
    doctor.set_defaults(handler=_doctor)

    return parser


def _add_launch_options(parser: argparse.ArgumentParser) -> None:
    """`dl` and `batch`'s selection and user-agent options (Named cookie
    sources §3, "Host launch (DEC-39)"; §4; DEC-40). Both selectors and the
    value checks are usage errors, before settings load."""
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--cookie-source", metavar="NAME", type=_source_name,
        help="use this registered cookie source (default: the cookie default)",
    )
    selection.add_argument(
        "--anonymous", action="store_true",
        help="use no cookies; the source store is not read",
    )
    parser.add_argument(
        "--user-agent", metavar="VALUE", type=launch.user_agent_option,
        help="user-agent for this run instead of downloads.user_agent "
        "(spell a VALUE beginning with - as --user-agent=VALUE)",
    )


def _job_id(value: str) -> int:
    """A job ID as the worker's clients take it (§6 *Arguments*): a usage
    error in the host's form, as `--user-agent`'s is (DEC-40)."""
    return worker.parse_id(value, "job ID must be a positive integer")


def _attempt(value: str) -> int:
    return worker.parse_id(value, "--attempt must be a positive integer")


def _passthrough(args: list[str]) -> list[str]:
    return args[1:] if args[:1] == ["--"] else args


def _source_name(value: str) -> str:
    if not cookie_store.NAME.fullmatch(value):
        raise argparse.ArgumentTypeError('source name must match [a-z0-9][a-z0-9_-]{0,63}')
    return value


def _cookies_list(args: argparse.Namespace, options: GlobalOptions) -> int:
    report = operations.list_cookies(options.config, options.overrides)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        lines = [f"Default: {report['default'] or 'anonymous'}"]
        lines.extend(render_cookie_source(source) for source in report['sources'])
        print('\n'.join(lines))
    return 0


def _cookies_browsers(args: argparse.Namespace, options: GlobalOptions) -> int:
    report = operations.cookie_browsers()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Platform: {report['platform']}; capabilities only, no installation/decryption probe")
        for browser in report['browsers']:
            print(f"{browser['id']}: profile: yes; container: {'yes' if browser['container'] else 'no'}; "
                  f"keyrings: {', '.join(browser['keyrings']) or 'none'}")
    return 0


def _cookies_inspect(args: argparse.Namespace, options: GlobalOptions) -> int:
    report = operations.inspect_cookies(args.name, options.config, options.overrides)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_cookie_inspection(report))
    return 0


def _cookies_import(args: argparse.Namespace, options: GlobalOptions) -> int:
    report = operations.import_cookies(args.name, args.file, replace=args.replace,
                                       config=options.config, overrides=options.overrides)
    print(render_cookie_inspection(report))
    return 0


def _cookies_add_browser(args: argparse.Namespace, options: GlobalOptions) -> int:
    report = operations.add_browser_cookies(args.name, args.browser, profile=args.profile,
                                            keyring=args.keyring, container=args.container,
                                            replace=args.replace, config=options.config, overrides=options.overrides)
    print(render_cookie_inspection(report))
    return 0


def _cookies_firefox(args: argparse.Namespace, options: GlobalOptions) -> int:
    report = operations.firefox_cookies(args.profile, options.config, options.overrides)
    print(render_cookie_inspection(report))
    return 0


def _ask_line(prompt: str) -> str:
    """Prompt on standard output and read one line; end of input ends the
    prompt line before it reaches the caller as `EOFError` (§5, DEC-38)."""
    try:
        return input(prompt)
    except EOFError:
        print()
        raise


def _cookies_setup(args: argparse.Namespace, options: GlobalOptions) -> int:
    outcome = operations.cookie_setup(options.config, options.overrides, ask=_ask_line)
    if outcome.kept is not None:
        print(f'Keeping the default cookie source {outcome.kept}; it was not refreshed.')
    if outcome.report is None:
        print('The cookie default stays anonymous; add a source later with '
              'dlx cookies firefox, add-browser, or import.')
    else:
        print(render_cookie_inspection(outcome.report))
    return 0


def _cookies_refresh(args: argparse.Namespace, options: GlobalOptions) -> int:
    report = operations.refresh_cookies(args.name, options.config, options.overrides)
    print(render_cookie_inspection(report))
    return 0


def render_cookie_inspection(report: dict) -> str:
    lines = [render_cookie_source(report['source']), render_cookie_counts(report)]
    for domain in report['domains']:
        presence = 'present' if domain['session_cookie_present'] else 'absent'
        lines.append(f"  {domain['domain']}: {render_cookie_counts(domain)}; recognized session cookie {presence}")
    return '\n'.join(lines)


def _cookies_default(args: argparse.Namespace, options: GlobalOptions) -> int:
    default = operations.cookie_default(args.name, anonymous=args.anonymous,
                                        config=options.config, overrides=options.overrides)
    print(default or 'anonymous')
    return 0


def _cookies_cleanup(args: argparse.Namespace, options: GlobalOptions) -> int:
    counts = operations.cookie_cleanup(options.config, options.overrides)
    print(f"Removed {counts.removed} abandoned cookie snapshots; "
          f"skipped {counts.skipped} in use or unverifiable.")
    return 0


def render_cookie_source(source: dict) -> str:
    """Render the same metadata fields as JSON, without individual cookies."""
    origin = {key: source[key] for key in ('path', 'browser', 'profile', 'keyring', 'container') if key in source}
    return (f"{source['name']}: {source['kind']}; default: {'yes' if source['is_default'] else 'no'}\n"
            f"  origin: {json.dumps(origin, ensure_ascii=False)}\n"
            f"  revision: {source['revision']}; refreshed_at: {source['refreshed_at']}")


def render_cookie_counts(summary: dict) -> str:
    return '; '.join(f'{key}: {summary[key]}' for key in ('total', 'session', 'expired', 'unexpired'))


def _compose(args: argparse.Namespace, options: GlobalOptions) -> int:
    return operations.run_compose(_passthrough(args.args), options.config, options.overrides)


def _launch_options(args: argparse.Namespace) -> dict:
    return dict(cookie_source=args.cookie_source, anonymous=args.anonymous,
                user_agent=args.user_agent)


def _dl(args: argparse.Namespace, options: GlobalOptions) -> int:
    return operations.download(args.preset, args.urls, options.config, options.overrides,
                               **_launch_options(args))


def _batch(args: argparse.Namespace, options: GlobalOptions) -> int:
    result = operations.batch(args.preset, args.queue, options.config, options.overrides,
                              **_launch_options(args))
    print(f"Queue: {result.queue}")
    print(
        f"Running detached in tmux session {result.session}. "
        f"Watch it with: dlx watch {result.preset}"
    )
    return 0


def _watch(args: argparse.Namespace, options: GlobalOptions) -> int:
    return operations.watch(args.target, options.config, options.overrides)


def _shell(args: argparse.Namespace, options: GlobalOptions) -> int:
    return operations.shell(options.config, options.overrides)


def _vpn(args: argparse.Namespace, options: GlobalOptions) -> int:
    return operations.vpn(_passthrough(args.args), options.config, options.overrides)


def _servers(args: argparse.Namespace, options: GlobalOptions) -> int:
    for line in operations.list_servers(
        args.city, args.country, options.config, options.overrides
    ):
        print(line)
    return 0


# Persistent worker and jobs §6, "Job operations, output, and exit statuses"
# (DEC-55).

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
DETACH_SIGNALS = (signal.SIGINT, signal.SIGHUP, signal.SIGTERM)


def escape(text: str) -> str:
    r"""Human output's form of an interpolated string: each C0 control
    character, DEL, and C1 control character as `\xNN`; nothing else."""
    return _CONTROL.sub(lambda match: f"\\x{ord(match.group()):02x}", text)


def _jobs_errors(handler):
    """An operation error's reason may come from a reply: escape it."""
    @functools.wraps(handler)
    def run(args: argparse.Namespace, options: GlobalOptions) -> int:
        try:
            return handler(args, options)
        except DownloadinuxError as error:
            raise type(error)(escape(str(error))) from None
    return run


def _json(value: object) -> None:
    print(json.dumps(value, indent=2))


def _job_state(job: dict) -> str:
    wait = job["wait"]
    if job["state"] == "queued" and wait is not None:
        return "queued: vpn" if wait["reason"] == "vpn" else f"queued: throttle until {escape(wait['until'])}"
    if job["state"] == "running" and job["cancel_requested"]:
        return "running: cancelling"
    return job["state"]


def render_jobs(jobs: list[dict]) -> str:
    """`jobs list`'s table: columns two spaces apart, each but the last
    padded to its widest cell, header included (§6 *Output*)."""
    rows = [("ID", "STATE", "PRESET", "QUEUE", "ATTEMPT", "URL")]
    rows += [(str(job["id"]), _job_state(job), job["preset"], "lane" if job["lane"] else "immediate",
              str(job["attempt"]), escape(job["url"])) for job in jobs]
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]) - 1)]
    return "\n".join("  ".join([*(cell.ljust(width) for cell, width in zip(row, widths)), row[-1]])
                     for row in rows)


def _or_dash(value: object) -> str:
    return "-" if value is None else escape(str(value))


def render_job(view: host_jobs.JobView) -> str:
    """`jobs show`'s block: each value from the 21st character (§6 *Output*)."""
    job = view.job
    wait = job["wait"]
    fields = [
        ("URL", escape(job["url"])),
        ("Preset", job["preset"]),
        ("Queue", f"{job['preset']} lane" if job["lane"] else "immediate"),
        ("Submission", str(job["submission"])),
        ("Cookies", "anonymous" if job["source"] is None
         else f"{escape(job['source'])} at revision {escape(job['revision'])}"),
        ("User-agent", escape(job["user_agent"]) or "(empty)"),
        ("Attempt", str(job["attempt"])),
        ("Wait", "-" if wait is None else "vpn" if wait["reason"] == "vpn"
         else f"throttle until {escape(wait['until'])}"),
        ("Cancel requested", "yes" if job["cancel_requested"] else "no"),
        ("Created", escape(job["created_at"])),
        ("Queued", escape(job["queued_at"])),
        ("Started", _or_dash(job["started_at"])),
        ("Finished", _or_dash(job["finished_at"])),
        ("Exit status", _or_dash(job["exit_status"])),
        ("Error", _or_dash(job["error"])),
        *((f"Log, attempt {attempt}", escape(str(path))) for attempt, path in view.logs),
    ]
    lines = [f"Job {job['id']}: {job['state']}"]
    lines += [f"  {label}:".ljust(19) + " " + value for label, value in fields]
    return "\n".join(lines)


@_jobs_errors
def _jobs_list(args: argparse.Namespace, options: GlobalOptions) -> int:
    jobs = host_jobs.list_jobs(args.all, options.config, options.overrides)
    if args.json:
        _json({"jobs": jobs})
    elif not jobs:
        print("No jobs." if args.all else "No queued, running, failed, or interrupted jobs.")
    else:
        print(render_jobs(jobs))
    return 0


@_jobs_errors
def _jobs_show(args: argparse.Namespace, options: GlobalOptions) -> int:
    view = host_jobs.show(args.id, options.config, options.overrides)
    if args.json:
        _json({"job": view.job, "logs": [{"attempt": n, "path": str(path)} for n, path in view.logs]})
    else:
        print(render_job(view))
    return 0


def _ignore(number, frame) -> None:
    pass


def _detach(number, frame) -> None:
    # Further signals are ignored, so the stream's ending completes and the
    # first signal sets the status (§6 *Output*).
    for detach in DETACH_SIGNALS:
        signal.signal(detach, _ignore)
    raise _Detached(number)


@contextlib.contextmanager
def _following_signals():
    """Scope DEC-55's detach handlers to following, including its cleanup."""
    previous = {number: signal.getsignal(number) for number in DETACH_SIGNALS}
    try:
        yield
    finally:
        for number, handler in previous.items():
            if handler is not None:  # None: a handler Python did not install
                signal.signal(number, handler)


def _detached_job(job_id: int) -> str:
    return (f"detached from job {job_id}, which carries on in the worker; follow it with "
            f"dlx jobs logs --follow {job_id} or cancel it with dlx jobs cancel {job_id}")


def _render_follow(events, *, ids=(), lane: str | None = None) -> int:
    """Render DEC-57's typed events; exhaustion includes the child's exit check.

    The generator owns stream cleanup even when a notice or byte write fails.
    Fixed-job detach reports all references minus observed ends, in input order.
    """
    remaining = dict.fromkeys(ids)
    records, selected, done = [], False, False
    with _following_signals():
        try:
            for number in DETACH_SIGNALS:
                signal.signal(number, _detach)
            with contextlib.closing(events):
                for event in events:
                    if isinstance(event, host_follow.FollowJob):
                        selected = True
                        job = event.job
                        print(f"Job {job['id']}: {escape(job['url'])}", file=sys.stderr)
                    elif isinstance(event, host_follow.FollowWait):
                        wait = event.wait
                        reason = ('resumed' if wait is None else 'waiting for VPN'
                                  if wait['reason'] == 'vpn' else
                                  f"waiting for throttle until {escape(wait['until'])}")
                        print(f"job {event.id} {reason}", file=sys.stderr)
                    elif isinstance(event, host_follow.FollowLog):
                        sys.stdout.buffer.write(event.data)
                        sys.stdout.buffer.flush()
                    elif isinstance(event, host_follow.FollowEnd):
                        records.append(event.job)
                        remaining.pop(event.job['id'], None)
                    elif isinstance(event, host_follow.FollowDone):
                        done = True
                if not done:
                    raise DownloadinuxError('the worker went away; see dlx jobs list')
        except _Detached as detached:
            if lane is not None:
                print(f"detached from {escape(lane)} lane, which carries on in the worker; "
                      f"follow it with dlx watch {escape(lane)} or inspect and cancel jobs with "
                      "dlx jobs list and dlx jobs cancel ID", file=sys.stderr)
            elif remaining:
                for job_id in remaining:
                    print(_detached_job(job_id), file=sys.stderr)
            else:
                print('detached; all followed jobs have ended; inspect with dlx jobs list',
                      file=sys.stderr)
            return 128 + detached.number
    if lane is not None:
        if not selected:
            print(f"No queued or running jobs in {escape(lane)} lane.")
        return 0
    return host_follow.download_status(records)


def _write(chunks) -> None:
    """Each chunk to standard output's byte stream as it arrives. Closing
    the generator, however this ends, ends the stream and Compose."""
    out = sys.stdout.buffer
    with contextlib.closing(chunks):
        for data in chunks:
            out.write(data)
            out.flush()


@_jobs_errors
def _jobs_logs(args: argparse.Namespace, options: GlobalOptions) -> int:
    chunks = host_jobs.logs(args.id, args.attempt, args.follow, options.config, options.overrides)
    if not args.follow:
        _write(chunks)
        return 0
    with _following_signals():
        try:
            for number in DETACH_SIGNALS:
                signal.signal(number, _detach)
            _write(chunks)
        except _Detached as detached:
            print(_detached_job(args.id), file=sys.stderr)
            return 128 + detached.number
    return 0


@_jobs_errors
def _jobs_cancel(args: argparse.Namespace, options: GlobalOptions) -> int:
    outcomes = host_jobs.cancel(args.ids, options.config, options.overrides)
    for outcome in outcomes:
        job_id = outcome["id"]
        print({
            "cancelled": f"job {job_id} cancelled",
            "requested": f"job {job_id}: cancel requested; it ends when its downloader exits",
            "refused": f"job {job_id} is {outcome['state']}; only queued or running jobs can be cancelled",
            "unknown": f"unknown job ID {job_id}",
        }[outcome["outcome"]])
    return 0 if all(outcome["outcome"] in ("cancelled", "requested") for outcome in outcomes) else 1


def _retry_line(line: host_jobs.RetryLine) -> str:
    outcome = line.outcome
    if outcome is None:
        if line.record is None:
            return f"unknown job ID {line.id}"
        state = line.record["state"]
    elif outcome["outcome"] == "requeued":
        return f"job {line.id} queued again as attempt {line.record['attempt'] + 1}"
    elif outcome["outcome"] == "shadowed":
        return f"job {line.id} not retried: job {outcome['holder']} already holds its preset and URL"
    elif outcome["outcome"] == "unknown":
        return f"unknown job ID {line.id}"
    else:
        state = outcome["state"]
    return f"job {line.id} is {state}; only failed, cancelled, or interrupted jobs can be retried"


@_jobs_errors
def _jobs_retry(args: argparse.Namespace, options: GlobalOptions) -> int:
    lines = host_jobs.retry(args.ids, options.config, options.overrides, **_launch_options(args))
    for line in lines:
        print(_retry_line(line))
    return 0 if all(line.outcome and line.outcome["outcome"] == "requeued" for line in lines) else 1


@_jobs_errors
def _jobs_export(args: argparse.Namespace, options: GlobalOptions) -> int:
    urls = host_jobs.export(args.preset, options.config, options.overrides)
    sys.stdout.buffer.write("".join(f"{url}\n" for url in urls).encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


def _doctor(args: argparse.Namespace, options: GlobalOptions) -> int:
    result = operations.doctor(options.config, options.overrides)
    if args.json:
        print(json.dumps(result.as_dict(), indent=2))
    else:
        print(render_health(result))
    return 0 if result.ok else 1


def _lifecycle(args: argparse.Namespace, options: GlobalOptions) -> int:
    operations.lifecycle(args.command, options.config, options.overrides)
    return 0


def _init(args: argparse.Namespace, options: GlobalOptions) -> int:
    path = operations.init(options.config, options.overrides)
    print(f"Wrote {path}")
    return 0


def _config_import_env(args: argparse.Namespace, options: GlobalOptions) -> int:
    result = migrate.import_env(
        args.env, args.force, options.config, options.overrides
    )
    for warning in result.warnings:
        print(f"{PROG}: warning: {warning}", file=sys.stderr)
    print(f"Wrote {result.settings_file} from {result.env_file}, importing:")
    for key, value in result.imported:
        print(f"  {key} = {_toml(value)}")
    if result.credentials_file is not None:
        print(f"Wrote {result.credentials_file} (token: {result.token.value})")
    else:
        print(f"No NordVPN token imported (token: {result.token.value})")
    if result.jar is migrate.JarOutcome.COPIED:
        print(f"Copied the cookie jar to {result.jar_destination}")
    elif result.jar is migrate.JarOutcome.KEPT:
        print(f"Kept the existing cookie jar at {result.jar_destination}")
    else:
        print(f"No cookie jar at {result.jar_source}; nothing copied")
    if result.not_imported:
        print(f"Not imported: {', '.join(result.not_imported)}")
    return 0


def _read_token() -> str:
    if sys.stdin.isatty():
        return getpass.getpass("NordVPN token: ").strip()
    return sys.stdin.readline().strip()


def _config_set_token(args: argparse.Namespace, options: GlobalOptions) -> int:
    token = args.token if args.token is not None else _read_token()
    path = operations.set_token(token, options.config, options.overrides)
    # Flushed so it precedes Docker's and the login's own messages in a log.
    print(f"Stored the NordVPN token in {path}", flush=True)
    result = operations.log_in(token, options.config, options.overrides)
    if result.login is operations.Login.VPN_OFF:
        print("vpn.enabled is false: the token is kept for when the VPN is enabled.")
    elif result.login is operations.Login.NOT_RUNNING:
        print("The container is not running; it logs in with the token at the next dlx up.")
    else:
        print("Logged in.")
        if result.connect is not None:
            target = " ".join(result.connect.server) or "the fastest server"
            if result.connect.connected:
                print(f"Connected to {target}.")
            else:
                print(
                    f"{PROG}: warning: could not connect to {target}; try dlx vpn on",
                    file=sys.stderr,
                )
    return 0


def _config_user_agent(args: argparse.Namespace, options: GlobalOptions) -> int:
    if args.value is not None:
        # DEC-37: name the file, never echo the value.
        write = operations.set_user_agent(args.value, options.config, options.overrides)
        if write.written:
            print(f"downloads.user_agent set in {write.settings_file}.")
        else:
            print(f"downloads.user_agent already has that value in {write.settings_file}.")
        return 0
    result = operations.refresh_user_agent(options.config, options.overrides)
    if result.major is None:
        # DEC-21: a host without Chrome warns rather than fails.
        print(
            f"{PROG}: warning: cannot read Chrome's version from {result.plist}; "
            "downloads.user_agent is unchanged",
            file=sys.stderr,
        )
        return 0
    print(f"User-agent pinned to Chrome {result.major} (host has {result.version}).")
    return 0


def _config_show(args: argparse.Namespace, options: GlobalOptions) -> int:
    report = operations.show_config(options.config, options.overrides)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_config(report))
    return 0


def _toml(value: object) -> str:
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _missing(exists: bool) -> str:
    return "" if exists else "  (missing)"


def render_config(report: dict) -> str:
    """`config show`'s text form: one `key = value  (source)` line per
    setting, then the files, the token's state, and the mounts."""
    lines = [
        f"{s['key']} = {_toml(s['value'])}  ({s['source']})"
        for s in report["settings"]
    ]
    config_file, creds_file = report["config_file"], report["credentials_file"]
    lines += [
        "",
        f"settings file:    {config_file['path']}{_missing(config_file['exists'])}",
        f"credentials file: {creds_file['path']}{_missing(creds_file['exists'])}",
        f"token:            {report['token']}",
        "",
        "mounts:",
    ]
    lines += [
        f"  {m['container']:<10} <- {m['host']}  ({m['access']})"
        f"{_missing(m['exists'])}"
        for m in report["mounts"]
    ]
    return "\n".join(lines)


_HEALTH_MARKS = {
    health.Status.OK: ("✓", "32"),
    health.Status.WARN: ("!", "33"),
    health.Status.FAIL: ("✗", "31"),
    health.Status.SKIP: ("-", "2"),
}


def render_health(result: health.Result) -> str:
    """DEC-22's human view; only marks receive terminal colour."""
    colour = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    lines = [PROG]
    for check in result.checks:
        mark, code = _HEALTH_MARKS[check.status]
        if colour:
            mark = f"\033[{code}m{mark}\033[0m"
        lines.append(f"  {mark} {check.summary}")
        if check.fix is not None:
            lines.append(f"      fix: {check.fix}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except DownloadinuxError as error:
        # An argument type may raise the project's usage error, which keeps
        # the host's error form rather than argparse's (DEC-40).
        print(f"{PROG}: error: {error}", file=sys.stderr)
        return error.exit_status
    handler = getattr(args, "handler", None)
    if handler is None:
        # Bare invocation is reserved for the TUI; until Stage 4 it prints
        # help (DEC-7).
        parser.print_help()
        return 0
    try:
        return handler(args, _global_options(args))
    except DownloadinuxError as error:
        print(f"{PROG}: error: {error}", file=sys.stderr)
        return error.exit_status
    except OSError as error:
        # Host application: OS failures are operation errors, including
        # reads, atomic writes, and subprocess creation (review-checklist §1).
        failure = DownloadinuxError(
            f"{error.filename or '<stdin/stdout>'}: {error.strerror}"
        )
        print(f"{PROG}: error: {failure}", file=sys.stderr)
        return failure.exit_status
