"""Typed host following operations (Persistent worker and jobs §6; DEC-57).

The container client polls the service. The host validates its complete
sequence through host_worker.stream, without opening state or mutating work.
Closing a generator owns the same complete child cleanup as jobs logs.
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator, Sequence
from copy import deepcopy
from dataclasses import dataclass

from downloadinux import compose, host_worker, job_protocol, presets, settings, worker
from downloadinux.errors import DownloadinuxError
from downloadinux.settings import Host


@dataclass(frozen=True)
class FollowJob:
    job: dict


@dataclass(frozen=True)
class FollowWait:
    id: int
    wait: dict | None


@dataclass(frozen=True)
class FollowLog:
    id: int
    attempt: int
    data: bytes


@dataclass(frozen=True)
class FollowEnd:
    job: dict


@dataclass(frozen=True)
class FollowDone:
    pass


FollowEvent = FollowJob | FollowWait | FollowLog | FollowEnd | FollowDone
IDENTITY = ('id', 'attempt', 'preset', 'url', 'lane', 'source', 'revision', 'user_agent')


def _check(valid: bool) -> None:
    if not valid:
        raise DownloadinuxError('downloadinux-worker follow printed unexpected output')


def _record(value: object) -> bool:
    return (job_protocol.record(value) and worker._is_id(value['id'])
            and worker._is_id(value['attempt']))


class _Sequence:
    """One child's event sequence, with a private selection and byte counter."""

    def __init__(self, job_id: int | None, lane: str | None):
        self.job_id, self.lane = job_id, lane
        self.selected = None
        self.wait = None
        self.initial_wait = None
        self.offset = 0
        self.seen = False
        self.done = False

    def accept(self, reply: dict) -> FollowEvent:
        _check(not self.done)
        event = reply.get('event')
        if event == 'job':
            return self._job(reply)
        if event == 'done':
            _check(job_protocol._keys(reply, 'version', 'event') and self.selected is None
                   and (self.lane is not None or self.seen))
            self.done = True
            return FollowDone()
        _check(self.selected is not None)
        if event == 'wait':
            return self._wait(reply)
        # A queued selection's already-observed wait must be emitted first.
        _check(self.initial_wait is None)
        if event == 'log':
            return self._log(reply)
        if event == 'end':
            return self._end(reply)
        _check(False)

    def _job(self, reply: dict) -> FollowJob:
        value = reply.get('job')
        _check(job_protocol._keys(reply, 'version', 'event', 'job') and self.selected is None
               and _record(value))
        if self.lane is not None:
            _check(value['lane'] == self.lane and value['state'] in ('queued', 'running'))
        else:
            _check(not self.seen and value['id'] == self.job_id)
        self.selected = deepcopy(value)
        self.seen = True
        self.wait, self.offset = None, 0
        self.initial_wait = deepcopy(value['wait']) if value['state'] == 'queued' else None
        return FollowJob(deepcopy(value))

    def _wait(self, reply: dict) -> FollowWait:
        value = reply.get('wait')
        _check(job_protocol._keys(reply, 'version', 'event', 'id', 'wait')
               and worker._is_id(reply['id']) and reply['id'] == self.selected['id']
               and job_protocol.wait(value) and value != self.wait
               and (self.initial_wait is None or value == self.initial_wait))
        self.wait = deepcopy(value)
        self.initial_wait = None
        return FollowWait(reply['id'], deepcopy(value))

    def _log(self, reply: dict) -> FollowLog:
        data = job_protocol.strict_base64(reply.get('data'))
        _check(job_protocol._keys(reply, 'version', 'event', 'id', 'attempt', 'offset', 'data')
               and worker._is_id(reply['id']) and reply['id'] == self.selected['id']
               and worker._is_id(reply['attempt']) and reply['attempt'] == self.selected['attempt']
               and data is not None and 0 < len(data) <= worker.CHUNK
               and type(reply['offset']) is int and 0 <= reply['offset'] <= worker.MAX_ID
               and reply['offset'] == self.offset + len(data))
        self.offset = reply['offset']
        return FollowLog(reply['id'], reply['attempt'], data)

    def _end(self, reply: dict) -> FollowEnd:
        value = reply.get('job')
        _check(job_protocol._keys(reply, 'version', 'event', 'job') and _record(value)
               and all(value[key] == self.selected[key] for key in IDENTITY)
               and value['state'] in worker.ENDED and value['wait'] is None and self.wait is None
               and (value['exit_status'] is None or type(value['exit_status']) is int
                    and 0 <= value['exit_status'] <= 255))
        self.selected = None
        return FollowEnd(deepcopy(value))


def _events(effective, host, args, job_id, lane, capture, spawn) -> Iterator[FollowEvent]:
    sequence = _Sequence(job_id, lane)
    with contextlib.closing(host_worker.stream(effective, host, args, capture=capture, spawn=spawn,
                                               allow_empty=True)) as replies:
        for reply in replies:
            event = sequence.accept(reply)
            if not isinstance(event, FollowDone):
                yield event
    if not sequence.done:
        raise DownloadinuxError(host_worker.WENT_AWAY)
    # Exit validation and cleanup above must complete before success is visible.
    yield FollowDone()


def follow_jobs(
    ids: Sequence[int],
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
    *,
    capture: compose.Capture | None = None,
    spawn: compose.Spawn | None = None,
) -> Iterator[FollowEvent]:
    """Follow distinct references in input order using one single-ID child at a time."""
    if (not isinstance(ids, Sequence) or isinstance(ids, (str, bytes, bytearray)) or not ids
            or any(not worker._is_id(value) for value in ids)):
        raise DownloadinuxError('follow IDs must be a nonempty ordered sequence of positive integers')
    ids = tuple(dict.fromkeys(ids))
    effective = settings.load(config, overrides, host)
    yielded = False
    for job_id in ids:
        try:
            with contextlib.closing(_events(effective, host, ['follow', str(job_id)], job_id, None,
                                           capture, spawn)) as events:
                for event in events:
                    if not isinstance(event, FollowDone):
                        yielded = True
                        yield event
        except DownloadinuxError as error:
            if yielded and getattr(error, 'worker_connectivity', False):
                raise DownloadinuxError(host_worker.WENT_AWAY) from None
            raise
    yield FollowDone()


def follow_lane(
    preset: str = 'video',
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
    *,
    capture: compose.Capture | None = None,
    spawn: compose.Spawn | None = None,
) -> Iterator[FollowEvent]:
    """Follow current and newly observed work in a canonical batch lane to idle."""
    effective = settings.load(config, overrides, host)
    lane = presets.resolve(preset).name
    with contextlib.closing(_events(effective, host, ['follow', '--lane', lane], None, lane,
                                   capture, spawn)) as events:
        yield from events


def download_status(records: Sequence[dict]) -> int:
    """Reduce checked final records using §6's single/multiple-job rule (DEC-44/57)."""
    if records and all(record['state'] == 'succeeded' for record in records):
        return 0
    if len(records) == 1:
        return records[0]['exit_status'] or 1
    return 1
