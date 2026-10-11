"""The presets and `dl` (P0001-C04-c; target design, *Presets* and *Host
application*, "pinned for P0001-C04"; DEC-10, DEC-20), with fake runners: no
test starts Docker. `dl`'s cookie selection, user-agent options and launch
order are in `test_host_launch.py` (Named cookie sources §3, DEC-39)."""

import json
import re

import pytest

from downloadinux import checkout, cookie_store, launch, presets
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


def runner(word, urls, agent=AGENT):
    """An anonymous `dl` (the empty store's default): the runner's list."""
    return [*EXEC, *launch.runner_argv("download", cookie_store.ANONYMOUS, agent, word, urls)]


@pytest.mark.parametrize("word", ["video", "v", "gallery", "gal", "g"])
@pytest.mark.parametrize("set_before", [True, False])
def test_dl_runs_the_runner_uncaptured(calls, capsys, word, set_before):
    option = ["--set", f"downloads.user_agent={AGENT}"]
    argv = [*option, "dl", word, *URLS] if set_before else ["dl", word, *URLS, *option]
    status, out, err = run(capsys, *argv)
    assert (status, out, err) == (0, "", "")
    [(kind, argv, _)] = calls
    # Uncaptured, so no -T: the downloaders draw progress on the terminal.
    assert kind == "run"
    assert compose_args(argv) == runner(word, URLS)


def test_dl_uses_the_settings_file_user_agent(calls, capsys, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(f"[downloads]\nuser_agent = {json.dumps(AGENT)}\n")
    assert run(capsys, "--config", str(config), "dl", "v", URLS[0])[0] == 0
    assert compose_args(calls[0][1]) == runner("v", URLS[:1])


def test_dl_reaches_the_configured_project_and_returns_the_status(calls, capsys):
    calls.statuses.append(3)
    status, _, _ = run(capsys, "dl", "gallery", URLS[0], "--set", "container.project=second")
    assert status == 3
    argv = calls[0][1]
    assert argv[argv.index("--project-name") + 1] == "second"


def test_dl_needs_no_vpn_and_checks_nothing_first(calls, capsys):
    assert run(capsys, "--set", "vpn.enabled=false", "dl", "v", URLS[0])[0] == 0
    # One command: no running check, no VPN refusal.
    assert [kind for kind, _, _ in calls] == ["run"]


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


@pytest.mark.parametrize("batch", [False, True])
def test_the_lists_name_the_given_jar(batch):
    # The batch variant adds -q to gallery-dl only (Host application, `batch`).
    assert presets.argv("video", AGENT, batch=batch, cookies=JAR) == VIDEO
    quiet = ["-q"] if batch else []
    assert presets.argv("gallery", AGENT, batch=batch, cookies=JAR) == [GALLERY[0], *quiet, *GALLERY[1:]]
    # Gallery cookies stay Instagram-only (DEC-6).
    gallery = presets.argv("gallery", AGENT, batch=batch, cookies=JAR)
    assert [item for item in gallery if JAR in item] == [f"extractor.instagram.cookies={JAR}"]


@pytest.mark.parametrize("batch", [False, True])
def test_anonymous_lists_have_no_cookie_argument(batch):
    video = presets.argv("video", AGENT, batch=batch, cookies=None)
    assert video == [item for item in VIDEO if item not in ("--cookies", JAR)]
    gallery = presets.argv("gallery", AGENT, batch=batch, cookies=None)
    quiet = ["-q"] if batch else []
    assert gallery == [GALLERY[0], *quiet, *GALLERY[1:-2]]
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
    assert [
        (p.session, p.queue, p.log_prefix, p.title, p.colour)
        for p in (presets.resolve("video"), presets.resolve("gallery"))
    ] == [
        ("batch-video", "dlmp4_queue", "dlmp4", "Batch Download", "blue"),
        ("batch-gallery", "dlgal_queue", "dlgal", "Gallery Batch Download", "magenta"),
    ]


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
