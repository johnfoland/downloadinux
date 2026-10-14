"""Docker Linux process assertions for the Stage 3 fixture, never job state."""
import json
import os
from pathlib import Path
import sys


def current(record):
    try:
        fields = Path(f'/proc/{record["pid"]}/stat').read_text().rsplit(')', 1)[1].split()
    except FileNotFoundError:
        return None
    if fields[19] != record['start']:
        return None  # the PID was reused
    return fields


def tree(ready, alive):
    parent, child = ready['parent'], ready['child']
    assert child is not None, 'detach readiness has no child'
    assert parent['pid'] != child['pid'], 'child is the parent'
    assert parent['pgid'] == child['pgid'] == parent['pid'], 'fixture tree is not one downloader group'
    lifetime_pipes = []
    for role, record in (('parent', parent), ('child', child)):
        fields = current(record)
        if alive:
            assert fields is not None and fields[0] != 'Z', f'{role} process is not alive'
            assert int(fields[2]) == record['pgid'], f'{role} process group changed'
            lease = str(Path(ready['jar']).parent / 'lease')
            links = []
            for fd in Path(f'/proc/{record["pid"]}/fd').iterdir():
                try:
                    links.append(os.readlink(fd))
                except FileNotFoundError:
                    pass
            assert lease in links, f'{role} did not inherit the snapshot lease'
            lifetime_pipes.append({link for link in links if link.startswith('pipe:[')})
        else:
            assert fields is None, f'{role} process has not been reaped'
    if alive:
        assert lifetime_pipes[0] & lifetime_pipes[1], 'child did not inherit the parent lifetime pipe'


def no_follow():
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit() or entry.name == '1':
            continue
        try:
            argv = (entry / 'cmdline').read_bytes().split(b'\0')[:-1]
        except FileNotFoundError:
            continue
        for program, following in zip(argv, argv[1:]):
            if program.rsplit(b'/', 1)[-1] == b'downloadinux-worker' and following == b'follow':
                return False
    return True


if __name__ == '__main__':
    if sys.argv[1] == 'no-follow':
        sys.exit(0 if no_follow() else 1)
    tree(json.loads(Path(sys.argv[2]).read_text()), sys.argv[1] == 'alive')
