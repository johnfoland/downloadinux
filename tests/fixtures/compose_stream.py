"""Plays Compose's part for the streaming host call's tests (target design,
Persistent worker and jobs §1, *Streaming*; DEC-55).

The one argument is a JSON list of steps, each `[action, value]`, run in
order; the process then exits 0 unless a step ends it:

- `["line", TEXT]`: write TEXT (UTF-8, a lone surrogate escape standing for
  the byte it escapes) and a newline to standard output.
- `["raw", TEXT]`: write TEXT with no newline.
- `["await", PATH]`: wait until PATH exists.
- `["ignore-term", null]`: ignore `SIGTERM` from now on.
- `["eof", PATH]`: read standard input to end of file, then create PATH.
- `["sleep", SECONDS]`: sleep.
- `["hold", null]`: sleep forever, whatever standard input does.
- `["exit", STATUS]`: exit with STATUS.

Standard output is flushed after every write, so the host sees each line as
the real client prints it.
"""
import json
import os
from pathlib import Path
import signal
import sys
import time

out = sys.stdout.buffer
for action, value in json.loads(sys.argv[1]):
    if action == 'line':
        out.write(value.encode('utf-8', 'surrogateescape') + b'\n')
        out.flush()
    elif action == 'raw':
        out.write(value.encode('utf-8', 'surrogateescape'))
        out.flush()
    elif action == 'await':
        while not os.path.exists(value):
            time.sleep(0.01)
    elif action == 'ignore-term':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    elif action == 'eof':
        while os.read(0, 65536):
            pass
        Path(value).touch()
    elif action == 'sleep':
        time.sleep(value)
    elif action == 'hold':
        while True:
            time.sleep(1)
    elif action == 'exit':
        sys.exit(value)
    else:
        raise SystemExit(f'unknown action {action!r}')
