"""The worker service and its client subcommands (DEC-50, DEC-51, DEC-54).

Target design, *Persistent worker and jobs* §1, "Service lifecycle and
failure handling", "Client subcommands and replies", and "Job clients:
listing, cancel, retry, and logs". Standard library
only (§1, DEC-41): the service drives the owned job store, the scheduler
(DEC-47), and the execution library (DEC-48) through their public
operations, serially on one thread, and the clients speak to it over one
Unix socket. Nothing here reads settings, cookie material, or host paths.

Log lines name no URL, user-agent, or cookie value.
"""
import base64
from collections.abc import Mapping
from datetime import datetime, timezone
import errno
import json
import math
import os
from pathlib import Path
import select
import selectors
import signal
import socket
import stat
import sys
import time
import traceback
from typing import Callable

from . import job_protocol, job_store, worker_execution, worker_scheduler
from .errors import ConfigError, DownloadinuxError

PROG = 'downloadinux'
LOG_PREFIX = 'downloadinux-worker: '
SUBCOMMANDS = ('serve', 'status', 'submit', 'show', 'list', 'cancel', 'retry', 'logs', 'follow')
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
CHUNK = 1048576  # bytes: the most one `logs` request reads (DEC-54 *Replies*)
LOG_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC  # a planted FIFO cannot stall a read
ENDED = ('succeeded', 'failed', 'cancelled', 'interrupted')  # §3's terminal states
SUBMIT_KEYS = ('preset', 'lane', 'source', 'revision', 'user_agent', 'urls')  # §2's submission inputs
STDIN_SUBCOMMANDS = ('submit', 'list', 'retry')  # each reads one JSON object as its ARGS
INVALID = 'invalid request'
TOO_MANY_IDS = 'too many job IDs: the reply would exceed 32 MiB'
RETRY_KEYS = ('id', 'source', 'revision', 'user_agent')  # one `retry` entry (DEC-54 *Requests*)
# The store's cancel outcomes as `cancel` replies name them (DEC-54 *Replies*).
CANCEL_OUTCOMES = {job_store.CANCELLED: 'cancelled', job_store.SIGNAL: 'requested',
                   job_store.REFUSED: 'refused', job_store.UNKNOWN: 'unknown'}
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
        elif command == 'cancel':
            args = {'ids': parse_cancel(rest)}
        elif command == 'logs':
            job_id, attempt, logs_follow = parse_logs(rest)
        elif command == 'follow':
            ids, lane = parse_follow(rest)
        elif rest:
            raise ConfigError(f'{command}: unexpected operand {rest[0]!r}')
        elif command in STDIN_SUBCOMMANDS:
            args = read_object(sys.stdin, command)
        else:
            args = {}
    except ConfigError as error:
        _error(str(error))
        return 2
    if command == 'serve':
        return serve(cookies, state, socket_path=socket_path, **service)
    if command == 'logs':
        return logs(socket_path, job_id, attempt, logs_follow, timeout=timeout)
    if command == 'follow':
        return follow(socket_path, ids, lane, timeout=timeout)
    return request(socket_path, command, args, timeout=timeout)


def _subcommand(argv: list[str]) -> tuple[str, list[str]]:
    if not argv:
        raise ConfigError('a subcommand is required: serve, status, submit, show, list, cancel, retry, logs, or follow')
    if argv[0] not in SUBCOMMANDS:
        raise ConfigError(f'unknown subcommand {argv[0]!r}; use serve, status, submit, show, list, cancel, retry, logs, or follow')
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
    """One job ID (DEC-51)."""
    if len(rest) != 1:
        raise ConfigError('show: exactly one job ID is required')
    return parse_id(rest[0], 'show: job ID must be a positive integer')


def parse_cancel(rest: list[str]) -> list[int]:
    """One or more job IDs, in the order given, repeats included (DEC-54 *Usage*)."""
    if not rest:
        raise ConfigError('cancel: at least one job ID is required')
    return [parse_id(word, 'cancel: job ID must be a positive integer') for word in rest]


def parse_logs(rest: list[str]) -> tuple[int, int | None, bool]:
    """`ID [--attempt N] [--follow]`, options at most once, in any order (DEC-54 *Usage*).

    Every word beginning with `-` is an option, and `--attempt`'s value is
    the next word, whatever it is. The option errors are found scanning
    the words in order; the operand and value forms are checked after.
    Returns the ID, the attempt or None, and whether to follow.
    """
    operands, value, follow, index = [], None, False, 0
    while index < len(rest):
        word = rest[index]
        index += 1
        if not word.startswith('-'):
            operands.append(word)
        elif word == '--attempt':
            if index == len(rest):
                raise ConfigError('logs: --attempt needs a value')
            if value is not None:
                raise ConfigError('logs: --attempt given more than once')
            value, index = rest[index], index + 1
        elif word == '--follow':
            if follow:
                raise ConfigError('logs: --follow given more than once')
            follow = True
        else:
            raise ConfigError(f'logs: unknown option {word!r}')
    if len(operands) != 1:
        raise ConfigError('logs: exactly one job ID is required')
    job_id = parse_id(operands[0], 'logs: job ID must be a positive integer')
    attempt = None if value is None else parse_id(value, 'logs: --attempt must be a positive integer')
    return job_id, attempt, follow


def parse_follow(rest: list[str]) -> tuple[list[int], str | None]:
    """Fixed IDs or one canonical lane; scan-order errors precede value checks (DEC-57)."""
    operands, lane, index = [], None, 0
    while index < len(rest):
        word = rest[index]
        index += 1
        if not word.startswith('-'):
            operands.append(word)
        elif word == '--lane':
            if lane is not None:
                raise ConfigError('follow: --lane given more than once')
            if index == len(rest):
                raise ConfigError('follow: --lane needs a value')
            lane, index = rest[index], index + 1
        else:
            raise ConfigError(f'follow: unknown option {word!r}')
    if lane is not None:
        if operands:
            raise ConfigError('follow: --lane cannot be combined with job IDs')
        if lane not in job_store.PRESETS:
            raise ConfigError('follow: lane must be video or gallery')
        return [], lane
    if not operands:
        raise ConfigError('follow: at least one job ID is required')
    return list(dict.fromkeys(parse_id(word, 'follow: job ID must be a positive integer')
                              for word in operands)), None


def parse_id(word: str, reason: str) -> int:
    """`show`'s form: ASCII digits, no leading zero, from 1 to 9223372036854775807 (DEC-51)."""
    if (not word or word[0] not in '123456789' or any(char not in '0123456789' for char in word)
            or len(word) > len(str(MAX_ID)) or int(word) > MAX_ID):
        raise ConfigError(reason)
    return int(word)


def read_object(stdin, command: str) -> dict:
    """`submit`'s, `list`'s, or `retry`'s standard input: at most LIMIT bytes of one strict UTF-8 JSON object.

    Its members are not examined: the worker validates them (DEC-51,
    DEC-54). Anything else, a read failure included, is the usage error
    `<command>: standard input must be one JSON object`.
    """
    usage = f'{command}: standard input must be one JSON object'
    try:
        data = stdin.buffer.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise ValueError('over the limit')
        value = decode(data)
    except Exception:
        raise ConfigError(usage) from None
    if type(value) is not dict:
        raise ConfigError(usage)
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
    bounds the whole exchange (30 seconds outside tests). An OS error
    writing standard output is status 1 (DEC-54).
    """
    try:
        reply = _ask(socket_path, op, args, timeout)
    except _NotAnswering as failure:
        return _not_answering(str(failure))
    if not _print(reply):
        return 1
    return 1 if 'error' in reply else 0


def logs(socket_path: Path, job_id: int, attempt: int | None, follow: bool, *,
         timeout: float = CLIENT_TIMEOUT) -> int:
    """`logs`: one request per chunk, printing replies as DEC-54's *`logs`' output* pins.

    The first request names `attempt` (None for the job's current one)
    and offset 0; later ones the first reply's attempt and the previous
    reply's offset. Prints each reply with data, and the reply it stops
    after: without `follow` the first empty one, with it the first empty
    one whose attempt has ended. While following, after any other empty
    reply it waits TICK, and before each request and during each wait it
    watches standard input, exiting 0 at end of file without printing
    more; without `follow`, standard input is never read. Each request has
    its own `timeout`. Returns 0, 1 after an error reply or an OS error
    writing standard output, or 3 with DEC-51's detail.
    """
    offset, first = 0, True
    while True:
        if follow and _input_ended(0):
            return 0
        try:
            reply = _ask(socket_path, 'logs', {'id': job_id, 'attempt': attempt, 'offset': offset}, timeout)
            if 'error' not in reply and not _is_log_chunk(reply):
                raise _NotAnswering('unexpected reply')
        except _NotAnswering as failure:
            return _not_answering(str(failure))
        if 'error' in reply:
            _print(reply)
            return 1
        if first:
            attempt, first = reply['attempt'], False
        offset = reply['offset']
        empty = reply['data'] == ''
        stop = empty and (not follow or reply['ended'])
        if (not empty or stop) and not _print(reply):
            return 1
        if stop:
            return 0
        if empty and _input_ended(TICK):
            return 0


class _FollowExit(Exception):
    """A follower detached, printed a refusal, or could not write an event."""

    def __init__(self, status: int):
        self.status = status


def follow(socket_path: Path, ids: list[int], lane: str | None, *,
           timeout: float = CLIENT_TIMEOUT) -> int:
    """Poll short requests, draining one captured attempt before selecting another.

    A terminal observation must precede the empty ended read, so a runner's
    final append is never skipped (§1, Multi-job and lane following; DEC-57).
    Only the service opens logs and state. EOF owns the client's lifetime.
    """
    def ask(op, args):
        if _input_ended(0):
            raise _FollowExit(0)
        reply = _ask(socket_path, op, args, timeout)
        if 'error' in reply:
            _print(reply)
            raise _FollowExit(1)
        return reply

    def emit(event, **fields):
        if not _print({'version': VERSION, 'event': event, **fields}):
            raise _FollowExit(1)

    def checked(reply, job_id=None):
        record = reply.get('job')
        if (set(reply) != {'version', 'job'} or not job_protocol.record(record)
                or not _is_id(record['id']) or not _is_id(record['attempt'])
                or job_id is not None and record['id'] != job_id):
            raise _NotAnswering('unexpected reply')
        return record

    pending = iter(ids)
    try:
        while True:
            if lane is None:
                job_id = next(pending, None)
                if job_id is None:
                    break
                record = checked(ask('show', {'id': job_id}), job_id)
            else:
                reply = ask('lane', {'preset': lane})
                if set(reply) == {'version', 'job'} and reply['job'] is None:
                    break
                record = checked(reply)
                if record['lane'] != lane or record['state'] not in ('queued', 'running'):
                    raise _NotAnswering('unexpected reply')
                job_id = record['id']
            attempt, offset, previous_wait = record['attempt'], 0, None
            emit('job', job=record)
            while True:
                if record['attempt'] != attempt:
                    _print({'version': VERSION, 'error':
                            f'job {job_id} was retried while following; inspect with dlx jobs show {job_id}'})
                    return 1
                wait = record['wait'] if record['state'] == 'queued' else None
                if wait != previous_wait:
                    emit('wait', id=job_id, wait=wait)
                    previous_wait = wait
                chunk = ask('logs', {'id': job_id, 'attempt': attempt, 'offset': offset})
                data = job_protocol.strict_base64(chunk.get('data'))
                if (not _is_log_chunk(chunk) or chunk['attempt'] != attempt or data is None
                        or len(data) > CHUNK or chunk['offset'] != offset + len(data)):
                    raise _NotAnswering('unexpected reply')
                offset = chunk['offset']
                if data:
                    emit('log', id=job_id, attempt=attempt, offset=offset, data=chunk['data'])
                elif record['state'] in ENDED and chunk['ended']:
                    emit('end', job=record)
                    break
                elif _input_ended(TICK):
                    return 0
                record = checked(ask('show', {'id': job_id}), job_id)
        emit('done')
        return 0
    except _FollowExit as ending:
        return ending.status
    except _NotAnswering as failure:
        return _not_answering(str(failure))


def _is_log_chunk(reply: dict) -> bool:
    """A `logs` success reply of DEC-54's shape; any other is unexpected."""
    return (set(reply) == {'version', 'attempt', 'ended', 'offset', 'data'} and _is_id(reply['attempt'])
            and type(reply['ended']) is bool and _is_int(reply['offset']) and 0 <= reply['offset'] <= MAX_ID
            and type(reply['data']) is str)


def _input_ended(wait: float) -> bool:
    """Watch standard input for up to `wait` seconds; True at end of file.

    Data is read and discarded: only end of file detaches a follower
    (DEC-54). Standard input that cannot be watched at all counts as end
    of file, since nothing could then hold the follower open.
    """
    deadline = time.monotonic() + wait
    while True:
        try:
            readable, _, _ = select.select([0], [], [], max(0.0, deadline - time.monotonic()))
            if not readable:
                return False
            if os.read(0, 65536) == b'':
                return True
        except OSError:
            return True


class _NotAnswering(Exception):
    """The worker could not be reached or did not reply properly; the message is DEC-51's detail."""


def _ask(socket_path: Path, op: str, args: dict, timeout: float) -> dict:
    """One exchange: the accepted reply, or `_NotAnswering`."""
    payload = encode({'version': VERSION, 'op': op, 'args': args})
    try:
        data = _exchange(socket_path, payload, timeout)
    except TimeoutError:
        raise _NotAnswering('no reply within 30 seconds') from None
    except OSError as error:
        raise _NotAnswering(f'{socket_path}: {error.strerror}' if error.strerror else 'connection failed') from None
    reply = accept(data)
    if reply is None:
        raise _NotAnswering('unexpected reply')
    return reply


def _print(reply: dict) -> bool:
    """Write one reply on standard output, flushed; False after an OS error writing it.

    On failure standard output is pointed at /dev/null, so the interpreter's
    own flush at exit cannot fail again and print a traceback (DEC-54).
    """
    try:
        sys.stdout.buffer.write(encode(reply))
        sys.stdout.flush()
    except OSError:
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(devnull, sys.stdout.fileno())
            finally:
                os.close(devnull)
        except (OSError, ValueError):
            pass  # no descriptor to repoint (a test's captured output)
        return False
    return True


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


class _Stopping(Exception):
    """A stop request arrived while the worker read a request or sent a reply (DEC-52)."""


def _never() -> bool:
    return False


def _wait(sock: socket.socket, deadline: float, stop: Callable[[], bool]) -> None:
    """Before each socket call: raise on a stop request, else wait at most TICK, never past the deadline.

    A signal handler only sets the stop flag and the interrupted call
    resumes (PEP 475), so the TICK bound is what lets a stop end a stalled
    exchange within half a second (DEC-52).
    """
    if stop():
        raise _Stopping
    sock.settimeout(min(TICK, _remaining(deadline)))


def _send(sock: socket.socket, payload: bytes, deadline: float, stop: Callable[[], bool] = _never) -> None:
    view = memoryview(payload)
    while view:
        _wait(sock, deadline, stop)
        try:
            view = view[sock.send(view):]
        except TimeoutError:
            pass  # the next _wait checks the stop flag and the deadline


def _receive(sock: socket.socket, deadline: float, stop: Callable[[], bool] = _never) -> bytes:
    """Read to EOF, stopping once more than LIMIT bytes have arrived."""
    chunks, size = [], 0
    while size <= LIMIT:
        _wait(sock, deadline, stop)
        try:
            chunk = sock.recv(min(1 << 20, LIMIT + 1 - size))
        except TimeoutError:
            continue  # the next _wait checks the stop flag and the deadline
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b''.join(chunks)


def accept(data: bytes) -> dict | None:
    """The reply in the output form with `version` first, or None when it is unexpected.

    Expected: at most LIMIT bytes of one strict UTF-8 JSON object whose
    `version` is the integer 1 and which either has exactly `version` and
    a string `error`, or has no `error` (DEC-51 *Output form*). The one
    implementation of this rule: the client applies it to the worker's
    reply, and the host to the client's output (§1, "Host invocation and
    reply handling"; DEC-53).
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


def _is_id(value: object) -> bool:
    """An integer in `ID`'s range, 1 to 9223372036854775807 (DEC-51)."""
    return _is_int(value) and 1 <= value <= MAX_ID


def _is_id_list(value: object) -> bool:
    return type(value) is list and bool(value) and all(_is_id(item) for item in value)


def _is_name(value: object, names: tuple[str, ...]) -> bool:
    return type(value) is str and value in names


def _is_name_list(value: object, names: tuple[str, ...]) -> bool:
    return type(value) is list and bool(value) and all(_is_name(item, names) for item in value)


def _record(record: job_store.JobRecord) -> dict:
    """A record as `show` gives it: every §2 field in §2's order (DEC-51)."""
    return {field: getattr(record, field) for field in job_store.RECORD_FIELDS}


# Reply bounds (DEC-54 *Submission size*, issue #79). The worker refuses an
# operation whose bound exceeds LIMIT before the store runs it; no reply is
# larger than its bound, so a committed operation's reply always fits.

def _submit_bound(urls: list[str]) -> int:
    """128 bytes, plus 34 and the URL's JSON string in the output form per distinct URL."""
    return 128 + sum(34 + len(json.dumps(url, ensure_ascii=True)) for url in dict.fromkeys(urls))


def _ids_bound(ids: list[int], per_id: int) -> int:
    """128 bytes plus `per_id` per distinct ID: 72 for `cancel`, 100 for `retry`."""
    return 128 + per_id * len(set(ids))


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
        a log line, and so is one being served when a stop is requested
        (DEC-52): the loop then stops. An operation that already ran keeps
        its effect. Exceptions from the operations other than the store's
        refusals propagate: they are internal errors.
        """
        try:
            connection, _ = self.listener.accept()
        except OSError:
            return  # gone before it was accepted
        stopping = lambda: self.stop.requested
        with connection:
            try:
                data = _receive(connection, time.monotonic() + SERVER_TIMEOUT, stopping)
            except (OSError, _Stopping):
                return
            if stopping():
                return  # the request was read, but the operation has not run
            reply = self._answer(data)
            try:
                _send(connection, encode(reply), time.monotonic() + SERVER_TIMEOUT, stopping)
            except (OSError, _Stopping):
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
        return {'status': self._status, 'submit': self._submit, 'show': self._show, 'list': self._list,
                'cancel': self._cancel, 'retry': self._retry, 'logs': self._logs, 'lane': self._lane}

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
        if _submit_bound(args['urls']) > LIMIT:
            raise _Refused('submission too large: its reply would exceed 32 MiB')  # issue #79, before any commit
        result = self.store.submit(args['preset'], args['urls'], lane=args['lane'], source=args['source'],
                                   revision=args['revision'], user_agent=args['user_agent'])
        return {'submission': result.submission,
                'created': [{'id': job.id, 'url': job.url} for job in result.created],
                'duplicates': [{'id': job.id, 'url': job.url} for job in result.duplicates]}

    def _show(self, args: dict) -> dict:
        if set(args) != {'id'} or not _is_id(args['id']):
            raise _Refused(INVALID)
        return {'job': _record(self._known(args['id']))}

    def _lane(self, args: dict) -> dict:
        """Read the true lane head without changing jobs or deadlines (§1, DEC-57)."""
        if set(args) != {'preset'} or not _is_name(args['preset'], job_store.PRESETS):
            raise _Refused(INVALID)
        preset = args['preset']
        for record in self.store.list(state='running', preset=preset):
            if record.lane == preset:
                return {'job': _record(record)}
        for queue in self.store.queue_heads():
            if queue.lane == preset:
                return {'job': _record(queue.head)}
        return {'job': None}

    def _known(self, job_id: int) -> job_store.JobRecord:
        """The job's record; an unknown ID is refused as `unknown job ID <id>` (DEC-51, DEC-54)."""
        try:
            return self.store.get(job_id)
        except DownloadinuxError as error:
            # The store names its database; the reply names only the ID.
            if str(error) == f'{self.store.path}: unknown job ID {job_id}':
                raise _Refused(f'unknown job ID {job_id}') from None
            raise

    def _list(self, args: dict) -> dict:
        """Every record matching each non-null filter, in ID order (DEC-54 *Requests*, *Replies*)."""
        if (set(args) != {'ids', 'states', 'preset'}
                or args['ids'] is not None and not _is_id_list(args['ids'])
                or args['states'] is not None and not _is_name_list(args['states'], job_store.STATES)
                or args['preset'] is not None and not _is_name(args['preset'], job_store.PRESETS)):
            raise _Refused(INVALID)
        ids = None if args['ids'] is None else set(args['ids'])
        states = None if args['states'] is None else set(args['states'])
        jobs = [_record(record) for record in self.store.list(preset=args['preset'])
                if (ids is None or record.id in ids) and (states is None or record.state in states)]
        if len(encode({'version': VERSION, 'jobs': jobs})) > LIMIT:
            raise _Refused('the job listing would exceed 32 MiB; narrow it')
        return {'jobs': jobs}

    def _cancel(self, args: dict) -> dict:
        """The execution library's cancel, so running jobs' runners are signalled (§5, DEC-48, DEC-54).

        A signal failure raises DEC-48's message after the store's outcomes
        have committed, and becomes the error reply.
        """
        if set(args) != {'ids'} or not _is_id_list(args['ids']):
            raise _Refused(INVALID)
        if _ids_bound(args['ids'], 72) > LIMIT:
            raise _Refused(TOO_MANY_IDS)
        return {'outcomes': [{'id': outcome.id, 'outcome': CANCEL_OUTCOMES[outcome.outcome], 'state': outcome.state}
                             for outcome in self.executor.cancel(args['ids'])]}

    def _logs(self, args: dict) -> dict:
        """One chunk of an attempt's log (DEC-54 *Replies*).

        `ended` is decided before the read, so bytes a runner wrote just
        before it exited are read by a later request, never lost.
        """
        if (set(args) != {'id', 'attempt', 'offset'} or not _is_id(args['id'])
                or args['attempt'] is not None and not _is_id(args['attempt'])
                or not _is_int(args['offset']) or not 0 <= args['offset'] <= MAX_ID):
            raise _Refused(INVALID)
        job_id, offset = args['id'], args['offset']
        record = self._known(job_id)
        attempt = record.attempt if args['attempt'] is None else args['attempt']
        if attempt > record.attempt:
            raise _Refused(f'job {job_id} has no attempt {attempt}')
        ended = attempt < record.attempt or record.state in ENDED
        data = self._read_log(self.store.logs / job_store.log_name(job_id, attempt), offset)
        return {'attempt': attempt, 'ended': ended, 'offset': offset + len(data),
                'data': base64.b64encode(data).decode('ascii')}

    def _read_log(self, log: Path, offset: int) -> bytes:
        """At most CHUNK bytes from `offset`; the descriptor is closed before returning.

        The `logs` directory is opened without following a final symlink,
        as DEC-48's launch opens it, and the log relative to it read-only,
        close-on-exec, nonblocking, and not following a symlink. A log that
        does not exist reads as empty. Anything but a regular file, a
        symlink included (`O_NOFOLLOW`'s ELOOP), is `<log>: not a regular
        file`; any other OS error is `<log>: <OS reason>` (DEC-54).
        """
        try:
            directory = os.open(self.store.logs, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as error:
            raise DownloadinuxError(_os_reason(log, error)) from None
        try:
            try:
                fd = os.open(log.name, LOG_READ_FLAGS, dir_fd=directory)
            except FileNotFoundError:
                return b''
            except OSError as error:
                if error.errno == errno.ELOOP:
                    raise DownloadinuxError(f'{log}: not a regular file') from None
                raise DownloadinuxError(_os_reason(log, error)) from None
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise DownloadinuxError(f'{log}: not a regular file')
                return os.pread(fd, min(CHUNK, MAX_ID - offset), offset)
            except OSError as error:
                raise DownloadinuxError(_os_reason(log, error)) from None
            finally:
                _close(fd)
        finally:
            _close(directory)

    def _retry(self, args: dict) -> dict:
        """The store's retry with the selections the host pinned (§5 *Retry*, DEC-46, DEC-54).

        The store validates the values; an invalid one refuses the whole
        request with the store's message.
        """
        if set(args) != {'jobs'} or type(args['jobs']) is not list or not args['jobs'] or not all(
                type(entry) is dict and set(entry) == set(RETRY_KEYS) and _is_id(entry['id'])
                and all(entry[key] is None or type(entry[key]) is str for key in ('source', 'revision'))
                and type(entry['user_agent']) is str for entry in args['jobs']):
            raise _Refused(INVALID)
        if _ids_bound([entry['id'] for entry in args['jobs']], 100) > LIMIT:
            raise _Refused(TOO_MANY_IDS)
        result = self.store.retry([tuple(entry[key] for key in RETRY_KEYS) for entry in args['jobs']])
        return {'submission': result.submission,
                'outcomes': [{'id': outcome.id, 'outcome': outcome.outcome, 'state': outcome.state,
                              'holder': outcome.holder} for outcome in result.outcomes]}

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


def _close(fd: int) -> None:
    """Close a descriptor the worker has finished reading; a close error changes no reply."""
    try:
        os.close(fd)
    except OSError:
        pass


def _report(report: bool, reason: str) -> bool:
    if report:
        _error(reason)
    return False


def _internal_error() -> None:
    _error('internal error')
    traceback.print_exc(file=sys.stderr)
    sys.stderr.flush()
