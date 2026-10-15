"""Shared presentation boundaries (DEC-58), independent of the CLI parser."""

import subprocess
import sys
from pathlib import Path

import pytest

from downloadinux import operations, render


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
