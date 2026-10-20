"""Shared presentation boundaries (DEC-58), independent of the CLI parser."""

import subprocess
import sys
from pathlib import Path

import pytest
from test_host_jobs import NOW, ROW, publish, record

from downloadinux import (cookie_selectors, cookie_store, cookie_writer, cookiejar, operations,
                          render)


def test_fresh_import_has_no_cli_or_argparse():
    result = subprocess.run([sys.executable, '-c',
        'import sys; import downloadinux.render; '
        'assert "downloadinux.cli" not in sys.modules; '
        'assert "argparse" not in sys.modules'], capture_output=True, text=True)
    assert (result.returncode, result.stdout, result.stderr) == (0, '', '')


@pytest.mark.parametrize('login,connect,lines,warning', [
    (operations.Login.VPN_OFF, None,
     ['vpn.enabled is false: the token is kept for when the VPN is enabled.'], None),
    (operations.Login.NOT_RUNNING, None,
     ['The container is not running; it logs in with the token at the next dlx up.'], None),
    (operations.Login.LOGGED_IN, None, ['Logged in.'], None),
    (operations.Login.LOGGED_IN, operations.Connect(('United_States', 'New_York'), True),
     ['Logged in.', 'Connected to United_States New_York.'], None),
    (operations.Login.LOGGED_IN, operations.Connect((), True),
     ['Logged in.', 'Connected to the fastest server.'], None),
    (operations.Login.LOGGED_IN, operations.Connect(('us8649',), False),
     ['Logged in.'], 'could not connect to us8649; try dlx vpn on'),
    (operations.Login.LOGGED_IN, operations.Connect((), False),
     ['Logged in.'], 'could not connect to the fastest server; try dlx vpn on'),
])
def test_login_messages(login, connect, lines, warning):
    assert render.login_messages(operations.LoginResult(login, connect)) == (lines, warning)


@pytest.mark.parametrize('outcome,state,expected', [
    ('cancelled', 'cancelled', 'job 7 cancelled'),
    ('requested', 'running', 'job 7: cancel requested; it ends when its downloader exits'),
    ('refused', 'succeeded', 'job 7 is succeeded; only queued or running jobs can be cancelled'),
    ('unknown', None, 'unknown job ID 7'),
])
def test_cancel_line(outcome, state, expected):
    assert render.cancel_line(dict(id=7, outcome=outcome, state=state)) == expected


def test_stored_token_message():
    assert render.token_stored(Path('/settings/credentials.toml')) == (
        'Stored the NordVPN token in /settings/credentials.toml')


@pytest.mark.parametrize('fields,cells', [
    (dict(state='running', lane='video'),
     ('3', 'running', 'video', 'lane', '2', 'https://example.com/3')),
    (dict(state='queued', preset='gallery', lane=None),
     ('3', 'queued', 'gallery', 'immediate', '2', 'https://example.com/3')),
    (dict(state='queued', wait={'reason': 'vpn'}),
     ('3', 'queued: vpn', 'video', 'immediate', '2', 'https://example.com/3')),
    (dict(state='queued', wait={'reason': 'throttle', 'until': '2026-10-13T12:00:00Z\x1b'}),
     ('3', 'queued: throttle until 2026-10-13T12:00:00Z\\x1b', 'video', 'immediate', '2',
      'https://example.com/3')),
    (dict(state='running', cancel_requested=True),
     ('3', 'running: cancelling', 'video', 'immediate', '2', 'https://example.com/3')),
    (dict(state='failed', url='https://example.com/[b]\x1b\x9b'),
     ('3', 'failed', 'video', 'immediate', '2', 'https://example.com/[b]\\x1b\\x9b')),
])
def test_job_cells(fields, cells):
    assert render.job_cells(record(3, attempt=2, **fields)) == cells


def test_render_jobs_builds_its_rows_from_job_cells():
    jobs = [record(2, state='running', lane='video'), record(10, state='queued', attempt=12)]
    assert render.render_jobs(jobs) == (
        'ID  STATE    PRESET  QUEUE      ATTEMPT  URL\n'
        '2   running  video   lane       1        https://example.com/2\n'
        '10  queued   video   immediate  12       https://example.com/10')


def test_no_jobs():
    assert render.no_jobs(True) == 'No jobs.'
    assert render.no_jobs(False) == 'No queued, running, failed, or interrupted jobs.'


def test_follow_job_escapes_the_url():
    assert render.follow_job(record(7, url='https://example.com/[b]\x1b\r')) == (
        'Job 7: https://example.com/[b]\\x1b\\x0d')


@pytest.mark.parametrize('wait,text', [
    (None, 'job 7 resumed'),
    ({'reason': 'vpn'}, 'job 7 waiting for VPN'),
    ({'reason': 'throttle', 'until': 'tomorrow\x1b\r'},
     'job 7 waiting for throttle until tomorrow\\x1b\\x0d'),
])
def test_follow_wait(wait, text):
    assert render.follow_wait(7, wait) == text


@pytest.mark.parametrize('submission,references,expected', [
    (None, (), 'No URLs in queue: /queue[b]\\x1b'),
    (7, (), 'Queued 0 jobs; skipped 0 active duplicates in gallery lane.'),
    (7, ((1, 'a', False), (2, 'b', True)),
     'Queued 1 jobs; skipped 1 active duplicates in gallery lane.'),
])
def test_batch_report(submission, references, expected):
    from downloadinux.host_downloads import BatchImport, SubmissionReference
    result = BatchImport(Path('/queue[b]\x1b'), 'gallery', submission,
                         tuple(SubmissionReference(*reference) for reference in references))
    assert render.batch_report(result) == expected


# `render.cookie_cells` (TUI §1 *Package*).


def test_cookie_cells_for_a_file_and_a_browser_source(tmp_path):
    root = tmp_path / 'cookies'
    publish(root, 'alpha', 'synthetic', select=True)
    origin = cookie_selectors.normalize('firefox', container='Work', platform='linux').origin()
    material = cookiejar.parse((cookiejar.HEADER + ROW.format('x')).encode(), Path('fixture'))
    cookie_writer.publish(root, 'work', material, origin, expected=None, clock=lambda: NOW)
    alpha, work = cookie_store.list_metadata(cookie_writer.ensure_store(root))['sources']
    assert render.cookie_cells(alpha) == (
        'alpha', 'file', '{"path": "/external/export.txt"}', alpha['revision'],
        alpha['refreshed_at'], 'yes')
    assert render.cookie_cells(work) == (
        'work', 'browser',
        '{"browser": "firefox", "profile": null, "keyring": null, "container": "Work"}',
        work['revision'], work['refreshed_at'], 'no')
    for source in (alpha, work):
        name, kind, origin, revision, refreshed, default = render.cookie_cells(source)
        assert render.render_cookie_source(source) == (
            f'{name}: {kind}; default: {default}\n  origin: {origin}\n'
            f'  revision: {revision}; refreshed_at: {refreshed}')
