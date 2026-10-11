"""`just cookies`, exercised through the real `just` (Named cookie sources §5,
"Cutover recipes (DEC-40)"; DEC-38): it forwards every word to `cookies
firefox`, with no host yt-dlp, probe, or user-agent lookup. `cookies firefox`
itself is in `test_cookie_firefox.py`."""

import os
import shutil
import subprocess

import pytest

from downloadinux import checkout
from test_recipes import recipe

ARGV_STUB = """#!/bin/sh
for arg in "$@"; do printf '%s\\0' "$arg"; done >"$ARGV_LOG"
"""


def just_cookies(tmp_path, *args):
    executable = shutil.which("just")
    assert executable is not None  # the gate needs `just`: fail, never skip
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "uv"
    stub.write_text(ARGV_STUB)
    stub.chmod(0o755)
    log = tmp_path / "argv"
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "ARGV_LOG": str(log)}
    subprocess.run([executable, "--justfile", str(checkout.asset_path("justfile")), "cookies", *args],
                   cwd=checkout.checkout_root(), env=env, check=True, timeout=60)
    return [arg.decode() for arg in log.read_bytes().split(b"\0")[:-1]]


@pytest.mark.parametrize("args", [[], ["my profile"], ["/Users/me/Library/Firefox/Profiles/x y.default"]])
def test_just_cookies_forwards_each_word_to_cookies_firefox(tmp_path, args):
    assert just_cookies(tmp_path, *args) == ["run", "downloadinux", "cookies", "firefox", *args]


def test_the_recipe_is_only_the_cli_wrapper():
    assert recipe("cookies") == [
        "# Export a Firefox profile as the source firefox and make it the default: "
        "just cookies [profile]",
        "cookies *args:",
        '    @uv run downloadinux cookies firefox "$@"',
    ]
    body = "\n".join(recipe("cookies"))
    for word in ("yt-dlp", "probe", "youtube.com", "useragent", "user-agent", "cookies.txt"):
        assert word not in body
