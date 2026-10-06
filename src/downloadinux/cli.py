"""The `downloadinux` / `dlx` command line (DEC-7).

The CLI only parses arguments and prints; each subcommand is a thin front for
an operation the TUI will call (target design, *Host application*).
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from downloadinux import migrate, operations
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
        help="validate and store the NordVPN token",
        description="Store the NordVPN token in the credentials file. Without "
        "TOKEN it prompts on a terminal, else reads one line from stdin.",
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

    init = commands.add_parser(
        "init", help="write a starter settings file (VPN off) if none exists"
    )
    _add_global_options(init, 1)
    init.set_defaults(handler=_init)

    return parser


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
    print(f"Stored the NordVPN token in {path}")
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
