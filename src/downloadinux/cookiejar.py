"""Strict, reusable Netscape material and safe summaries (DEC-30).

No settings, browser, or host operations are imported here. Cookie records are
private credential material; only ``summary`` belongs in public output.
"""
from dataclasses import dataclass, field
from http.cookiejar import MozillaCookieJar, HTTPONLY_ATTR, HTTPONLY_PREFIX
from io import StringIO
from pathlib import Path
import re
import time
import warnings

from .errors import DownloadinuxError

HEADER = '# Netscape HTTP Cookie File\n'
MAGIC = re.compile(r'^#(?: Netscape)? HTTP Cookie File')


class CookieFormatError(DownloadinuxError):
    """Invalid credential material, with no input contents in the message."""


def invalid(path: Path, line: int, field: str) -> CookieFormatError:
    return CookieFormatError(f'{path}: line {line}: invalid {field}; supply a valid Netscape cookie file')


@dataclass(repr=False)
class Material:
    jar: MozillaCookieJar = field(repr=False)


def parse(data: bytes, path: Path) -> Material:
    """Validate before MozillaCookieJar can discard or overwrite a row (§2)."""
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError as error:
        line = data[:error.start].count(b'\n') + 1
        raise invalid(path, line, 'UTF-8') from None
    lines = text.split('\n')
    if not MAGIC.match(lines[0].removesuffix('\r')):
        raise invalid(path, 1, 'header')
    jar = MozillaCookieJar()
    keys = set()
    for number, raw in enumerate(lines[1:], 2):
        raw = raw.removesuffix('\r')
        httponly = raw.startswith(HTTPONLY_PREFIX)
        row = raw[len(HTTPONLY_PREFIX):] if httponly else raw
        if not httponly and (not row.strip() or row.lstrip().startswith('#')):
            continue
        fields = row.split('\t')
        if len(fields) != 7:
            raise invalid(path, number, 'field count')
        domain, subdomains, path_value, secure, expiry, name, value = fields
        if not domain or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in domain) or domain.startswith(('#', '$')):
            raise invalid(path, number, 'domain')
        if subdomains not in ('TRUE', 'FALSE'):
            raise invalid(path, number, 'subdomain flag')
        if (subdomains == 'TRUE') != domain.startswith('.'):
            raise invalid(path, number, 'domain/subdomain flag')
        if not path_value.startswith('/') or '\r' in path_value or '\x00' in path_value:
            raise invalid(path, number, 'path')
        if secure not in ('TRUE', 'FALSE'):
            raise invalid(path, number, 'secure flag')
        if expiry and not re.fullmatch(r'[0-9]+', expiry, flags=re.ASCII):
            raise invalid(path, number, 'expiry')
        for label, text_value in [('name', name), ('value', value)]:
            if '\r' in text_value or '\x00' in text_value:
                raise invalid(path, number, label)
        key = (domain, path_value, name)
        if key in keys:
            raise invalid(path, number, 'duplicate domain/path/name')
        keys.add(key)
        # Zero is a session, unlike the library's default interpretation.
        try:
            fields[4] = '' if not expiry or int(expiry) == 0 else expiry
            normalized = (HTTPONLY_PREFIX if httponly else '') + '\t'.join(fields)
            single = MozillaCookieJar()
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                single._really_load(StringIO(HEADER + normalized + '\n'), str(path), True, True)
            cookie, = single
            # The library represents an empty Netscape name as value=None.
            # Restore the actual key before combining rows, avoiding collisions
            # with a named cookie whose name equals that empty-name value.
            if name == '':
                cookie.name, cookie.value = '', value
            jar.set_cookie(cookie)
        except Exception:
            raise invalid(path, number, 'cookie record') from None
    return Material(jar)


def read(path: Path) -> Material:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise DownloadinuxError(f'{path}: {error.strerror or "cannot read cookie file"}') from None
    return parse(data, path)


def serialize(material: Material, path: Path) -> str:
    """Netscape serialization matching MozillaCookieJar.save, without I/O."""
    rows = [HEADER.rstrip('\n')]
    for cookie in material.jar:
        name, value = (cookie.name, cookie.value) if cookie.value is not None else ('', cookie.name)
        domain = (HTTPONLY_PREFIX if cookie.has_nonstandard_attr(HTTPONLY_ATTR) else '') + cookie.domain
        rows.append('\t'.join((domain, 'TRUE' if cookie.domain.startswith('.') else 'FALSE', cookie.path,
                               'TRUE' if cookie.secure else 'FALSE', '' if cookie.expires is None else str(cookie.expires), name, value)))
    result = '\n'.join(rows) + '\n'
    # Callers may supply a modified jar: never publish material we cannot read.
    parse(result.encode('utf-8'), path)
    return result


def summary(material: Material, *, now: float | None = None) -> dict:
    """Counts partition total; presence is local evidence, never login validity."""
    now = time.time() if now is None else now
    counts = dict(total=0, session=0, expired=0, unexpired=0)
    domains = {}
    for cookie in material.jar:
        entry = domains.setdefault(cookie.domain, dict(domain=cookie.domain, **dict.fromkeys(counts, 0), session_cookie_present=False))
        category = 'session' if cookie.expires is None else 'expired' if cookie.expires <= now else 'unexpired'
        for target in (counts, entry):
            target['total'] += 1
            target[category] += 1
        domain = cookie.domain.lstrip('.').lower()
        instagram = domain == 'instagram.com' or domain.endswith('.instagram.com')
        youtube = domain == 'youtube.com' or domain.endswith('.youtube.com')
        entry['session_cookie_present'] |= (instagram and cookie.name == 'sessionid') or (youtube and cookie.name in ('__Secure-3PSID', 'SAPISID'))
    return dict(**counts, domains=[domains[key] for key in sorted(domains)])
