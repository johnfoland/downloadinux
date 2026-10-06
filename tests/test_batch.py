"""`batch`, `watch`, and the container's zsh helpers (P0001-C04-d; target
design, *Host application*, "Downloads, pinned for P0001-C04", and *Container
contract*), with fake runners: no test starts Docker."""

import os
import subprocess

import pytest

from downloadinux import checkout, presets
from test_container_commands import calls, compose_args, run  # noqa: F401 (fixture)

AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
SCRIPT = (
    'source /home/dl/.zsh-custom/batch.zsh && _batch_downloader_core "$@"; '
    'echo; echo "[done — press a key]"; read -k1'
)
EXEC = ["exec", "-u", "dl", "downloadinux"]
ROWS = {
    "video": ("batch-video", "dlmp4_queue", "dlmp4", "Batch Download", "blue"),
    "gallery": ("batch-gallery", "dlgal_queue", "dlgal", "Gallery Batch Download", "magenta"),
}


def tmux(preset, container_queue):
    session, _, prefix, title, colour = ROWS[preset]
    return [
        *EXEC, "tmux", "new-session", "-d", "-s", session, "-c", "/downloads",
        "zsh", "-f", "-c", SCRIPT, "zsh", container_queue, prefix, title, colour,
        *presets.argv(preset, AGENT, batch=True),
    ]


@pytest.fixture
def downloads(tmp_path):
    path = tmp_path / "My Downloads"
    path.mkdir()
    return path


def batch(capsys, downloads, *argv):
    return run(capsys, "--set", f"paths.downloads={downloads}",
               "--set", f"downloads.user_agent={AGENT}", "batch", *argv)


@pytest.mark.parametrize("preset,word", [
    ("video", "video"), ("video", "v"), ("gallery", "gallery"), ("gallery", "gal"), ("gallery", "g"),
])
def test_default_queue_builds_the_pinned_tmux_argv(calls, capsys, downloads, preset, word):
    queue = ROWS[preset][1]
    (downloads / queue).write_text("https://example.com/1\n")
    status, out, err = batch(capsys, downloads, word)
    assert (status, err) == (0, "")
    assert out == (
        f"Queue: /downloads/{queue}\n"
        f"Running detached in tmux session {ROWS[preset][0]}. Watch it with: dlx watch {preset}\n"
    )
    [(kind, argv, _)] = calls
    assert kind == "run"  # uncaptured: no -T
    assert compose_args(argv) == tmux(preset, f"/downloads/{queue}")


def test_gallery_batch_has_quiet_right_after_gallery_dl(calls, capsys, downloads):
    (downloads / "dlgal_queue").write_text("")
    batch(capsys, downloads, "gallery")
    program = compose_args(calls[0][1])
    start = program.index("gallery-dl")
    assert program[start:start + 3] == ["gallery-dl", "-q", "--config"]


@pytest.mark.parametrize("preset", ["video", "gallery"])
def test_a_given_queue_with_spaces_maps_to_its_container_path(calls, capsys, downloads, preset):
    queue = downloads / "lists dir" / "my queue.txt"
    queue.parent.mkdir()
    queue.write_text("")
    status, out, _ = batch(capsys, downloads, preset, str(queue))
    assert status == 0
    assert out.splitlines()[0] == "Queue: /downloads/lists dir/my queue.txt"
    assert compose_args(calls[0][1]) == tmux(preset, "/downloads/lists dir/my queue.txt")


def test_a_relative_queue_resolves_against_the_working_directory(calls, capsys, downloads, monkeypatch):
    (downloads / "sub").mkdir()
    (downloads / "sub" / "q.txt").write_text("")
    monkeypatch.chdir(downloads / "sub")
    status, out, _ = batch(capsys, downloads, "video", "q.txt")
    assert status == 0
    assert compose_args(calls[0][1]) == tmux("video", "/downloads/sub/q.txt")


def test_a_tilde_queue_expands_home(calls, capsys, home):
    downloads = home / "Down loads"
    downloads.mkdir()
    (downloads / "q.txt").write_text("")
    status, _, _ = batch(capsys, "~/Down loads", "v", "~/Down loads/q.txt")
    assert status == 0
    assert compose_args(calls[0][1]) == tmux("video", "/downloads/q.txt")


def test_a_symlinked_downloads_path_is_compared_as_written(calls, capsys, tmp_path):
    drive = tmp_path / "drive"
    drive.mkdir()
    (drive / "q.txt").write_text("")
    link = tmp_path / "link"
    link.symlink_to(drive)
    # Under the link as written: accepted.
    assert batch(capsys, link, "v", str(link / "q.txt"))[0] == 0
    assert compose_args(calls[0][1]) == tmux("video", "/downloads/q.txt")
    # The same file through its resolved path is not under paths.downloads.
    status, _, err = batch(capsys, link, "v", str(drive / "q.txt"))
    assert status == 1
    assert "is outside paths.downloads" in err


@pytest.mark.parametrize("given", [None, "missing.txt", "."])
def test_a_missing_queue_exits_1_and_runs_nothing(calls, capsys, downloads, monkeypatch, given):
    monkeypatch.chdir(downloads)
    argv = ["video"] if given is None else ["video", given]
    status, out, err = batch(capsys, downloads, *argv)
    expected = downloads / ("dlmp4_queue" if given is None else given)
    assert (status, out) == (1, "")
    assert err == f"downloadinux: error: no such queue file: {os.path.abspath(expected)}\n"
    assert calls == []


@pytest.mark.parametrize("where", ["sibling", "prefix"])
def test_a_queue_outside_downloads_exits_1_and_runs_nothing(calls, capsys, downloads, where):
    # "prefix": a sibling whose name starts with the downloads directory's.
    outside = downloads.parent / ("elsewhere" if where == "sibling" else downloads.name + "2")
    outside.mkdir()
    queue = outside / "q.txt"
    queue.write_text("")
    status, out, err = batch(capsys, downloads, "gallery", str(queue))
    assert (status, out) == (1, "")
    assert err == (
        f"downloadinux: error: {queue} is outside paths.downloads ({downloads}); "
        "the container cannot see it\n"
    )
    assert calls == []


@pytest.mark.parametrize("preset", ["video", "gallery"])
def test_compose_failing_exits_1_with_the_pinned_message(calls, capsys, downloads, preset):
    (downloads / ROWS[preset][1]).write_text("")
    calls.statuses.append(1)
    status, out, err = batch(capsys, downloads, preset)
    assert (status, out) == (1, "")
    assert err == (
        f"downloadinux: error: could not start tmux session {ROWS[preset][0]}; "
        f"if one is already running, attach with dlx watch {preset}\n"
    )


def test_batch_never_starts_interactive_zsh(calls, capsys, downloads):
    # D3 Done when: no `zsh -i` and no `.zshrc`.
    for preset in ROWS:
        (downloads / ROWS[preset][1]).write_text("")
        batch(capsys, downloads, preset)
    for _, argv, _ in calls:
        assert "-i" not in argv
        assert not any(".zshrc" in arg for arg in argv)
        assert not any("-ic" == arg or arg.startswith("zsh -i") for arg in argv)


def test_batch_reaches_the_configured_project(calls, capsys, downloads):
    (downloads / "dlmp4_queue").write_text("")
    batch(capsys, downloads, "v", "--set", "container.project=second")
    argv = calls[0][1]
    assert argv[argv.index("--project-name") + 1] == "second"


def test_an_unknown_batch_preset_is_a_usage_error(calls, capsys, downloads):
    with pytest.raises(SystemExit) as exit_info:
        batch(capsys, downloads, "audio")
    assert exit_info.value.code == 2
    assert calls == []


def test_missing_docker_on_batch_is_cli_error(tmp_path, monkeypatch, capsys, downloads):
    (downloads / "dlmp4_queue").write_text("")
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    status, _, err = batch(capsys, downloads, "video")
    assert status == 1
    assert err == "downloadinux: error: docker: No such file or directory\n"


# --- watch ---------------------------------------------------------------------


@pytest.mark.parametrize("argv,session", [
    ([], "batch-video"), (["video"], "batch-video"), (["v"], "batch-video"),
    (["gallery"], "batch-gallery"), (["gal"], "batch-gallery"), (["g"], "batch-gallery"),
    (["my-session"], "my-session"),
])
def test_watch_attaches_uncaptured_and_returns_the_status(calls, capsys, argv, session):
    calls.statuses.append(4)
    status, out, err = run(capsys, "watch", *argv)
    assert (status, out, err) == (4, "", "")
    [(kind, call, _)] = calls
    assert kind == "run"
    assert compose_args(call) == [*EXEC, "tmux", "attach", "-t", session]


# --- the container's zsh helpers ----------------------------------------------

ZSH = """
for f in "$ZSH_HELPERS"/*.zsh; do source "$f"; done
# Stubs print their arguments NUL-separated, one call per line.
yt-dlp() { printf '%s\\0' yt-dlp "$@"; print; }
gallery-dl() { printf '%s\\0' gallery-dl "$@"; print; }
_batch_downloader_core() { printf '%s\\0' _batch_downloader_core "$@"; print; }
whence -w dlmp4 dlgal dlmp4_batch dlgal_batch _batch_downloader_core
dlmp4 "$URL"
dlgal "$URL"
dlmp4_batch "$QUEUE"
dlgal_batch "$QUEUE"
"""


def test_the_shell_helpers_run_the_package_lists(tmp_path):
    # Run under real zsh without startup files; fails, not skips, without zsh.
    queue = tmp_path / "queue file"
    queue.write_text("https://example.com/q\n")
    url = "https://example.com/watch?v=1&x=2"
    env = {
        "PATH": os.environ["PATH"], "HOME": str(tmp_path),
        "ZSH_HELPERS": str(checkout.asset_path("zsh")),
        "DL_USER_AGENT": AGENT, "URL": url, "QUEUE": str(queue),
    }
    result = subprocess.run(["zsh", "-f", "-c", ZSH], env=env, capture_output=True,
                            text=True, check=False)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[:5] == [
        "dlmp4: function", "dlgal: function", "dlmp4_batch: function",
        "dlgal_batch: function", "_batch_downloader_core: function",
    ]
    words = [line.split("\0")[:-1] for line in lines[5:]]
    assert words == [
        [*presets.argv("video", AGENT), url],
        [*presets.argv("gallery", AGENT), url],
        ["_batch_downloader_core", str(queue), "dlmp4", "Batch Download", "blue",
         *presets.argv("video", AGENT, batch=True)],
        ["_batch_downloader_core", str(queue), "dlgal", "Gallery Batch Download", "magenta",
         *presets.argv("gallery", AGENT, batch=True)],
    ]


def test_the_shell_starts_in_downloads_and_names_no_old_path():
    zshrc = checkout.asset_path("zsh/zshrc").read_text()
    assert "cd /downloads" in zshrc
    for name in ("zshrc", "aliases.zsh", "batch.zsh", "functions.zsh"):
        text = checkout.asset_path(f"zsh/{name}").read_text()
        assert "HOME/Downloads" not in text and "--config-location " not in text, name


def test_batch_zsh_holds_only_the_loop():
    # `dlx batch` sources it alone, so it defines the loop and nothing else.
    text = checkout.asset_path("zsh/batch.zsh").read_text()
    code = [line for line in text.splitlines() if line and not line.lstrip().startswith("#")]
    assert code[0] == "_batch_downloader_core() {"
    assert code[-1] == "}"
    assert "_batch_downloader_core()" not in checkout.asset_path("zsh/functions.zsh").read_text()
