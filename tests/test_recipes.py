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
    return lines[start:end]


def test_doctor_neither_requires_nor_reads_env():
    body = "\n".join(recipe("doctor"))
    assert ".env" not in body
    assert "NORDVPN_TOKEN=" not in body
    assert "config-value.sh token" in body


def test_env_appears_only_in_setup_and_env():
    allowed = recipe("setup") + recipe("env")
    assert [line for line in justfile() if ".env" in line and line not in allowed] == []
