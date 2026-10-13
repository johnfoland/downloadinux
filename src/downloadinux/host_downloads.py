"""Host submission and queue import, without presentation or following.

Target design, Persistent worker and jobs §6, Host submission and queue
import (DEC-56). The existing fronts switch together later, under §7.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from downloadinux import compose, cookie_pinning, host_jobs, job_store, launch, presets, queue_file, settings
from downloadinux.errors import DownloadinuxError
from downloadinux.settings import Host


@dataclass(frozen=True)
class SubmissionReference:
    id: int
    url: str
    duplicate: bool


@dataclass(frozen=True)
class DownloadSubmission:
    submission: int
    references: tuple[SubmissionReference, ...]


@dataclass(frozen=True)
class BatchImport:
    queue: Path
    preset: str
    submission: int | None
    references: tuple[SubmissionReference, ...]


def _preset(word: str) -> presets.Preset:
    try:
        return presets.resolve(word)
    except (KeyError, TypeError):
        raise DownloadinuxError('submission preset must be video or gallery') from None


def _option(user_agent: str | None) -> None:
    if user_agent is not None:
        if type(user_agent) is not str:
            job_store._validate_selection(None, None, user_agent)
        launch.user_agent_option(user_agent)


def _agent(effective: settings.Settings, override: str | None) -> str:
    agent = launch.effective_user_agent(effective['downloads.user_agent'], override)
    # Share the store's pure validator; this never opens a job store (§1).
    job_store._validate_selection(None, None, agent)
    return agent


def _references(reply: dict, urls: Sequence[str]) -> tuple[SubmissionReference, ...]:
    references = {item['url']: SubmissionReference(item['id'], item['url'], duplicate)
                  for key, duplicate in (('created', False), ('duplicates', True))
                  for item in reply[key]}
    return tuple(references[url] for url in dict.fromkeys(urls))


def submit_download(
    preset: str,
    urls: Sequence[str],
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
    *,
    cookie_source: str | None = None,
    anonymous: bool = False,
    user_agent: str | None = None,
    capture: compose.Capture | None = None,
    feed: compose.Feed | None = None,
) -> DownloadSubmission:
    """Validate, pin once, submit once to an immediate queue (DEC-31, DEC-56)."""
    _option(user_agent)
    effective = settings.load(config, overrides, host)
    canonical = _preset(preset).name
    urls = job_store._validate_submission(canonical, urls, None, None, None, '')
    agent = _agent(effective, user_agent)
    selection = cookie_pinning.pin(effective['paths.cookies'], cookie_source, anonymous=anonymous)
    reply = host_jobs.submit(canonical, urls, None, selection, agent, config, overrides, host,
                             capture=capture, feed=feed)
    return DownloadSubmission(reply['submission'], _references(reply, urls))


def import_batch(
    preset: str,
    queue: str | Path | None = None,
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
    *,
    cookie_source: str | None = None,
    anonymous: bool = False,
    user_agent: str | None = None,
    capture: compose.Capture | None = None,
    feed: compose.Feed | None = None,
) -> BatchImport:
    """Submit the initial URLs to their lane, then consume the checked reply.

    Submission commits before consumption. Neither a lost reply nor a failed
    rewrite is undone or automatically retried (Persistent worker §1/§6).
    """
    _option(user_agent)
    effective = settings.load(config, overrides, host)
    canonical = _preset(preset)
    imported = queue_file.read(effective['paths.downloads'] / canonical.queue if queue is None else queue)
    agent = _agent(effective, user_agent)
    if not imported.urls:
        return BatchImport(imported.path, canonical.name, None, ())
    urls = job_store._validate_submission(canonical.name, imported.urls, canonical.name, None, None, '')
    selection = cookie_pinning.pin(effective['paths.cookies'], cookie_source, anonymous=anonymous)
    reply = host_jobs.submit(canonical.name, urls, canonical.name, selection, agent, config, overrides, host,
                             capture=capture, feed=feed)
    references = _references(reply, urls)
    queue_file.consume(imported, (reference.url for reference in references))
    return BatchImport(imported.path, canonical.name, reply['submission'], references)
