"""`config user-agent` (P0001-C04-b; Host application, "Downloads, pinned for
P0001-C04"; DEC-10, DEC-21). Chrome's bundle is a real plist written by
`plistlib` (or a real bad one), never a faked read (review-checklist §2)."""

import os
import plistlib
import tomllib

import pytest

from downloadinux import cli, files, useragent

PINNED_151 = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)


@pytest.fixture
def plist(tmp_path, monkeypatch):
    """Chrome's Info.plist, at a fixture path."""
    path = tmp_path / "Google Chrome.app" / "Contents" / "Info.plist"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(useragent, "CHROME_PLIST", path)
    return path


def chrome(plist, version, **extra):
    data = {"CFBundleIdentifier": "com.google.Chrome", **extra}
    if version is not None:
        data["CFBundleShortVersionString"] = version
    plist.write_bytes(plistlib.dumps(data))


@pytest.fixture
def config(tmp_path):
    return tmp_path / "settings" / "config.toml"


def user_agent(capsys, config, *argv):
    status = cli.main(["--config", str(config), "config", "user-agent", *argv])
    out, err = capsys.readouterr()
    return status, out, err


def stored(config):
    return tomllib.loads(config.read_text())["downloads"]["user_agent"]


def test_pins_the_major_version_exactly(plist, config, capsys):
    chrome(plist, "151.0.7000.12")
    config.parent.mkdir()
    config.write_text('[downloads]\nuser_agent = "old"\n')
    status, out, err = user_agent(capsys, config)
    assert (status, err) == (0, "")
    assert out == "User-agent pinned to Chrome 151 (host has 151.0.7000.12).\n"
    assert stored(config) == PINNED_151


def test_edits_keep_comments_mode_and_other_settings(plist, config, capsys):
    chrome(plist, "152.0.1.2")
    config.parent.mkdir()
    before = (
        "# my settings\n"
        "[paths]\n"
        'downloads = "~/Media"  # the external drive\n'
        "\n"
        "[downloads]\n"
        "# keep in step with the cookies\n"
        'user_agent = "Mozilla/5.0 old"  # refreshed by dlx\n'
        "\n"
        "[vpn]\n"
        "enabled = true\n"
    )
    config.write_text(before)
    config.chmod(0o640)
    status, _, _ = user_agent(capsys, config)
    assert status == 0
    after = config.read_text()
    assert after == before.replace(
        '"Mozilla/5.0 old"', f'"{PINNED_151.replace("151", "152")}"'
    )
    assert os.stat(config).st_mode & 0o777 == 0o640


def test_a_file_without_downloads_gains_the_table(plist, config, capsys):
    chrome(plist, "151.0.7000.12")
    config.parent.mkdir()
    config.write_text('# mine\n[vpn]\nenabled = true\n')
    assert user_agent(capsys, config)[0] == 0
    data = tomllib.loads(config.read_text())
    assert data == {"vpn": {"enabled": True}, "downloads": {"user_agent": PINNED_151}}
    assert config.read_text().startswith("# mine\n[vpn]\nenabled = true\n")


def test_a_file_already_holding_the_value_is_not_rewritten(plist, config, capsys):
    chrome(plist, "151.0.7000.12")
    config.parent.mkdir()
    config.write_text(f'[downloads]\nuser_agent = "{PINNED_151}"\n')
    old = os.stat(config)
    os.utime(config, ns=(old.st_atime_ns, old.st_mtime_ns - 10**9))
    old = os.stat(config)
    status, out, _ = user_agent(capsys, config)
    assert status == 0
    assert out == "User-agent pinned to Chrome 151 (host has 151.0.7000.12).\n"
    new = os.stat(config)
    assert (new.st_ino, new.st_mtime_ns) == (old.st_ino, old.st_mtime_ns)


def test_no_settings_file_writes_the_starter_file(plist, config, capsys):
    chrome(plist, "151.0.7000.12")
    status, _, _ = user_agent(capsys, config, "--set", "vpn.server=us8649")
    assert status == 0
    assert config.read_text() == files.starter_text(
        {"vpn.server": "us8649", "downloads.user_agent": PINNED_151}
    )
    assert stored(config) == PINNED_151


def test_the_new_value_beats_a_set_user_agent_in_a_new_file(plist, config, capsys):
    chrome(plist, "151.0.7000.12")
    assert user_agent(capsys, config, "--set", "downloads.user_agent=other")[0] == 0
    assert stored(config) == PINNED_151


WARNING = (
    "downloadinux: warning: cannot read Chrome's version from {}; "
    "downloads.user_agent is unchanged\n"
)


@pytest.mark.parametrize("case", [
    "missing", "no key", "not digits", "empty major", "not a string",
    "garbage", "truncated", "not a dict",
])
@pytest.mark.parametrize("existing", [False, True])
def test_an_unreadable_version_warns_and_writes_nothing(plist, config, capsys, case, existing):
    if case == "no key":
        chrome(plist, None, CFBundleVersion="151.0.7000.12")
    elif case == "not digits":
        chrome(plist, "151b.0.7000.12")
    elif case == "empty major":
        chrome(plist, ".151")
    elif case == "not a string":
        chrome(plist, 151)
    elif case == "garbage":
        plist.write_bytes(b"not a plist at all")
    elif case == "truncated":
        plist.write_bytes(plistlib.dumps({"CFBundleShortVersionString": "151.0"})[:80])
    elif case == "not a dict":
        plist.write_bytes(plistlib.dumps(["151.0.7000.12"]))
    if existing:
        config.parent.mkdir()
        config.write_text('[downloads]\nuser_agent = "old"\n')
        before = os.stat(config)
    status, out, err = user_agent(capsys, config)
    assert (status, out) == (0, "")
    assert err == WARNING.format(plist)
    if existing:
        after = os.stat(config)
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
        assert stored(config) == "old"
    else:
        assert not config.parent.exists()


@pytest.mark.parametrize("major", ["١٥١", "²"])
def test_non_ascii_digits_are_not_a_major(plist, config, capsys, major):
    chrome(plist, f"{major}.0")
    assert user_agent(capsys, config)[2] == WARNING.format(plist)


@pytest.mark.skipif(os.getuid() == 0, reason="root bypasses permissions")
@pytest.mark.parametrize("existing", [False, True])
def test_an_unwritable_settings_directory_exits_1_naming_the_file(plist, config, capsys, existing):
    # review-checklist §1: the real failure, a directory without write access.
    chrome(plist, "151.0.7000.12")
    config.parent.mkdir()
    if existing:
        config.write_text('[downloads]\nuser_agent = "old"\n')
    config.parent.chmod(0o500)
    try:
        status, out, err = user_agent(capsys, config)
    finally:
        config.parent.chmod(0o700)
    assert (status, out) == (1, "")
    assert err == f"downloadinux: error: {config}: Permission denied\n"
    assert list(config.parent.iterdir()) == ([config] if existing else [])


def test_an_invalid_settings_file_is_a_configuration_error(plist, config, capsys):
    chrome(plist, "151.0.7000.12")
    config.parent.mkdir()
    config.write_text("[downloads]\nuser_agent = 5\n")
    status, _, err = user_agent(capsys, config)
    assert status == 2
    assert err.startswith(f"downloadinux: error: {config}: downloads.user_agent: ")


def test_the_default_user_agent_is_the_template_for_151():
    from downloadinux import settings

    assert useragent.user_agent("151") == settings.DEFAULT_USER_AGENT == PINNED_151
