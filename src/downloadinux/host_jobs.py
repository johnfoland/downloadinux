"""The host's job operations (target design, Persistent worker and jobs §6,
"Job operations, output, and exit statuses"; DEC-55).

The `jobs` commands are thin fronts for these, and the Stage 4 TUI will
call them. Each loads the effective settings as every command does and
reaches the worker only through `host_worker.call` or `host_worker.stream`,
so the running check and §1's failure texts apply (DEC-53). Each checks its
reply against DEC-54's shape for its client, exactly; anything else is
`downloadinux-worker SUB printed unexpected output`. Nothing here opens the
job store or a job log (§1).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from downloadinux import compose, cookie_pinning, cookie_store, host_worker, job_store, presets, settings, worker
from downloadinux.errors import DownloadinuxError
from downloadinux.settings import Host

# `jobs list`'s default states, and `jobs export`'s (§6).
LISTED = ("queued", "running", "failed", "interrupted")
EXPORTED = ("failed", "cancelled", "interrupted")
ENDED = ("succeeded", "failed", "cancelled", "interrupted")  # §3's terminal states
# The states each outcome fits (§6 DEC-55 *Reply shapes*); None is unknown.
CANCEL_STATES = {"cancelled": ("cancelled",), "requested": ("running",), "refused": ENDED, "unknown": (None,)}
RETRY_STATES = {"requeued": ("queued",), "refused": ("queued", "running", "succeeded"),
                "shadowed": job_store.RETRYABLE, "unknown": (None,)}


# Reply shapes (§6 DEC-55 *Reply shapes*). A boolean is never an integer.

from downloadinux.job_protocol import (
    _int, _positive, _str, _optional_str, _keys, wait as _wait,
    record as _record, strict_base64 as _strict_base64,
)


def _outcomes(outcomes: object, ids: Sequence[int], states: dict, keys: tuple[str, ...]) -> bool:
    """One outcome per requested ID, repeats collapsed to the first, in
    request order, each with a state that fits it."""
    return (type(outcomes) is list
            and all(_keys(outcome, *keys) and _positive(outcome["id"]) for outcome in outcomes)
            and [outcome["id"] for outcome in outcomes] == list(dict.fromkeys(ids))
            and all(_str(outcome["outcome"]) and outcome["outcome"] in states
                    and outcome["state"] in states[outcome["outcome"]] for outcome in outcomes))


def _retry_outcome(outcome: dict) -> bool:
    """`holder` is the job holding its preset and URL when shadowed, else null."""
    return _positive(outcome["holder"]) if outcome["outcome"] == "shadowed" else outcome["holder"] is None


def _references(value: object) -> bool:
    return type(value) is list and all(
        _keys(item, "id", "url") and _positive(item["id"]) and _str(item["url"]) for item in value)


def _corresponds(reply: dict, urls: Sequence[str]) -> bool:
    """Each distinct input once, unique IDs, each array in input order (DEC-56)."""
    order = {url: index for index, url in enumerate(dict.fromkeys(urls))}
    groups = (reply["created"], reply["duplicates"])
    references = [item for group in groups for item in group]
    if (len(references) != len(order)
            or {item["url"] for item in references} != set(order)
            or len({item["id"] for item in references}) != len(references)):
        return False
    for group in groups:
        positions = [order[item["url"]] for item in group]
        if any(left >= right for left, right in zip(positions, positions[1:])):
            return False
    return True


def _checked(reply: dict, subcommand: str, valid: bool) -> dict:
    if not valid:
        raise DownloadinuxError(f"{host_worker.PROGRAM} {subcommand} printed unexpected output")
    return reply


def _check_listing(reply: dict) -> list[dict]:
    jobs = reply.get("jobs")
    valid = _keys(reply, "version", "jobs") and type(jobs) is list and all(_record(job) for job in jobs)
    return _checked(reply, "list", valid)["jobs"]


# The operations (§6 DEC-55 *Operations*).

def _request(value: dict) -> str:
    """A request object on the client's standard input, in the output form."""
    return worker.encode(value).decode("ascii")


def _list(effective: settings.Settings, host: Host, ids, states, preset, capture, feed) -> list[dict]:
    request = _request({"ids": ids, "states": states, "preset": preset})
    return _check_listing(host_worker.call(effective, host, ["list"], request, capture=capture, feed=feed))


def submit(
    preset: str,
    urls: Sequence[str],
    lane: str | None,
    selection: cookie_store.Pinned | cookie_store.Anonymous,
    user_agent: str,
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
    *,
    capture: compose.Capture | None = None,
    feed: compose.Feed | None = None,
) -> dict:
    """One `submit` request with an already pinned selection and the
    effective user-agent; the checked reply. It pins nothing: `dl` and
    `batch` pin before calling it, from the cutover (§7)."""
    if type(selection) is cookie_store.Pinned:
        source, revision = selection.source, selection.revision
    elif type(selection) is cookie_store.Anonymous:
        source = revision = None
    else:
        raise TypeError(f"not a pinned or anonymous selection: {selection!r}")
    urls = tuple(urls)
    effective = settings.load(config, overrides, host)
    request = _request({"preset": preset, "lane": lane, "source": source, "revision": revision,
                        "user_agent": user_agent, "urls": list(urls)})
    reply = host_worker.call(effective, host, ["submit"], request, capture=capture, feed=feed)
    valid = (_keys(reply, "version", "submission", "created", "duplicates") and _positive(reply["submission"])
             and _references(reply["created"]) and _references(reply["duplicates"])
             and _corresponds(reply, urls))
    return _checked(reply, "submit", valid)


def list_jobs(
    all: bool = False,
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
    *,
    capture: compose.Capture | None = None,
    feed: compose.Feed | None = None,
) -> list[dict]:
    """The queued, running, failed, and interrupted jobs, or every job with
    `all`, in ID order."""
    effective = settings.load(config, overrides, host)
    return _list(effective, host, None, None if all else list(LISTED), None, capture, feed)


@dataclass(frozen=True)
class JobView:
    """`show`'s record and its log paths, `(attempt, host path)` in attempt order."""
    job: dict
    logs: tuple[tuple[int, Path], ...]


def show(
    job_id: int,
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
    *,
    capture: compose.Capture | None = None,
    feed: compose.Feed | None = None,
) -> JobView:
    """The job's record and one log path per attempt that has started: every
    attempt to the current one, which is skipped while the job is queued.
    The host never opens them (§1)."""
    effective = settings.load(config, overrides, host)
    reply = host_worker.call(effective, host, ["show", str(job_id)], capture=capture, feed=feed)
    job = _checked(reply, "show", _keys(reply, "version", "job") and _record(reply["job"]))["job"]
    started = job["attempt"] - (job["state"] == "queued")
    logs = Path(effective["paths.state"]) / "jobs" / "logs"
    return JobView(job, tuple((n, logs / job_store.log_name(job["id"], n)) for n in range(1, started + 1)))


def logs(
    job_id: int,
    attempt: int | None = None,
    follow: bool = False,
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
    *,
    capture: compose.Capture | None = None,
    spawn: compose.Spawn | None = None,
) -> Iterator[bytes]:
    """The attempt's log bytes, each nonempty chunk as it arrives, through
    the streaming call. Every reply must name the first reply's attempt and
    carry strict base64, and when the stream ends normally the last must
    have empty `data`, and `ended` true when following. Closing this
    generator ends the stream, and with it Compose (DEC-55)."""
    effective = settings.load(config, overrides, host)
    args = ["logs", str(job_id), *(["--attempt", str(attempt)] if attempt is not None else []),
            *(["--follow"] if follow else [])]
    first = last = None
    with contextlib.closing(host_worker.stream(effective, host, args, capture=capture, spawn=spawn)) as replies:
        for reply in replies:
            data = _strict_base64(reply.get("data"))
            valid = (_keys(reply, "version", "attempt", "ended", "offset", "data") and _positive(reply["attempt"])
                     and type(reply["ended"]) is bool and _int(reply["offset"]) and reply["offset"] >= 0
                     and data is not None and reply["attempt"] == (first or reply)["attempt"])
            last = _checked(reply, "logs", valid)
            first = first or last
            if data:
                yield data
    _checked(last, "logs", last["data"] == "" and (last["ended"] or not follow))


def cancel(
    ids: Sequence[int],
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
    *,
    capture: compose.Capture | None = None,
    feed: compose.Feed | None = None,
) -> list[dict]:
    """One `cancel` request with the IDs as given; one outcome per ID,
    repeats collapsed to the first, in request order."""
    effective = settings.load(config, overrides, host)
    reply = host_worker.call(effective, host, ["cancel", *map(str, ids)], capture=capture, feed=feed)
    valid = (_keys(reply, "version", "outcomes")
             and _outcomes(reply["outcomes"], ids, CANCEL_STATES, ("id", "outcome", "state")))
    return _checked(reply, "cancel", valid)["outcomes"]


@dataclass(frozen=True)
class RetryLine:
    """One requested ID: its listed record (None when unknown) and, for a
    job that was sent, the reply's outcome (None otherwise)."""
    id: int
    record: dict | None
    outcome: dict | None


def retry(
    ids: Sequence[int],
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
    *,
    cookie_source: str | None = None,
    anonymous: bool = False,
    user_agent: str | None = None,
    capture: compose.Capture | None = None,
    feed: compose.Feed | None = None,
) -> list[RetryLine]:
    """`jobs retry` (§3 *Retry*, §6 *`jobs retry`*): read the IDs, repeats
    collapsed to the first, with one `list` request, then pin and send only
    the failed, cancelled, and interrupted ones. `anonymous` makes every
    job anonymous and `cookie_source` pins that source once; otherwise a
    job without a source stays anonymous and reads no store, and each
    distinct source is pinned once, to its current revision. `user_agent`
    replaces each job's stored one; `downloads.user_agent` is never read.
    All pinning happens before the one `retry` request, so a pinning
    failure retries nothing; with no retryable job nothing is pinned or
    sent. One line per ID, in request order."""
    effective = settings.load(config, overrides, host)
    requested = list(dict.fromkeys(ids))
    listed = {job["id"]: job for job in _list(effective, host, requested, None, None, capture, feed)}
    sent = [listed[job_id] for job_id in requested
            if job_id in listed and listed[job_id]["state"] in job_store.RETRYABLE]
    outcomes: dict[int, dict] = {}
    if sent:
        root = effective["paths.cookies"]
        if anonymous:
            pinned = {job["source"]: cookie_store.ANONYMOUS for job in sent}
        elif cookie_source is not None:
            selection = cookie_pinning.pin(root, cookie_source)
            pinned = {job["source"]: selection for job in sent}
        else:
            pinned = {None: cookie_store.ANONYMOUS}
            for job in sent:
                if job["source"] not in pinned:
                    pinned[job["source"]] = cookie_pinning.pin(root, job["source"])
        jobs = []
        for job in sent:
            selection = pinned[job["source"]]
            named = type(selection) is cookie_store.Pinned
            jobs.append({"id": job["id"], "source": selection.source if named else None,
                         "revision": selection.revision if named else None,
                         "user_agent": job["user_agent"] if user_agent is None else user_agent})
        reply = host_worker.call(effective, host, ["retry"], _request({"jobs": jobs}),
                                 capture=capture, feed=feed)
        valid = (_keys(reply, "version", "submission", "outcomes")
                 and _outcomes(reply["outcomes"], [job["id"] for job in sent], RETRY_STATES,
                               ("id", "outcome", "state", "holder"))
                 and all(_retry_outcome(outcome) for outcome in reply["outcomes"])
                 and (_positive(reply["submission"]) if any(
                     outcome["outcome"] == "requeued" for outcome in reply["outcomes"])
                      else reply["submission"] is None))
        outcomes = {outcome["id"]: outcome for outcome in _checked(reply, "retry", valid)["outcomes"]}
    return [RetryLine(job_id, listed.get(job_id), outcomes.get(job_id)) for job_id in requested]


def export(
    preset: str,
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
    *,
    capture: compose.Capture | None = None,
    feed: compose.Feed | None = None,
) -> list[str]:
    """The URLs of the preset's failed, cancelled, and interrupted jobs, in
    ID order. `preset` may be an alias."""
    effective = settings.load(config, overrides, host)
    jobs = _list(effective, host, None, list(EXPORTED), presets.resolve(preset).name, capture, feed)
    return [job["url"] for job in jobs]
