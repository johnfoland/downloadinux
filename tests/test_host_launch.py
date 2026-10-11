"""`dl` and `batch` through the container runner (Named cookie sources §3,
"Host launch (DEC-39)"; §4; §5; DEC-40), driven through `cli.main` with a
recording Compose runner: no test starts Docker. Every store is real, and so
is every refresh, replacement, damage, and migration."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from downloadinux import checkout, cli, compose, cookiejar, cookie_store as store, launch, operations
from test_container_commands import compose_args

AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 'x'"
URLS = ["https://example.com/a?b=1&c=2", "https://example.com/with space"]
EXEC = ["exec", "-u", "dl", "downloadinux"]
STAGE1 = "/cookies/cookies.txt"
USAGE = "downloadinux: error: --user-agent: VALUE must be nonempty and contain no CR or LF\n"
CONFIGURED = ("downloadinux: error: downloads.user_agent contains CR or LF; set a single-line "
              "value with dlx config user-agent VALUE\n")
QUEUES = {"video": "dlmp4_queue", "gallery": "dlgal_queue"}


def netscape(value):
    return (cookiejar.HEADER + f".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\t{value}\n").encode()


class Rig:
    """A cookies root, a downloads directory, origin files, and a recording
    `docker compose` runner whose `hook` runs before it returns."""

    def __init__(self, tmp_path, monkeypatch):
        self.cookies = tmp_path / "managed cookies"
        self.downloads = tmp_path / "My Downloads"
        self.origins = tmp_path / "origins"
        self.downloads.mkdir()
        self.origins.mkdir()
        self.calls = []
        self.statuses = []
        self.hook = None

        def run(argv, environ):
            self.calls.append(list(argv))
            if self.hook is not None:
                self.hook()
            return self.statuses.pop(0) if self.statuses else 0

        monkeypatch.setattr(compose, "run_process", run)
        monkeypatch.setattr(compose, "capture_process", lambda argv, environ: (0, ""))

    @property
    def overrides(self):
        return [f"paths.cookies={self.cookies}", f"paths.downloads={self.downloads}"]

    def sets(self, agent=AGENT):
        """`--set` options for the roots and, unless None, the user-agent."""
        options = [item for pair in self.overrides for item in ("--set", pair)]
        return options if agent is None else [*options, "--set", f"downloads.user_agent={agent}"]

    def register(self, name, value):
        origin = self.origins / f"{name}.txt"
        origin.write_bytes(netscape(value))
        operations.import_cookies(name, str(origin), overrides=self.overrides)
        return self.revision(name)

    def revision(self, name):
        return store.read(self.cookies).sources[name].revision

    def queue(self, preset):
        (self.downloads / QUEUES[preset]).write_text("https://example.com/q\n")
        return f"/downloads/{QUEUES[preset]}"

    def program(self):
        [argv] = self.calls
        args = compose_args(argv)
        assert args[:4] == EXEC
        assert not any(STAGE1 in arg for arg in argv)
        return args[4:]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


def run(capsys, *argv):
    try:
        status = cli.main(list(argv))
    except SystemExit as exit_info:
        status = exit_info.code
    out, err = capsys.readouterr()
    assert "Traceback" not in err and "COOKIE_CANARY" not in out + err
    return status, out, err


def expected(command, selection, preset, operands, agent=AGENT):
    args = launch.runner_argv(command, selection, agent, preset, operands)
    return args if command == "download" else launch.batch_program(preset, args)


def launch_argv(rig, command, preset, *options, agent=AGENT):
    if command == "dl":
        return [*rig.sets(agent), "dl", *options, preset, *URLS], URLS
    return [*rig.sets(agent), "batch", *options, preset], [rig.queue(presets_name(preset))]


def presets_name(word):
    return "video" if word in ("video", "v") else "gallery"


COMMANDS = [("dl", "download"), ("batch", "batch")]


# --- selection --------------------------------------------------------------

@pytest.mark.parametrize("cli_command,command", COMMANDS)
@pytest.mark.parametrize("preset", ["video", "gallery", "v", "g"])
def test_a_named_source_pins_its_current_revision(rig, capsys, cli_command, command, preset):
    alpha = rig.register("alpha", "ALPHA")
    rig.register("beta", "BETA")
    operations.cookie_default("beta", overrides=rig.overrides)
    argv, operands = launch_argv(rig, cli_command, preset, "--cookie-source", "alpha")
    status, _, err = run(capsys, *argv)
    assert (status, err) == (0, "")
    assert rig.program() == expected(command, store.Pinned("alpha", alpha), preset, operands)


@pytest.mark.parametrize("cli_command,command", COMMANDS)
@pytest.mark.parametrize("preset", ["video", "gallery"])
def test_without_a_selector_the_default_is_pinned(rig, capsys, cli_command, command, preset):
    rig.register("alpha", "ALPHA")
    beta = rig.register("beta", "BETA")
    operations.cookie_default("beta", overrides=rig.overrides)
    argv, operands = launch_argv(rig, cli_command, preset)
    assert run(capsys, *argv)[0] == 0
    assert rig.program() == expected(command, store.Pinned("beta", beta), preset, operands)


@pytest.mark.parametrize("cli_command,command", COMMANDS)
@pytest.mark.parametrize("preset", ["video", "gallery"])
def test_an_anonymous_default_launches_anonymously(rig, capsys, cli_command, command, preset):
    rig.register("alpha", "ALPHA")  # registered, but registration selects nothing
    argv, operands = launch_argv(rig, cli_command, preset)
    assert run(capsys, *argv)[0] == 0
    assert rig.program() == expected(command, store.ANONYMOUS, preset, operands)


@pytest.mark.parametrize("cli_command,command", COMMANDS)
@pytest.mark.parametrize("preset", ["video", "gallery"])
def test_explicit_anonymous_wins_over_a_named_default(rig, capsys, cli_command, command, preset):
    rig.register("alpha", "ALPHA")
    operations.cookie_default("alpha", overrides=rig.overrides)
    argv, operands = launch_argv(rig, cli_command, preset, "--anonymous")
    assert run(capsys, *argv)[0] == 0
    assert rig.program() == expected(command, store.ANONYMOUS, preset, operands)


def test_the_batch_report_is_unchanged(rig, capsys):
    rig.queue("gallery")
    status, out, err = run(capsys, *rig.sets(), "batch", "--anonymous", "gal")
    assert (status, err) == (0, "")
    assert out == ("Queue: /downloads/dlgal_queue\n"
                   "Running detached in tmux session batch-gallery. Watch it with: dlx watch gallery\n")


# --- a change after submission ----------------------------------------------

def refresh(rig):
    (rig.origins / "alpha.txt").write_bytes(netscape("ALPHA_TWO"))
    operations.refresh_cookies("alpha", overrides=rig.overrides)


def switch_default(rig):
    operations.cookie_default("beta", overrides=rig.overrides)


def replace(rig):
    other = rig.origins / "replacement.txt"
    other.write_bytes(netscape("REPLACED"))
    operations.import_cookies("alpha", str(other), replace=True, overrides=rig.overrides)


@pytest.mark.parametrize("change", [refresh, switch_default, replace])
@pytest.mark.parametrize("cli_command,command", COMMANDS)
def test_a_change_before_the_runner_starts_keeps_the_pinned_revision(rig, capsys, change, cli_command, command):
    alpha = rig.register("alpha", "ALPHA")
    rig.register("beta", "BETA")
    operations.cookie_default("alpha", overrides=rig.overrides)
    pinned_bytes = (rig.cookies / "sources" / "alpha" / f"{alpha}.txt").read_bytes()
    rig.hook = lambda: change(rig)
    argv, operands = launch_argv(rig, cli_command, "video")
    assert run(capsys, *argv)[0] == 0
    # Compose received the first revision, and the store changed meanwhile.
    assert rig.program() == expected(command, store.Pinned("alpha", alpha), "video", operands)
    snapshot = store.read(rig.cookies)
    assert (snapshot.default, snapshot.sources["alpha"].revision) != ("alpha", alpha)
    # The runner's later read of that pair still returns the old bytes.
    assert store.read_pinned(rig.cookies, store.Pinned("alpha", alpha)).data == pinned_bytes
    assert b"ALPHA" in pinned_bytes and b"ALPHA_TWO" not in pinned_bytes


# --- order: usage errors before settings ------------------------------------

@pytest.fixture
def broken_settings(tmp_path):
    path = tmp_path / "broken.toml"
    path.write_text("[downloads\n")
    return path


SELECTOR_ERRORS = [
    (["--cookie-source", "Bad Name"], "argument --cookie-source: source name must match"),
    (["--cookie-source", "alpha", "--anonymous"], "not allowed with argument"),
    (["--anonymous", "--cookie-source", "alpha"], "not allowed with argument"),
]


@pytest.mark.parametrize("command", ["dl", "batch"])
@pytest.mark.parametrize("options,message", SELECTOR_ERRORS)
def test_selector_usage_errors_win_over_invalid_settings(rig, capsys, broken_settings, command, options, message):
    operands = ["video", URLS[0]] if command == "dl" else ["video"]
    status, out, err = run(capsys, "--config", str(broken_settings), *rig.sets(), command, *options, *operands)
    assert (status, out) == (2, "")
    assert f"usage: downloadinux {command}" in err and message in err
    assert str(broken_settings) not in err
    assert rig.calls == [] and not rig.cookies.exists()


@pytest.mark.parametrize("command", ["dl", "batch"])
@pytest.mark.parametrize("value", ["", "a\nb", "a\rb"])
@pytest.mark.parametrize("spelling", ["separate", "equals"])
def test_an_invalid_user_agent_option_is_the_exact_usage_error(rig, capsys, broken_settings, command, value, spelling):
    option = ["--user-agent", value] if spelling == "separate" else [f"--user-agent={value}"]
    operands = ["video", URLS[0]] if command == "dl" else ["video"]
    status, out, err = run(capsys, "--config", str(broken_settings), command, *operands, *option)
    assert (status, out, err) == (2, "", USAGE)
    assert rig.calls == [] and not rig.cookies.exists()


# --- order: after settings, before pinning ----------------------------------

@pytest.fixture
def legacy_jar(rig):
    """A nonempty Stage 1 jar: any pin would migrate it to `legacy`."""
    rig.cookies.mkdir(mode=0o700)
    path = rig.cookies / "cookies.txt"
    path.write_bytes(netscape("COOKIE_CANARY"))
    return path


@pytest.mark.parametrize("cli_command,command", COMMANDS)
@pytest.mark.parametrize("agent", ["a\nb", "a\rb", "\n"])
def test_a_multiline_configured_user_agent_refuses_before_migrating(rig, capsys, tmp_path, legacy_jar, cli_command, command, agent):
    settings = tmp_path / "config.toml"
    settings.write_text(f"[downloads]\nuser_agent = {json.dumps(agent)}\n")
    argv, _ = launch_argv(rig, cli_command, "video", agent=None)
    status, out, err = run(capsys, "--config", str(settings), *argv)
    assert (status, out, err) == (1, "", CONFIGURED)
    assert rig.calls == [] and not (rig.cookies / "sources.json").exists()


@pytest.mark.parametrize("cli_command,command", COMMANDS)
def test_an_explicit_user_agent_overrides_a_multiline_setting(rig, capsys, tmp_path, cli_command, command):
    settings = tmp_path / "config.toml"
    settings.write_text('[downloads]\nuser_agent = "a\\nb"\n')
    before = settings.read_bytes()
    argv, operands = launch_argv(rig, cli_command, "video", "--anonymous", "--user-agent", "Explicit/1.0",
                                 agent=None)
    assert run(capsys, "--config", str(settings), *argv)[0] == 0
    assert rig.program() == expected(command, store.ANONYMOUS, "video", operands, "Explicit/1.0")
    assert settings.read_bytes() == before


@pytest.mark.parametrize("queue", [None, "missing.txt"])
def test_a_bad_queue_refuses_before_migrating(rig, capsys, legacy_jar, queue):
    status, out, err = run(capsys, *rig.sets(), "batch", "video", *([] if queue is None else [queue]))
    path = rig.downloads / "dlmp4_queue" if queue is None else Path.cwd() / queue
    assert (status, out) == (1, "")
    assert err == f"downloadinux: error: no such queue file: {path}\n"
    assert rig.calls == [] and not (rig.cookies / "sources.json").exists()


def test_a_queue_outside_downloads_refuses_before_migrating(rig, capsys, tmp_path, legacy_jar):
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("")
    status, _, err = run(capsys, *rig.sets(), "batch", "video", str(outside))
    assert status == 1 and "is outside paths.downloads" in err
    assert rig.calls == [] and not (rig.cookies / "sources.json").exists()


# --- order: pinning failures launch nothing ---------------------------------

def unregistered(rig):
    rig.register("alpha", "ALPHA")
    return ["--cookie-source", "beta"], f"{rig.cookies / 'sources.json'}: source beta is not registered; choose a listed source"


def deleted_revision(rig):
    alpha = rig.register("alpha", "ALPHA")
    operations.cookie_default("alpha", overrides=rig.overrides)
    path = rig.cookies / "sources" / "alpha" / f"{alpha}.txt"
    path.unlink()
    return [], f"{path}: No such file or directory"


def invalid_stage1_jar(rig):
    rig.cookies.mkdir(mode=0o700)
    (rig.cookies / "cookies.txt").write_bytes(b"not a cookie jar\n")
    return [], str(rig.cookies / "cookies.txt")


def unreadable_registry(rig):
    rig.register("alpha", "ALPHA")
    (rig.cookies / "sources.json").chmod(0)
    return [], f"{rig.cookies / 'sources.json'}: Permission denied"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-0 file")
@pytest.mark.parametrize("damage", [unregistered, deleted_revision, invalid_stage1_jar, unreadable_registry])
@pytest.mark.parametrize("cli_command,command", COMMANDS)
def test_a_pinning_failure_exits_1_with_the_path_bearing_error(rig, capsys, damage, cli_command, command):
    options, message = damage(rig)
    before = {path: path.read_bytes() for path in rig.cookies.rglob("*") if path.is_file()
              and os.access(path, os.R_OK)}
    argv, _ = launch_argv(rig, cli_command, "video", *options)
    status, out, err = run(capsys, *argv)
    assert (status, out) == (1, "")
    assert err.startswith("downloadinux: error: ") and message in err, err
    assert rig.calls == []
    if damage is invalid_stage1_jar:
        assert not (rig.cookies / "sources.json").exists()
    for path, data in before.items():
        assert path.read_bytes() == data


@pytest.mark.parametrize("cli_command,command", COMMANDS)
def test_anonymous_launches_past_a_corrupt_registry(rig, capsys, cli_command, command):
    rig.cookies.mkdir(mode=0o700)
    (rig.cookies / "sources.json").write_bytes(b"{")
    argv, operands = launch_argv(rig, cli_command, "gallery", "--anonymous")
    assert run(capsys, *argv)[0] == 0
    assert rig.program() == expected(command, store.ANONYMOUS, "gallery", operands)
    assert (rig.cookies / "sources.json").read_bytes() == b"{"
    assert sorted(os.listdir(rig.cookies)) == ["sources.json"]


@pytest.mark.parametrize("cli_command,command", COMMANDS)
def test_anonymous_never_migrates_an_invalid_stage1_jar(rig, capsys, cli_command, command):
    rig.cookies.mkdir(mode=0o700)
    (rig.cookies / "cookies.txt").write_bytes(b"not a cookie jar\n")
    argv, operands = launch_argv(rig, cli_command, "video", "--anonymous")
    assert run(capsys, *argv)[0] == 0
    assert "--anonymous" in rig.program()
    assert (rig.cookies / "cookies.txt").read_bytes() == b"not a cookie jar\n"
    assert sorted(os.listdir(rig.cookies)) == ["cookies.txt"]


@pytest.mark.parametrize("cli_command,command", COMMANDS)
def test_a_named_run_migrates_the_stage1_jar_to_legacy(rig, capsys, legacy_jar, cli_command, command):
    argv, operands = launch_argv(rig, cli_command, "video")
    assert run(capsys, *argv)[0] == 0
    snapshot = store.read(rig.cookies)
    assert snapshot.default == "legacy"
    legacy = snapshot.sources["legacy"]
    assert legacy.path == str(legacy_jar)
    assert rig.program() == expected(command, store.Pinned("legacy", legacy.revision), "video", operands)
    assert legacy_jar.read_bytes() == netscape("COOKIE_CANARY")


# --- the user-agent ---------------------------------------------------------

@pytest.mark.parametrize("cli_command,command", COMMANDS)
@pytest.mark.parametrize("spelling", [["--user-agent", "Override/2 (X11)"], ["--user-agent=Override/2 (X11)"]])
def test_the_user_agent_option_overrides_and_writes_no_settings(rig, capsys, tmp_path, cli_command, command, spelling):
    absent = tmp_path / "absent.toml"
    argv, operands = launch_argv(rig, cli_command, "video", "--anonymous", *spelling)
    assert run(capsys, "--config", str(absent), *argv)[0] == 0
    assert rig.program() == expected(command, store.ANONYMOUS, "video", operands, "Override/2 (X11)")
    assert not absent.exists()


@pytest.mark.parametrize("value", ["-x", "-smoke agent", "--anonymous"])
def test_a_dash_led_user_agent_needs_the_equals_spelling(rig, capsys, value):
    status, _, err = run(capsys, *rig.sets(), "dl", f"--user-agent={value}", "--anonymous", "video", URLS[0])
    assert (status, err) == (0, "")
    program = rig.program()
    assert program[program.index("--user-agent") + 1] == value


@pytest.mark.parametrize("cli_command,command", COMMANDS)
def test_an_empty_configured_user_agent_reaches_the_runner(rig, capsys, cli_command, command):
    argv, operands = launch_argv(rig, cli_command, "gallery", "--anonymous", agent="")
    assert run(capsys, *argv)[0] == 0
    assert rig.program() == expected(command, store.ANONYMOUS, "gallery", operands, "")


# --- option placement -------------------------------------------------------

PLACEMENTS = {
    "before": lambda options: ["dl", *options, "video", *URLS],
    "after-preset": lambda options: ["dl", "video", *options, *URLS],
    "after-urls": lambda options: ["dl", "video", *URLS, *options],
}


@pytest.mark.parametrize("where", PLACEMENTS)
def test_options_parse_before_and_after_the_positionals(rig, capsys, where):
    beta = rig.register("beta", "BETA")
    options = ["--cookie-source", "beta", "--user-agent=-dash"]
    assert run(capsys, *rig.sets(), *PLACEMENTS[where](options))[0] == 0
    assert rig.program() == expected("download", store.Pinned("beta", beta), "video", URLS, "-dash")


@pytest.mark.parametrize("where", ["before", "after-preset", "after-queue"])
def test_batch_options_parse_around_the_queue(rig, capsys, where):
    queue = rig.queue("video")
    words = {"before": ["--anonymous", "video", str(rig.downloads / "dlmp4_queue")],
             "after-preset": ["video", "--anonymous", str(rig.downloads / "dlmp4_queue")],
             "after-queue": ["video", str(rig.downloads / "dlmp4_queue"), "--anonymous"]}[where]
    assert run(capsys, *rig.sets(), "batch", *words)[0] == 0
    assert rig.program() == expected("batch", store.ANONYMOUS, "video", [queue])


def test_an_option_between_two_urls_is_a_usage_error(rig, capsys):
    status, out, err = run(capsys, *rig.sets(), "dl", "video", URLS[0], "--anonymous", URLS[1])
    assert (status, out) == (2, "")
    assert "unrecognized arguments" in err
    assert rig.calls == []


# --- statuses ---------------------------------------------------------------

@pytest.mark.parametrize("returned", [0, 3, 130])
def test_dl_returns_the_runners_status(rig, capsys, returned):
    rig.statuses.append(returned)
    status, out, err = run(capsys, *rig.sets(), "dl", "--anonymous", "v", URLS[0])
    assert (status, out, err) == (returned, "", "")


@pytest.mark.parametrize("preset,session", [("video", "batch-video"), ("gallery", "batch-gallery")])
def test_a_failing_batch_launch_keeps_the_tmux_message(rig, capsys, preset, session):
    rig.queue(preset)
    rig.statuses.append(1)
    status, out, err = run(capsys, *rig.sets(), "batch", "--anonymous", preset)
    assert (status, out) == (1, "")
    assert err == (f"downloadinux: error: could not start tmux session {session}; "
                   f"if one is already running, attach with dlx watch {preset}\n")


# --- the recipes, through the real `just` -----------------------------------

ARGV_STUB = """#!/bin/sh
for arg in "$@"; do printf '%s\\0' "$arg"; done >"$ARGV_LOG"
"""


def just(tmp_path, *args):
    executable = shutil.which("just")
    assert executable is not None  # the gate needs `just`: fail, never skip
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "uv"
    stub.write_text(ARGV_STUB)
    stub.chmod(0o755)
    log = tmp_path / "argv"
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "ARGV_LOG": str(log)}
    subprocess.run([executable, "--justfile", str(checkout.asset_path("justfile")), *args],
                   cwd=checkout.checkout_root(), env=env, check=True, timeout=60)
    return [arg.decode() for arg in log.read_bytes().split(b"\0")[:-1]]


WORDS = [
    ["--cookie-source", "alpha", "video", "https://example.com/a b"],
    ["--anonymous", "gallery", "https://example.com/x", "https://example.com/y"],
    ["--user-agent=-x y", "video", "https://example.com/v?a=1&b=2"],
    ["video", "--user-agent", "Mozilla/5.0 (X11) A", "-not-an-option", ""],
]


@pytest.mark.parametrize("words", WORDS)
def test_just_dl_forwards_each_word_unchanged(tmp_path, words):
    assert just(tmp_path, "dl", *words) == ["run", "downloadinux", "dl", *words]


@pytest.mark.parametrize("words", [
    ["video"],
    ["--cookie-source", "alpha", "gallery"],
    ["--anonymous", "video", "/tmp/my queue.txt"],
    ["--user-agent=-x y", "gallery", "~/q.txt"],
    ["video", ""],
])
def test_just_batch_forwards_each_word_unchanged(tmp_path, words):
    assert just(tmp_path, "batch", *words) == ["run", "downloadinux", "batch", *words]
