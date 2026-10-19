"""Human output shared by the CLI and TUI (DEC-58).

These renderers preserve the CLI's texts and escaping; they do not parse
arguments or install signal handlers.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from downloadinux import files, health, host_jobs, operations

PROG = "downloadinux"

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def escape(text: str) -> str:
    r"""Human output's form of an interpolated string: each C0 control
    character, DEL, and C1 control character as `\xNN`; nothing else."""
    return _CONTROL.sub(lambda match: f"\\x{ord(match.group()):02x}", text)


def follow_job(job: dict) -> str:
    """The selected-job notice shared by CLI and TUI (TUI §1 *Package*)."""
    return f"Job {job['id']}: {escape(job['url'])}"


def follow_wait(id: int, wait: dict | None) -> str:
    """A changed-wait notice (TUI §1 *Package*; DEC-57)."""
    reason = ('resumed' if wait is None else 'waiting for VPN'
              if wait['reason'] == 'vpn' else
              f"waiting for throttle until {escape(wait['until'])}")
    return f"job {id} {reason}"


def job_state(job: dict) -> str:
    wait = job["wait"]
    if job["state"] == "queued" and wait is not None:
        return "queued: vpn" if wait["reason"] == "vpn" else f"queued: throttle until {escape(wait['until'])}"
    if job["state"] == "running" and job["cancel_requested"]:
        return "running: cancelling"
    return job["state"]


def job_cells(job: dict) -> tuple[str, ...]:
    """One listed job's `jobs list` cells: ID, STATE, PRESET, QUEUE,
    ATTEMPT, and URL (TUI §1 *Package*)."""
    return (str(job["id"]), job_state(job), job["preset"], "lane" if job["lane"] else "immediate",
            str(job["attempt"]), escape(job["url"]))


def no_jobs(all: bool) -> str:
    """`jobs list`'s text for an empty listing (TUI §1 *Package*)."""
    return "No jobs." if all else "No queued, running, failed, or interrupted jobs."


def render_jobs(jobs: list[dict]) -> str:
    """`jobs list`'s table: columns two spaces apart, each but the last
    padded to its widest cell, header included (§6 *Output*)."""
    rows = [("ID", "STATE", "PRESET", "QUEUE", "ATTEMPT", "URL")]
    rows += [job_cells(job) for job in jobs]
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]) - 1)]
    return "\n".join("  ".join([*(cell.ljust(width) for cell, width in zip(row, widths)), row[-1]])
                     for row in rows)


def _or_dash(value: object) -> str:
    return "-" if value is None else escape(str(value))


def render_job(view: host_jobs.JobView) -> str:
    """`jobs show`'s block: each value from the 21st character (§6 *Output*)."""
    job = view.job
    wait = job["wait"]
    fields = [
        ("URL", escape(job["url"])),
        ("Preset", job["preset"]),
        ("Queue", f"{job['preset']} lane" if job["lane"] else "immediate"),
        ("Submission", str(job["submission"])),
        ("Cookies", "anonymous" if job["source"] is None
         else f"{escape(job['source'])} at revision {escape(job['revision'])}"),
        ("User-agent", escape(job["user_agent"]) or "(empty)"),
        ("Attempt", str(job["attempt"])),
        ("Wait", "-" if wait is None else "vpn" if wait["reason"] == "vpn"
         else f"throttle until {escape(wait['until'])}"),
        ("Cancel requested", "yes" if job["cancel_requested"] else "no"),
        ("Created", escape(job["created_at"])),
        ("Queued", escape(job["queued_at"])),
        ("Started", _or_dash(job["started_at"])),
        ("Finished", _or_dash(job["finished_at"])),
        ("Exit status", _or_dash(job["exit_status"])),
        ("Error", _or_dash(job["error"])),
        *((f"Log, attempt {attempt}", escape(str(path))) for attempt, path in view.logs),
    ]
    lines = [f"Job {job['id']}: {job['state']}"]
    lines += [f"  {label}:".ljust(19) + " " + value for label, value in fields]
    return "\n".join(lines)


def retry_line(line: host_jobs.RetryLine) -> str:
    outcome = line.outcome
    if outcome is None:
        if line.record is None:
            return f"unknown job ID {line.id}"
        state = line.record["state"]
    elif outcome["outcome"] == "requeued":
        return f"job {line.id} queued again as attempt {line.record['attempt'] + 1}"
    elif outcome["outcome"] == "shadowed":
        return f"job {line.id} not retried: job {outcome['holder']} already holds its preset and URL"
    elif outcome["outcome"] == "unknown":
        return f"unknown job ID {line.id}"
    else:
        state = outcome["state"]
    return f"job {line.id} is {state}; only failed, cancelled, or interrupted jobs can be retried"


def render_cookie_source(source: dict) -> str:
    """Render the same metadata fields as JSON, without individual cookies."""
    origin = {key: source[key] for key in ('path', 'browser', 'profile', 'keyring', 'container') if key in source}
    return (f"{source['name']}: {source['kind']}; default: {'yes' if source['is_default'] else 'no'}\n"
            f"  origin: {json.dumps(origin, ensure_ascii=False)}\n"
            f"  revision: {source['revision']}; refreshed_at: {source['refreshed_at']}")


def render_cookie_counts(summary: dict) -> str:
    return '; '.join(f'{key}: {summary[key]}' for key in ('total', 'session', 'expired', 'unexpired'))


def render_cookie_inspection(report: dict) -> str:
    lines = [render_cookie_source(report['source']), render_cookie_counts(report)]
    for domain in report['domains']:
        presence = 'present' if domain['session_cookie_present'] else 'absent'
        lines.append(f"  {domain['domain']}: {render_cookie_counts(domain)}; recognized session cookie {presence}")
    return '\n'.join(lines)


def toml_text(value: object) -> str:
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _missing(exists: bool) -> str:
    return "" if exists else "  (missing)"


def render_config(report: dict) -> str:
    """`config show`'s text form: one `key = value  (source)` line per
    setting, then the files, the token's state, and the mounts."""
    lines = [
        f"{s['key']} = {toml_text(s['value'])}  ({s['source']})"
        for s in report["settings"]
    ]
    config_file, creds_file = report["config_file"], report["credentials_file"]
    lines += [
        "",
        f"settings file:    {config_file['path']}{_missing(config_file['exists'])}",
        f"credentials file: {creds_file['path']}{_missing(creds_file['exists'])}",
        f"token:            {report['token']}",
        "",
        "mounts:",
    ]
    lines += [
        f"  {m['container']:<10} <- {m['host']}  ({m['access']})"
        f"{_missing(m['exists'])}"
        for m in report["mounts"]
    ]
    return "\n".join(lines)


_HEALTH_MARKS = {
    health.Status.OK: ("✓", "32"),
    health.Status.WARN: ("!", "33"),
    health.Status.FAIL: ("✗", "31"),
    health.Status.SKIP: ("-", "2"),
}


def render_health(result: health.Result) -> str:
    """DEC-22's human view; only marks receive terminal colour."""
    colour = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    lines = [PROG]
    for check in result.checks:
        mark, code = _HEALTH_MARKS[check.status]
        if colour:
            mark = f"\033[{code}m{mark}\033[0m"
        lines.append(f"  {mark} {check.summary}")
        if check.fix is not None:
            lines.append(f"      fix: {check.fix}")
    return "\n".join(lines)


def cancel_line(outcome: dict) -> str:
    job_id = outcome["id"]
    return {
        "cancelled": f"job {job_id} cancelled",
        "requested": f"job {job_id}: cancel requested; it ends when its downloader exits",
        "refused": f"job {job_id} is {outcome['state']}; only queued or running jobs can be cancelled",
        "unknown": f"unknown job ID {job_id}",
    }[outcome["outcome"]]


def token_stored(path: Path) -> str:
    return f"Stored the NordVPN token in {path}"


def login_messages(result: operations.LoginResult) -> tuple[list[str], str | None]:
    lines = []
    warning = None
    if result.login is operations.Login.VPN_OFF:
        lines.append("vpn.enabled is false: the token is kept for when the VPN is enabled.")
    elif result.login is operations.Login.NOT_RUNNING:
        lines.append("The container is not running; it logs in with the token at the next dlx up.")
    else:
        lines.append("Logged in.")
        if result.connect is not None:
            target = " ".join(result.connect.server) or "the fastest server"
            if result.connect.connected:
                lines.append(f"Connected to {target}.")
            else:
                warning = f"could not connect to {target}; try dlx vpn on"
    return lines, warning


def setting_write(write: operations.SettingWrite) -> str:
    """Configuration and paths, Settings writes: DEC-61's human report."""
    path = escape(str(write.settings_file))
    if write.value is None:
        if write.written:
            return f"Unset {write.key} in {path}; it now uses its default."
        return f"{write.key} is not set in {path}; nothing written."
    value = escape(files._toml_value(write.value))
    if write.written:
        return f"Set {write.key} = {value} in {path}."
    return f"{write.key} is already {value} in {path}; nothing written."
