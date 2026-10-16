"""The TUI's shared-operation boundary (target design, TUI §2; DEC-59)."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from downloadinux import host_downloads, host_follow, host_jobs, host_vpn, operations
from downloadinux.tui.process import Runners, Stream


@dataclass(frozen=True)
class Selection:
    """The existing submission/retry selectors (DEC-39, DEC-40)."""

    source: str | None = None
    anonymous: bool = False
    user_agent: str | None = None

    def keywords(self) -> dict:
        return dict(cookie_source=self.source, anonymous=self.anonymous,
                    user_agent=self.user_agent)


class Backend:
    """Delegate calls with the session options and one set of runners per call.

    The record callback runs on the caller's thread; the app dispatches it to
    the UI thread. Operations retain all validation and settings reloads.
    """

    def __init__(self, config: str | None, overrides: Sequence[str],
                 record: Callable[[str, str], None]):
        self._config = config
        self._overrides = overrides
        self._record = record

    def _runners(self, name: str) -> Runners:
        return Runners(lambda text: self._record(name, text))

    def _options(self) -> dict:
        return dict(config=self._config, overrides=self._overrides)

    def _call(self, name, operation, *args, runners=(), **kwargs):
        processes = self._runners(name)
        return operation(*args, **self._options(), **kwargs,
                         **{key: getattr(processes, key) for key in runners})

    def _stream(self, name, operation, *args):
        processes = self._runners(name)
        return Stream(lambda: operation(*args, **self._options(),
                                        capture=processes.capture, spawn=processes.spawn), processes)

    def jobs(self, all: bool = False):
        return self._call("jobs", host_jobs.list_jobs, all, runners=("capture", "feed"))

    def show(self, id: int):
        return self._call("show", host_jobs.show, id, runners=("capture", "feed"))

    def log(self, id: int, attempt: int | None):
        return self._stream("log", host_jobs.logs, id, attempt)

    def follow_job(self, id: int):
        return self._stream("follow_job", host_follow.follow_jobs, [id])

    def follow_lane(self, preset: str):
        return self._stream("follow_lane", host_follow.follow_lane, preset)

    def cancel(self, ids: Sequence[int]):
        return self._call("cancel", host_jobs.cancel, ids, runners=("capture", "feed"))

    def retry(self, ids: Sequence[int], selection: Selection = Selection()):
        return self._call("retry", host_jobs.retry, ids, **selection.keywords(),
                          runners=("capture", "feed"))

    def export_to(self, preset: str, path: str):
        return self._call("export_to", host_jobs.export_to, preset, path,
                          runners=("capture", "feed"))

    def submit(self, preset: str, urls: Sequence[str], selection: Selection = Selection()):
        return self._call("submit", host_downloads.submit_download, preset, urls,
                          **selection.keywords(), runners=("capture", "feed"))

    def import_queue(self, preset: str, path: str, selection: Selection = Selection()):
        return self._call("import_queue", host_downloads.import_batch, preset, path,
                          **selection.keywords(), runners=("capture", "feed"))

    def cookies(self):
        return self._call("cookies", operations.list_cookies)

    def inspect(self, name: str):
        return self._call("inspect", operations.inspect_cookies, name)

    def browsers(self):
        self._runners("browsers")
        return operations.cookie_browsers()

    def import_cookies(self, name: str, file: str, replace: bool):
        return self._call("import_cookies", operations.import_cookies, name, file, replace=replace)

    def add_browser(self, name: str, browser: str, profile: str | None,
                    keyring: str | None, container: str | None, replace: bool):
        return self._call("add_browser", operations.add_browser_cookies, name, browser,
                          profile=profile, keyring=keyring, container=container, replace=replace)

    def refresh(self, name: str):
        return self._call("refresh", operations.refresh_cookies, name)

    def set_default(self, name: str | None):
        if name is None:
            return self._call("set_default", operations.cookie_default, anonymous=True)
        return self._call("set_default", operations.cookie_default, name)

    def doctor(self):
        return self._call("doctor", operations.doctor, runners=("capture",))

    def config(self):
        return self._call("config", operations.show_config)

    def set_setting(self, key: str, value: str):
        return self._call("set_setting", operations.set_setting, key, value)

    def unset_setting(self, key: str):
        return self._call("unset_setting", operations.unset_setting, key)

    def set_token(self, token: str):
        processes = self._runners("set_token")
        path = operations.set_token(token, **self._options())
        login = operations.log_in(token, **self._options(), capture=processes.capture)
        return path, login

    def lifecycle(self, command: str):
        if command not in ("up", "down", "restart"):
            raise ValueError("lifecycle command must be up, down, or restart")
        return self._call("lifecycle", operations.lifecycle, command, runners=("runner",))

    def vpn_state(self):
        return self._call("vpn_state", host_vpn.state, runners=("capture",))

    def servers(self, city: str, country: str):
        return self._call("servers", operations.server_choices, city, country, runners=("capture",))

    def vpn_connect(self, target: str):
        return self._call("vpn_connect", host_vpn.connect, target, runners=("capture", "feed"))

    def vpn_disconnect(self):
        return self._call("vpn_disconnect", host_vpn.disconnect, runners=("capture", "feed"))
