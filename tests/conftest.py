"""Shared fixtures.

The checkout holds a real `.env` and cookie jar, and the host a real `~`: no
test may read or write any of them. Every test runs with `HOME` and the
working directory in a temporary directory and no `XDG_*` or
`DOWNLOADINUX_CONFIG` from the host.
"""

import pytest

# Made up for the tests: 64 lowercase hex characters, never a real token.
FAKE_TOKEN = "0123456789abcdef" * 4


@pytest.fixture(autouse=True)
def isolated_host(tmp_path, monkeypatch):
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in (
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "DOWNLOADINUX_CONFIG",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(work)
    return home


@pytest.fixture
def home(isolated_host):
    return isolated_host
