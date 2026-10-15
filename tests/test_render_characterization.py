"""Exact front output captured before the DEC-58 renderer move."""

from types import SimpleNamespace

import pytest

from downloadinux import cli, health, operations


def test_config_show_complete_text(monkeypatch, capsys):
    report = dict(
        settings=[dict(key='paths.downloads', value='/output', source='file'),
                  dict(key='vpn.enabled', value=False, source='default'),
                  dict(key='container.uid', value=42, source='override')],
        config_file=dict(path='/settings/config.toml', exists=True),
        credentials_file=dict(path='/settings/credentials.toml', exists=False),
        token='unset',
        mounts=[dict(container='/downloads', host='/output', access='rw', exists=True),
                dict(container='/state', host='/state', access='rw', exists=False)],
    )
    monkeypatch.setattr(operations, 'show_config', lambda *args: report)
    assert cli.main(['config', 'show']) == 0
    assert capsys.readouterr() == (
        'paths.downloads = "/output"  (file)\n'
        'vpn.enabled = false  (default)\n'
        'container.uid = 42  (override)\n\n'
        'settings file:    /settings/config.toml\n'
        'credentials file: /settings/credentials.toml  (missing)\n'
        'token:            unset\n\n'
        'mounts:\n'
        '  /downloads <- /output  (rw)\n'
        '  /state     <- /state  (rw)  (missing)\n', '')


@pytest.mark.parametrize('command', ['list', 'inspect'])
def test_cookie_metadata_and_inspection_complete_text(monkeypatch, capsys, command):
    source = dict(name='work', kind='file', is_default=True, path='/export file',
                  revision='a' * 32, refreshed_at='2026-10-15T00:00:00Z')
    report = dict(source=source, total=2, session=1, expired=1, unexpired=0,
                  domains=[dict(domain='example.com', total=2, session=1,
                                expired=1, unexpired=0, session_cookie_present=True)])
    monkeypatch.setattr(operations, 'list_cookies', lambda *args: dict(default='work', sources=[source]))
    monkeypatch.setattr(operations, 'inspect_cookies', lambda *args: report)
    metadata = ('work: file; default: yes\n'
                '  origin: {"path": "/export file"}\n'
                '  revision: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa; refreshed_at: 2026-10-15T00:00:00Z\n')
    expected = 'Default: work\n' + metadata if command == 'list' else (
        metadata + 'total: 2; session: 1; expired: 1; unexpired: 0\n'
        '  example.com: total: 2; session: 1; expired: 1; unexpired: 0; recognized session cookie present\n')
    assert cli.main(['cookies', command, *(['work'] if command == 'inspect' else [])]) == 0
    assert capsys.readouterr() == (expected, '')


def test_doctor_complete_text(monkeypatch, capsys):
    result = SimpleNamespace(ok=False, checks=[
        SimpleNamespace(status=status, summary=summary, fix=fix)
        for status, summary, fix in [
            (health.Status.OK, 'works', None), (health.Status.WARN, 'warning', 'try this'),
            (health.Status.FAIL, 'failed', None), (health.Status.SKIP, 'skipped', None)]])
    monkeypatch.setattr(operations, 'doctor', lambda *args: result)
    assert cli.main(['doctor']) == 1
    assert capsys.readouterr() == (
        'downloadinux\n  ✓ works\n  ! warning\n      fix: try this\n  ✗ failed\n  - skipped\n', '')
