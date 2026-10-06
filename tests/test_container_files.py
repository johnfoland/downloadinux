"""The container's own files, read as text: what the entrypoint says."""

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
