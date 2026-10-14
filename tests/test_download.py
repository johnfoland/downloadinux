"""Presets and downloader lists after worker cutover (DEC-6, DEC-10, DEC-44).

Submission option and pinning coverage is in test_host_launch.py.
"""

import json
import re

import pytest

from downloadinux import checkout, presets
from test_container_commands import calls, compose_args, run  # noqa: F401 (fixture)

# Spaces and parentheses, as a real user-agent has, plus a quote.
AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 'x'"
URLS = ["https://example.com/a?b=1&c=2", "https://example.com/second"]
# The runner's private jar (Named cookie sources §3, "Child argument lists").
JAR = "/state/cookie-invocations/0123456789abcdef0123456789abcdef/cookies.txt"

VIDEO = [
    "yt-dlp", "--config-locations", "/config/video/yt-dlp.conf",
    "--paths", "/downloads/dlmp4", "--cookies", JAR,
    "--add-headers", f"User-Agent:{AGENT}",
]
GALLERY = [
    "gallery-dl", "--config", "/config/gallery/config.json",
    "-o", "extractor.archive=/downloads/dlgal/gallery-dl-archive.sqlite3",
    "-o", f"extractor.user-agent={AGENT}",
    "-o", f"extractor.instagram.user-agent={AGENT}",
    "-o", "extractor.instagram.base-directory=/downloads/dlgal",
    "-o", f"extractor.instagram.cookies={JAR}",
]
EXEC = ["exec", "-u", "dl", "downloadinux"]












@pytest.mark.parametrize("argv", [["dl", "audio", URLS[0]], ["dl", "video"], ["dl"]])
def test_an_unknown_preset_or_a_missing_url_is_a_usage_error(calls, capsys, argv):
    with pytest.raises(SystemExit) as exit_info:
        run(capsys, *argv)
    assert exit_info.value.code == 2
    assert "usage: downloadinux dl" in capsys.readouterr().err
    assert calls == []


def test_missing_docker_on_dl_is_cli_error(tmp_path, monkeypatch, capsys):
    # review-checklist §1, the real failure: no `docker` on the PATH.
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    status, out, err = run(capsys, "dl", "video", URLS[0])
    assert (status, out) == (1, "")
    assert err == "downloadinux: error: docker: No such file or directory\n"


# Named cookie sources §3, "Child argument lists" (DEC-6, DEC-20, DEC-36): the
# runner names its private jar, or passes no cookie argument when anonymous.


def test_the_lists_name_the_given_jar():
    assert presets.argv("video", AGENT, cookies=JAR) == VIDEO
    assert presets.argv("gallery", AGENT, cookies=JAR) == GALLERY
    # Gallery cookies stay Instagram-only (DEC-6).
    gallery = presets.argv("gallery", AGENT, cookies=JAR)
    assert [item for item in gallery if JAR in item] == [f"extractor.instagram.cookies={JAR}"]


def test_anonymous_lists_have_no_cookie_argument():
    video = presets.argv("video", AGENT, cookies=None)
    assert video == [item for item in VIDEO if item not in ("--cookies", JAR)]
    gallery = presets.argv("gallery", AGENT, cookies=None)
    assert gallery == GALLERY[:-2]
    assert GALLERY[-2:] == ["-o", f"extractor.instagram.cookies={JAR}"]
    for listed in (video, gallery):
        assert not [item for item in listed if "cookie" in item.lower()]


@pytest.mark.parametrize("name", ["video", "gallery"])
def test_the_cookie_argument_has_no_default(name):
    # No caller can get the Stage 1 shared jar by leaving the argument out.
    with pytest.raises(TypeError):
        presets.argv(name, AGENT)
    assert not hasattr(presets, "COOKIES")


def test_aliases_and_the_batch_table():
    assert {word: presets.resolve(word).name for word in presets.ALIASES} == {
        "video": "video", "v": "video", "gallery": "gallery", "gal": "gallery", "g": "gallery",
    }
    assert [(p.name, p.queue) for p in presets.PRESETS.values()] == [
        ('video', 'dlmp4_queue'), ('gallery', 'dlgal_queue')]
    assert set(presets.Preset.__dataclass_fields__) == {'name', 'queue'}
    assert not hasattr(presets, 'LOOP')
    assert '-q' not in presets.argv('gallery', AGENT, cookies=JAR)



# --- the preset files ---------------------------------------------------------

PRESET_FILES = ["presets/video/yt-dlp.conf", "presets/gallery/config.json"]
# DEC-10: no output root, cookie path, or user-agent, and no host path.
FORBIDDEN = [
    "--paths", "--cookies", "--user-agent", "user-agent", "base-directory",
    "cookies", "archive", "~", "/home", "/downloads", "/cookies",
]


@pytest.mark.parametrize("name", PRESET_FILES)
def test_preset_files_carry_media_behavior_only(name):
    text = checkout.asset_path(name).read_text()
    assert [word for word in FORBIDDEN if word in text] == []


def test_the_presets_keep_their_media_settings():
    video = checkout.asset_path("presets/video/yt-dlp.conf").read_text()
    for line in ['--format "bv*+ba/b"', "--merge-output-format mp4", "--js-runtimes node",
                 "--sponsorblock-remove sponsor", "--sleep-requests 3"]:
        assert re.search(rf"^{re.escape(line)}$", video, re.M), line
    gallery = json.loads(checkout.asset_path("presets/gallery/config.json").read_text())
    assert gallery == {
        "extractor": {
            "sleep": "1-3",
            "sleep-request": "1-3",
            "instagram": {
                "directory": ["{username}", "{post_shortcode}"],
                "filename": "{num:>02}_{date:%Y}-{date:%m}-{date:%d}_{post_shortcode}_{username}.{extension}",
            },
        },
        "downloader": {},
        "output": {},
        "postprocessor": {},
    }


def test_the_old_config_directory_is_gone():
    assert not (checkout.checkout_root() / "config").exists()
