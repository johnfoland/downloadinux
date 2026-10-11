"""Doctor's Stage 2 cookie and user-agent rows (Named cookie sources §5;
DEC-39), through `health.source_checks` over real stores under `tmp_path`.

Stores are built with the source writer, and every failure is a real damaged
store, not a patched reader. Nothing here is wired into `doctor` yet: the
Stage 1 rows stay in `health.inspect` until the cutover.
"""

from datetime import datetime, timezone
import os

import pytest

from downloadinux import cookie_store as store, cookie_writer as writer, cookiejar, health, useragent
from test_doctor import by_id, docker, doctor_paths, run_json  # noqa: F401 (fixtures)

NOW = 1_800_000_000
LATER, EARLIER = NOW + 3600, NOW - 3600
AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
STORE_FIX = "repair or restore the file it names"
YOUTUBE_ASIDE = " (fine unless you need age-gated or members-only video)"
IDS = ["cookies.jar", "cookies.instagram", "cookies.youtube", "downloads.user_agent"]


def line(domain, name, expiry=LATER, value="COOKIE_CANARY"):
    flag = "TRUE" if domain.startswith(".") else "FALSE"
    return f"{domain}\t{flag}\t/\tTRUE\t{expiry}\t{name}\t{value}\n"


def jar(*lines):
    return (cookiejar.HEADER + "".join(lines)).encode()


def register(root, name, data, *, revision="a" * 32):
    """A file source published and selected by the real writer."""
    material = cookiejar.parse(data, root / "import.txt")
    return writer.publish(
        root, name, material, dict(kind="file", path=str(root / "import.txt")),
        expected=None, select=True, revision_factory=lambda: revision,
        clock=lambda: datetime(2026, 10, 11, tzinfo=timezone.utc),
    )


def checks(root, user_agent=AGENT):
    result = health.source_checks(root, user_agent, now=NOW)
    assert [check.id for check in result] == IDS
    return result


def skipped(check, summary):
    assert (check.status, check.summary, check.fix) == (health.Status.SKIP, summary, None)


# --- anonymous ---------------------------------------------------------------

@pytest.mark.parametrize("state", ["initialized", "absent", "empty directory", "header-only legacy"])
def test_an_anonymous_default_skips_every_cookie_check(tmp_path, state):
    root = tmp_path / "cookies"
    if state == "initialized":
        writer.initialize(root)
    elif state == "empty directory":
        root.mkdir()
    elif state == "header-only legacy":
        root.mkdir()
        (root / "cookies.txt").write_bytes(jar())
    jar_check, instagram, youtube, agent = checks(root)
    for check in (jar_check, instagram, youtube):
        skipped(check, "anonymous cookie default")
    assert agent.status is health.Status.OK
    # Doctor migrates like any source operation (§5).
    assert store.read(root).record() == dict(version=1, default=None, sources={})


# --- named defaults ----------------------------------------------------------

def test_a_migrated_legacy_default_reports_true_counts(tmp_path):
    (tmp_path / "cookies.txt").write_bytes(jar(
        line(".instagram.com", "sessionid", ""),
        line("youtube.com", "SAPISID", EARLIER),
        line("example.com", "a", NOW),
        line("example.com", "b", LATER),
    ))
    jar_check, instagram, youtube, _ = checks(tmp_path)
    assert (jar_check.status, jar_check.summary, jar_check.fix) == (
        health.Status.OK, "source legacy: 4 cookies (2 expired)", None
    )
    assert store.read(tmp_path).default == "legacy"
    assert instagram.status is health.Status.OK
    assert (youtube.status, youtube.summary) == (health.Status.WARN, "youtube: session cookie expired" + YOUTUBE_ASIDE)


def test_a_registered_default_reports_true_counts(tmp_path):
    register(tmp_path, "work", jar(line("example.com", "a"), line("example.com", "b", "")))
    jar_check, *_ = checks(tmp_path)
    assert (jar_check.status, jar_check.summary) == (health.Status.OK, "source work: 2 cookies (0 expired)")


def test_an_empty_named_revision_warns_and_skips_the_sites(tmp_path):
    register(tmp_path, "work", jar())
    jar_check, instagram, youtube, _ = checks(tmp_path)
    assert (jar_check.status, jar_check.summary, jar_check.fix) == (
        health.Status.WARN, "source work has no cookies; public downloads still work",
        "dlx cookies refresh work",
    )
    skipped(instagram, "no usable cookie source")
    skipped(youtube, "no usable cookie source")


# --- store failures ----------------------------------------------------------

REVISION = "b" * 32


def invalid_legacy(root):
    (root / "cookies.txt").write_bytes(b"COOKIE_CANARY\n")
    legacy = root / "cookies.txt"
    return (f"{legacy}: line 1: invalid header; supply a valid Netscape cookie file; "
            f"repair or move aside {legacy} before importing valid cookie data")


def invalid_json(root):
    writer.initialize(root)
    (root / "sources.json").write_text("{COOKIE_CANARY")
    return f"{root / 'sources.json'}: invalid registry JSON; repair the source store or retry the operation"


def deleted_revision(root):
    register(root, "work", jar(line("example.com", "a")), revision=REVISION)
    path = store.revision_path(root, "work", REVISION)
    path.unlink()
    return f"{path}: No such file or directory"


def invalid_revision_line(root):
    register(root, "work", jar(line("example.com", "a")), revision=REVISION)
    path = store.revision_path(root, "work", REVISION)
    path.write_bytes(jar("example.com\tFALSE\t/\tCOOKIE_CANARY\n"))
    return f"{path}: line 2: invalid field count; supply a valid Netscape cookie file"


def unreadable_registry(root):
    writer.initialize(root)
    (root / "sources.json").chmod(0o000)
    return f"{root / 'sources.json'}: Permission denied"


def root_is_a_file(root):
    root.rmdir()
    root.write_bytes(b"COOKIE_CANARY")
    return f"{root / 'sources.json'}: Not a directory"


@pytest.mark.parametrize("damage", [
    invalid_legacy, invalid_json, deleted_revision, invalid_revision_line,
    pytest.param(unreadable_registry, marks=pytest.mark.skipif(os.getuid() == 0, reason="root reads mode 000")),
    root_is_a_file,
])
def test_a_store_failure_is_a_failed_check_and_the_report_continues(tmp_path, damage):
    root = tmp_path / "cookies"
    root.mkdir()
    expected = damage(root)
    try:
        jar_check, instagram, youtube, agent = checks(root)
    finally:
        if (root / "sources.json").exists() and root.is_dir():
            (root / "sources.json").chmod(0o600)
    assert (jar_check.status, jar_check.summary, jar_check.fix) == (health.Status.FAIL, expected, STORE_FIX)
    assert "COOKIE_CANARY" not in jar_check.summary
    skipped(instagram, "no usable cookie source")
    skipped(youtube, "no usable cookie source")
    assert (agent.status, agent.summary) == (health.Status.OK, "explicit user-agent set")


@pytest.mark.parametrize("filename,expected", [("/x/sources.json", "/x/sources.json: Busy"), (None, "{root}: Busy")])
def test_a_raw_os_error_is_a_failed_check_too(tmp_path, monkeypatch, filename, expected):
    # The store wraps the operating-system failures it knows of; one it does
    # not (no real store produces it today) still becomes the check.
    def ensure(root):
        raise OSError(16, "Busy", filename)

    monkeypatch.setattr(writer, "ensure_store", ensure)
    jar_check, instagram, youtube, _ = checks(tmp_path)
    assert (jar_check.status, jar_check.summary, jar_check.fix) == (
        health.Status.FAIL, expected.format(root=tmp_path), STORE_FIX
    )
    skipped(instagram, "no usable cookie source")
    skipped(youtube, "no usable cookie source")


# --- site checks -------------------------------------------------------------

INSTAGRAM_FIX = "log in to Instagram, then: dlx cookies refresh work"
SITE_CASES = [
    # (site, records, status, summary)
    ("instagram", [line("www.instagram.com", "sessionid")], "ok", "instagram: session cookie present"),
    ("instagram", [line(".instagram.com", "sessionid", "")], "ok", "instagram: session cookie present"),
    ("instagram", [line("instagram.com", "sessionid", EARLIER), line("www.instagram.com", "sessionid")],
     "ok", "instagram: session cookie present"),
    ("instagram", [line("www.instagram.com", "sessionid", NOW)], "warn", "instagram: session cookie expired"),
    ("instagram", [line("www.instagram.com", "csrftoken")], "warn", "instagram: no session cookie"),
    ("instagram", [line("notinstagram.com", "sessionid")], "warn", "instagram: no session cookie"),
]
for name in ("__Secure-3PSID", "SAPISID"):
    SITE_CASES += [
        ("youtube", [line(".youtube.com", name)], "ok", "youtube: session cookie present"),
        ("youtube", [line("www.youtube.com", name, "")], "ok", "youtube: session cookie present"),
        ("youtube", [line(".youtube.com", name, EARLIER)], "warn", "youtube: session cookie expired" + YOUTUBE_ASIDE),
        ("youtube", [line("notyoutube.com", name)], "warn", "youtube: no session cookie" + YOUTUBE_ASIDE),
    ]
SITE_CASES.append(("youtube", [line(".youtube.com", "PREF")], "warn", "youtube: no session cookie" + YOUTUBE_ASIDE))


@pytest.mark.parametrize("site,records,status,summary", SITE_CASES)
def test_site_checks(tmp_path, site, records, status, summary):
    register(tmp_path, "work", jar(*records))
    _, instagram, youtube, _ = checks(tmp_path)
    check = instagram if site == "instagram" else youtube
    fix = None if status == "ok" or site == "youtube" else INSTAGRAM_FIX
    assert (check.status.value, check.summary, check.fix) == (status, summary, fix)


AGREEMENT = [
    [line(".instagram.com", "sessionid"), line("www.youtube.com", "SAPISID", EARLIER)],
    [line("instagram.com", "sessionid", EARLIER), line("youtube.com", "__Secure-3PSID", "")],
    [line("notinstagram.com", "sessionid"), line("youtube.com", "PREF")],
    [line("www.instagram.com", "csrftoken"), line("notyoutube.com", "SAPISID")],
    [line("a.instagram.com", "sessionid", NOW), line("example.com", "sessionid"), line("youtube.com", "SID")],
]


@pytest.mark.parametrize("records", AGREEMENT)
def test_site_checks_agree_with_the_inspection_summary(tmp_path, records):
    register(tmp_path, "work", jar(*records))
    _, instagram, youtube, _ = checks(tmp_path)
    summary = cookiejar.summary(store.load_revision(tmp_path, "work", "a" * 32), now=NOW)
    for check, site in ((instagram, "instagram.com"), (youtube, "youtube.com")):
        present = check.status is health.Status.OK or "expired" in check.summary
        domains = [d for d in summary["domains"]
                   if d["domain"].lstrip(".") == site or d["domain"].endswith("." + site)]
        assert present == any(d["session_cookie_present"] for d in domains)


# --- user-agent --------------------------------------------------------------

@pytest.fixture
def no_chrome_lookup(monkeypatch):
    def refuse(*args):
        raise AssertionError("the Stage 2 row reads no Chrome version")

    monkeypatch.setattr(useragent, "chrome_version", refuse)


@pytest.mark.parametrize("value,status,summary,fix", [
    (AGENT, "ok", "explicit user-agent set", None),
    ("curl/8.0", "ok", "explicit user-agent set", None),
    ("", "warn", "user-agent is empty", "dlx config user-agent VALUE"),
    ("a\rb", "warn", "user-agent contains CR or LF; downloads refuse it", "dlx config user-agent VALUE"),
    ("a\nb", "warn", "user-agent contains CR or LF; downloads refuse it", "dlx config user-agent VALUE"),
])
def test_user_agent_row(tmp_path, no_chrome_lookup, value, status, summary, fix):
    *_, agent = checks(tmp_path, value)
    assert (agent.status.value, agent.summary, agent.fix) == (status, summary, fix)


# --- doctor is unchanged -----------------------------------------------------

def test_doctor_still_reads_the_stage_1_jar_and_never_migrates(docker, doctor_paths, capsys):
    status, report, _ = run_json(capsys, doctor_paths)
    found = by_id(report)
    assert found["cookies.jar"]["summary"] == f"{doctor_paths['jar']}: 2 cookies"
    assert found["cookies.instagram"]["summary"] == "instagram: session cookie present"
    assert not (doctor_paths["cookies"] / "sources.json").exists()
