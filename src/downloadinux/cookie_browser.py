"""Direct, diagnostic-contained host extraction (Named cookie sources §2).

Imported only for browser operations. No YoutubeDL, URL, or extractor lifecycle.
"""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import re
import warnings

from . import cookiejar
from .cookie_selectors import Selector
from .errors import DownloadinuxError


class Diagnostics:
    """The locked YDLLogger interface; retain categories, never raw messages."""
    def __init__(self):
        self.failed = False

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


def extract(selector: Selector) -> cookiejar.Material:
    """Export strictly or fail safely, including dependency/serialization faults."""
    context = (f'{selector.browser} (profile={selector.profile!r}, '
               f'keyring={selector.keyring!r}, container={selector.container!r})')
    message = (f'{context}: cookie export failed; check the profile, browser schema, '
               'and keychain/keyring access, or use cookies import with a Netscape file')
    diagnostics = Diagnostics()
    try:
        # Third-party messages/prints/warnings may contain credentials. Never
        # forward them, including unexpected exceptions (review-checklist §3).
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()), warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            from yt_dlp.cookies import extract_cookies_from_browser
            jar = extract_cookies_from_browser(selector.browser, selector.profile, diagnostics,
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
        raise DownloadinuxError(message) from None
