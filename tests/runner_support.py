"""Stub downloaders for driving the real container runner (DEC-36).

Shared by the runner's own tests and the worker's execution tests, which run
the runner as a real process with these stubs first on ``PATH``.
"""

# Each stub writes ``<name>-<role>-<pid>.json`` once it has recorded what it
# received, then follows its mode: comma-separated words from
# STUB_MODE_<name> (else STUB_MODE). Terminating signals are recorded and
# then re-raised with the default action, so the stub dies by that signal.
STUB = r'''#!{python} -I
import json, os, signal, stat, subprocess, sys, time
log = os.environ['STUB_LOG']
name = os.path.basename(sys.argv[0])
role = os.environ.get('STUB_ROLE', 'child')
mode = os.environ.get('STUB_MODE_' + name.replace('-', '_').upper(), os.environ.get('STUB_MODE', ''))
if role == 'grandchild':
    mode = os.environ.get('STUB_GC_MODE', '')
words = dict(word.partition('=')[::2] for word in mode.split(',') if word)
base = os.path.join(log, '%s-%s-%d' % (name, role, os.getpid()))

def on_signal(number, frame):
    with open(base + '.signals', 'a') as handle:
        handle.write('%d\n' % number)
        handle.flush()
        os.fsync(handle.fileno())
    if number != signal.SIGWINCH:
        signal.signal(number, signal.SIG_DFL)
        os.kill(os.getpid(), number)
for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGWINCH):
    signal.signal(number, on_signal)

jar = None
for index, item in enumerate(sys.argv):
    if item == '--cookies':
        jar = sys.argv[index + 1]
    elif item.startswith('extractor.instagram.cookies='):
        jar = item.split('=', 1)[1]
fds = []
for fd in range(3, 256):
    try:
        status = os.fstat(fd)
    except OSError:
        continue
    kind = 'fifo' if stat.S_ISFIFO(status.st_mode) else 'reg' if stat.S_ISREG(status.st_mode) else 'other'
    fds.append([fd, kind, status.st_dev, status.st_ino])
lease = None
if jar is not None and os.path.exists(os.path.join(os.path.dirname(jar), 'lease')):
    status = os.stat(os.path.join(os.path.dirname(jar), 'lease'))
    lease = [status.st_dev, status.st_ino]
null, stdin = os.stat('/dev/null'), os.fstat(0)
record = dict(argv=sys.argv[1:], pid=os.getpid(), pgid=os.getpgid(0), jar=jar, lease=lease, fds=fds,
              jar_bytes=open(jar, 'rb').read().decode('latin-1') if jar and os.path.exists(jar) else None,
              stdin_null=(stdin.st_rdev, stdin.st_mode) == (null.st_rdev, null.st_mode),
              cwd=os.getcwd(), mark=os.environ.get('STUB_MARK'))
with open(base + '.tmp', 'w') as handle:
    json.dump(record, handle)
os.rename(base + '.tmp', base + '.json')

if 'extra' in words:
    open(os.path.join(os.path.dirname(jar), 'extra'), 'w').close()
if 'grandchild' in words:
    subprocess.Popen([sys.argv[0], 'grandchild'], close_fds=False, env=dict(os.environ, STUB_ROLE='grandchild'))
if 'wait' in words:
    release = os.path.join(log, 'release-gc' if role == 'grandchild' else 'release')
    while not os.path.exists(release):
        time.sleep(0.01)
if 'selfkill' in words:
    if int(words['selfkill']) != signal.SIGKILL:
        signal.signal(int(words['selfkill']), signal.SIG_DFL)
    os.kill(os.getpid(), int(words['selfkill']))
sys.exit(int(words.get('exit', 0)))
'''
