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
