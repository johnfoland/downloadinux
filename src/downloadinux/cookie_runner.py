"""The container runner: a private cookie snapshot around one downloader run (DEC-36).

Named cookie sources §3, "Runner interface and process lifetime". The image
installs ``downloadinux-runner``, which calls ``main`` under the system
``/usr/bin/python3 -I``. This module and everything it imports use only the
standard library and the container-side cookie modules: never settings, the
source writer, host pinning, browser support, ``tomlkit`` or yt-dlp. Roots,
the source/revision pair and the user-agent come from the caller; nothing
here resolves a default, reads the registry or migrates.

Errors name paths, never cookie contents.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

from . import cookie_snapshot, cookie_store as store, presets
from .errors import ConfigError, DownloadinuxError

PROG = 'downloadinux'
COMMANDS = ('download', 'cleanup')
VALUED = ('--cookies', '--state', '--source', '--revision', '--user-agent')
# Handled while the runner lives, and forwarded to the child's group (DEC-36).
HANDLED = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGWINCH)
TERMINATING = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


class Request:
    """A validated command line, built before any filesystem access (§3, *Interface*)."""

    def __init__(self, command: str, options: dict, operands: list):
        self.command = command
        self.state = Path(options['--state'])
        if command == 'cleanup':
            return
        self.cookies = Path(options['--cookies'])
        self.user_agent = options['--user-agent']
        if '--anonymous' in options:
            self.pinned = store.ANONYMOUS
        else:
            self.pinned = store.Pinned(options['--source'], options['--revision'])
        self.preset = presets.PRESETS[operands[0]]
        self.operands = operands[1:]


def parse(argv: list) -> Request:
    """Accept exactly the interface's shapes; anything else is a usage error (DEC-36).

    Options come first, each value the next list element taken verbatim (a
    user-agent may begin with ``-`` or be empty). The first other element is
    PRESET (cleanup takes none), and every element after it is an operand.
    """
    if not argv:
        raise ConfigError('a subcommand is required: download or cleanup')
    command, rest = argv[0], argv[1:]
    if command not in COMMANDS:
        raise ConfigError(f'unknown subcommand {command!r}; use download or cleanup')
    allowed = ('--state',) if command == 'cleanup' else (*VALUED, '--anonymous')
    options, index = {}, 0
    while index < len(rest) and rest[index].startswith('-'):
        option = rest[index]
        if option not in allowed:
            raise ConfigError(f'{command}: unknown option {option!r}')
        if option in options:
            raise ConfigError(f'{command}: {option} given more than once')
        if option == '--anonymous':
            options[option], index = True, index + 1
            continue
        if index + 1 == len(rest):
            raise ConfigError(f'{command}: {option} needs a value')
        options[option], index = rest[index + 1], index + 2
    operands = rest[index:]
    for option in ('--state',) if command == 'cleanup' else ('--cookies', '--state', '--user-agent'):
        if option not in options:
            raise ConfigError(f'{command}: {option} is required')
    for option in ('--cookies', '--state'):
        if options.get(option) == '':
            raise ConfigError(f'{command}: {option} must name a directory')
    if command == 'cleanup':
        if operands:
            raise ConfigError(f'cleanup: unexpected operand {operands[0]!r}')
        return Request(command, options, operands)
    _validate_selection(command, options)
    if '\r' in options['--user-agent'] or '\n' in options['--user-agent']:
        raise ConfigError(f'{command}: --user-agent must not contain CR or LF')
    if not operands:
        raise ConfigError(f'{command}: PRESET is required: video or gallery')
    if operands[0] not in presets.PRESETS:
        # The host resolves aliases; only canonical names reach the runner.
        raise ConfigError(f'{command}: unknown preset {operands[0]!r}; use video or gallery')
    if command == 'download' and len(operands) < 2:
        raise ConfigError('download: at least one URL is required')
    if '' in operands[1:]:
        raise ConfigError(f'{command}: an empty URL or queue path is not allowed')
    return Request(command, options, operands)


def _validate_selection(command: str, options: dict) -> None:
    """``--source NAME --revision REVISION`` or ``--anonymous``, exactly one (§1's identifier rules)."""
    named = [option for option in ('--source', '--revision') if option in options]
    if '--anonymous' in options:
        if named:
            raise ConfigError(f'{command}: --anonymous conflicts with {named[0]}')
        return
    if not named:
        raise ConfigError(f'{command}: give --source NAME --revision REVISION, or --anonymous')
    if len(named) == 1:
        other = '--revision' if named[0] == '--source' else '--source'
        raise ConfigError(f'{command}: {named[0]} needs {other}')
    if not store.NAME.fullmatch(options['--source']):
        raise ConfigError(f'{command}: invalid source name {options["--source"]!r}')
    if not store.REVISION.fullmatch(options['--revision']):
        raise ConfigError(f'{command}: invalid revision {options["--revision"]!r}')


def child_argv(request: Request, jar: Path | None) -> list:
    """The per-job downloader list (DEC-36, DEC-44).

    Only the runner knows the private jar path; anonymous runs omit cookies.
    """
    cookies = None if jar is None else str(jar)
    return [*presets.argv(request.preset.name, request.user_agent, cookies=cookies), *request.operands]


class _Signals:
    """Forward handled signals to the child's group; before launch, note termination (DEC-36).

    The runner never exits early because of a signal once the child runs:
    Python retries the interrupted wait or read after the handler (PEP 475).
    """

    def __init__(self):
        self.group = None
        self.pending = None
        self.previous = {}

    def install(self) -> None:
        for number in HANDLED:
            self.previous[number] = signal.signal(number, self.handle)

    def restore(self) -> None:
        for number, handler in self.previous.items():
            signal.signal(number, handler)

    def handle(self, number, frame=None) -> None:
        if self.group is None:
            if number in TERMINATING and self.pending is None:
                self.pending = number
            return
        try:
            os.killpg(self.group, number)
        except (ProcessLookupError, PermissionError):
            # The group has gone (ESRCH), or holds only zombies (EPERM on
            # macOS); either way the runner keeps waiting.
            pass


class _Unconfirmed(DownloadinuxError):
    """The lifetime pipe failed: consumers may still be using the jar."""


def run(request: Request) -> int:
    """Allocate, launch, wait for every lease holder, then tear down (§3, *Lifetime*; DEC-36)."""
    signals = _Signals()
    signals.install()
    try:
        if signals.pending is not None:
            return 128 + signals.pending
        invocation = cookie_snapshot.allocate(request.cookies, request.state, request.pinned, request.user_agent)
        try:
            status = _launch(request, invocation, signals)
        except _Unconfirmed:
            # Removal could delete a jar still in use: keep it for cleanup (DEC-27).
            if invocation is not None:
                invocation.abandon()
            raise
        except BaseException as failure:
            # Teardown failure takes precedence over the original one (DEC-27).
            if invocation is not None:
                try:
                    invocation.close()
                except DownloadinuxError as teardown:
                    raise teardown from failure
            raise
        if invocation is not None:
            invocation.close()
        return status
    finally:
        signals.restore()


def _launch(request: Request, invocation, signals: _Signals) -> int:
    """Start the child in its own group and return its mapped status after the pipe's end-of-file."""
    if signals.pending is not None:
        # A termination signal before launch aborts it (DEC-36).
        return 128 + signals.pending
    program = child_argv(request, None if invocation is None else invocation.jar)
    try:
        reader, writer = os.pipe()
    except OSError as error:
        raise DownloadinuxError(f'lifetime pipe: {error.strerror}') from None
    try:
        # Only the lease and the pipe's write end travel with the child;
        # both are non-inheritable everywhere else (DEC-36).
        passed = (writer,) if invocation is None else (invocation.lease_fd, writer)
        try:
            child = subprocess.Popen(program, stdin=subprocess.DEVNULL, pass_fds=passed, process_group=0)
        except OSError as error:
            raise DownloadinuxError(f'{error.filename or program[0]}: {error.strerror}') from None
        signals.group = child.pid
        os.close(writer)
        writer = None
        if signals.pending is not None:
            # It arrived while the child was starting: the child gets it.
            signals.handle(signals.pending)
        status = child.wait()
        _drain(reader, invocation)
        signals.group = None
    finally:
        if writer is not None:
            os.close(writer)
        os.close(reader)
    return 128 - status if status < 0 else status


def _drain(reader: int, invocation) -> None:
    """Read until every process holding the write end has exited or closed it (DEC-36).

    That set is the set holding the lease. Zombies hold no descriptors, so a
    non-reaping PID 1 cannot delay end-of-file, unlike an emptying group.
    """
    try:
        while os.read(reader, 4096):
            pass
    except OSError as error:
        where = 'lifetime pipe' if invocation is None else str(invocation.path)
        raise _Unconfirmed(f'{where}: {error.strerror}; cannot confirm the downloader has exited; '
                           'snapshot residue may remain') from None


def cleanup(request: Request) -> int:
    """Remove abandoned snapshots and print the counts (§3, *Exit status*); never reads cookies."""
    result = cookie_snapshot.cleanup(request.state)
    try:
        sys.stdout.write(json.dumps({'removed': result.removed, 'skipped': result.skipped}) + '\n')
        sys.stdout.flush()
    except OSError as error:
        # Python's recipe for a closed pipe: aim stdout at /dev/null, so the
        # exit-time flush cannot fail again and change the exit status.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        raise DownloadinuxError(f'<stdout>: {error.strerror}') from None
    return 0


def main(argv=None) -> int:
    """The child's status, or 0/1/2 (§3, *Exit status*; DEC-36), with errors in the host's form."""
    try:
        request = parse(list(sys.argv[1:] if argv is None else argv))
        if request.command == 'cleanup':
            return cleanup(request)
        return run(request)
    except DownloadinuxError as error:
        print(f'{PROG}: error: {error}', file=sys.stderr)
        return error.exit_status
    except OSError as error:
        print(f'{PROG}: error: {error.filename or "<stdout>"}: {error.strerror}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
