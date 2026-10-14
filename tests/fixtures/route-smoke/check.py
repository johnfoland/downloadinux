"""Host-side assertions for the gate's host-route smoke test (Named cookie
sources §3, "Host launch (DEC-39)"), over what the stub downloader logged in
the check project's downloads mount. Run by scripts/smoke.sh as
``python3 -I check.py STEP LOG COOKIES ...``. Prints the step name on
success; on failure, a message naming no cookie material.
"""
import hashlib
import json
from pathlib import Path
import re
import sys

JAR = re.compile(r'/state/cookie-invocations/[0-9a-f]{32}/cookies\.txt')


def fail(message):
    print(f'Host-route smoke check failed: {message}', file=sys.stderr)
    sys.exit(1)


def check(condition, message):
    if not condition:
        fail(message)


def calls(log, count):
    records = sorted(log.glob('call-*.json'))
    check(len(records) == count, f'expected {count} downloader calls, found {len(records)}')
    result = []
    for path in records:
        record = json.loads(path.read_text())
        record['argv'] = path.with_suffix('.argv').read_bytes().split(b'\0')[:-1]
        record['argv'] = [arg.decode() for arg in record['argv']]
        result.append(record)
    return result


def adjacent(argv, first, second):
    return any(argv[i:i + 2] == [first, second] for i in range(len(argv) - 1))


def pinned(record, cookies, source, revision):
    check(record['jar'] is not None and JAR.fullmatch(record['jar']), 'cookie argument is not a private jar')
    expected = hashlib.sha256((cookies / 'sources' / source / f'{revision}.txt').read_bytes()).hexdigest()
    check(record['sha256'] == expected, 'private jar differs from the pinned revision')
    check((record['source'], record['revision']) == (source, revision), 'snapshot metadata names another revision')


def named_download(log, cookies, source, revision, agent, *urls):
    records = calls(log, len(urls))
    by_url = {record['argv'][-1]: record for record in records}
    check(set(by_url) == set(urls), 'each job must carry exactly its own URL')
    check(len({record['jar'] for record in records}) == len(urls), 'jobs shared a private jar')
    for url in urls:
        record = by_url[url]
        check(record['program'] == 'yt-dlp', 'video ran another program')
        check(adjacent(record['argv'], '--paths', '/downloads/dlmp4'), 'video output path moved')
        check(adjacent(record['argv'], '--add-headers', f'User-Agent:{agent}'), 'configured user-agent missing')
        pinned(record, cookies, source, revision)


def anonymous_download(log, cookies, agent):
    [record] = calls(log, 1)
    check(record['jar'] is None, 'anonymous run received a cookie argument')
    check('--cookies' not in record['argv'], 'anonymous run received --cookies')
    check(adjacent(record['argv'], '--add-headers', f'User-Agent:{agent}'), 'explicit user-agent missing')


def batch(log, cookies, source, revision, agent, *urls):
    records = calls(log, len(urls))
    check(len({record['jar'] for record in records}) == len(urls), 'lane jobs shared a private jar')
    by_url = {record['argv'][-1]: record for record in records}
    check(set(by_url) == set(urls), 'lane URLs differ')
    for url in urls:
        record = by_url[url]
        check(record['program'] == 'gallery-dl', 'gallery ran another program')
        check('-q' not in record['argv'], 'retired gallery quiet argument')
        check(adjacent(record['argv'], '-o', 'extractor.archive=/downloads/dlgal/gallery-dl-archive.sqlite3'), 'archive path moved')
        check(adjacent(record['argv'], '-o', 'extractor.instagram.base-directory=/downloads/dlgal'), 'gallery output path moved')
        check(record['argv'][-1] == url, 'batch URL out of order')
        check(adjacent(record['argv'], '-o', f'extractor.user-agent={agent}'), 'explicit user-agent missing')
        pinned(record, cookies, source, revision)


STEPS = {'named-download': named_download, 'anonymous-download': anonymous_download, 'batch': batch}

step, log, cookies, *rest = sys.argv[1:]
STEPS[step](Path(log), Path(cookies), *rest)
print(f'Host-route {step} passed.')
