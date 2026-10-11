"""The justfile's recipes, read as text: what each one runs and reads."""

import ast
import os
import shutil
import subprocess

import pytest

from downloadinux import checkout
from test_doctor import CONTAINER_IDS


def justfile() -> list[str]:
    return checkout.asset_path("justfile").read_text().splitlines()


def recipe(name: str) -> list[str]:
    """A recipe's comment (its menu text), header, and body."""
    lines = justfile()
    header = next(
        i for i, line in enumerate(lines)
        if line and not line[0].isspace() and not line.startswith("#")
        and ":=" not in line and line.split(":", 1)[0].split()[0] == name
    )
    start = header
    while start > 0 and lines[start - 1].startswith("# "):
        start -= 1
    end = next(
        (i for i in range(header + 1, len(lines)) if lines[i] and not lines[i][0].isspace()),
        len(lines),
    )
    while not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def test_doctor_neither_requires_nor_reads_env():
    body = "\n".join(recipe("doctor"))
    assert ".env" not in body
    assert "NORDVPN_TOKEN=" not in body
    assert recipe("doctor")[-1].strip() == '@uv run downloadinux doctor "$@"'


def test_env_appears_only_in_setup_and_env():
    allowed = recipe("setup") + recipe("env")
    assert [line for line in justfile() if ".env" in line and line not in allowed] == []


def test_container_recipes_wrap_the_cli():
    # Target design, Migration, "Pinned for P0001-C03".
    assert recipe("shell")[-1].strip() == "@uv run downloadinux shell"
    assert recipe("vpn")[-1].strip() == '@uv run downloadinux vpn -- "$@"'
    assert [line.strip() for line in recipe("status")[-2:]] == [
        "@{{compose}} ps",
        "@uv run downloadinux vpn status || true",
    ]
    body = [line.strip() for line in recipe("servers")]
    assert 'uv run downloadinux servers "$1" "$2"' in body
    # The header and the hint stay in the recipe: the script never printed them.
    assert body[-1].startswith('echo "  Use one with:')
    assert any(line.startswith('echo "  Least-loaded servers in') for line in body)


def test_no_recipe_runs_nordvpn_or_curl_directly_for_the_converted_commands():
    for name in ("shell", "vpn", "servers", "status"):
        body = "\n".join(recipe(name))
        assert "{{dex}}" not in body and "python3" not in body, name


def test_gate_has_seven_steps_and_no_servers_script():
    body = "\n".join(recipe("check"))
    steps = [line.split('"')[1].split()[0] for line in recipe("check") if line.strip().startswith('step "')]
    assert steps == [f"{n}/7" for n in range(1, 8)]
    assert "all 7 steps passed" in body
    assert "servers.py" not in body
    assert not (checkout.checkout_root() / "scripts" / "servers.py").exists()


def test_useragent_wraps_the_cli():
    # Target design, Migration, "Pinned for P0001-C04" (DEC-10); Named
    # cookie sources §4 forwards optional arguments.
    body = recipe("useragent")
    assert body[-1].strip() == '@uv run downloadinux config user-agent "$@"'
    assert "python3" not in "\n".join(body)
    assert recipe("cookies")[-1].strip() == "just useragent"
    assert "just useragent" in [line.strip() for line in recipe("setup")]


ARGV_STUB = """#!/bin/sh
for arg in "$@"; do printf '%s\\0' "$arg"; done >"$ARGV_LOG"
"""


@pytest.mark.parametrize("args", [
    [],
    ["Mozilla/5.0 (X11; Linux x86_64) A B"],
])
def test_useragent_forwards_each_word_unchanged(tmp_path, args):
    # The real `just`, with a stub `uv` first on PATH; the gate needs `just`,
    # so its absence fails rather than skips.
    just = shutil.which("just")
    assert just is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "uv"
    stub.write_text(ARGV_STUB)
    stub.chmod(0o755)
    log = tmp_path / "argv"
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
           "ARGV_LOG": str(log)}
    subprocess.run(
        [just, "--justfile", str(checkout.asset_path("justfile")), "useragent", *args],
        cwd=checkout.checkout_root(), env=env, check=True, timeout=60,
    )
    argv = log.read_bytes().split(b"\0")[:-1]
    assert [arg.decode() for arg in argv] == [
        "run", "downloadinux", "config", "user-agent", *args,
    ]


def test_doctor_is_only_the_cli_wrapper():
    body = "\n".join(recipe("doctor"))
    assert "config-value.sh" not in body
    assert "defaults" not in body
    assert "grep" not in body


def test_dl_and_batch_forward_every_word():
    # Named cookie sources §5, "Cutover recipes (DEC-40)": the CLI validates.
    assert recipe("dl") == [
        "# Download with a preset: just dl [--cookie-source NAME | --anonymous] "
        "[--user-agent=UA] video|gallery <url>...",
        "dl +args:",
        '    @uv run downloadinux dl "$@"',
    ]
    assert recipe("batch") == [
        "# Queue in detached tmux: just batch [dl's options] video|gallery [file]",
        "batch +args:",
        '    @uv run downloadinux batch "$@"',
    ]


def smoke() -> str:
    return checkout.asset_path("scripts/smoke.sh").read_text()


def test_smoke_runs_and_asserts_structured_doctor_json():
    body = smoke()
    assert '"${cli[@]}" doctor --json' in body
    assert '"vpn.login", "vpn.connected", "vpn.killswitch", "vpn.exit_ip"' in body


def test_smoke_requires_exactly_the_ten_container_ids_each_ok():
    # P0001-C05 review, finding 4: a missing or renamed ID must fail the gate.
    body = smoke()
    start = body.index("assert container_ids == [") + len("assert container_ids == ")
    listed = ast.literal_eval(body[start:body.index("]", start) + 1])
    assert listed == CONTAINER_IDS
    assert 'for name in container_ids:\n    assert checks[name]["status"] == "ok"' in body


def test_smoke_runs_doctor_on_a_fresh_init_config():
    # Target design, Migration, "Pinned for P0001-C06", last bullet.
    body = smoke()
    lines = [line.strip() for line in body.splitlines()]
    assert 'cli=(uv run --locked downloadinux --config "$smoke_dir/absent.toml" "${sets[@]}")' in lines
    assert 'fresh=(uv run --locked downloadinux --config "$smoke_dir/config.toml")' in lines
    init = lines.index('"${fresh[@]}" "${sets[@]}" init >/dev/null')
    doctor = lines.index('if ! "${fresh[@]}" doctor --json >"$smoke_dir/doctor-init.json"; then')
    assert lines.index('"${cli[@]}" doctor --json | python3 -c \'') < init < doctor
    assert 'assert report["ok"] is True, report' in lines
    assert 'assert checks["config.file"]["status"] == "ok", checks["config.file"]' in lines


def test_watch_wraps_the_cli():
    # Target design, Migration, "Pinned for P0001-C04".
    assert recipe("watch")[-1].strip() == '@uv run downloadinux watch "$1"'


def test_no_recipe_names_the_old_home():
    # Its variable went with the last recipe that used it.
    lines = justfile()
    variables = [
        line.split(":=")[0].strip() for line in lines
        if ":=" in line and not line[0].isspace() and not line.startswith("set ")
    ]
    assert variables == ["compose", "dex", "probe"]
    assert not any("/home/dl" in line for line in lines)


def test_the_gate_syntax_checks_the_batch_loop():
    assert any("zsh/batch.zsh" in line for line in recipe("check"))


def test_setup_runs_its_settings_and_token_steps_and_never_writes_env():
    # Target design, Migration, "Pinned for P0001-C06" (DEC-23).
    body = [line.strip() for line in recipe("setup")]
    script = checkout.asset_path("scripts/setup.sh").read_text()
    assert "just env" not in "\n".join(body) + script
    assert body.index("bash scripts/setup.sh") == 4  # after the shebang and set
    headings = [
        line.split("==> ")[1].split("\\033")[0]
        for line in body + script.splitlines() if "==> " in line
    ]
    assert sorted(headings) == [
        "1/5  Settings",
        "2/5  NordVPN token",
        "3/5  Exporting browser cookies",
        "4/5  Building the image (first run takes a few minutes)",
        "5/5  Starting up",
    ]
    assert body[-3:] == [
        'echo ""',
        "just doctor",
        "printf '\\n\\033[1;32mReady.\\033[0m  Get in with:  \\033[1;36mjust shell\\033[0m\\n'",
    ]


def test_setup_cookie_branches_each_own_one_user_agent_refresh():
    # Target design, Migration, "Cookie and user-agent follow-ups" (DEC-25).
    body = [line.strip() for line in recipe("setup")]
    refresh = body.index("just useragent")
    alternate = body.index("else", refresh)
    export = body.index("just cookies || echo \"    Skipped — run 'just cookies' later.\"")
    assert body.count("just useragent") == 1
    assert refresh < alternate < export
    assert body[refresh - 1] == (
        'echo "    Already have an Instagram session, leaving cookies alone."'
    )


def test_env_menu_text_names_the_import():
    menu = recipe("env")[0]
    assert menu.startswith("# Write a legacy .env")
    assert "config import-env" in menu
