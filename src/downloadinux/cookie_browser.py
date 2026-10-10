"""Direct, diagnostic-contained host extraction (Named cookie sources §2).

Imported only for browser operations. No YoutubeDL, URL, or extractor lifecycle.
"""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from contextvars import ContextVar
from io import StringIO
from pathlib import Path
import re
from threading import RLock
import warnings

from . import cookiejar
from .cookie_selectors import Selector
from .errors import DownloadinuxError


# The locked extraction API has no jar factory parameter. Scope its constructor
# override and diagnostic containment to one adapter call at a time (DEC-29).
_EXTRACTION_LOCK = RLock()
_DIAGNOSTICS = ContextVar('browser_cookie_diagnostics', default=None)


class Diagnostics:
    """The locked YDLLogger interface; retain categories, never raw messages."""
    def __init__(self):
        self.failed = False
        self.collision = False

    def debug(self, message):
        pass

    def info(self, message):
        # Chromium's extraction summary can report loss on info alone.
        if re.search(r'\([1-9][0-9]* could not be decrypted\)', message):
            self.failed = True

    def warning(self, message, only_once=False, *, once=False):
        self.failed = True

    def error(self, message, *, is_error=True):
        self.failed = True

    def progress_bar(self):
        return None


@contextmanager
def _collision_guard(upstream, diagnostics):
    """Guard the real extractors' insertion boundary (Named cookie sources §2)."""
    original = upstream.YoutubeDLCookieJar

    class StrictCookieJar(original):
        def __init__(self, *args, **kwargs):
            self._stored_keys = set()
            self._netscape_keys = set()
            super().__init__(*args, **kwargs)

        def set_cookie(self, cookie):
            # Other threads using yt-dlp during the scoped override keep its
            # usual behavior. Only this extraction context rejects duplicates.
            if _DIAGNOSTICS.get() is diagnostics:
                stored = (cookie.domain.lower(), cookie.path, cookie.name)
                # Match the shared serializer's empty-name representation too.
                name = cookie.name if cookie.value is not None else ''
                netscape = (cookie.domain.lower(), cookie.path, name)
                if stored in self._stored_keys or netscape in self._netscape_keys:
                    diagnostics.failed = diagnostics.collision = True
                    raise ValueError('duplicate cookie key')
                self._stored_keys.add(stored)
                self._netscape_keys.add(netscape)
            return super().set_cookie(cookie)

    token = _DIAGNOSTICS.set(diagnostics)
    upstream.YoutubeDLCookieJar = StrictCookieJar
    try:
        yield
    finally:
        upstream.YoutubeDLCookieJar = original
        _DIAGNOSTICS.reset(token)


def extract(selector: Selector) -> cookiejar.Material:
    """Export strictly or fail safely, including dependency/serialization faults."""
    context = (f'{selector.browser} (profile={selector.profile!r}, '
               f'keyring={selector.keyring!r}, container={selector.container!r})')
    message = (f'{context}: cookie export failed; check the profile, browser schema, '
               'and keychain/keyring access, or use cookies import with a Netscape file')
    diagnostics = Diagnostics()
    try:
        # Third-party messages/prints/warnings may contain credentials. Never
        # forward them, including unexpected exceptions (DEC-29, §2).
        with _EXTRACTION_LOCK, redirect_stdout(StringIO()), redirect_stderr(StringIO()), warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            from yt_dlp import cookies as upstream
            with _collision_guard(upstream, diagnostics):
                jar = upstream.extract_cookies_from_browser(selector.browser, selector.profile, diagnostics,
                                                           keyring=selector.keyring, container=selector.container)
            if diagnostics.failed or caught:
                raise ValueError('incomplete extraction')
            # yt-dlp save mutates session expiry and drops available HttpOnly.
            # The shared Mozilla serializer retains both, including expired
            # records, and validates normalized keys before returning (§2).
            path = Path('browser export')
            text = cookiejar.serialize(cookiejar.Material(jar), path)
            material = cookiejar.parse(text.encode('utf-8'), path)
            if len(material.jar) != len(jar) or diagnostics.failed or caught:
                raise ValueError('incomplete serialization')
            return material
    except Exception:
        if diagnostics.collision:
            remedy = 'choose an explicit Firefox container, or ' if selector.browser == 'firefox' else ''
            message = (f'{context}: cookie export contains duplicate domain/path/name keys; '
                       f'{remedy}use cookies import '
                       'with a Netscape file containing distinct keys')
        raise DownloadinuxError(message) from None
