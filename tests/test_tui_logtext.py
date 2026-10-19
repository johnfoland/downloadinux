"""Byte rendering without Textual (TUI §4 *Log text*; DEC-60)."""

import subprocess
import sys

import pytest

from downloadinux import render
from downloadinux.tui.logtext import DROPPED, FAILURE, NOTICE, LogText


def test_import_does_not_load_textual():
    result = subprocess.run([sys.executable, '-c',
        'import sys; import downloadinux.tui.logtext; '
        'assert not any(n == "textual" or n.startswith("textual.") for n in sys.modules)'],
        capture_output=True, text=True)
    assert (result.returncode, result.stdout, result.stderr) == (0, '', '')


@pytest.mark.parametrize('chunks', [
    [b'\xe2', b'\x82\xac'], [b'\xe2\x82', b'\xac'], [b'\xe2', b'\x82', b'\xac'],
])
def test_split_multibyte_character(chunks):
    log = LogText('dropped')
    for chunk in chunks[:-1]:
        log.feed(chunk)
        assert log.lines == []
    log.feed(chunks[-1])
    assert log.lines == [('€', '')]


@pytest.mark.parametrize('chunks,lines', [
    ([b'a\xffb\xc0'], [('a�b�', '')]),
    ([b'a\nb\n'], [('a', ''), ('b', '')]),
    ([b'a\r\nb'], [('a', ''), ('b', '')]),
    ([b'a\r', b'\nb'], [('a', ''), ('b', '')]),
    ([b'a\rb'], [('b', '')]),
    ([b'abc\rd'], [('d', '')]),
    ([b'a\tb'], [('a\tb', '')]),
    ([b'last line'], [('last line', '')]),
    ([b'\n\n'], [('', ''), ('', '')]),
])
def test_byte_lines(chunks, lines):
    log = LogText('dropped')
    for chunk in chunks:
        log.feed(chunk)
    assert log.lines == lines


def test_a_trailing_cr_waits_and_then_replaces_the_whole_line():
    log = LogText('dropped')
    log.feed(b'abc\r')
    assert log.lines == [('abc', '')]
    log.feed(b'')
    assert log.lines == [('abc', '')]
    log.feed(b'd')
    assert log.lines == [('d', '')]


@pytest.mark.parametrize('codepoint', [
    *[n for n in range(0x20) if n not in (9, 10, 13)], *range(0x7f, 0xa0),
])
def test_every_other_control_is_escaped(codepoint):
    log = LogText('dropped')
    character = chr(codepoint)
    log.feed(character.encode('utf-8'))
    assert log.lines == [(render.escape(character), '')]


@pytest.mark.parametrize('data,text', [(b'partial', 'partial'), (b'abc\r', 'abc'),
                                        (b'\xe2\x82', '�')])
def test_notice_finishes_bytes_and_the_next_bytes_start_a_new_line(data, text):
    log = LogText('dropped')
    log.feed(data)
    log.notice('notice\r\x1b[b]', NOTICE)
    log.feed(b'next')
    assert log.lines == [(text, ''), ('notice\r\x1b[b]', 'cyan'), ('next', '')]


def test_notice_resets_the_decoder():
    log = LogText('dropped')
    log.feed(b'\xe2')
    log.notice('error', FAILURE)
    log.feed(b'\x82\xac')
    assert log.lines == [('�', ''), ('error', 'bold red'), ('��', '')]


def test_default_limit_and_marker():
    log = LogText('dropped')
    log.feed(b''.join(f'{n}\n'.encode() for n in range(10_000)))
    assert log.lines == [(str(n), '') for n in range(10_000)]
    log.feed(b'10000')
    assert log.lines == [('dropped', DROPPED), *[(str(n), '') for n in range(1, 10_001)]]
    log.feed(b'\rnew')
    assert len(log.lines) == 10_001 and log.lines[-1] == ('new', '')


def test_notices_and_unterminated_lines_count_but_marker_does_not():
    log = LogText('dropped', limit=2)
    log.feed(b'first')
    log.notice('notice', NOTICE)
    assert log.lines == [('first', ''), ('notice', NOTICE)]
    log.feed(b'last')
    assert log.lines == [('dropped', DROPPED), ('notice', NOTICE), ('last', '')]
    log.notice('error', FAILURE)
    assert log.lines == [('dropped', DROPPED), ('last', ''), ('error', FAILURE)]
    log.feed(b'new\nnewer\nnewest')
    assert log.lines == [('dropped', DROPPED), ('newer', ''), ('newest', '')]
