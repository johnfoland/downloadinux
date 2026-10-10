"""Browser capabilities and selectors without loading browser code (DEC-29)."""
from dataclasses import dataclass
import sys

from .errors import ConfigError, DownloadinuxError
from .settings import user_path

BROWSERS = ('firefox', 'chrome', 'chromium')
# yt-dlp 2026.08.19 SUPPORTED_KEYRINGS, in its declaration order.
KEYRINGS = ('KWALLET', 'KWALLET5', 'KWALLET6', 'GNOMEKEYRING', 'BASICTEXT')


def platform_name(platform: str | None = None) -> str:
    platform = sys.platform if platform is None else platform
    if platform == 'darwin':
        return 'macos'
    if platform.startswith('linux'):
        return 'linux'
    raise ConfigError('cookie browser operations support only macOS and Linux; use Netscape import instead')


def support(*, platform: str | None = None) -> dict:
    target = platform_name(platform)
    return dict(platform=target, browsers=[
        dict(id=browser, profile=True, container=browser == 'firefox',
             keyrings=list(KEYRINGS) if target == 'linux' and browser != 'firefox' else [])
        for browser in BROWSERS
    ])


@dataclass(frozen=True)
class Selector:
    browser: str
    profile: str | None = None
    keyring: str | None = None
    container: str | None = None

    def origin(self) -> dict:
        return dict(kind='browser', browser=self.browser, profile=self.profile,
                    keyring=self.keyring, container=self.container)


def normalize(browser: str, profile: str | None = None, keyring: str | None = None,
              container: str | None = None, *, platform: str | None = None) -> Selector:
    target = platform_name(platform)
    if browser not in BROWSERS:
        raise ConfigError('browser must be firefox, chrome, or chromium')
    for field, value in [('profile', profile), ('keyring', keyring), ('container', container)]:
        if value is not None and (type(value) is not str or not value or any(c in value for c in '\x00\r\n')):
            raise ConfigError(f'{browser}: invalid {field} selector')
    if keyring is not None and (target != 'linux' or browser == 'firefox' or keyring not in KEYRINGS):
        raise ConfigError(f'{browser}: --keyring requires a supported Linux Chromium keyring; see cookies browsers')
    if container is not None and browser != 'firefox':
        raise ConfigError(f'{browser}: --container is supported only by Firefox')
    if profile is not None and ('/' in profile or profile.startswith('~')):
        try:
            profile = str(user_path(profile))
        except OSError as error:
            raise DownloadinuxError(f'{browser}: profile {profile!r}: {error.strerror or "cannot normalize profile path"}; use Netscape import instead') from None
    return Selector(browser, profile, keyring, container)
