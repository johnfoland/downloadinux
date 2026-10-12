"""The worker service and its client subcommands (DEC-50, DEC-51).

Target design, *Persistent worker and jobs* §1, "Service lifecycle and
failure handling" and "Client subcommands and replies". Standard library
only (§1, DEC-41): the service drives the owned job store, the scheduler
(DEC-47), and the execution library (DEC-48) through their public
operations, serially on one thread, and the clients speak to it over one
Unix socket. Nothing here reads settings, cookie material, or host paths.

Log lines name no URL, user-agent, or cookie value.
"""
from collections.abc import Mapping
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import selectors
import signal
import socket
import stat
import sys
import time
import traceback
from typing import Callable

from . import job_store, worker_execution, worker_scheduler
from .errors import ConfigError, DownloadinuxError

PROG = 'downloadinux'
LOG_PREFIX = 'downloadinux-worker: '
SUBCOMMANDS = ('serve', 'status', 'submit', 'show')
SOCKET = Path('/run/downloadinux/worker.sock')
DOWNLOADS = Path('/downloads')
VERSION = 1
LIMIT = 32 * 1024 * 1024  # bytes, each way, and for `submit`'s standard input
TICK = 0.5  # seconds: the longest wait for a connection or a stop (DEC-50 *Loop*)
RETRY = 1  # seconds between ownership attempts while another worker holds the lock
REPEAT = 60  # seconds before the waiting line, or an identical failure line, repeats
SERVER_TIMEOUT = 10  # seconds to read a request, and again to send its reply
CLIENT_TIMEOUT = 30  # seconds a client allows in all (DEC-51 *Wire protocol*)
MAX_ID = 9223372036854775807  # SQLite's largest integer key
SUBMIT_KEYS = ('preset', 'lane', 'source', 'revision', 'user_agent', 'urls')  # §2's submission inputs
SUBMIT_USAGE = 'submit: standard input must be one JSON object'
INVALID = 'invalid request'
UNSUPPORTED = 'unsupported request version'


def main(argv: list[str] | None = None) -> int:
    """`downloadinux-worker SUBCOMMAND …`: the image's entry point (§1 *Packaging*).

    `main` passes no test arguments: the socket, download root,
    environment, and clocks are always the image's (DEC-50).
    """
    return run(sys.argv[1:] if argv is None else list(argv))


def run(argv: list[str], *, socket_path: Path = SOCKET, timeout: float = CLIENT_TIMEOUT, **service) -> int:
    """Parse `argv`, then serve or run one client request.

    Keyword-only test arguments: `socket_path` for every subcommand, the
    client's overall `timeout`, and, for `serve` only, `serve`'s own
    keywords. A usage error prints `downloadinux: error: REASON` and exits
    2 before any socket or store is touched (DEC-51).
    """
    try:
        command, rest = _subcommand(argv)
        if command == 'serve':
            cookies, state = parse_serve(rest)
        elif command == 'show':
            args = {'id': parse_show(rest)}
        elif rest:
            raise ConfigError(f'{command}: unexpected operand {rest[0]!r}')
        elif command == 'submit':
            args = read_submission(sys.stdin)
        else:
            args = {}
    except ConfigError as error:
        _error(str(error))
        return 2
    if command == 'serve':
        return serve(cookies, state, socket_path=socket_path, **service)
    return request(socket_path, command, args, timeout=timeout)


def _subcommand(argv: list[str]) -> tuple[str, list[str]]:
    if not argv:
        raise ConfigError('a subcommand is required: serve, status, submit, or show')
    if argv[0] not in SUBCOMMANDS:
        raise ConfigError(f'unknown subcommand {argv[0]!r}; use serve, status, submit, or show')
    return argv[0], argv[1:]


def parse_serve(rest: list[str]) -> tuple[str, str]:
    """`--cookies DIR --state DIR`, each once, in either order, and no operand.

    The messages are the runner's `parse` messages with `serve` as the
    subcommand (DEC-36, DEC-51); each value is the next list element,
    verbatim and nonempty. Returns the values as given.
    """
    options, index = {}, 0
    while index < len(rest) and rest[index].startswith('-'):
        option = rest[index]
        if option not in ('--cookies', '--state'):
            raise ConfigError(f'serve: unknown option {option!r}')
        if option in options:
            raise ConfigError(f'serve: {option} given more than once')
        if index + 1 == len(rest):
            raise ConfigError(f'serve: {option} needs a value')
        options[option], index = rest[index + 1], index + 2
    for option in ('--cookies', '--state'):
        if option not in options:
            raise ConfigError(f'serve: {option} is required')
    for option in ('--cookies', '--state'):
        if options[option] == '':
            raise ConfigError(f'serve: {option} must name a directory')
    if index < len(rest):
        raise ConfigError(f'serve: unexpected operand {rest[index]!r}')
    return options['--cookies'], options['--state']


def parse_show(rest: list[str]) -> int:
    """One job ID: ASCII digits, no leading zero, from 1 to 9223372036854775807 (DEC-51)."""
    if len(rest) != 1:
        raise ConfigError('show: exactly one job ID is required')
    word = rest[0]
    if (not word or word[0] not in '123456789' or any(char not in '0123456789' for char in word)
            or len(word) > len(str(MAX_ID)) or int(word) > MAX_ID):
        raise ConfigError('show: job ID must be a positive integer')
    return int(word)


def read_submission(stdin) -> dict:
    """`submit`'s standard input: at most LIMIT bytes of one strict UTF-8 JSON object.

    Its members are not examined: the worker validates them (DEC-51).
    Anything else, a read failure included, is a usage error.
    """
    try:
        data = stdin.buffer.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise ValueError('over the limit')
        value = decode(data)
    except Exception:
        raise ConfigError(SUBMIT_USAGE) from None
    if type(value) is not dict:
        raise ConfigError(SUBMIT_USAGE)
    return value


def _error(reason: str) -> None:
    print(f'{PROG}: error: {reason}', file=sys.stderr, flush=True)


def _log(text: str) -> None:
    print(LOG_PREFIX + text, flush=True)


def _os_reason(path: object, error: OSError) -> str:
    """`<path>: <OS reason>` (DEC-50)."""
    return f'{path}: {error.strerror or "operation failed"}'


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# The output form and strict decoding (DEC-51 *Output form*, *Wire protocol*).

def encode(value: dict) -> bytes:
    """One line: ASCII-escaped JSON, `,` and `:` without spaces, then a newline."""
    return (json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False) + '\n').encode('ascii')


def _unique(pairs: list) -> dict:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError('repeated key')
    return dict(pairs)


def _no_constant(name: str):
    raise ValueError(f'{name} is not allowed')


def _finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError('non-finite number')
    return value


def decode(data: bytes) -> object:
    """Strict UTF-8 JSON: no repeated key, no `NaN`/`Infinity`, finite numbers.

    Raises ValueError for anything else, whatever the decoder raised: any
    exception while decoding means the input was unreadable.
    """
    try:
        return json.loads(data.decode('utf-8'), object_pairs_hook=_unique,
                          parse_constant=_no_constant, parse_float=_finite)
    except Exception:
        raise ValueError('not strict JSON') from None


def _has_surrogate(value: object) -> bool:
    """Whether any string, key or value, holds a surrogate code point (walked without recursion)."""
    pending = [value]
    while pending:
        item = pending.pop()
        if type(item) is str:
            if any('\ud800' <= char <= '\udfff' for char in item):
                return True
        elif type(item) is dict:
            pending.extend(item.keys())
            pending.extend(item.values())
        elif type(item) is list:
            pending.extend(item)
    return False


def _is_int(value: object) -> bool:
    return type(value) is int  # a boolean is not an integer


# The client side (DEC-51).

def request(socket_path: Path, op: str, args: dict, *, timeout: float = CLIENT_TIMEOUT) -> int:
    """Send one request, print the reply, and return the client's exit status.

    0 for a success reply, 1 for an error reply, each reprinted on standard
    output in the output form; 3, with DEC-51's detail on standard error,
    when the worker cannot be reached or does not reply properly. `timeout`
    bounds the whole exchange (30 seconds outside tests).
    """
    payload = encode({'version': VERSION, 'op': op, 'args': args})
    try:
        data = _exchange(socket_path, payload, timeout)
    except TimeoutError:
        return _not_answering('no reply within 30 seconds')
    except OSError as error:
        return _not_answering(f'{socket_path}: {error.strerror}' if error.strerror else 'connection failed')
    reply = _accept(data)
    if reply is None:
        return _not_answering('unexpected reply')
    sys.stdout.buffer.write(encode(reply))
    sys.stdout.flush()
    return 1 if 'error' in reply else 0


def _not_answering(detail: str) -> int:
    _error(f'the worker is not answering: {detail}')
    return 3


def _exchange(socket_path: Path, payload: bytes, timeout: float) -> bytes:
    """Connect, send, shut down the sending side, and read to EOF, all before the deadline.

    A worker that closes while the request is still being sent may already
    have replied (an over-limit request), so a send failure still reads.
    Raises TimeoutError past the deadline and OSError otherwise; returns
    at most LIMIT + 1 bytes.
    """
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(_remaining(deadline))
        sock.connect(os.fspath(socket_path))
        try:
            _send(sock, payload, deadline)
            sock.shutdown(socket.SHUT_WR)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return _receive(sock, deadline)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('deadline passed')
    return remaining


def _send(sock: socket.socket, payload: bytes, deadline: float) -> None:
    view = memoryview(payload)
    while view:
        sock.settimeout(_remaining(deadline))
        view = view[sock.send(view):]


def _receive(sock: socket.socket, deadline: float) -> bytes:
    """Read to EOF, stopping once more than LIMIT bytes have arrived."""
    chunks, size = [], 0
    while size <= LIMIT:
        sock.settimeout(_remaining(deadline))
        chunk = sock.recv(min(1 << 20, LIMIT + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b''.join(chunks)


def _accept(data: bytes) -> dict | None:
    """The reply in the output form with `version` first, or None when it is unexpected.

    Expected: at most LIMIT bytes of one strict UTF-8 JSON object whose
    `version` is the integer 1 and which either has exactly `version` and
    a string `error`, or has no `error` (DEC-51 *Output form*).
    """
    if len(data) > LIMIT:
        return None
    try:
        reply = decode(data)
    except ValueError:
        return None
    if type(reply) is not dict or not _is_int(reply.get('version')) or reply['version'] != VERSION:
        return None
    if 'error' in reply and (set(reply) != {'version', 'error'} or type(reply['error']) is not str):
        return None
    reply = {'version': VERSION, **{key: value for key, value in reply.items() if key != 'version'}}
    try:
        encode(reply)
    except Exception:
        return None  # e.g. nesting too deep to reprint
    return reply


# The service (DEC-50).

class _Refused(Exception):
    """A request the worker answers with an error reply."""


class _Stop:
    """A one-way stop request, set only by the `SIGTERM` and `SIGINT` handlers."""

    def __init__(self):
        self.requested = False

    def __call__(self, number, frame) -> None:
        self.requested = True


class _Repeats:
    """Writes a line unless it repeats the last one within REPEAT seconds (monotonic)."""

    def __init__(self, monotonic: Callable[[], float]):
        self._monotonic = monotonic
        self._last: tuple[str, float] | None = None

    def write(self, text: str) -> None:
        now = self._monotonic()
        if self._last is not None and self._last[0] == text and now - self._last[1] < REPEAT:
            return
        self._last = (text, now)
        _log(text)


def serve(cookies: str, state: str, *, socket_path: Path = SOCKET, downloads: Path = DOWNLOADS,
          environ: Mapping[str, str] | None = None, clock: Callable[[], datetime] = _utc_now,
          monotonic: Callable[[], float] = time.monotonic,
          sleep: Callable[[float], None] = time.sleep) -> int:
    """`downloadinux-worker serve`: start, loop until stopped, stop; returns the exit status.

    `cookies` and `state` are the parsed option values, as given. The
    keyword-only arguments are test seams `main` never passes: the socket
    path, the download root, the environment mapping read for `DL_VPN` and
    the throttle seam (the runner still inherits the process environment),
    the aware UTC wall clock shared by the store and the scheduler, the
    monotonic clock for the once-a-minute lines and the scheduler's VPN
    retry, and the sleep between ownership attempts. The execution
    library's shutdown wait keeps its own real clock.

    Handlers for `SIGTERM` and `SIGINT` are installed for the call and the
    previous ones restored when it returns. Must run on the main thread.
    """
    environ = os.environ if environ is None else environ
    vpn = environ.get('DL_VPN') == 'on'
    try:
        delay = worker_scheduler.throttle_override(environ)
    except DownloadinuxError as error:
        _error(str(error))
        return 1
    stop = _Stop()
    previous = {number: signal.signal(number, stop) for number in (signal.SIGTERM, signal.SIGINT)}
    try:
        service = _Service(Path(cookies), state, Path(socket_path), Path(downloads), vpn=vpn, delay=delay,
                           stop=stop, clock=clock, monotonic=monotonic, sleep=sleep)
        return service.run()
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


class _Service:
    """One `serve` run: the store, scheduler, executor, listener, and stop flag."""

    def __init__(self, cookies: Path, state: str, socket_path: Path, downloads: Path, *, vpn: bool,
                 delay: Callable[[], int] | None, stop: _Stop, clock: Callable[[], datetime],
                 monotonic: Callable[[], float], sleep: Callable[[float], None]):
        self.cookies = cookies
        self.state = state
        self.socket_path = socket_path
        self.downloads = downloads
        self.vpn = vpn
        self.delay = delay
        self.stop = stop
        self.clock = clock
        self.monotonic = monotonic
        self.sleep = sleep
        self.failures = _Repeats(monotonic)
        self.store: job_store.JobStore | None = None
        self.scheduler: worker_scheduler.Scheduler | None = None
        self.executor: worker_execution.Executor | None = None
        self.listener: socket.socket | None = None
        self.bound: tuple[int, int] | None = None  # (st_dev, st_ino) of the socket this run bound
        self.selector: selectors.BaseSelector | None = None

    def run(self) -> int:
        try:
            if not self._own():
                return 0  # stopped while another worker held the lock: nothing opened
            self._start()
        except DownloadinuxError as error:
            _error(str(error))
            return self._abandon()
        except Exception:
            _internal_error()
            self._abandon()
            return 1
        internal = False
        if not self.stop.requested:
            internal = self._loop()
        return self._stop(internal)

    # Start (DEC-50 steps 3 to 5).

    def _own(self) -> bool:
        """Take ownership, retrying each second; False when a stop ends the wait."""
        options = {'clock': self.clock}
        if self.delay is not None:
            options['delay'] = self.delay
        logged_at = None
        while True:
            store = job_store.open_store(Path(self.state), **options)
            if store is not job_store.BUSY:
                self.store = store
                return True
            now = self.monotonic()
            if logged_at is None or now - logged_at >= REPEAT:
                _log(f'another worker holds {self.state}/{job_store.DIRECTORY}/{job_store.LOCK}; waiting')
                logged_at = now
            if self.stop.requested:
                return False
            self.sleep(RETRY)
            if self.stop.requested:
                return False

    def _start(self) -> None:
        marked = self.store.reconcile()
        if marked:
            _log(f'marked {len(marked)} running jobs interrupted (worker restarted)')
        self.executor = worker_execution.Executor(self.store, cookies=self.cookies, state=Path(self.state),
                                                  downloads=self.downloads)
        self.scheduler = worker_scheduler.Scheduler(self.store, self.executor.launch, vpn=self.vpn,
                                                    clock=self.clock, monotonic=self.monotonic)
        if not self.stop.requested:
            self._listen()

    def _listen(self) -> None:
        """Replace a stale socket, bind under umask 0077, chmod 0600, and listen."""
        path = self.socket_path
        try:
            status = os.lstat(path)
        except FileNotFoundError:
            pass
        except OSError as error:
            raise DownloadinuxError(_os_reason(path, error)) from None
        else:
            if not stat.S_ISSOCK(status.st_mode):
                raise DownloadinuxError(f'{path}: exists and is not a socket')
            try:
                os.unlink(path)  # a previous run's socket: the container's filesystem outlives a restart
            except FileNotFoundError:
                pass
            except OSError as error:
                raise DownloadinuxError(_os_reason(path, error)) from None
        try:
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            mask = os.umask(0o077)
            try:
                self.listener.bind(os.fspath(path))
            finally:
                os.umask(mask)
            status = os.lstat(path)
            self.bound = (status.st_dev, status.st_ino)
            os.chmod(path, 0o600)
            self.listener.listen()
            self.listener.setblocking(False)
            self.selector = selectors.DefaultSelector()
            self.selector.register(self.listener, selectors.EVENT_READ)
        except OSError as error:
            raise DownloadinuxError(_os_reason(path, error)) from None
        _log(f'listening on {path}')

    def _abandon(self) -> int:
        """A startup failure: close what was opened and return 1."""
        self._close_socket(report=False)
        if self.store is not None:
            try:
                self.store.close()
            except DownloadinuxError:
                pass
        return 1

    # Loop (DEC-50 *Loop* and *Failures in the loop*).

    def _loop(self) -> bool:
        """Run iterations until a stop request; True after an internal error."""
        try:
            while not self.stop.requested:
                if self.selector.select(TICK) and not self.stop.requested:
                    self._serve_one()
                if self.stop.requested:
                    break
                try:
                    self.executor.poll()
                except DownloadinuxError as error:
                    self.failures.write(f'recording a runner exit failed: {error}')
                if self.stop.requested:
                    break
                try:
                    self.scheduler.run_pass()
                except DownloadinuxError as error:
                    self.failures.write(f'scheduling failed: {error}')
        except Exception:
            _internal_error()
            return True
        return False

    def _serve_one(self) -> None:
        """Accept one connection, read its request, answer it, and close it.

        A client that times out or goes away is dropped without a reply or
        a log line. Exceptions from the operations other than the store's
        refusals propagate: they are internal errors.
        """
        try:
            connection, _ = self.listener.accept()
        except OSError:
            return  # gone before it was accepted
        with connection:
            try:
                data = _read_request(connection)
            except OSError:
                return
            reply = self._answer(data)
            try:
                deadline = time.monotonic() + SERVER_TIMEOUT
                _send(connection, encode(reply), deadline)
            except OSError:
                return

    def _answer(self, data: bytes) -> dict:
        try:
            if len(data) > LIMIT:
                raise _Refused(INVALID)
            try:
                value = decode(data)
            except ValueError:
                raise _Refused(INVALID) from None
            if type(value) is not dict:
                raise _Refused(INVALID)
            version = value.get('version')
            if _is_int(version) and version != VERSION:
                raise _Refused(UNSUPPORTED)
            if set(value) != {'version', 'op', 'args'} or not _is_int(version) or _has_surrogate(value):
                raise _Refused(INVALID)
            operation = self._operations().get(value['op']) if type(value['op']) is str else None
            if operation is None or type(value['args']) is not dict:
                raise _Refused(INVALID)
            try:
                return {'version': VERSION, **operation(value['args'])}
            except DownloadinuxError as error:
                raise _Refused(str(error)) from None
        except _Refused as refusal:
            return {'version': VERSION, 'error': str(refusal)}

    def _operations(self) -> dict[str, Callable[[dict], dict]]:
        return {'status': self._status, 'submit': self._submit, 'show': self._show}

    def _status(self, args: dict) -> dict:
        if args:
            raise _Refused(INVALID)
        return {'running': len(self.store.list(state='running')), 'queued': len(self.store.list(state='queued'))}

    def _submit(self, args: dict) -> dict:
        """Exactly §2's six inputs with their JSON types; the store validates their values."""
        if (set(args) != set(SUBMIT_KEYS) or type(args['preset']) is not str
                or type(args['user_agent']) is not str
                or any(args[key] is not None and type(args[key]) is not str
                       for key in ('lane', 'source', 'revision'))
                or type(args['urls']) is not list or any(type(url) is not str for url in args['urls'])):
            raise _Refused(INVALID)
        result = self.store.submit(args['preset'], args['urls'], lane=args['lane'], source=args['source'],
                                   revision=args['revision'], user_agent=args['user_agent'])
        return {'submission': result.submission,
                'created': [{'id': job.id, 'url': job.url} for job in result.created],
                'duplicates': [{'id': job.id, 'url': job.url} for job in result.duplicates]}

    def _show(self, args: dict) -> dict:
        """One record with every §2 field in §2's order."""
        if set(args) != {'id'} or not _is_int(args['id']) or not 1 <= args['id'] <= MAX_ID:
            raise _Refused(INVALID)
        job_id = args['id']
        try:
            record = self.store.get(job_id)
        except DownloadinuxError as error:
            # The store names its database; the reply names only the ID (DEC-51).
            if str(error) == f'{self.store.path}: unknown job ID {job_id}':
                raise _Refused(f'unknown job ID {job_id}') from None
            raise
        return {'job': {field: getattr(record, field) for field in job_store.RECORD_FIELDS}}

    # Stop (DEC-50 *Stop*, §5 *Shutdown*).

    def _stop(self, internal: bool) -> int:
        _log('stopping')
        status = 1 if internal else 0
        if not self._close_socket(report=True):
            status = 1
        try:
            self.executor.shutdown(self.scheduler)
        except DownloadinuxError as error:
            _error(str(error))
            status = 1
        except Exception:
            _internal_error()
            status = 1
        try:
            self.store.close()
        except DownloadinuxError as error:
            _error(str(error))
            status = 1
        return status

    def _close_socket(self, *, report: bool) -> bool:
        """Close the listener and remove the path only if it still names the bound socket.

        Returns False when an OS error was reported.
        """
        ok = True
        if self.selector is not None:
            self.selector.close()
            self.selector = None
        if self.listener is not None:
            try:
                self.listener.close()
            except OSError as error:
                ok = _report(report, _os_reason(self.socket_path, error))
            self.listener = None
        if self.bound is not None:
            try:
                status = os.lstat(self.socket_path)
                if (status.st_dev, status.st_ino) == self.bound:
                    os.unlink(self.socket_path)
            except FileNotFoundError:
                pass
            except OSError as error:
                ok = _report(report, _os_reason(self.socket_path, error))
            self.bound = None
        return ok


def _report(report: bool, reason: str) -> bool:
    if report:
        _error(reason)
    return False


def _internal_error() -> None:
    _error('internal error')
    traceback.print_exc(file=sys.stderr)
    sys.stderr.flush()


def _read_request(connection: socket.socket) -> bytes:
    """Read to EOF within SERVER_TIMEOUT, stopping once more than LIMIT bytes arrived."""
    return _receive(connection, time.monotonic() + SERVER_TIMEOUT)
