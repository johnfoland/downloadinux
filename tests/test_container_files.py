"""Structural container contracts; the gate also tests the real entrypoint."""

import re
import shlex

from downloadinux import checkout


def entrypoint() -> str:
    return checkout.asset_path("scripts/entrypoint.sh").read_text()


def test_entrypoint_messages_name_the_host_commands():
    # Target design, Container contract, "Pinned for P0001-C04" (issue #18).
    text = entrypoint()
    assert 'warn "NORDVPN_TOKEN not set. Store one on the host with: just token <token>"' in text
    assert 'log "Ready. Shell in with: just shell (or dlx shell)"' in text
    # The old hints: a token on a container command line, and the raw exec.
    assert "vpn login <token>" not in text
    assert "compose -- exec" not in text


def test_no_placeholder_jar_and_no_user_agent_channel():
    # Named cookie sources §5 (DEC-33): the image seeds no shared jar, and
    # the shortcuts' DL_USER_AGENT channel is gone.
    assert "/cookies/cookies.txt" not in entrypoint()
    assert "cookies.txt" not in entrypoint()
    base = checkout.asset_path("compose/base.yaml").read_text()
    assert "DL_USER_AGENT" not in base and "USER_AGENT" not in base


def test_runtime_home_repairs_all_entries_after_identity_without_recursing():
    # Target design, Container contract, Runtime home ownership (DEC-26).
    text = entrypoint()
    repair = re.search(r"for d in ([^;]+); do\n(.*?)\ndone", text[text.index("# Runtime home entries"):], re.S)
    assert repair is not None
    assert shlex.split(repair[1]) == [
        "/home/dl/.local",
        "/home/dl/.local/share",
        "/home/dl/.local/share/downloadinux",
        "/home/dl/.config",
    ]
    commands = [shlex.split(line) for line in repair[2].splitlines() if line.strip()]
    assert commands == [["mkdir", "-p", "$d"], ["chown", "-h", "$uid:$gid", "$d"]]
    assert text.index("# Runtime home entries") > text.index('usermod -u "$uid" dl')
    # There is no identity-change guard around the repair, and no hidden
    # recursive home ownership command outside the loop.
    assert "if " not in text[text.index("# --- Directories"):text.index("# Runtime home entries")]
    for line in text.splitlines():
        if line.strip().startswith("chown "):
            assert shlex.split(line) == ["chown", "-h", "$uid:$gid", "$d"]


def test_history_volume_keeps_its_nested_target():
    text = checkout.asset_path("compose/base.yaml").read_text()
    assert re.search(
        r"- type: volume\n\s+source: zsh-history\n\s+target: /home/dl/\.local/share/downloadinux\n",
        text,
    )


# The worker's packaging (target design, Persistent worker and jobs §1,
# "Packaging" and its pinned paragraph; DEC-41).


def code_lines(text: str) -> list[str]:
    """A wrapper's lines without its docstring."""
    start = text.index('"""')
    end = text.index('"""', start + 3) + 3
    return (text[:start] + text[end:]).splitlines()


def test_worker_wrapper_is_the_runners_with_the_worker_module():
    runner = checkout.asset_path("scripts/downloadinux-runner").read_text()
    worker = checkout.asset_path("scripts/downloadinux-worker").read_text()
    assert code_lines(worker) == [
        line.replace("downloadinux.cookie_runner", "downloadinux.worker") for line in code_lines(runner)
    ]
    assert code_lines(worker) != code_lines(runner)
    assert worker.startswith("#!/usr/bin/python3 -I\n")
    assert "sys.path.insert(0, '/opt/downloadinux')\n" in worker
    assert "from downloadinux.worker import main  # noqa: E402\n" in worker
    assert worker.endswith("sys.exit(main())\n")
    assert checkout.asset_path("scripts/downloadinux-worker").stat().st_mode & 0o777 == 0o755


def test_dockerfile_installs_the_worker_beside_the_runner_and_runs_it():
    text = checkout.asset_path("Dockerfile").read_text()
    block = text[text.index("# --- Cookie runner"):text.index("COPY scripts/entrypoint.sh")]
    assert "COPY scripts/downloadinux-worker /usr/local/bin/downloadinux-worker\n" in block
    assert "chmod 755 /usr/local/bin/downloadinux-runner /usr/local/bin/downloadinux-worker" in block
    assert 'Persistent worker and jobs §1, "Packaging"' in block
    commands = [line for line in text.splitlines() if line.startswith("CMD")]
    assert commands == ['CMD ["downloadinux-worker", "serve", "--cookies", "/cookies", "--state", "/state"]']
    assert text.rstrip().endswith(commands[0])


def test_entrypoint_prepares_the_socket_directory_after_the_home_repair_and_before_the_vpn():
    text = entrypoint()
    line = 'install -d -m 0700 -o "$uid" -g "$gid" /run/downloadinux\n'
    assert text.count("/run/downloadinux") == 1 and text.count(line) == 1
    position = text.index(line)
    repair = text.index("# Runtime home entries")
    assert text.index("done\n", repair) < position < text.index("start_nordvpn() {")
    assert position < text.index("\n  start_nordvpn\n") < text.index("exec gosu dl")


def test_compose_runs_an_init_with_a_30_second_grace_period():
    text = checkout.asset_path("compose/base.yaml").read_text()
    service = text[text.index("  downloadinux:\n"):text.index("\nvolumes:")]
    assert re.search(r"^    init: true$", service, re.M)
    assert re.search(r"^    stop_grace_period: 30s$", service, re.M)
