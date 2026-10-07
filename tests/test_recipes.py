"""The justfile's recipes, read as text: what each one runs and reads."""

import ast

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
    # Target design, Migration, "Pinned for P0001-C04" (DEC-10).
    body = recipe("useragent")
    assert body[-1].strip() == "@uv run downloadinux config user-agent"
    assert "python3" not in "\n".join(body)
    assert recipe("cookies")[-1].strip() == "just useragent"


def test_doctor_is_only_the_cli_wrapper():
    body = "\n".join(recipe("doctor"))
    assert "config-value.sh" not in body
    assert "defaults" not in body
    assert "grep" not in body


def test_dl_wraps_the_cli():
    # Target design, Migration, "Pinned for P0001-C04".
    assert recipe("dl")[-1].strip() == '@uv run downloadinux dl "$@"'


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


def test_batch_and_watch_wrap_the_cli():
    # Target design, Migration, "Pinned for P0001-C04": the queue only when given.
    assert recipe("batch")[-1].strip() == '@uv run downloadinux batch "$1" ${2:+"$2"}'
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
