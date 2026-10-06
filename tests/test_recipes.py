"""The justfile's recipes, read as text: what each one runs and reads."""

from downloadinux import checkout


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
    assert "config-value.sh token" in body


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
    # Target design, Migration, "Pinned for P0001-C04" (DEC-10).
    body = recipe("useragent")
    assert body[-1].strip() == "@uv run downloadinux config user-agent"
    assert "python3" not in "\n".join(body)
    assert recipe("cookies")[-1].strip() == "just useragent"


def test_doctor_reads_the_user_agent_setting_and_no_config_file():
    body = "\n".join(recipe("doctor"))
    assert "config-value.sh downloads.user_agent" in body
    assert "config/" not in body
    assert "disagree" not in body


def test_dl_wraps_the_cli():
    # Target design, Migration, "Pinned for P0001-C04".
    assert recipe("dl")[-1].strip() == '@uv run downloadinux dl "$@"'


def test_doctor_tests_the_downloads_mount():
    body = "\n".join(recipe("doctor"))
    assert "{{dex}} test -d /downloads" in body
    assert "{{chome}}" not in body
