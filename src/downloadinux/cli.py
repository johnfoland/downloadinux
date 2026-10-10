"""The `downloadinux` / `dlx` command line (DEC-7).

The CLI only parses arguments and prints; each subcommand is a thin front for
an operation the TUI will call (target design, *Host application*).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from downloadinux import cookie_store, health, migrate, operations, presets, servers
from downloadinux.errors import DownloadinuxError

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

    cookies = commands.add_parser('cookies', help='inspect cookie sources and select their future default')
    _add_global_options(cookies, 1)
    cookie_commands = cookies.add_subparsers(dest='cookie_command', metavar='SUBCOMMAND', required=True)
    cookie_browsers = cookie_commands.add_parser('browsers', help='report supported browser selectors without probing installation')
    _add_global_options(cookie_browsers, 2)
    cookie_browsers.add_argument('--json', action='store_true', help='print JSON')
    cookie_browsers.set_defaults(handler=_cookies_browsers)
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
        help="pin downloads.user_agent to the host Chrome's major version",
        description="Read the host Chrome's version (macOS, /Applications) and "
        "write the matching Chrome user-agent into the settings file. Without "
        "a readable Chrome it warns and changes nothing.",
    )
    _add_global_options(user_agent, 2)
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
        description="Run a preset's downloader in the container with the "
        "output root, cookie jar, and user-agent from the settings. PRESET is "
        "video (v: yt-dlp) or gallery (gal, g: gallery-dl).",
    )
    _add_global_options(dl, 1)
    dl.add_argument("preset", choices=list(presets.ALIASES), metavar="PRESET")
    dl.add_argument("urls", nargs="+", metavar="URL")
    dl.set_defaults(handler=_dl)

    batch = commands.add_parser(
        "batch",
        help="run a queue file in a detached tmux session",
        description="Start the batch loop on a queue file under "
        "paths.downloads in a detached tmux session in the container. The "
        "queue defaults to dlmp4_queue (video) or dlgal_queue (gallery) in "
        "paths.downloads.",
    )
    _add_global_options(batch, 1)
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

    doctor = commands.add_parser(
        "doctor", help="check configuration, Docker, the container, and VPN"
    )
    _add_global_options(doctor, 1)
    doctor.add_argument("--json", action="store_true", help="print JSON")
    doctor.set_defaults(handler=_doctor)

    return parser


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
        lines = [render_cookie_source(report['source']), render_cookie_counts(report)]
        for domain in report['domains']:
            presence = 'present' if domain['session_cookie_present'] else 'absent'
            lines.append(f"  {domain['domain']}: {render_cookie_counts(domain)}; recognized session cookie {presence}")
        print('\n'.join(lines))
    return 0


def _cookies_default(args: argparse.Namespace, options: GlobalOptions) -> int:
    default = operations.cookie_default(args.name, anonymous=args.anonymous,
                                        config=options.config, overrides=options.overrides)
    print(default or 'anonymous')
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


def _dl(args: argparse.Namespace, options: GlobalOptions) -> int:
    return operations.download(args.preset, args.urls, options.config, options.overrides)


def _batch(args: argparse.Namespace, options: GlobalOptions) -> int:
    result = operations.batch(args.preset, args.queue, options.config, options.overrides)
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
    result = operations.refresh_user_agent(options.config, options.overrides)
    if result.major is None:
        # DEC-21: `just cookies` runs this after every export, so a host
        # without Chrome must not fail it.
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
    args = parser.parse_args(argv)
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
