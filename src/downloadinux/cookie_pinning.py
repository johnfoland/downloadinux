"""Host submission pinning: resolve a request to stable IDs (§3, DEC-31).

Explicit anonymous touches nothing. Otherwise the first source access migrates
once (§5, DEC-33), then the explicit name, the application default, or
anonymous is chosen and that source's current revision is validated and
pinned. Startup later reads only the pair (``cookie_store.read_pinned``).
"""
from pathlib import Path

from . import cookie_store as store, cookie_writer
from .errors import ConfigError, DownloadinuxError


def pin(root: Path, name: str | None = None, *, anonymous: bool = False) -> store.Pinned | store.Anonymous:
    """Selectors are validated before any I/O; never fall back to another account."""
    if type(anonymous) is not bool:
        raise ConfigError('anonymous selection must be true or false')
    if name is not None:
        if type(name) is not str or not store.NAME.fullmatch(name):
            raise ConfigError('source name must match [a-z0-9][a-z0-9_-]{0,63}')
        if anonymous:
            raise ConfigError('cookie source and anonymous selections conflict')
    if anonymous:
        return store.ANONYMOUS
    snapshot = cookie_writer.ensure_store(root)
    selected = snapshot.default if name is None else name
    if selected is None:
        return store.ANONYMOUS
    source = snapshot.sources.get(selected)
    if source is None:
        raise DownloadinuxError(f'{root / "sources.json"}: source {selected} is not registered; choose a listed source')
    pinned = store.Pinned(selected, source.revision)
    store.read_pinned(root, pinned)
    return pinned
