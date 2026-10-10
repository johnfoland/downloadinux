"""The presets and `dl` (P0001-C04-c; target design, *Presets* and *Host
application*, "pinned for P0001-C04"; DEC-10, DEC-20), with fake runners: no
test starts Docker."""

import json
import os
import re

import pytest

from downloadinux import checkout, presets
from test_container_commands import calls, compose_args, run  # noqa: F401 (fixture)

# Spaces and parentheses, as a real user-agent has, plus a quote.
AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 'x'"
URLS = ["https://example.com/a?b=1&c=2", "https://example.com/second"]

VIDEO = [
    "yt-dlp", "--config-locations", "/config/video/yt-dlp.conf",
    "--paths", "/downloads/dlmp4", "--cookies", "/cookies/cookies.txt",
    "--add-headers", f"User-Agent:{AGENT}",
]
GALLERY = [
    "gallery-dl", "--config", "/config/gallery/config.json",
    "-o", "extractor.archive=/downloads/dlgal/gallery-dl-archive.sqlite3",
    "-o", f"extractor.user-agent={AGENT}",
    "-o", f"extractor.instagram.user-agent={AGENT}",
    "-o", "extractor.instagram.base-directory=/downloads/dlgal",
    "-o", "extractor.instagram.cookies=/cookies/cookies.txt",
]
EXEC = ["exec", "-u", "dl", "downloadinux"]


@pytest.mark.parametrize("word,expected", [
    ("video", VIDEO), ("v", VIDEO), ("gallery", GALLERY), ("gal", GALLERY), ("g", GALLERY),
])
@pytest.mark.parametrize("set_before", [True, False])
def test_dl_runs_the_whole_pinned_argument_list(calls, capsys, word, expected, set_before):
    option = ["--set", f"downloads.user_agent={AGENT}"]
    argv = [*option, "dl", word, *URLS] if set_before else ["dl", word, *URLS, *option]
    status, out, err = run(capsys, *argv)
    assert (status, out, err) == (0, "", "")
    [(kind, argv, _)] = calls
    # Uncaptured, so no -T: the downloaders draw progress on the terminal.
    assert kind == "run"
    assert compose_args(argv) == [*EXEC, *expected, *URLS]


def test_dl_uses_the_settings_file_user_agent(calls, capsys, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(f"[downloads]\nuser_agent = {json.dumps(AGENT)}\n")
    assert run(capsys, "--config", str(config), "dl", "v", URLS[0])[0] == 0
    assert compose_args(calls[0][1]) == [*EXEC, *VIDEO, URLS[0]]


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


def test_batch_lists_add_quiet_only_to_gallery():
    # The batch variant of each list (Host application, `batch`).
    assert presets.argv("video", AGENT, batch=True) == VIDEO
    assert presets.argv("gallery", AGENT, batch=True) == [GALLERY[0], "-q", *GALLERY[1:]]


# Named cookie sources §3, "Child argument lists" (DEC-6, DEC-20, DEC-36): the
# runner names its private jar, or passes no cookie argument when anonymous.
JAR = "/state/cookie-invocations/0123456789abcdef0123456789abcdef/cookies.txt"


@pytest.mark.parametrize("batch", [False, True])
def test_default_cookie_argument_is_the_stage1_jar(batch):
    for name in ("video", "gallery"):
        assert presets.argv(name, AGENT, batch=batch) == presets.argv(
            name, AGENT, batch=batch, cookies="/cookies/cookies.txt")
    assert presets.COOKIES == "/cookies/cookies.txt"


@pytest.mark.parametrize("batch", [False, True])
def test_named_jar_replaces_only_the_cookie_path(batch):
    assert presets.argv("video", AGENT, batch=batch, cookies=JAR) == [
        item.replace("/cookies/cookies.txt", JAR) for item in VIDEO
    ]
    quiet = ["-q"] if batch else []
    assert presets.argv("gallery", AGENT, batch=batch, cookies=JAR) == [
        GALLERY[0], *quiet, *(item.replace("/cookies/cookies.txt", JAR) for item in GALLERY[1:])
    ]
    # Gallery cookies stay Instagram-only (DEC-6).
    gallery = presets.argv("gallery", AGENT, batch=batch, cookies=JAR)
    assert [item for item in gallery if JAR in item] == [f"extractor.instagram.cookies={JAR}"]


@pytest.mark.parametrize("batch", [False, True])
def test_anonymous_lists_have_no_cookie_argument(batch):
    video = presets.argv("video", AGENT, batch=batch, cookies=None)
    assert video == [item for item in VIDEO if item not in ("--cookies", "/cookies/cookies.txt")]
    gallery = presets.argv("gallery", AGENT, batch=batch, cookies=None)
    quiet = ["-q"] if batch else []
    assert gallery == [GALLERY[0], *quiet, *GALLERY[1:-2]]
    assert GALLERY[-2:] == ["-o", "extractor.instagram.cookies=/cookies/cookies.txt"]
    for listed in (video, gallery):
        assert not [item for item in listed if "cookie" in item.lower()]


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


@pytest.mark.parametrize("registry", ["default", "damaged"])
def test_dl_still_uses_the_stage1_jar_and_ignores_sources(calls, capsys, tmp_path, registry):
    """Pinning and snapshots are unused groundwork until the coordinated
    cutover (target design, Named cookie sources §5): `dl` neither reads the
    registry nor allocates a private jar."""
    cookies, state = tmp_path / "cookies", tmp_path / "state"
    sources = cookies / "sources" / "alpha"
    sources.mkdir(parents=True)
    state.mkdir()
    (sources / ("1" * 32 + ".txt")).write_text("# Netscape HTTP Cookie File\n")
    record = {"version": 1, "default": "alpha", "sources": {"alpha": {
        "kind": "file", "path": "/external.txt", "revision": "1" * 32,
        "refreshed_at": "2026-10-10T12:00:00Z"}}}
    (cookies / "sources.json").write_text(json.dumps(record) if registry == "default" else "{")
    before = (cookies / "sources.json").read_bytes()
    status, _, _ = run(capsys, "--set", f"paths.cookies={cookies}", "--set", f"paths.state={state}",
                       "--set", f"downloads.user_agent={AGENT}", "dl", "video", URLS[0])
    assert status == 0
    assert compose_args(calls[0][1]) == [*EXEC, *VIDEO, URLS[0]]
    assert (cookies / "sources.json").read_bytes() == before
    assert os.listdir(state) == []
